"""Git-backed snapshot repository for Minecraft worlds.

Design (inspired by the FastBack mod): each snapshot is a single commit on
its own **orphan branch** named ``snapshot/<iso-timestamp>[-<label>]``. Branches
don't share history, so deleting one never breaks another — but git's
content-addressed object store still deduplicates blobs *across* snapshots,
giving us effective per-file incremental storage at zero extra code.

We disable delta compression for ``*.mca`` / ``*.mcc`` via
``.git/info/attributes``. MC region files are already zlib-compressed, and
git's delta attempts net out negative: slower AND usually no size win.

The repo is **bare** (no working tree). All operations against a world run
with explicit ``GIT_WORK_TREE`` + ``GIT_INDEX_FILE`` pointing at a
temporary index, so we never mutate the repo's shared state mid-flight and
never touch the world directory itself.

Restore uses ``git archive`` piped through Python's ``tarfile`` with the
``data`` filter — no shell tar required, cross-platform, and path-traversal
safe.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SNAPSHOT_REF_PREFIX = "refs/heads/snapshot/"
_LABEL_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class StorageError(Exception):
    """Any error from the snapshot repository."""


@dataclass(frozen=True)
class Snapshot:
    id: str                    # full commit SHA (40 hex chars)
    ref: str                   # "refs/heads/snapshot/..."
    label: str | None
    timestamp: datetime        # timezone-aware, UTC
    subject: str               # first line of commit message
    world_name: str | None = None   # from commit metadata; None on legacy snapshots

    @property
    def short_id(self) -> str:
        return self.id[:12]


class SnapshotRepo:
    """A bare git repository used as a content-addressed snapshot store."""

    def __init__(self, repo_path: Path | str):
        self.repo_path = Path(repo_path).resolve()

    # ---- repo lifecycle -----------------------------------------------------

    def is_initialized(self) -> bool:
        return (self.repo_path / "HEAD").is_file()

    def init(self) -> None:
        """Create the repo if it doesn't exist. Idempotent."""
        self.repo_path.mkdir(parents=True, exist_ok=True)
        if not self.is_initialized():
            _run([
                "git", "init", "--bare",
                "--initial-branch=main", str(self.repo_path),
            ])
        # Always (re)write attributes — cheap, and corrects drift if someone
        # edited them by hand.
        info_dir = self.repo_path / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        (info_dir / "attributes").write_text(
            "*.mca -delta\n*.mcc -delta\n", encoding="utf-8",
        )
        # session.lock is a live-process marker — excluding it keeps the add
        # path clean when allow_live=True and the server still holds the lock,
        # and prevents stale locks from shipping in restores.
        (info_dir / "exclude").write_text("session.lock\n", encoding="utf-8")
        # Ensure commit-author identity exists (fresh repos often don't).
        if not self._config_get("user.email"):
            self._git("config", "user.email", "chunkvault@localhost")
        if not self._config_get("user.name"):
            self._git("config", "user.name", "chunkvault")

    # ---- snapshot / list / restore / delete --------------------------------

    def snapshot(
        self,
        world_path: Path | str,
        label: str | None = None,
        *,
        timestamp: datetime | None = None,
        allow_live: bool = False,
    ) -> Snapshot:
        if not self.is_initialized():
            raise StorageError(f"Repo not initialized: {self.repo_path}")
        world = Path(world_path).resolve()
        if not world.is_dir():
            raise StorageError(f"World path is not a directory: {world}")
        if not allow_live:
            _check_session_lock(world)

        now = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
        ts_compact = now.strftime("%Y%m%dT%H%M%SZ")
        branch_suffix = ts_compact
        if label:
            safe = _LABEL_SAFE.sub("-", label).strip("-")
            if safe:
                branch_suffix = f"{ts_compact}-{safe}"
        branch = f"snapshot/{branch_suffix}"
        full_ref = f"refs/heads/{branch}"

        if self._ref_exists(full_ref):
            raise StorageError(
                f"Snapshot ref already exists: {full_ref}. "
                f"Wait a second or pass a distinct label."
            )

        # For incremental speed: prime the private index from the most recent
        # snapshot of this world. `git add --all` uses index stat info to skip
        # unchanged files, so a warm baseline + MC's per-file mtime updates
        # means we only rehash the regions that actually changed between
        # snapshots — the core optimization for multi-GB worlds.
        prev = self._latest_for_world(world.name)
        prev_tree = self._tree_of(prev.id) if prev is not None else None

        with tempfile.TemporaryDirectory(prefix="chunkvault-idx-") as tmpdir:
            tmp_index = Path(tmpdir) / "index"
            env = {
                "GIT_INDEX_FILE": str(tmp_index),
                "GIT_WORK_TREE": str(world),
            }
            if prev_tree is not None:
                try:
                    # Prime the index with the prev tree's blobs. We
                    # intentionally skip ``update-index --refresh`` here —
                    # that command stamps every file as "matches index"
                    # based on stat alone, which races with sub-second
                    # mtime resolution on Windows: two writes within the
                    # same mtime tick get falsely classified as unchanged.
                    # Without --refresh, ``git add --all`` re-hashes all
                    # files whose stat info isn't pre-populated; less of a
                    # speedup but correctness is non-negotiable. The
                    # chunk-store backend (default) avoids this entirely
                    # via content-addressed chunk dedup.
                    self._git("read-tree", prev_tree, env=env)
                except StorageError:
                    pass
            self._git("add", "--all", env=env)
            tree_sha = self._git("write-tree", env=env).stdout.strip()

        metadata = {
            "label": label,
            "timestamp": now.isoformat(),
            "world_name": world.name,
        }
        subject = f"snapshot {ts_compact}"
        if label:
            subject += f" ({label})"
        message = f"{subject}\n\n{json.dumps(metadata)}\n"

        commit_sha = self._git(
            "commit-tree", tree_sha,
            input=message,
        ).stdout.strip()
        self._git("update-ref", full_ref, commit_sha)

        return Snapshot(
            id=commit_sha,
            ref=full_ref,
            label=label,
            timestamp=now,
            subject=subject,
            world_name=world.name,
        )

    def list(self) -> list[Snapshot]:
        """All snapshots, newest first (by metadata timestamp)."""
        result = self._git(
            "for-each-ref",
            "--format=%(refname)%09%(objectname)%09%(subject)",
            SNAPSHOT_REF_PREFIX + "*",
        )
        snapshots: list[Snapshot] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            ref, sha, subject = line.split("\t", 2)
            label, ts, world_name = self._read_metadata(sha)
            snapshots.append(Snapshot(
                id=sha, ref=ref, label=label, timestamp=ts, subject=subject,
                world_name=world_name,
            ))
        snapshots.sort(key=lambda s: s.timestamp, reverse=True)
        return snapshots

    def _latest_for_world(self, world_name: str) -> Snapshot | None:
        """Most recent snapshot whose world_name metadata matches.

        Used to seed incremental snapshot indexing. Returns None for the
        first-ever snapshot of a new world, or for worlds whose prior
        snapshots predate the world_name field.
        """
        for snap in self.list():
            if snap.world_name == world_name:
                return snap
        return None

    def _tree_of(self, commit_sha: str) -> str | None:
        """Look up the tree SHA pointed to by a commit."""
        try:
            r = self._git("rev-parse", f"{commit_sha}^{{tree}}")
            return r.stdout.strip() or None
        except StorageError:
            return None

    def get(self, id_or_label: str) -> Snapshot | None:
        """Find a snapshot by full/short commit SHA or by label."""
        want = id_or_label.strip()
        for snap in self.list():
            if snap.id == want or snap.id.startswith(want):
                return snap
            if snap.label == want:
                return snap
        return None

    def restore(
        self,
        snapshot: Snapshot | str,
        dest: Path | str,
        paths: Iterable[str] | None = None,
    ) -> None:
        """Extract a snapshot (or subset) into ``dest``.

        ``paths`` are POSIX-style paths relative to the snapshotted world root
        (e.g. ``["region/r.0.0.mca"]``). Omit to restore the whole snapshot.
        ``dest`` is created if it doesn't exist; existing files are overwritten.
        """
        snap_id = snapshot.id if isinstance(snapshot, Snapshot) else snapshot
        dest_path = Path(dest)
        dest_path.mkdir(parents=True, exist_ok=True)

        args = ["archive", "--format=tar", snap_id]
        path_list = list(paths) if paths is not None else []
        if path_list:
            args += ["--"] + path_list

        archive_bytes = self._git_binary(*args)
        if not archive_bytes:
            return

        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r") as tf:
            tf.extractall(dest_path, filter="data")

    def delete(self, snapshot: Snapshot | str) -> None:
        """Remove a snapshot's ref. Pack/loose objects remain until gc."""
        if isinstance(snapshot, Snapshot):
            self._git("update-ref", "-d", snapshot.ref, snapshot.id)
        else:
            snap = self.get(snapshot)
            if snap is None:
                raise StorageError(f"No such snapshot: {snapshot!r}")
            self._git("update-ref", "-d", snap.ref, snap.id)

    def diff_snapshots(
        self, snap_a: Snapshot | str, snap_b: Snapshot | str,
    ) -> "WorldDiff":
        """Chunk-level diff between two snapshots (restore-then-diff).

        Restores both snapshots to temporary directories and runs
        ``diff_worlds``. Simple and byte-for-byte correct but I/O-heavy.
        For large worlds prefer ``diff_snapshots_fast``.
        """
        from ..diff.world import diff_worlds
        a, b = self._resolve_pair(snap_a, snap_b)
        with tempfile.TemporaryDirectory(prefix="chunkvault-diff-a-") as da, \
             tempfile.TemporaryDirectory(prefix="chunkvault-diff-b-") as db:
            self.restore(a, Path(da))
            self.restore(b, Path(db))
            return diff_worlds(Path(da), Path(db))

    def diff_snapshots_fast(
        self, snap_a: Snapshot | str, snap_b: Snapshot | str,
    ) -> "WorldDiff":
        """Chunk-level diff via ``git diff-tree`` — only touches changed regions.

        Uses three layered optimizations:

        1. ``git diff-tree --raw`` lists changed paths AND their blob SHAs
           in a single call.
        2. A long-running ``cat-file --batch`` subprocess fetches every
           needed blob without per-file fork overhead.
        3. A SQLite cache keyed by blob SHA memoizes per-chunk hashes —
           once a blob has been parsed its chunks never need re-parsing.

        Output is identical to :meth:`diff_snapshots`.
        """
        from ..diff.world import ChunkDiff, WorldDiff
        from ..mca.region import parse_region_filename
        from .batch import BatchCatFile
        from .cache import ChunkHashCache

        a, b = self._resolve_pair(snap_a, snap_b)
        result = WorldDiff(
            old_root=Path(f"<snapshot {a.short_id}>"),
            new_root=Path(f"<snapshot {b.short_id}>"),
        )

        try:
            entries = self._diff_tree_raw(a.id, b.id)
        except StorageError:
            return self.diff_snapshots(a, b)

        # (dim_key, rx, rz) -> (old_region_sha | None, new_region_sha | None)
        region_shas: dict[tuple[str, int, int], tuple[str | None, str | None]] = {}
        # (dim_key, world_cx, world_cz) -> (old_mcc_sha | None, new_mcc_sha | None)
        mcc_shas: dict[tuple[str, int, int], tuple[str | None, str | None]] = {}
        for old_sha, new_sha, path in entries:
            parsed = _parse_world_path(path)
            if parsed is None:
                continue
            kind, dim_key, name = parsed
            if kind == "region":
                coords = parse_region_filename(Path(name))
                if coords is not None:
                    region_shas[(dim_key, coords.rx, coords.rz)] = (old_sha, new_sha)
            elif kind == "mcc":
                mcc_coords = _parse_mcc_filename(name)
                if mcc_coords is not None:
                    mcc_shas[(dim_key, *mcc_coords)] = (old_sha, new_sha)

        # A .mcc that changed without its containing region also changing means
        # the region bytes are identical on both sides but an external-chunk
        # payload moved. Record the region's shared sha so we still diff it.
        for (dim_key, wcx, wcz) in list(mcc_shas):
            region_key = (dim_key, wcx >> 5, wcz >> 5)
            if region_key in region_shas:
                continue
            region_path = f"{dim_key}/r.{wcx >> 5}.{wcz >> 5}.mca"
            shared_sha = self._resolve_path_sha(a.id, region_path)
            if shared_sha is None:
                continue
            region_shas[region_key] = (shared_sha, shared_sha)

        cache_path = self.repo_path / "chunkvault-cache.sqlite"
        with BatchCatFile(self.repo_path) as batch, \
             ChunkHashCache(cache_path) as cache:
            for key in sorted(region_shas):
                dim_key, rx, rz = key
                old_sha, new_sha = region_shas[key]
                region_path = f"{dim_key}/r.{rx}.{rz}.mca"
                old_hashes = self._hashes_side(
                    old_sha, dim_key, rx, rz, region_path,
                    batch, cache, mcc_shas,
                    side="old", commit_rev=a.id, result=result,
                )
                new_hashes = self._hashes_side(
                    new_sha, dim_key, rx, rz, region_path,
                    batch, cache, mcc_shas,
                    side="new", commit_rev=b.id, result=result,
                )
                self._emit_region_diff(
                    dim_key, rx, rz, old_hashes, new_hashes, result,
                )
        return result

    # ---- fast-path internals -----------------------------------------------

    def _diff_tree_raw(
        self, rev_a: str, rev_b: str,
    ) -> list[tuple[str | None, str | None, str]]:
        """Parse ``git diff-tree --raw -z`` into a list of (old_sha, new_sha, path).

        Zero-SHAs (0000...) become None.
        """
        raw = self._git(
            "diff-tree", "--no-commit-id", "--raw", "-r", "-z",
            rev_a, rev_b,
        ).stdout
        # Format: ":<mode> <mode> <oldsha> <newsha> <status>\0<path>\0"
        parts = raw.split("\x00")
        # Drop trailing empty caused by terminating \0
        if parts and parts[-1] == "":
            parts = parts[:-1]
        out: list[tuple[str | None, str | None, str]] = []
        for i in range(0, len(parts), 2):
            if i + 1 >= len(parts):
                break
            meta = parts[i]
            path = parts[i + 1]
            if not meta.startswith(":"):
                continue
            tokens = meta[1:].split()
            if len(tokens) < 5:
                continue
            _old_mode, _new_mode, old_sha, new_sha, _status = tokens[:5]
            out.append((
                None if _is_zero_sha(old_sha) else old_sha,
                None if _is_zero_sha(new_sha) else new_sha,
                path,
            ))
        return out

    def _hashes_side(
        self,
        region_sha: str | None,
        dim_key: str,
        rx: int,
        rz: int,
        region_path: str,
        batch,
        cache,
        mcc_shas: dict,
        *,
        side: str,
        commit_rev: str,
        result: "WorldDiff",
    ) -> dict[tuple[int, int], bytes] | None:
        """Return chunk hashes for one side of a region.

        Layered cache lookup:

        1. ``cache.get_blob_chunks(region_sha)`` returns the chunk enumeration
           (with internal hashes filled in). Hit → skip MCA parse.
        2. For each external chunk, resolve its mcc SHA, then check
           ``cache.get_external_hash(region_sha, cx, cz, mcc_sha)``. Hit →
           skip both fetch and hash. Miss → fetch the mcc, hash, populate.
        3. On cold blob: parse, derive enumeration + internal hashes, store.
        """
        from ..diff.world import RegionError
        from ..mca.hasher import hash_chunk
        from ..mca.region import MCAError, RawChunk, Region
        from .cache import ChunkRecord

        if region_sha is None:
            return None

        cached_records = cache.get_blob_chunks(region_sha)
        if cached_records is not None:
            return self._resolve_records(
                cached_records, region_sha, dim_key, rx, rz,
                batch, cache, mcc_shas,
                side=side, commit_rev=commit_rev,
            )

        region_bytes = batch.fetch(region_sha)
        if region_bytes is None:
            return None

        try:
            region = Region.from_bytes(
                region_bytes, rx=rx, rz=rz,
                source=f"{region_sha[:8]}:{region_path}",
            )
            records: list[ChunkRecord] = []
            for chunk in region.iter_chunks():
                if chunk.external:
                    records.append(ChunkRecord(
                        cx=chunk.cx, cz=chunk.cz,
                        external=True, internal_hash=None,
                    ))
                else:
                    records.append(ChunkRecord(
                        cx=chunk.cx, cz=chunk.cz,
                        external=False, internal_hash=hash_chunk(chunk),
                    ))
            cache.store_blob_chunks(region_sha, records)
            return self._resolve_records(
                records, region_sha, dim_key, rx, rz,
                batch, cache, mcc_shas,
                side=side, commit_rev=commit_rev,
            )
        except MCAError as e:
            result.errors.append(RegionError(
                dimension_key=dim_key, rx=rx, rz=rz,
                side=side,  # type: ignore[arg-type]
                path=Path(f"{region_sha[:8]}:{region_path}"),
                message=str(e),
            ))
            return None

    def _resolve_records(
        self,
        records,
        region_sha: str,
        dim_key: str,
        rx: int,
        rz: int,
        batch,
        cache,
        mcc_shas: dict,
        *,
        side: str,
        commit_rev: str,
    ) -> dict[tuple[int, int], bytes]:
        """Walk a cached/fresh chunk record list, materializing per-chunk hashes.

        Internal chunks return their cached hash directly. External chunks need
        the mcc SHA for this side and (cache miss) the mcc bytes themselves.
        """
        from ..mca.hasher import hash_chunk
        from ..mca.region import RawChunk

        out: dict[tuple[int, int], bytes] = {}
        for rec in records:
            if not rec.external:
                # internal_hash is guaranteed not-None for internal records
                out[(rec.cx, rec.cz)] = rec.internal_hash  # type: ignore[assignment]
                continue
            world_cx = rx * 32 + rec.cx
            world_cz = rz * 32 + rec.cz
            mcc_pair = mcc_shas.get((dim_key, world_cx, world_cz))
            mcc_sha = None
            if mcc_pair is not None:
                mcc_sha = mcc_pair[0] if side == "old" else mcc_pair[1]
            if mcc_sha is None:
                mcc_sha = self._resolve_path_sha(
                    commit_rev,
                    f"{dim_key}/c.{world_cx}.{world_cz}.mcc",
                )
            mcc_key = mcc_sha or ""
            cached_h = cache.get_external_hash(region_sha, rec.cx, rec.cz, mcc_key)
            if cached_h is not None:
                out[(rec.cx, rec.cz)] = cached_h
                continue
            mcc_bytes = batch.fetch(mcc_sha) if mcc_sha else None
            # Recover the on-disk compression byte: for external chunks it's
            # always 0x80-bit-set-plus-the-real-scheme. We don't have the
            # original chunk in hand, but we DO have the on-disk stub from
            # a fresh re-parse... avoid reparsing: the external hash only
            # depends on (compression & ~0x80, mcc_payload). The 0x80 bit is
            # masked in hash_chunk anyway, and the underlying scheme defaults
            # to zlib for MC; we recover by reading just the 5-byte chunk
            # header from the region blob if needed. For pragmatism: re-fetch
            # the region (cheap from batch — likely the OS page cache) only
            # when we need the actual byte. Keep it simple: synthesize a
            # minimal RawChunk that encodes scheme=zlib (the only one MC
            # ever uses for external chunks in practice).
            synthetic = RawChunk(
                cx=rec.cx, cz=rec.cz, timestamp=0,
                compression=0x82, payload=b"", external=True,
            )
            h = hash_chunk(synthetic, external_payload=mcc_bytes or b"")
            cache.store_external_hash(region_sha, rec.cx, rec.cz, mcc_key, h)
            out[(rec.cx, rec.cz)] = h
        return out

    def _emit_region_diff(
        self,
        dim_key: str,
        rx: int,
        rz: int,
        old_hashes,
        new_hashes,
        result: "WorldDiff",
    ) -> None:
        from ..diff.world import ChunkDiff
        if old_hashes is None and new_hashes is None:
            return
        a_map = old_hashes or {}
        b_map = new_hashes or {}
        for local in sorted(set(a_map) | set(b_map)):
            old_h = a_map.get(local)
            new_h = b_map.get(local)
            if old_h is None:
                kind = "added"
            elif new_h is None:
                kind = "removed"
            elif old_h != new_h:
                kind = "modified"
            else:
                continue
            lcx, lcz = local
            result.changes.append(ChunkDiff(
                dimension_key=dim_key,
                rx=rx, rz=rz,
                cx=rx * 32 + lcx,
                cz=rz * 32 + lcz,
                kind=kind,
                old_hash=old_h,
                new_hash=new_h,
            ))

    def _resolve_path_sha(self, rev: str, path: str) -> str | None:
        """Return the git blob SHA at ``<commit_rev>:<path>``, or ``None``.

        ``rev`` must be a commit or tree reference, not a blob SHA.
        Git rejects ``<rev>:<path>^{{blob}}`` as "needed a single revision" —
        the colon-path form is itself already a path resolution, so we just
        use it bare.
        """
        try:
            r = self._git("rev-parse", "--verify", f"{rev}:{path}")
            return r.stdout.strip() or None
        except StorageError:
            return None

    def _resolve_pair(
        self, snap_a: Snapshot | str, snap_b: Snapshot | str,
    ) -> tuple[Snapshot, Snapshot]:
        a = snap_a if isinstance(snap_a, Snapshot) else self.get(snap_a)
        b = snap_b if isinstance(snap_b, Snapshot) else self.get(snap_b)
        if a is None:
            raise StorageError(f"No such snapshot: {snap_a!r}")
        if b is None:
            raise StorageError(f"No such snapshot: {snap_b!r}")
        return a, b

    def gc(self, *, aggressive: bool = False, prune: bool = True) -> None:
        """Run git garbage collection to reclaim space from deleted snapshots.

        `prune=True` (default) removes unreachable objects immediately via
        `--prune=now`. Set `aggressive=True` to also repack more thoroughly
        at the cost of time — rarely worth it for this workload.
        """
        args = ["gc"]
        if aggressive:
            args.append("--aggressive")
        if prune:
            args.append("--prune=now")
        self._git(*args)

    # ---- internals ----------------------------------------------------------

    def _git(
        self, *args: str,
        env: dict[str, str] | None = None,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        full_env = os.environ.copy()
        full_env["GIT_DIR"] = str(self.repo_path)
        if env:
            full_env.update(env)
        result = subprocess.run(
            ["git"] + list(args),
            capture_output=True,
            text=True,
            env=full_env,
            input=input,
        )
        if result.returncode != 0:
            raise StorageError(
                f"git {' '.join(args)} failed (rc={result.returncode}): "
                f"{(result.stderr or result.stdout).strip()}"
            )
        return result

    def _git_binary(self, *args: str) -> bytes:
        """Run git capturing binary stdout."""
        full_env = os.environ.copy()
        full_env["GIT_DIR"] = str(self.repo_path)
        result = subprocess.run(
            ["git"] + list(args),
            capture_output=True,
            env=full_env,
        )
        if result.returncode != 0:
            raise StorageError(
                f"git {' '.join(args)} failed (rc={result.returncode}): "
                f"{result.stderr.decode('utf-8', 'replace').strip()}"
            )
        return result.stdout

    def _ref_exists(self, ref: str) -> bool:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", ref],
            env={**os.environ, "GIT_DIR": str(self.repo_path)},
            capture_output=True,
        )
        return result.returncode == 0

    def _config_get(self, key: str) -> str:
        result = subprocess.run(
            ["git", "config", "--get", key],
            env={**os.environ, "GIT_DIR": str(self.repo_path)},
            capture_output=True, text=True,
        )
        return result.stdout.strip()

    def _read_metadata(self, sha: str) -> tuple[str | None, datetime, str | None]:
        body = self._git("log", "-1", "--format=%B", sha).stdout
        label: str | None = None
        world_name: str | None = None
        timestamp: datetime | None = None
        # Metadata JSON sits after the first blank line, on its own.
        parts = body.split("\n\n", 1)
        if len(parts) == 2:
            try:
                meta = json.loads(parts[1].strip() or "{}")
                label = meta.get("label")
                world_name = meta.get("world_name")
                ts_s = meta.get("timestamp")
                if isinstance(ts_s, str):
                    timestamp = datetime.fromisoformat(ts_s)
            except (json.JSONDecodeError, ValueError):
                pass
        if timestamp is None:
            cr = self._git("log", "-1", "--format=%cI", sha)
            try:
                timestamp = datetime.fromisoformat(cr.stdout.strip())
            except ValueError:
                timestamp = datetime.now(timezone.utc)
        return label, timestamp, world_name


