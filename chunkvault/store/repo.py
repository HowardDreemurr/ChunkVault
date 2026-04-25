"""ChunkSnapshotRepo — chunk-level semantic snapshot backend.

Layout under the repo path::

    repo/
    ├── chunks/           ← content-addressed pool (raw chunk + .mcc payloads)
    ├── files/            ← content-addressed pool (whole non-region files)
    ├── manifests/        ← one .mcbk per snapshot
    └── index.sqlite      ← snapshot registry + hash-presence index

Snapshot flow:

    For each region file in each dimension:
        for each chunk:
            payload = chunk.payload (or .mcc bytes if external)
            h = hash_chunk(chunk, external_payload=...)
            if h not in index:
                chunks/<h>  ← payload
                index.add_chunk(h)
            manifest.add(cx, cz, compression, ts, h)
    For each non-region file (filtered by exclude rules):
        sha = sha256(content)
        if sha not in index:
            files/<sha>  ← content
            index.add_file(sha)
        manifest.add_file(rel_path, sha)
    Write manifest, register in index.

Restore flow is the reverse — read manifest, fetch each chunk + file from
the pools, reassemble the world tree on disk.

The whole point: a chunk that hasn't changed across N snapshots only ever
gets stored ONCE in chunks/. For a 100 GB MC world with 100+ historical
snapshots, this collapses what git stores as a few terabytes into well
under one.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from ..mca.hasher import hash_chunk
from ..mca.nbt_lite import decompress_chunk_payload, find_version_info
from ..mca.region import (
    EXTERNAL_FLAG,
    MCAError,
    PackedChunk,
    Region,
    pack_region,
)
from ..world.layout import enumerate_region_dirs, iter_region_files
from .chunk_store import ChunkStore
from .index import IndexDB, LogSnapshotRow, SnapshotRow
from .progress import ProgressCallback, ProgressEvent, _emit
from .log_manifest import (
    LogFileRecord,
    LogManifest,
    LogManifestError,
    read_log_manifest,
    write_log_manifest,
)
from .log_store import LogStore
from .manifest import (
    ChunkRecord,
    FileRecord,
    Manifest,
    ManifestHeader,
    RegionRecord,
    read_manifest,
    write_manifest,
)

# Default-excluded paths — irrelevant for save state and harmful when
# captured (logs balloon snapshot size, session.lock should never restore
# into a live server, etc.).
DEFAULT_EXCLUDE: tuple[str, ...] = (
    "session.lock",
    "logs/**",
    "crash-reports/**",
    "*.log",
    "usercache.json",
)


class ChunkRepoError(Exception):
    """Anything that goes wrong in the chunk-store snapshot backend."""


@dataclass
class VerifyReport:
    """Result of :meth:`ChunkSnapshotRepo.verify`."""
    ok_chunks: int = 0
    ok_files: int = 0
    corrupt_chunks: int = 0
    corrupt_files: int = 0
    missing_referenced: int = 0
    missing_manifests: int = 0
    orphan_blobs: int = 0
    repaired: int = 0


@dataclass(frozen=True)
class GCResult:
    """Counts returned from :meth:`ChunkSnapshotRepo.gc`."""
    chunks: int
    files: int
    logs: int

    def __iter__(self):
        # Backwards-compat: the old gc() returned a 2-tuple. Allow tuple-unpacking
        # for code that did `chunks, files = repo.gc()`.
        yield self.chunks
        yield self.files


@dataclass
class FsckReport:
    """Result of :meth:`ChunkSnapshotRepo.fsck`. Counts of issues found."""
    orphan_manifests: list[str] = field(default_factory=list)
    dangling_rows: list[str] = field(default_factory=list)
    orphan_log_manifests: list[str] = field(default_factory=list)
    dangling_log_rows: list[str] = field(default_factory=list)
    stray_temp_files: list[str] = field(default_factory=list)

    @property
    def total_issues(self) -> int:
        return (len(self.orphan_manifests) + len(self.dangling_rows)
                + len(self.orphan_log_manifests) + len(self.dangling_log_rows)
                + len(self.stray_temp_files))

    @property
    def clean(self) -> bool:
        return self.total_issues == 0

    def summary(self) -> str:
        if self.clean:
            return "fsck: clean"
        return (
            f"fsck: {self.total_issues} issue(s) — "
            f"orphan-manifests={len(self.orphan_manifests)}, "
            f"dangling-rows={len(self.dangling_rows)}, "
            f"orphan-log-manifests={len(self.orphan_log_manifests)}, "
            f"dangling-log-rows={len(self.dangling_log_rows)}, "
            f"stray-temp-files={len(self.stray_temp_files)}"
        )


class RoundTripVerificationError(ChunkRepoError):
    """The snapshot was created but failed round-trip verification.

    The snapshot row is intentionally **kept** in the index so the user can
    inspect, retry, or explicitly delete it. We refuse to silently swallow
    this — a backup that doesn't restore the original is not a backup.

    Carries both the snapshot and the report so callers can decide what to do.
    """

    def __init__(self, snapshot, report):
        self.snapshot = snapshot
        self.report = report
        super().__init__(
            f"snapshot {snapshot.short_id} (label={snapshot.label!r}) failed "
            f"round-trip verification: {report.summary()}"
        )


@dataclass(frozen=True)
class ChunkSnapshot:
    id: str                    # 32-hex-char unique snapshot id
    label: str | None
    timestamp: datetime
    world_name: str
    mc_version: str | None
    data_version: int | None
    manifest_path: Path

    @property
    def short_id(self) -> str:
        return self.id[:12]


@dataclass(frozen=True)
class LogSnapshot:
    id: str
    label: str | None
    timestamp: datetime
    source_path: str | None
    server_count: int
    file_count: int
    manifest_path: Path

    @property
    def short_id(self) -> str:
        return self.id[:12]


class ChunkSnapshotRepo:
    def __init__(self, repo_path: Path | str):
        self.repo_path = Path(repo_path).resolve()
        self.chunks = ChunkStore(self.repo_path)
        self.logs = LogStore(self.repo_path)
        from .tile_store import TileStore
        self.tiles = TileStore(self.repo_path)
        self.manifests_dir = self.repo_path / "manifests"
        self.log_manifests_dir = self.repo_path / "log-snapshots"
        self.index_path = self.repo_path / "index.sqlite"

    # ---- repo lifecycle -----------------------------------------------------

    def is_initialized(self) -> bool:
        return self.index_path.is_file()

    def init(self) -> None:
        self.repo_path.mkdir(parents=True, exist_ok=True)
        self.chunks.init()
        self.logs.init()
        self.tiles.init()
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.log_manifests_dir.mkdir(parents=True, exist_ok=True)
        # Create index DB if absent
        with IndexDB(self.index_path):
            pass

    # ---- snapshot -----------------------------------------------------------

    def snapshot(
        self,
        world_path: Path | str,
        label: str | None = None,
        *,
        timestamp: datetime | None = None,
        allow_live: bool = False,
        exclude: Iterable[str] | None = None,
        world_name: str | None = None,
        progress_cb: ProgressCallback = None,
        verify_roundtrip: bool = True,
    ) -> ChunkSnapshot:
        if not self.is_initialized():
            raise ChunkRepoError(f"Repo not initialized: {self.repo_path}")
        world = Path(world_path).resolve()
        if not world.is_dir():
            raise ChunkRepoError(f"World path is not a directory: {world}")
        if not allow_live:
            from ..storage.repo import _check_session_lock
            _check_session_lock(world)

        # Sanity: a real MC world has level.dat OR a region/ subdir. Anything
        # else is probably a user pointing at the wrong directory (the
        # archive folder, the parent dir, the vault itself, etc.) — warn
        # loudly, not silently snapshot 84 unrelated files.
        if not _looks_like_mc_world(world):
            raise ChunkRepoError(
                f"{world} doesn't look like a Minecraft world directory "
                f"(no level.dat and no region/ subdirectory). If you meant "
                f"to ingest backup archives, use `chunkvault ingest` instead. "
                f"If this really IS your world, point at the directory that "
                f"contains level.dat (often <server>/world/)."
            )

        # Pick the snapshot's timestamp: explicit caller value wins, else
        # auto-detect from level.dat's LastPlayed (best for copied/archived
        # save folders where "now" is meaningless), else region mtime,
        # else fall back to the current wall clock.
        if timestamp is not None:
            ts = timestamp.astimezone(timezone.utc)
            ts_source = "explicit"
        else:
            detected, ts_source = _detect_world_timestamp(world)
            ts = (detected or datetime.now(timezone.utc)).astimezone(timezone.utc)
            if detected is None:
                ts_source = "now"
        ts_ms = int(ts.timestamp() * 1000)

        # LastPlayed is also stored verbatim in the manifest — even when the
        # user overrides the snapshot timestamp, knowing the world's intrinsic
        # "last save" time is useful for retime --from-level-dat later.
        last_played_ms = _read_level_dat_last_played(world) or 0

        mc_version, data_version = _read_level_dat_version(world)

        effective_world_name = world_name or world.name
        manifest = Manifest(header=ManifestHeader(
            timestamp_ms=ts_ms,
            label=label,
            world_name=effective_world_name,
            mc_version=mc_version or "",
            data_version=data_version or 0,
            last_played_ms=last_played_ms,
        ))
        _emit(progress_cb, ProgressEvent(
            kind="phase_start", phase="snapshot",
            label=f"snapshot {effective_world_name} (ts={ts.isoformat()}, "
                  f"source={ts_source})",
        ))

        exclude_patterns = tuple(exclude) if exclude is not None else DEFAULT_EXCLUDE

        new_chunks = 0
        new_files = 0
        chunk_count = 0
        region_count = 0
        file_count = 0
        chunks_referenced: list[bytes] = []
        files_referenced: list[bytes] = []

        with IndexDB(self.index_path) as index:
            # 1) Region files — chunk-level dedup
            visited_paths: set[Path] = set()
            # Pre-count regions across all dimensions for progress reporting.
            all_regions: list[tuple[Path, int, int, str]] = []
            for region_dir in enumerate_region_dirs(world):
                for rx, rz, region_path in iter_region_files(region_dir.path):
                    all_regions.append((region_path, rx, rz, region_dir.dimension_key))
            total_regions = len(all_regions)
            _emit(progress_cb, ProgressEvent(
                kind="phase_start", phase="regions",
                label="region files", total=total_regions,
            ))
            regions_by_dim: dict[str, list[RegionRecord]] = {}
            for i, (region_path, rx, rz, dim_key) in enumerate(all_regions, 1):
                visited_paths.add(region_path)
                region_record, region_new = self._snapshot_region(
                    region_path, rx, rz, dim_key, index,
                    visited_mcc=visited_paths,
                )
                if region_record is None:
                    continue
                regions_by_dim.setdefault(dim_key, []).append(region_record)
                region_count += 1
                chunk_count += len(region_record.chunks)
                new_chunks += region_new
                for c in region_record.chunks:
                    chunks_referenced.append(c.content_hash)
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="regions",
                    label=f"r.{rx}.{rz}.mca", current=i, total=total_regions,
                ))
            for dim_key, regions in regions_by_dim.items():
                manifest.dimensions[dim_key] = regions
            _emit(progress_cb, ProgressEvent(
                kind="phase_done", phase="regions",
                current=total_regions, total=total_regions,
                detail={"new_chunks": new_chunks},
            ))

            # 2) Non-region files — whole-file dedup
            # Walking a TB-scale world tree can take minutes by itself; emit
            # an info phase so the bar isn't blank during the scan.
            _emit(progress_cb, ProgressEvent(
                kind="phase_start", phase="scan_files",
                label="walking world tree for non-region files",
            ))
            files_to_process = []
            for abs_path in _walk_world_files(world):
                if abs_path in visited_paths:
                    continue
                rel = abs_path.relative_to(world).as_posix()
                if _matches_any(rel, exclude_patterns):
                    continue
                files_to_process.append((abs_path, rel))
            _emit(progress_cb, ProgressEvent(
                kind="phase_done", phase="scan_files",
                label=f"found {len(files_to_process)} files",
            ))
            _emit(progress_cb, ProgressEvent(
                kind="phase_start", phase="files",
                label="non-region files", total=len(files_to_process),
            ))
            for i, (abs_path, rel) in enumerate(files_to_process, 1):
                content = abs_path.read_bytes()
                sha = hashlib.sha256(content).digest()
                if not index.has_file(sha):
                    self.chunks.store_file(sha, content)
                    index.add_files([sha])  # presence row
                    new_files += 1
                manifest.files.append(FileRecord(relative_path=rel, sha256=sha))
                file_count += 1
                files_referenced.append(sha)
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="files",
                    label=rel, current=i, total=len(files_to_process),
                ))
            _emit(progress_cb, ProgressEvent(
                kind="phase_done", phase="files",
                current=len(files_to_process), total=len(files_to_process),
                detail={"new_files": new_files},
            ))

            # 3) Persist manifest to disk FIRST. write_manifest is atomic
            #    (temp + rename), so the file is either fully written or
            #    not present — never half-written. If the process is killed
            #    between this step and the index commit below, fsck will
            #    find the orphan manifest and either commit it (if intact)
            #    or delete it.
            snap_id = uuid.uuid4().hex
            manifest_path = self.manifests_dir / f"{snap_id}.mcbk"
            _emit(progress_cb, ProgressEvent(
                kind="phase_start", phase="write_manifest",
                label=f"{manifest_path.name} ({chunk_count} chunks)",
            ))
            write_manifest(manifest_path, manifest)
            _emit(progress_cb, ProgressEvent(
                kind="phase_done", phase="write_manifest",
            ))

            # 4) Now do the index work — refs FIRST, snapshot row LAST.
            #    The snapshot row is the visibility marker: a row in the
            #    snapshots table only ever appears when refs are already
            #    correctly incremented. fsck uses the row's presence as
            #    proof that refs were committed.
            _emit(progress_cb, ProgressEvent(
                kind="phase_start", phase="update_index",
                label=f"refcount {len(chunks_referenced)} chunks "
                      f"+ {len(files_referenced)} files",
            ))
            index.adjust_chunk_refs(chunks_referenced, delta=1)
            index.adjust_file_refs(files_referenced, delta=1)
            index.add_snapshot(SnapshotRow(
                id=snap_id,
                label=label,
                world_name=effective_world_name,
                timestamp_ms=ts_ms,
                manifest_path=str(manifest_path.relative_to(self.repo_path)),
                mc_version=mc_version,
                data_version=data_version,
                chunk_count=chunk_count,
                region_count=region_count,
                file_count=file_count,
                new_chunk_count=new_chunks,
                new_file_count=new_files,
            ))
            _emit(progress_cb, ProgressEvent(
                kind="phase_done", phase="update_index",
            ))

        snap_obj = ChunkSnapshot(
            id=snap_id, label=label, timestamp=ts,
            world_name=effective_world_name, mc_version=mc_version,
            data_version=data_version, manifest_path=manifest_path,
        )

        # Render thumbnail tiles + per-dim PNG sidecars. Failures here
        # never block the snapshot — the chunk pool is the durable record;
        # tiles are a cheap derived artifact that backfill can rebuild later.
        try:
            from ..viz.snapshot_render import (
                ensure_tiles_for_manifest, write_snapshot_sidecars,
            )
            ensure_tiles_for_manifest(self, manifest, progress_cb=progress_cb)
            write_snapshot_sidecars(
                self, snap_id, manifest, progress_cb=progress_cb,
            )
        except Exception as e:
            _emit(progress_cb, ProgressEvent(
                kind="warning", phase="render_tiles",
                label=f"thumbnail render failed (snapshot still committed): {e}",
            ))

        _emit(progress_cb, ProgressEvent(
            kind="finish", phase="snapshot",
            label=label or snap_id[:12],
            detail={
                "snapshot_id": snap_id, "new_chunks": new_chunks,
                "new_files": new_files, "chunk_count": chunk_count,
                "file_count": file_count,
            },
        ))

        # Default-on round-trip self-check: restore the snapshot we just made
        # to a temp dir, walk the two trees in lockstep, fail loudly if the
        # restored bytes don't reproduce the source's chunks/files. Costs a
        # full restore + walk; opt out with `verify_roundtrip=False` when
        # bulk-ingesting and you'll run `chunkvault verify-roundtrip` later.
        if verify_roundtrip:
            from .roundtrip import verify_roundtrip as _verify_rt
            report = _verify_rt(
                self, snap_obj, world,
                exclude=exclude_patterns,
                progress_cb=progress_cb,
            )
            if not report.passed:
                raise RoundTripVerificationError(snap_obj, report)
        return snap_obj

    def _snapshot_region(
        self,
        region_path: Path,
        rx: int,
        rz: int,
        dim_key: str,
        index: IndexDB,
        *,
        visited_mcc: set[Path],
    ) -> tuple[RegionRecord | None, int]:
        """Hash + store every chunk in one region. Returns (record, new_chunk_count)."""
        try:
            region = Region(region_path)
        except MCAError:
            return None, 0
        new_count = 0
        rec = RegionRecord(rx=rx, rz=rz, chunks=[])
        try:
            chunks_iter = list(region.iter_chunks())
        except MCAError:
            return None, 0
        for chunk in chunks_iter:
            if chunk.external:
                world_cx = rx * 32 + chunk.cx
                world_cz = rz * 32 + chunk.cz
                mcc_path = region_path.parent / f"c.{world_cx}.{world_cz}.mcc"
                visited_mcc.add(mcc_path)
                payload = mcc_path.read_bytes() if mcc_path.is_file() else b""
                h = hash_chunk(chunk, external_payload=payload)
            else:
                payload = chunk.payload
                h = hash_chunk(chunk)
            # Skip the disk write entirely if this hash is already in the index
            # — saves the bytes-allocation for an existing-chunk fast path.
            # ref counting happens in bulk after the snapshot walk completes.
            if not index.has_chunk(h):
                masked = chunk.compression & ~EXTERNAL_FLAG
                blob = bytes([masked]) + payload
                self.chunks.store_chunk(h, blob)
                index.add_chunks([h])  # presence row; ref_count stays 0 here
                new_count += 1
            rec.chunks.append(ChunkRecord(
                cx=chunk.cx, cz=chunk.cz,
                compression=chunk.compression,
                timestamp=chunk.timestamp,
                content_hash=h,
            ))
        return rec, new_count

    # ---- enumeration --------------------------------------------------------

    def list(self) -> list[ChunkSnapshot]:
        with IndexDB(self.index_path) as index:
            rows = index.list_snapshots()
        return [self._row_to_snap(r) for r in rows]

    def get(self, id_or_label: str) -> ChunkSnapshot | None:
        with IndexDB(self.index_path) as index:
            row = index.get_snapshot(id_or_label)
        return self._row_to_snap(row) if row else None

    def _row_to_snap(self, row: SnapshotRow) -> ChunkSnapshot:
        return ChunkSnapshot(
            id=row.id, label=row.label,
            timestamp=datetime.fromtimestamp(row.timestamp_ms / 1000, tz=timezone.utc),
            world_name=row.world_name,
            mc_version=row.mc_version,
            data_version=row.data_version,
            manifest_path=self.repo_path / row.manifest_path,
        )

    # ---- restore ------------------------------------------------------------

    def restore(
        self,
        snapshot: ChunkSnapshot | str,
        dest: Path | str,
        paths: Iterable[str] | None = None,
        *,
        progress_cb: ProgressCallback = None,
    ) -> None:
        """Re-materialize a snapshot to ``dest``.

        Emits ``restore_regions`` then ``restore_files`` phases so the caller's
        progress bar can show real movement: a TB-scale world's restore can
        take hours, and a bar that reads "0/?" the whole time looks frozen.
        """
        snap = snapshot if isinstance(snapshot, ChunkSnapshot) else self.get(snapshot)
        if snap is None:
            raise ChunkRepoError(f"No such snapshot: {snapshot!r}")
        dest_path = Path(dest)
        dest_path.mkdir(parents=True, exist_ok=True)

        manifest = read_manifest(snap.manifest_path)
        path_filter = set(paths) if paths is not None else None

        total_regions = sum(len(rs) for rs in manifest.dimensions.values())
        total_files = len(manifest.files)

        if total_regions:
            _emit(progress_cb, ProgressEvent(
                kind="phase_start", phase="restore_regions",
                label=f"{total_regions} region files → {dest_path.name}",
                total=total_regions,
            ))
        region_i = 0
        for dim_key, regions in manifest.dimensions.items():
            for region in regions:
                region_i += 1
                rel = f"{dim_key}/r.{region.rx}.{region.rz}.mca"
                if path_filter is not None and not _path_matches_filter(rel, path_filter):
                    # Restore the .mca even if the filter targets only an mcc
                    # if any of this region's external chunks would be needed
                    if not any(
                        _path_matches_filter(
                            f"{dim_key}/c.{region.rx*32+c.cx}.{region.rz*32+c.cz}.mcc",
                            path_filter,
                        ) for c in region.chunks if c.external
                    ):
                        _emit(progress_cb, ProgressEvent(
                            kind="phase_progress", phase="restore_regions",
                            label=f"skip {rel}",
                            current=region_i, total=total_regions,
                        ))
                        continue
                self._restore_region(dest_path, dim_key, region, path_filter)
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="restore_regions",
                    label=rel, current=region_i, total=total_regions,
                ))
        if total_regions:
            _emit(progress_cb, ProgressEvent(
                kind="phase_done", phase="restore_regions",
                current=total_regions, total=total_regions,
            ))

        if total_files:
            _emit(progress_cb, ProgressEvent(
                kind="phase_start", phase="restore_files",
                label=f"{total_files} non-region files",
                total=total_files,
            ))
        file_i = 0
        for f in manifest.files:
            file_i += 1
            if path_filter is not None and not _path_matches_filter(
                f.relative_path, path_filter,
            ):
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="restore_files",
                    label=f"skip {f.relative_path}",
                    current=file_i, total=total_files,
                ))
                continue
            content = self.chunks.read_file(f.sha256)
            if content is None:
                raise ChunkRepoError(
                    f"file blob missing for {f.relative_path}: {f.sha256.hex()}"
                )
            target = dest_path / f.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            _emit(progress_cb, ProgressEvent(
                kind="phase_progress", phase="restore_files",
                label=f.relative_path,
                current=file_i, total=total_files,
            ))
        if total_files:
            _emit(progress_cb, ProgressEvent(
                kind="phase_done", phase="restore_files",
                current=total_files, total=total_files,
            ))

    def _restore_region(
        self, dest_root: Path, dim_key: str,
        region: RegionRecord, path_filter: set[str] | None,
    ) -> None:
        region_rel = f"{dim_key}/r.{region.rx}.{region.rz}.mca"
        packed: list[PackedChunk] = []
        # We always emit the whole region file (chunks within the same region
        # share the same .mca on disk; selectively writing parts isn't
        # meaningful). Filtering is at the region/mcc granularity.
        for c in region.chunks:
            blob = self.chunks.read_chunk(c.content_hash)
            if blob is None:
                raise ChunkRepoError(
                    f"chunk blob missing: dim={dim_key} ({c.cx},{c.cz}) "
                    f"hash={c.content_hash.hex()}"
                )
            # First byte is the masked compression scheme; rest is the payload
            # bytes that hash_chunk consumed when this chunk was stored.
            payload = blob[1:] if blob else b""
            if c.compression & EXTERNAL_FLAG:
                # External chunk: empty stub in MCA + write .mcc separately
                packed.append(PackedChunk(
                    cx=c.cx, cz=c.cz, timestamp=c.timestamp,
                    compression=c.compression, payload=b"",
                ))
                world_cx = region.rx * 32 + c.cx
                world_cz = region.rz * 32 + c.cz
                mcc_rel = f"{dim_key}/c.{world_cx}.{world_cz}.mcc"
                if path_filter is None or _path_matches_filter(mcc_rel, path_filter):
                    target = dest_root / mcc_rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
            else:
                packed.append(PackedChunk(
                    cx=c.cx, cz=c.cz, timestamp=c.timestamp,
                    compression=c.compression, payload=payload,
                ))
        if path_filter is None or _path_matches_filter(region_rel, path_filter):
            region_path = dest_root / region_rel
            region_path.parent.mkdir(parents=True, exist_ok=True)
            region_path.write_bytes(pack_region(packed))

    # ---- diff between snapshots --------------------------------------------

    def diff_snapshots(
        self,
        snap_a: ChunkSnapshot | str,
        snap_b: ChunkSnapshot | str,
    ) -> "WorldDiff":
        """Pure-manifest diff — no chunk store reads, just compare hashes.

        This is the chunk-store equivalent of git's ``diff-tree``: since each
        chunk's identity is captured in its content_hash and that hash lives
        in the manifest, comparing two manifests gives a complete chunk-level
        diff at the cost of two file reads.
        """
        from ..diff.world import ChunkDiff, WorldDiff
        a = snap_a if isinstance(snap_a, ChunkSnapshot) else self.get(snap_a)
        b = snap_b if isinstance(snap_b, ChunkSnapshot) else self.get(snap_b)
        if a is None:
            raise ChunkRepoError(f"No such snapshot: {snap_a!r}")
        if b is None:
            raise ChunkRepoError(f"No such snapshot: {snap_b!r}")

        ma = read_manifest(a.manifest_path)
        mb = read_manifest(b.manifest_path)
        result = WorldDiff(
            old_root=Path(f"<snapshot {a.short_id}>"),
            new_root=Path(f"<snapshot {b.short_id}>"),
            old_mc_version=ma.header.mc_version or None,
            new_mc_version=mb.header.mc_version or None,
            old_data_version=ma.header.data_version or None,
            new_data_version=mb.header.data_version or None,
            old_label=ma.header.label,
            new_label=mb.header.label,
        )

        map_a = _manifest_chunk_map(ma)
        map_b = _manifest_chunk_map(mb)
        for key in sorted(set(map_a) | set(map_b)):
            dim_key, rx, rz, lcx, lcz = key
            ha = map_a.get(key)
            hb = map_b.get(key)
            if ha is None:
                kind = "added"
            elif hb is None:
                kind = "removed"
            elif ha != hb:
                kind = "modified"
            else:
                continue
            result.changes.append(ChunkDiff(
                dimension_key=dim_key,
                rx=rx, rz=rz,
                cx=rx * 32 + lcx, cz=rz * 32 + lcz,
                kind=kind, old_hash=ha, new_hash=hb,
            ))
        return result

    # ---- verify -----------------------------------------------------------

    def verify(
        self,
        *,
        repair: bool = False,
        progress_cb: ProgressCallback = None,
    ) -> "VerifyReport":
        """Walk every blob on disk, recompute its hash, compare to its name.

        Also cross-checks that every chunk/file referenced by some manifest
        actually exists in the pool. With ``repair=True``, corrupt blobs are
        deleted; reachable-but-missing blobs are reported but never re-created
        (we have no way to conjure them).

        Emits four phases — ``verify_chunks``, ``verify_files``,
        ``verify_reachability``, ``verify_orphans`` — each with per-blob
        progress (batched every 100 to keep callback overhead negligible
        when scanning millions of blobs).
        """
        report = VerifyReport()
        BATCH = 100   # emit phase_progress every N items; finer feels jittery,
                      # coarser hides progress on small vaults

        # 1) Byte-level integrity — chunks store (masked_compression || payload),
        #    which is exactly what hash_chunk hashed; so blake2b of the file
        #    must equal the path-encoded hash.
        _emit(progress_cb, ProgressEvent(
            kind="phase_start", phase="verify_chunks",
            label="re-hashing chunk pool",
        ))
        i = 0
        for path, expected in _iter_pool_blobs(self.chunks.chunks_dir):
            i += 1
            content = path.read_bytes()
            actual = hashlib.blake2b(content, digest_size=16).digest()
            if actual == expected:
                report.ok_chunks += 1
            else:
                report.corrupt_chunks += 1
                if repair:
                    try:
                        path.unlink()
                        report.repaired += 1
                    except OSError:
                        pass
            if i % BATCH == 0:
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="verify_chunks",
                    label=f"{report.ok_chunks} ok, {report.corrupt_chunks} corrupt",
                    current=i,
                ))
        _emit(progress_cb, ProgressEvent(
            kind="phase_done", phase="verify_chunks",
            current=i, total=i,
            detail={"ok": report.ok_chunks, "corrupt": report.corrupt_chunks},
        ))

        # Files: keyed by sha256 of full content.
        _emit(progress_cb, ProgressEvent(
            kind="phase_start", phase="verify_files",
            label="re-hashing file pool",
        ))
        i = 0
        for path, expected in _iter_pool_blobs(self.chunks.files_dir):
            i += 1
            content = path.read_bytes()
            actual = hashlib.sha256(content).digest()
            if actual == expected:
                report.ok_files += 1
            else:
                report.corrupt_files += 1
                if repair:
                    try:
                        path.unlink()
                        report.repaired += 1
                    except OSError:
                        pass
            if i % BATCH == 0:
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="verify_files",
                    label=f"{report.ok_files} ok, {report.corrupt_files} corrupt",
                    current=i,
                ))
        _emit(progress_cb, ProgressEvent(
            kind="phase_done", phase="verify_files",
            current=i, total=i,
            detail={"ok": report.ok_files, "corrupt": report.corrupt_files},
        ))

        # 2) Reachability — any hash referenced by a manifest must exist on disk
        reachable_chunks: set[bytes] = set()
        reachable_files: set[bytes] = set()
        with IndexDB(self.index_path) as index:
            snap_rows = list(index.list_snapshots())
            _emit(progress_cb, ProgressEvent(
                kind="phase_start", phase="verify_reachability",
                label=f"walking {len(snap_rows)} manifests",
                total=len(snap_rows),
            ))
            for s_i, snap_row in enumerate(snap_rows, 1):
                manifest_path = self.repo_path / snap_row.manifest_path
                if not manifest_path.is_file():
                    report.missing_manifests += 1
                    _emit(progress_cb, ProgressEvent(
                        kind="phase_progress", phase="verify_reachability",
                        label=f"missing manifest: {snap_row.id[:12]}",
                        current=s_i, total=len(snap_rows),
                    ))
                    continue
                manifest = read_manifest(manifest_path)
                for regions in manifest.dimensions.values():
                    for region in regions:
                        for c in region.chunks:
                            reachable_chunks.add(c.content_hash)
                            if not self.chunks.has_chunk(c.content_hash):
                                report.missing_referenced += 1
                for f in manifest.files:
                    reachable_files.add(f.sha256)
                    if not self.chunks.has_file(f.sha256):
                        report.missing_referenced += 1
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="verify_reachability",
                    label=snap_row.label or snap_row.id[:12],
                    current=s_i, total=len(snap_rows),
                ))
            _emit(progress_cb, ProgressEvent(
                kind="phase_done", phase="verify_reachability",
                current=len(snap_rows), total=len(snap_rows),
            ))

        # 3) Orphan-blob count: blobs present on disk but not referenced.
        _emit(progress_cb, ProgressEvent(
            kind="phase_start", phase="verify_orphans",
            label="counting orphan blobs",
        ))
        i = 0
        for path, expected in _iter_pool_blobs(self.chunks.chunks_dir):
            i += 1
            if expected not in reachable_chunks:
                report.orphan_blobs += 1
            if i % BATCH == 0:
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="verify_orphans",
                    label=f"{report.orphan_blobs} orphans so far",
                    current=i,
                ))
        for path, expected in _iter_pool_blobs(self.chunks.files_dir):
            i += 1
            if expected not in reachable_files:
                report.orphan_blobs += 1
            if i % BATCH == 0:
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="verify_orphans",
                    label=f"{report.orphan_blobs} orphans so far",
                    current=i,
                ))
        _emit(progress_cb, ProgressEvent(
            kind="phase_done", phase="verify_orphans",
            current=i, total=i,
            detail={"orphans": report.orphan_blobs},
        ))

        return report

    # ---- log snapshots (independent subsystem) -----------------------------
    #
    # Logs are stored in a parallel content-addressed pool (logs/) with their
    # own JSON manifests (log-snapshots/). Nothing in the world snapshot path
    # touches them; you opt in by calling add_log_snapshot explicitly. Kept
    # separate so a bug in either system can't corrupt the other, and so users
    # who only want world history can ignore logs entirely.

    def add_log_snapshot(
        self,
        files_by_server: dict[str, list[tuple[str, bytes]]],
        *,
        label: str | None = None,
        source_path: Path | str | None = None,
        timestamp: datetime | None = None,
    ) -> "LogSnapshot":
        """Take a log snapshot of the given (server → [(path, bytes)]) map.

        Files are deduplicated globally by sha256 across all log snapshots.
        Same content seen across multiple ingests is stored once on disk.
        """
        if not self.is_initialized():
            raise ChunkRepoError(f"Repo not initialized: {self.repo_path}")

        ts = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
        ts_ms = int(ts.timestamp() * 1000)
        snap_id = uuid.uuid4().hex
        manifest = LogManifest(
            id=snap_id,
            label=label,
            timestamp_ms=ts_ms,
            source_path=str(source_path) if source_path else None,
        )

        new_files = 0
        file_count = 0
        all_shas: list[bytes] = []

        with IndexDB(self.index_path) as index:
            for server_name, files in files_by_server.items():
                records: list[LogFileRecord] = []
                for rel_posix, content in files:
                    sha = hashlib.sha256(content).digest()
                    all_shas.append(sha)
                    file_count += 1
                    if not index.has_log(sha):
                        self.logs.store_log(sha, content)
                        index.add_logs([sha])
                        new_files += 1
                    records.append(LogFileRecord(
                        relative_path=rel_posix,
                        sha256=sha,
                        size=len(content),
                        mtime_ms=ts_ms,
                    ))
                manifest.servers[server_name] = records

            index.adjust_log_refs(all_shas, delta=1)

            manifest_path = self.log_manifests_dir / f"{snap_id}.json"
            write_log_manifest(manifest_path, manifest)

            index.add_log_snapshot(LogSnapshotRow(
                id=snap_id,
                label=label,
                timestamp_ms=ts_ms,
                source_path=str(source_path) if source_path else None,
                manifest_path=str(manifest_path.relative_to(self.repo_path)),
                server_count=len(files_by_server),
                file_count=file_count,
                new_file_count=new_files,
            ))

        return LogSnapshot(
            id=snap_id,
            label=label,
            timestamp=ts,
            source_path=str(source_path) if source_path else None,
            server_count=len(files_by_server),
            file_count=file_count,
            manifest_path=manifest_path,
        )

    def list_log_snapshots(self) -> list["LogSnapshot"]:
        with IndexDB(self.index_path) as index:
            rows = index.list_log_snapshots()
        return [self._log_row_to_snap(r) for r in rows]

    def get_log_snapshot(self, id_or_label: str) -> "LogSnapshot | None":
        with IndexDB(self.index_path) as index:
            row = index.get_log_snapshot(id_or_label)
        return self._log_row_to_snap(row) if row else None

    def _log_row_to_snap(self, row: LogSnapshotRow) -> "LogSnapshot":
        return LogSnapshot(
            id=row.id, label=row.label,
            timestamp=datetime.fromtimestamp(row.timestamp_ms / 1000, tz=timezone.utc),
            source_path=row.source_path,
            server_count=row.server_count,
            file_count=row.file_count,
            manifest_path=self.repo_path / row.manifest_path,
        )

    def extract_logs(
        self,
        snapshot: "LogSnapshot | str",
        dest: Path | str,
        *,
        server: str | None = None,
    ) -> int:
        """Materialize a log snapshot's files into a flat directory tree.

        Layout: ``dest/<server>/<original-relative-path>``. Returns count.
        """
        snap = (snapshot if isinstance(snapshot, LogSnapshot)
                else self.get_log_snapshot(snapshot))
        if snap is None:
            raise ChunkRepoError(f"No such log snapshot: {snapshot!r}")
        manifest = read_log_manifest(snap.manifest_path)
        dest_path = Path(dest)
        dest_path.mkdir(parents=True, exist_ok=True)
        written = 0
        for server_name, files in manifest.servers.items():
            if server is not None and server_name != server:
                continue
            for rec in files:
                content = self.logs.read_log(rec.sha256)
                if content is None:
                    raise ChunkRepoError(
                        f"log blob missing for {server_name}/{rec.relative_path}: "
                        f"{rec.sha256.hex()}"
                    )
                target = dest_path / server_name / rec.relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                written += 1
        return written

    def delete_log_snapshot(self, snapshot: "LogSnapshot | str") -> None:
        snap = (snapshot if isinstance(snapshot, LogSnapshot)
                else self.get_log_snapshot(snapshot))
        if snap is None:
            raise ChunkRepoError(f"No such log snapshot: {snapshot!r}")
        log_refs: list[bytes] = []
        if snap.manifest_path.is_file():
            manifest = read_log_manifest(snap.manifest_path)
            for files in manifest.servers.values():
                for rec in files:
                    log_refs.append(rec.sha256)
        try:
            snap.manifest_path.unlink()
        except FileNotFoundError:
            pass
        with IndexDB(self.index_path) as index:
            index.adjust_log_refs(log_refs, delta=-1)
            index.remove_log_snapshot(snap.id)

    # ---- gc ----------------------------------------------------------------

    def fsck(self, *, repair: bool = True) -> "FsckReport":
        """Reconcile the repo's on-disk state with the index.

        Detects and (optionally) fixes the kinds of inconsistencies that
        a SIGINT / kill-9 / power-loss in the middle of a snapshot can
        leave behind:

        * **Orphan manifests**: ``manifests/<id>.mcbk`` exists but no
          snapshot row in the index points to it. Means snapshot() was
          interrupted between writing the manifest and committing the
          row. Repair: delete the manifest (the snapshot was never
          committed; refs weren't incremented for it).
        * **Dangling rows**: a snapshot row points at a manifest file
          that doesn't exist. Means the manifest was deleted out of
          band. Repair: delete the row and decrement the refs that
          would have been associated with it (best-effort; if those
          refs were already zeroed by gc, nothing changes).
        * **Stray ``.tmp.<pid>`` files** in the chunk/file/log/manifest
          directories: leftover from interrupted atomic writes. Repair:
          delete them.
        * **Stray index rows in chunks/files/log_files** with no actual
          on-disk blob: not technically a problem (gc will clean them
          on next sweep) but reported.

        With ``repair=False`` only reports — useful for dry-run.
        """
        from .log_manifest import LogManifestError, read_log_manifest

        report = FsckReport()

        # 1) Stray temp files everywhere
        for pool_root in (self.chunks.chunks_dir, self.chunks.files_dir,
                          self.logs.logs_dir, self.manifests_dir,
                          self.log_manifests_dir):
            if not pool_root.is_dir():
                continue
            for path in pool_root.rglob("*"):
                if path.is_file() and ".tmp." in path.name:
                    report.stray_temp_files.append(str(path))
                    if repair:
                        try:
                            path.unlink()
                        except OSError:
                            pass

        # 2) Snapshot row ↔ manifest reconciliation
        with IndexDB(self.index_path) as index:
            indexed_manifests = {
                row.id: row for row in index.list_snapshots()
            }
            on_disk_manifests = {
                p.stem: p for p in self.manifests_dir.glob("*.mcbk")
                if ".tmp." not in p.name
            }

            for snap_id, row in indexed_manifests.items():
                if snap_id not in on_disk_manifests:
                    report.dangling_rows.append(snap_id)
                    if repair:
                        # Reverse the refs this row claimed, then drop the row
                        chunk_refs, file_refs = self._collect_refs_from_row(
                            row, index,
                        )
                        index.adjust_chunk_refs(chunk_refs, delta=-1)
                        index.adjust_file_refs(file_refs, delta=-1)
                        index.remove_snapshot(snap_id)

            for manifest_id, manifest_path in on_disk_manifests.items():
                if manifest_id in indexed_manifests:
                    continue
                report.orphan_manifests.append(str(manifest_path))
                if repair:
                    # The snapshot wasn't committed — refs weren't bumped
                    # for this manifest, so deleting it is safe.
                    try:
                        manifest_path.unlink()
                    except OSError:
                        pass

            # 3) Same for log snapshots
            indexed_log_manifests = {
                row.id: row for row in index.list_log_snapshots()
            }
            on_disk_log_manifests = {
                p.stem: p for p in self.log_manifests_dir.glob("*.json")
                if ".tmp." not in p.name
            }
            for snap_id, row in indexed_log_manifests.items():
                if snap_id not in on_disk_log_manifests:
                    report.dangling_log_rows.append(snap_id)
                    if repair:
                        # Reverse log refs
                        try:
                            shas = self._collect_log_refs_from_row(row)
                            index.adjust_log_refs(shas, delta=-1)
                        except (LogManifestError, OSError):
                            pass
                        index.remove_log_snapshot(snap_id)
            for manifest_id, manifest_path in on_disk_log_manifests.items():
                if manifest_id in indexed_log_manifests:
                    continue
                report.orphan_log_manifests.append(str(manifest_path))
                if repair:
                    try:
                        manifest_path.unlink()
                    except OSError:
                        pass

        return report

    def _collect_refs_from_row(self, row, index) -> tuple[list[bytes], list[bytes]]:
        """Read a snapshot row's manifest and return its referenced hashes.
        Used by fsck to know which refs to reverse when dropping a dangling row."""
        manifest_path = self.repo_path / row.manifest_path
        if not manifest_path.is_file():
            return [], []
        try:
            manifest = read_manifest(manifest_path)
        except Exception:
            return [], []
        chunks: list[bytes] = []
        files: list[bytes] = []
        for regions in manifest.dimensions.values():
            for region in regions:
                for c in region.chunks:
                    chunks.append(c.content_hash)
        for f in manifest.files:
            files.append(f.sha256)
        return chunks, files

    def _collect_log_refs_from_row(self, row) -> list[bytes]:
        manifest_path = self.repo_path / row.manifest_path
        if not manifest_path.is_file():
            return []
        manifest = read_log_manifest(manifest_path)
        return [rec.sha256
                for files in manifest.servers.values()
                for rec in files]

    def gc(self) -> "GCResult":
        """Sweep zero-reference chunks, files, and log blobs.

        Uses the ref-count fast path: snapshot() and delete() (chunk + log
        flavors) maintain ref counts atomically. ``gc()`` picks rows with
        ref_count <= 0, deletes the on-disk blobs, drops the rows.

        Returns counts for each pool. Bootstraps from manifests if a legacy
        repo predates ref counting.
        """
        with IndexDB(self.index_path) as index:
            if index.needs_ref_bootstrap():
                self._bootstrap_refs(index)

            removed_chunks = 0
            removed_files = 0
            removed_logs = 0

            for h in index.gc_zero_ref_chunks():
                path = self.chunks._chunk_path(h)
                try:
                    path.unlink()
                    removed_chunks += 1
                except FileNotFoundError:
                    pass

            for h in index.gc_zero_ref_files():
                path = self.chunks._file_path(h)
                try:
                    path.unlink()
                    removed_files += 1
                except FileNotFoundError:
                    pass

            for h in index.gc_zero_ref_logs():
                path = self.logs._log_path(h)
                try:
                    path.unlink()
                    removed_logs += 1
                except FileNotFoundError:
                    pass

        return GCResult(
            chunks=removed_chunks, files=removed_files, logs=removed_logs,
        )

    def _bootstrap_refs(self, index: "IndexDB") -> None:
        """Populate ref counts for an index that predates ref counting.

        Walks every manifest, +1 per reference. Only runs once — after, the
        snapshot()/delete() flow keeps counts in sync.
        """
        for snap_row in index.list_snapshots():
            manifest_path = self.repo_path / snap_row.manifest_path
            if not manifest_path.is_file():
                continue
            manifest = read_manifest(manifest_path)
            chunks_in_manifest: list[bytes] = []
            files_in_manifest: list[bytes] = []
            for regions in manifest.dimensions.values():
                for region in regions:
                    for c in region.chunks:
                        chunks_in_manifest.append(c.content_hash)
            for f in manifest.files:
                files_in_manifest.append(f.sha256)
            index.adjust_chunk_refs(chunks_in_manifest, delta=1)
            index.adjust_file_refs(files_in_manifest, delta=1)

    # ---- delete -------------------------------------------------------------

    # ---- retime -------------------------------------------------------------

    def backup_index(self) -> Path:
        """Snapshot the SQLite index to ``index.sqlite.bak`` before risky ops.

        Overwrites any previous .bak — we keep ONE rollback point, not a
        history (the chunk pool itself is content-addressed and immutable;
        only the index + manifests carry mutable state). Restore by copying
        the .bak back over index.sqlite while no chunkvault process holds
        the WAL.
        """
        from shutil import copy2
        bak_path = self.index_path.with_name(self.index_path.name + ".bak")
        if self.index_path.is_file():
            copy2(self.index_path, bak_path)
        return bak_path

    def retime_snapshot(
        self,
        snapshot: ChunkSnapshot | str,
        new_timestamp: datetime,
    ) -> ChunkSnapshot:
        """Reassign a snapshot's timeline position.

        Atomic per snapshot: rewrite the manifest header, then update the
        index row in a single SQL transaction. Refuses to retime to a
        ``(label, timestamp)`` already occupied by another snapshot — a
        collision would make label-based lookups silently shadow rows.

        On the FIRST retime of a snapshot, the previous timestamp is
        captured into both the manifest and the index as
        ``original_timestamp_ms`` so the change is auditable. Subsequent
        retimes preserve that original (so "where was this snapshot born?"
        always answers, no matter how many times you adjust it).
        """
        snap = snapshot if isinstance(snapshot, ChunkSnapshot) else self.get(snapshot)
        if snap is None:
            raise ChunkRepoError(f"No such snapshot: {snapshot!r}")
        if not snap.manifest_path.is_file():
            raise ChunkRepoError(
                f"manifest missing for {snap.short_id}: {snap.manifest_path}"
            )

        new_ts_utc = new_timestamp.astimezone(timezone.utc)
        new_ts_ms = int(new_ts_utc.timestamp() * 1000)

        manifest = read_manifest(snap.manifest_path)
        old_ts_ms = manifest.header.timestamp_ms
        if old_ts_ms == new_ts_ms:
            return snap  # no-op

        # First retime captures the pre-existing timestamp as "original";
        # subsequent retimes preserve that first-original (so audit always
        # answers "where was this snapshot born?", not "what was it last").
        first_retime = manifest.header.original_timestamp_ms == 0
        new_original = (
            old_ts_ms if first_retime else manifest.header.original_timestamp_ms
        )

        with IndexDB(self.index_path) as index:
            # Collision: refuse if another snapshot already sits at this
            # exact (label, timestamp). Retime to current ts was the no-op above.
            existing_id = index.find_snapshot_by_label_and_timestamp(
                manifest.header.label or "", new_ts_ms,
            )
            if existing_id is not None and existing_id != snap.id:
                raise ChunkRepoError(
                    f"refusing retime: snapshot {existing_id[:12]} already "
                    f"sits at label={manifest.header.label!r} timestamp="
                    f"{new_ts_utc.isoformat()}"
                )

            manifest.header.timestamp_ms = new_ts_ms
            manifest.header.original_timestamp_ms = new_original
            write_manifest(snap.manifest_path, manifest)

            index.update_snapshot_timestamp(
                snap.id, new_ts_ms,
                original_timestamp_ms=new_original if first_retime else None,
            )

        return ChunkSnapshot(
            id=snap.id, label=snap.label,
            timestamp=new_ts_utc, world_name=snap.world_name,
            mc_version=snap.mc_version, data_version=snap.data_version,
            manifest_path=snap.manifest_path,
        )

    def retime_snapshot_from_manifest(
        self, snapshot: ChunkSnapshot | str,
    ) -> tuple[ChunkSnapshot, str]:
        """Convenience: retime to ``manifest.header.last_played_ms``.

        Returns ``(updated_snap, source)`` — ``source`` is "last_played" on
        success, or "no_last_played" when the manifest doesn't carry one
        (older v1 manifests, or v2 manifests where the field defaulted to
        zero because LastPlayed wasn't readable at snapshot time). Raises
        on the same conditions as ``retime_snapshot``.
        """
        snap = snapshot if isinstance(snapshot, ChunkSnapshot) else self.get(snapshot)
        if snap is None:
            raise ChunkRepoError(f"No such snapshot: {snapshot!r}")
        manifest = read_manifest(snap.manifest_path)
        lp_ms = manifest.header.last_played_ms
        if not lp_ms:
            return snap, "no_last_played"
        new_ts = datetime.fromtimestamp(lp_ms / 1000, tz=timezone.utc)
        updated = self.retime_snapshot(snap, new_ts)
        return updated, "last_played"

    # ---- delete -------------------------------------------------------------

    def delete(self, snapshot: ChunkSnapshot | str) -> None:
        snap = snapshot if isinstance(snapshot, ChunkSnapshot) else self.get(snapshot)
        if snap is None:
            raise ChunkRepoError(f"No such snapshot: {snapshot!r}")
        # Decrement ref counts for everything this snapshot held a reference to,
        # so a later gc can reclaim chunks/files that nothing else references.
        chunk_refs: list[bytes] = []
        file_refs: list[bytes] = []
        if snap.manifest_path.is_file():
            manifest = read_manifest(snap.manifest_path)
            for regions in manifest.dimensions.values():
                for region in regions:
                    for c in region.chunks:
                        chunk_refs.append(c.content_hash)
            for f in manifest.files:
                file_refs.append(f.sha256)
        try:
            snap.manifest_path.unlink()
        except FileNotFoundError:
            pass
        with IndexDB(self.index_path) as index:
            index.adjust_chunk_refs(chunk_refs, delta=-1)
            index.adjust_file_refs(file_refs, delta=-1)
            index.remove_snapshot(snap.id)


# ---- helpers ----------------------------------------------------------------

def _level_dat_candidates(world: Path) -> list[Path]:
    """Locate plausible level.dat paths for a directory.

    Tries ``world/level.dat`` first (when ``world`` IS the world dir), then
    falls back to a bounded search up to depth 2 (when ``world`` is a server
    root containing a world subdir like ``EX-Server/world/level.dat`` or
    ``EX-Server/survival/level.dat``).
    """
    candidates = [world / "level.dat"]
    if not candidates[0].is_file():
        try:
            for child in world.iterdir():
                if child.is_dir():
                    cand = child / "level.dat"
                    if cand.is_file():
                        candidates.append(cand)
        except OSError:
            pass
    return [c for c in candidates if c.is_file()]


def _read_level_dat_nbt(level_path: Path) -> bytes | None:
    """Decompress a level.dat to NBT bytes; return None on any failure."""
    try:
        raw = level_path.read_bytes()
        return decompress_chunk_payload(1, raw)
    except Exception:
        return None


def _read_level_dat_version(world: Path) -> tuple[str | None, int | None]:
    """Best-effort read of mc_version + data_version from a level.dat."""
    for level in _level_dat_candidates(world):
        nbt = _read_level_dat_nbt(level)
        if nbt is None:
            continue
        version = find_version_info(nbt)
        if version != (None, None):
            return version
    return None, None


def _read_level_dat_last_played(world: Path) -> int | None:
    """Best-effort read of ``Data.LastPlayed`` (Unix-epoch ms) from level.dat.

    Returns None if no level.dat is found or the field is absent. ``LastPlayed``
    is set whenever Minecraft saves the world, so it tracks "when this world
    state was created" much more accurately than ``datetime.now()`` for
    snapshots taken from copied/archived save folders.
    """
    from ..mca.nbt_lite import find_last_played
    for level in _level_dat_candidates(world):
        nbt = _read_level_dat_nbt(level)
        if nbt is None:
            continue
        lp = find_last_played(nbt)
        if lp is not None:
            return lp
    return None


def _detect_world_timestamp(world: Path) -> tuple[datetime | None, str]:
    """Pick the best available timestamp for a world directory.

    Priority chain:

    1. ``level.dat``'s ``LastPlayed`` field (NBT TAG_Long, Unix ms) — set
       whenever Minecraft saves the world.
    2. Newest ``region/*.mca`` mtime — when at least one region file was
       last touched (close enough to last save when level.dat is missing
       or pre-LastPlayed).
    3. None (caller falls back to ``datetime.now()``).

    Returns ``(timestamp, source)``. ``source`` is one of "last_played",
    "region_mtime", or "none" — useful for telling the user where the
    timestamp came from.
    """
    lp_ms = _read_level_dat_last_played(world)
    if lp_ms is not None and lp_ms > 0:
        return datetime.fromtimestamp(lp_ms / 1000, tz=timezone.utc), "last_played"

    newest_mtime: float = 0.0
    for region_dir in enumerate_region_dirs(world):
        for _, _, region_path in iter_region_files(region_dir.path):
            try:
                mtime = region_path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest_mtime = mtime
    if newest_mtime > 0:
        return datetime.fromtimestamp(newest_mtime, tz=timezone.utc), "region_mtime"
    return None, "none"


def _walk_world_files(world: Path) -> Iterator[Path]:
    """Yield every file under ``world``, recursively."""
    for entry in world.rglob("*"):
        if entry.is_file():
            yield entry


def _looks_like_mc_world(world: Path) -> bool:
    """Cheap sanity check: refuse to snapshot directories that obviously
    aren't MC worlds (the archive folder, the vault itself, /home, etc).

    Accepts ANY directory containing either:

    * ``level.dat`` at the root, OR
    * any sub-directory matching the pattern ``*/region/r.X.Z.mca`` (at any
      reasonable depth) — covers vanilla, Bukkit/Paper world_nether, custom
      world names from server.properties, datapack dimensions, and even
      "world is at the archive root with no wrapper" cases.

    Used to be: hardcoded list of known dim-dir names. That broke on real
    user data with custom world names.
    """
    if not world.is_dir():
        return False
    if (world / "level.dat").is_file():
        return True
    # Use the same finder enumerate_region_dirs uses — first hit is enough
    from ..world.layout import _find_region_dirs
    for _ in _find_region_dirs(world, max_depth=6):
        return True
    return False


def _manifest_chunk_map(manifest: Manifest) -> dict[tuple, bytes]:
    out: dict[tuple, bytes] = {}
    for dim_key, regions in manifest.dimensions.items():
        for region in regions:
            for c in region.chunks:
                out[(dim_key, region.rx, region.rz, c.cx, c.cz)] = c.content_hash
    return out


def _iter_pool_blobs(pool_dir: Path) -> Iterator[tuple[Path, bytes]]:
    """Yield (path, expected_hash) for every managed blob under pool_dir."""
    if not pool_dir.is_dir():
        return
    for path in pool_dir.rglob("*"):
        if not path.is_file():
            continue
        if ".tmp." in path.name:
            continue  # in-flight write; skip
        try:
            hex_str = (
                path.parent.parent.name + path.parent.name + path.name
            )
            yield path, bytes.fromhex(hex_str)
        except ValueError:
            continue


def _sweep_pool(pool_dir: Path, reachable: set[bytes]) -> int:
    """Delete files under pool_dir whose path-encoded hash isn't in reachable."""
    if not pool_dir.is_dir():
        return 0
    removed = 0
    for path in pool_dir.rglob("*"):
        if not path.is_file():
            continue
        # Path layout: pool_dir/XX/YY/<rest>; recover full hex
        try:
            hex_str = (
                path.parent.parent.name + path.parent.name + path.name
            )
            h = bytes.fromhex(hex_str)
        except ValueError:
            # Not a managed file (could be a *.tmp.* leftover) — skip.
            continue
        if h not in reachable:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _matches_any(rel_posix: str, patterns: tuple[str, ...]) -> bool:
    for pat in patterns:
        if pat.endswith("/**"):
            prefix = pat[:-3]
            if rel_posix == prefix or rel_posix.startswith(prefix + "/"):
                return True
        elif "/" in pat:
            if fnmatch.fnmatchcase(rel_posix, pat):
                return True
        else:
            base = rel_posix.rsplit("/", 1)[-1]
            if fnmatch.fnmatchcase(base, pat):
                return True
    return False


def _path_matches_filter(rel_posix: str, path_filter: set[str]) -> bool:
    """True if the path passes user's --path filter (exact or under-dir match)."""
    if rel_posix in path_filter:
        return True
    for prefix in path_filter:
        if rel_posix.startswith(prefix.rstrip("/") + "/"):
            return True
    return False