# ---- module-level helpers ---------------------------------------------------

def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command with text capture, raising StorageError on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise StorageError(
            f"{' '.join(cmd)} failed (rc={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def _check_session_lock(world: Path) -> None:
    """Refuse to snapshot if session.lock is held by a live MC process.

    MC uses a byte-range lock via Java ``FileChannel.tryLock`` to guard its
    world folder. We probe the same way: try to non-blockingly acquire an
    exclusive lock on byte 0; if it fails, MC (or something) has it.

    On Windows we use ``msvcrt.locking``; on POSIX ``fcntl.flock``. If the
    file doesn't exist or is empty, we fall back to a soft check: an empty
    lock file that was touched in the last 60 s is treated as suspicious.
    """
    lock = world / "session.lock"
    if not lock.exists():
        return

    # Soft check: empty & recently-touched is suspicious. We bail early here
    # because msvcrt.locking can't operate on a truly empty file.
    try:
        size = lock.stat().st_size
        mtime = lock.stat().st_mtime
    except OSError:
        return
    if size == 0:
        import time
        if time.time() - mtime < 60:
            raise StorageError(
                f"{world}: session.lock is empty but freshly touched — the "
                f"server may be in the middle of starting. Stop it and retry, "
                f"or pass allow_live=True."
            )
        return

    try:
        fh = open(lock, "r+b")
    except (PermissionError, OSError) as e:
        raise StorageError(
            f"{world}: cannot open session.lock: {e}. "
            f"Server may be running. Stop it or pass allow_live=True."
        ) from e

    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise StorageError(
                    f"{world}: session.lock is held — the server appears to be "
                    f"running. Stop it or pass allow_live=True.\nDetails: {e}"
                ) from e
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as e:
                raise StorageError(
                    f"{world}: session.lock is held — the server appears to be "
                    f"running. Stop it or pass allow_live=True.\nDetails: {e}"
                ) from e
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fh.close()


_REGION_FILENAME_RE = re.compile(r"^r\.-?\d+\.-?\d+\.mc[ar]$")
_MCC_FILENAME_RE = re.compile(r"^c\.(-?\d+)\.(-?\d+)\.mcc$")
_ZERO_SHA = "0" * 40


def _is_zero_sha(sha: str) -> bool:
    return sha == _ZERO_SHA


def _parse_world_path(posix: str) -> tuple[str, str, str] | None:
    """Categorize a git-tree path as (kind, dimension_key, basename).

    ``kind`` is ``"region"`` for ``*.mca``/``*.mcr``, ``"mcc"`` for ``*.mcc``,
    or None if the path isn't under a known region directory.
    """
    parts = posix.split("/")
    if len(parts) < 2:
        return None
    name = parts[-1]
    parent_parts = parts[:-1]
    # Region files live in a directory called "region" (or that path ends
    # with "/region"). Validate this pattern.
    if parent_parts[-1] != "region":
        return None
    dim_key = "/".join(parent_parts)
    if _REGION_FILENAME_RE.match(name):
        return "region", dim_key, name
    if _MCC_FILENAME_RE.match(name):
        return "mcc", dim_key, name
    return None


def _parse_mcc_filename(name: str) -> tuple[int, int] | None:
    m = _MCC_FILENAME_RE.match(name)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2))


def git_available() -> bool:
    """Quick check — `git --version` succeeds."""
    if shutil.which("git") is None:
        return False
    try:
        _run(["git", "--version"])
        return True
    except StorageError:
        return False
