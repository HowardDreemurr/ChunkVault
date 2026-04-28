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


_REGION_CACHE_FORMAT_V1 = 1


def _pack_region_cache(records: list[ChunkRecord]) -> bytes:
    """Serialize a region's chunk records for the ``region_cache`` table.

    Layout: 1B format-version, 2B chunk-count (big-endian), then per record
    1B cx + 1B cz + 1B compression + 4B timestamp (signed) + 16B
    content_hash = 23 bytes/record. Compact — a fully populated 1024-chunk
    region fits in ~23 KB.
    """
    import struct
    out = bytearray()
    out.append(_REGION_CACHE_FORMAT_V1)
    out += struct.pack(">H", len(records))
    for c in records:
        if len(c.content_hash) != 16:
            raise ValueError(
                f"content_hash must be 16 bytes (got {len(c.content_hash)})"
            )
        out += struct.pack(">BBBi", c.cx, c.cz, c.compression, c.timestamp)
        out += c.content_hash
    return bytes(out)


def _unpack_region_cache(blob: bytes) -> list[ChunkRecord]:
    """Inverse of :func:`_pack_region_cache`. Raises ValueError on a malformed
    blob; callers fall back to the slow path."""
    import struct
    if len(blob) < 3:
        raise ValueError("region_cache blob too short")
    version = blob[0]
    if version != _REGION_CACHE_FORMAT_V1:
        raise ValueError(f"unknown region_cache format version: {version}")
    (count,) = struct.unpack_from(">H", blob, 1)
    expected = 3 + count * 23
    if len(blob) != expected:
        raise ValueError(
            f"region_cache blob size {len(blob)} != expected {expected} "
            f"for {count} chunks"
        )
    out: list[ChunkRecord] = []
    off = 3
    for _ in range(count):
        cx, cz, compression, timestamp = struct.unpack_from(">BBBi", blob, off)
        off += 7
        out.append(ChunkRecord(
            cx=cx, cz=cz, compression=compression, timestamp=timestamp,
            content_hash=blob[off:off + 16],
        ))
        off += 16
    return out


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


@dataclass
class RetimePlan:
    """One snapshot's pending retime in a :meth:`repair_timestamps` plan."""
    snap_id: str
    world_name: str
    old_ts_ms: int
    new_ts_ms: int
    old_label: str
    new_label: str


@dataclass
class DuplicateGroup:
    """A set of snapshots that should collapse into one after repair."""
    world_name: str
    target_ts_ms: int
    winner_id: str
    loser_ids: list[str] = field(default_factory=list)


@dataclass
class RepairReport:
    """Result of :meth:`ChunkSnapshotRepo.repair_timestamps`."""
    scanned: int = 0
    already_correct: int = 0
    # Count of snapshots whose manifest header had last_played_ms == 0 but
    # whose level.dat in the file pool was successfully read to recover it.
    # This is the "rescue" path for snapshots taken before chunkvault
    # started reading LastPlayed at ingest time.
    recovered_from_pool: int = 0
    # Snapshots whose index row's timestamp/label disagrees with the
    # manifest header — typically because a previous repair was Ctrl+C'd
    # between manifest write (atomic, succeeded) and the SQL update.
    # On apply we sync index ← manifest (manifest is authoritative).
    index_lag_to_reconcile: int = 0
    index_lag_reconciled: int = 0
    no_last_played: list[tuple[str, str]] = field(default_factory=list)  # (id, label)
    unreadable: list[tuple[str, str]] = field(default_factory=list)      # (id, error)
    to_retime: list[RetimePlan] = field(default_factory=list)
    duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    to_delete: list[tuple[str, str]] = field(default_factory=list)        # (id, label)
    # Populated when dry_run=False:
    applied: bool = False
    deleted: list[str] = field(default_factory=list)
    retimed: list[str] = field(default_factory=list)
    errors: list[tuple[str, str, str]] = field(default_factory=list)      # (op, id, msg)

    def summary(self) -> str:
        verdict = (
            "APPLIED" if self.applied else
            ("CLEAN"
             if not (self.to_retime or self.to_delete or self.index_lag_to_reconcile)
             else "DRY-RUN")
        )
        return (
            f"{verdict}  scanned={self.scanned}, correct={self.already_correct}, "
            f"need_retime={len(self.to_retime)}, "
            f"recovered_from_pool={self.recovered_from_pool}, "
            f"index_lag={self.index_lag_to_reconcile}, "
            f"duplicate_groups={len(self.duplicate_groups)}, "
            f"to_delete={len(self.to_delete)}, "
            f"no_last_played={len(self.no_last_played)}, "
            f"unreadable={len(self.unreadable)}, "
            f"errors={len(self.errors)}"
        )


@dataclass
class MigrateMcaReport:
    """Result of :meth:`ChunkSnapshotRepo.migrate_mca_files_to_chunks`."""
    scanned: int = 0
    snapshots_with_mca_files: int = 0
    mca_files_total: int = 0
    unreadable: list[tuple[str, str]] = field(default_factory=list)  # (id, err)
    # Populated when dry_run=False:
    applied: bool = False
    snapshots_rewritten: int = 0
    files_migrated: int = 0
    chunks_added: int = 0
    skipped_external: int = 0
    errors: list[tuple[str, str, str]] = field(default_factory=list)  # (op, id, msg)

    def summary(self) -> str:
        verdict = (
            "APPLIED" if self.applied else
            ("CLEAN" if self.mca_files_total == 0 else "DRY-RUN")
        )
        return (
            f"{verdict}  scanned={self.scanned}, "
            f"snaps_with_mca_files={self.snapshots_with_mca_files}, "
            f"files_total={self.mca_files_total}, "
            f"files_migrated={self.files_migrated}, "
            f"chunks_added={self.chunks_added}, "
            f"snaps_rewritten={self.snapshots_rewritten}, "
            f"skipped_external={self.skipped_external}, "
            f"errors={len(self.errors)}"
        )


@dataclass
class BackfillStats:
    """Counts returned from :meth:`ChunkSnapshotRepo.backfill_region_cache`."""
    written: int = 0
    already_cached: int = 0
    skipped_external: int = 0   # region had external (.mcc) chunks
    skipped_missing: int = 0    # .mca not present in source world
    errors: int = 0


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
    # Manifest-vs-index ref-count desync. Manifests reference these
    # chunks/files but the index has fewer (or no) ref counts for them
    # — typical aftermath of a Ctrl+C during migrate-mca-files between
    # the manifest write and the SQL ref bumps. fsck(repair=True)
    # catches this up by reading manifests and adding the missing refs.
    manifest_unreferenced_chunks: int = 0
    manifest_unreferenced_files: int = 0
    repaired: int = 0

    @property
    def total_issues(self) -> int:
        return (len(self.orphan_manifests) + len(self.dangling_rows)
                + len(self.orphan_log_manifests) + len(self.dangling_log_rows)
                + len(self.stray_temp_files)
                + self.manifest_unreferenced_chunks
                + self.manifest_unreferenced_files)

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
            f"stray-temp-files={len(self.stray_temp_files)}, "
            f"manifest-unreferenced-chunks={self.manifest_unreferenced_chunks}, "
            f"manifest-unreferenced-files={self.manifest_unreferenced_files}"
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

    def backfill_region_cache(
        self,
        world: Path | str,
        snapshot: "ChunkSnapshot | str",
        *,
        progress_cb: ProgressCallback = None,
    ) -> "BackfillStats":
        """Populate ``region_cache`` from an existing snapshot's manifest +
        the original world tree.

        Used when a user re-ingests an archive that was processed by an older
        version (no cache entries) — instead of repeating the full snapshot
        pipeline (parse + hash every chunk), we cheaply hash each .mca's
        bytes and pair the sha with the manifest's pre-computed chunk
        records. Roughly: ~30 ms × N regions instead of ~10 s × N regions.

        Skipped: regions whose .mca isn't present in ``world`` (archive
        layout drift), regions containing external chunks (their fingerprint
        depends on .mcc bytes too — see ``_snapshot_region``), and entries
        that already exist in the cache.
        """
        world_path = Path(world)
        snap = snapshot if isinstance(snapshot, ChunkSnapshot) else self.get(snapshot)
        if snap is None:
            raise ChunkRepoError(f"No such snapshot: {snapshot!r}")
        manifest = read_manifest(snap.manifest_path)

        total = sum(len(rs) for rs in manifest.dimensions.values())
        _emit(progress_cb, ProgressEvent(
            kind="phase_start", phase="backfill_region_cache",
            label=f"{total} regions from {snap.label or snap.id[:12]}",
            total=total,
        ))

        stats = BackfillStats()
        i = 0
        with IndexDB(self.index_path) as index:
            for dim_key, regions in manifest.dimensions.items():
                for region in regions:
                    i += 1
                    if any(c.external for c in region.chunks):
                        stats.skipped_external += 1
                        _emit(progress_cb, ProgressEvent(
                            kind="phase_progress", phase="backfill_region_cache",
                            label=f"skip(ext) {dim_key}/r.{region.rx}.{region.rz}.mca",
                            current=i, total=total,
                        ))
                        continue
                    mca_path = world_path.joinpath(
                        *dim_key.split("/"),
                        f"r.{region.rx}.{region.rz}.mca",
                    )
                    if not mca_path.is_file():
                        stats.skipped_missing += 1
                        _emit(progress_cb, ProgressEvent(
                            kind="phase_progress", phase="backfill_region_cache",
                            label=f"skip(absent) {dim_key}/r.{region.rx}.{region.rz}.mca",
                            current=i, total=total,
                        ))
                        continue
                    try:
                        region_bytes = mca_path.read_bytes()
                    except OSError:
                        stats.skipped_missing += 1
                        continue
                    region_sha = hashlib.sha256(region_bytes).digest()
                    if index.get_region_cache(region_sha) is not None:
                        stats.already_cached += 1
                    else:
                        try:
                            blob = _pack_region_cache(region.chunks)
                        except ValueError:
                            stats.errors += 1
                            continue
                        index.put_region_cache(region_sha, blob)
                        stats.written += 1
                    _emit(progress_cb, ProgressEvent(
                        kind="phase_progress", phase="backfill_region_cache",
                        label=f"{dim_key}/r.{region.rx}.{region.rz}.mca",
                        current=i, total=total,
                    ))

        _emit(progress_cb, ProgressEvent(
            kind="phase_done", phase="backfill_region_cache",
            current=i, total=total,
            detail={
                "written": stats.written,
                "already_cached": stats.already_cached,
                "skipped_external": stats.skipped_external,
                "skipped_missing": stats.skipped_missing,
                "errors": stats.errors,
            },
        ))
        return stats

    def _snapshot_region(  # noqa: C901 — the two fast paths plus a slow path
        self,
        region_path: Path,
        rx: int,
        rz: int,
        dim_key: str,
        index: IndexDB,
        *,
        visited_mcc: set[Path],
    ) -> tuple[RegionRecord | None, int]:
        """Hash + store every chunk in one region. Returns (record, new_chunk_count).

        Two-tier fast path:

        1. **Region-content cache.** Key = sha256(region_bytes). On hit and
           every cached chunk hash still present in the pool, return the
           cached records with zero parsing and zero per-chunk hashing.
           Without this, every snapshot of an unchanged world re-hashes
           ~1024 chunks per region — for a 50K-region world that's tens of
           millions of pointless hashes per snapshot.

        2. **Bulk presence probe.** When the region IS new (or cache stale),
           collapse 1024 individual ``has_chunk`` SELECTs into one
           ``has_chunks_bulk`` query. ~1000× fewer SQLite round-trips on
           cold regions.

        Regions with external (.mcc) chunks bypass the region cache because
        a region's bytes can be unchanged while its referenced .mcc files
        change — keying by region_sha alone would return stale hashes. They
        still benefit from the bulk presence probe.
        """
        try:
            region_bytes = region_path.read_bytes()
        except OSError:
            return None, 0
        region_sha = hashlib.sha256(region_bytes).digest()

        cached_blob = index.get_region_cache(region_sha)
        if cached_blob is not None:
            try:
                cached_records = _unpack_region_cache(cached_blob)
            except ValueError:
                cached_records = None  # corrupt entry — fall through and rebuild
            if cached_records is not None:
                cached_hashes = [c.content_hash for c in cached_records]
                # Cache may outlive its chunks if a gc reclaimed them. Verify
                # via one bulk lookup; if any are missing, fall through to
                # the slow path which will re-store them and refresh the row.
                present = index.has_chunks_bulk(cached_hashes)
                if all(h in present for h in cached_hashes):
                    for c in cached_records:
                        if c.compression & EXTERNAL_FLAG:
                            world_cx = rx * 32 + c.cx
                            world_cz = rz * 32 + c.cz
                            visited_mcc.add(
                                region_path.parent
                                / f"c.{world_cx}.{world_cz}.mcc"
                            )
                    return RegionRecord(
                        rx=rx, rz=rz, chunks=cached_records,
                    ), 0

        try:
            region = Region.from_bytes(
                region_bytes, rx=rx, rz=rz, source=region_path.name,
            )
        except MCAError:
            return None, 0
        try:
            chunks_iter = list(region.iter_chunks())
        except MCAError:
            return None, 0

        rec = RegionRecord(rx=rx, rz=rz, chunks=[])
        # Hold (hash, masked_compression, payload) so we can bulk-check all
        # hashes in one SQLite call before deciding what to write.
        pending: list[tuple[bytes, int, bytes]] = []
        has_external = False
        for chunk in chunks_iter:
            if chunk.external:
                has_external = True
                world_cx = rx * 32 + chunk.cx
                world_cz = rz * 32 + chunk.cz
                mcc_path = region_path.parent / f"c.{world_cx}.{world_cz}.mcc"
                visited_mcc.add(mcc_path)
                payload = mcc_path.read_bytes() if mcc_path.is_file() else b""
                h = hash_chunk(chunk, external_payload=payload)
            else:
                payload = chunk.payload
                h = hash_chunk(chunk)
            masked = chunk.compression & ~EXTERNAL_FLAG
            pending.append((h, masked, payload))
            rec.chunks.append(ChunkRecord(
                cx=chunk.cx, cz=chunk.cz,
                compression=chunk.compression,
                timestamp=chunk.timestamp,
                content_hash=h,
            ))

        unique_hashes = list({h for h, _, _ in pending})
        present = index.has_chunks_bulk(unique_hashes)
        new_hashes: list[bytes] = []
        written: set[bytes] = set()
        for h, masked, payload in pending:
            if h in present or h in written:
                continue
            blob = bytes([masked]) + payload
            self.chunks.store_chunk(h, blob)
            new_hashes.append(h)
            written.add(h)
        if new_hashes:
            index.add_chunks(new_hashes)

        # Populate the region cache for next time. Externals would invalidate
        # the keying scheme (see docstring), so they don't get cached.
        if not has_external:
            try:
                index.put_region_cache(
                    region_sha, _pack_region_cache(rec.chunks),
                )
            except ValueError:
                # Pack rejected something (e.g. bad hash length). The snapshot
                # itself is fine; just skip caching this region.
                pass

        return rec, len(new_hashes)

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

            # 4) Manifest-vs-index reconciliation: every chunk/file that an
            # indexed manifest references must have a presence row + adequate
            # ref_count. Missing rows mean a previous write (snapshot,
            # migrate-mca, etc.) was Ctrl+C'd between manifest write and
            # SQL update — the manifest is the durable truth, so we add
            # what's missing here. This catches the migrate-mca chaos
            # window where chunks landed in the pool + manifest but never
            # got registered in the index.
            expected_chunk_refs: dict[bytes, int] = {}
            expected_file_refs: dict[bytes, int] = {}
            for snap_id, row in indexed_manifests.items():
                if snap_id not in on_disk_manifests:
                    continue   # already handled above as dangling row
                try:
                    m = read_manifest(on_disk_manifests[snap_id])
                except Exception:
                    continue
                for regions in m.dimensions.values():
                    for region in regions:
                        for c in region.chunks:
                            expected_chunk_refs[c.content_hash] = (
                                expected_chunk_refs.get(c.content_hash, 0) + 1
                            )
                for f in m.files:
                    expected_file_refs[f.sha256] = (
                        expected_file_refs.get(f.sha256, 0) + 1
                    )

            # Check each expected ref against the index. We only DETECT
            # under-references here (manifest needs more than index has);
            # over-references (index has refs but manifest doesn't) are
            # leaks gc could catch later, less urgent.
            cur = index._conn.execute("SELECT content_hash, ref_count FROM chunks")
            actual_chunk_refs = {bytes(h): r for (h, r) in cur.fetchall()}
            cur = index._conn.execute("SELECT content_hash, ref_count FROM files")
            actual_file_refs = {bytes(h): r for (h, r) in cur.fetchall()}

            chunk_underrefs: list[tuple[bytes, int]] = []   # (hash, missing_count)
            for h, want in expected_chunk_refs.items():
                have = actual_chunk_refs.get(h, 0)
                if have < want:
                    chunk_underrefs.append((h, want - have))
            file_underrefs: list[tuple[bytes, int]] = []
            for sha, want in expected_file_refs.items():
                have = actual_file_refs.get(sha, 0)
                if have < want:
                    file_underrefs.append((sha, want - have))

            report.manifest_unreferenced_chunks = len(chunk_underrefs)
            report.manifest_unreferenced_files = len(file_underrefs)
            if repair and (chunk_underrefs or file_underrefs):
                try:
                    index._conn.execute("BEGIN")
                    for h, missing in chunk_underrefs:
                        index._conn.execute(
                            "INSERT INTO chunks (content_hash, ref_count) "
                            "VALUES (?, ?) "
                            "ON CONFLICT(content_hash) DO UPDATE "
                            "SET ref_count = ref_count + ?",
                            (h, missing, missing),
                        )
                    for sha, missing in file_underrefs:
                        index._conn.execute(
                            "INSERT INTO files (content_hash, ref_count) "
                            "VALUES (?, ?) "
                            "ON CONFLICT(content_hash) DO UPDATE "
                            "SET ref_count = ref_count + ?",
                            (sha, missing, missing),
                        )
                    index._conn.execute("COMMIT")
                    report.repaired += len(chunk_underrefs) + len(file_underrefs)
                except Exception:
                    index._conn.execute("ROLLBACK")
                    raise

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

    def repair_timestamps(
        self, *,
        dry_run: bool = True,
        fsck_first: bool = True,
        progress_cb: ProgressCallback = None,
    ) -> "RepairReport":
        """Walk every snapshot and align ``timestamp_ms`` with
        ``manifest.last_played_ms`` (level.dat's authoritative time).

        Solves the historical bug where ``ingest_archive`` would fall back to
        ``datetime.now()`` when a filename didn't match its time-stamp regex,
        producing wrong-timestamped snapshots whose labels also embedded the
        wrong time. The fix data is on disk: the manifest header's
        ``last_played_ms`` was always read correctly from level.dat at
        snapshot time, so we don't need the source archive in hand.

        Stages (computed first, applied second):

        1. Group snapshots by ``(world_name, last_played_ms)`` for entries
           where ``last_played_ms > 0`` and differs from current
           ``timestamp_ms``. Multiple snapshots in a group are *duplicates*
           (created by repeated ingest of the same archive under different
           wrong fallback timestamps).
        2. For each duplicate group: pick a winner (prefer one whose current
           ``timestamp_ms`` already matches ``last_played_ms``; ties broken
           by id-string sort), mark losers for deletion.
        3. For each surviving snapshot whose timestamp is wrong: retime to
           ``last_played_ms``, and if its label follows the
           ``<world_name>-<YYYY-MM-DD-HH-MM-SS>`` ingest pattern, update
           the label to match.

        With ``dry_run=True`` (default), nothing is written — only a report
        is produced. ``dry_run=False`` applies the plan in dependency order
        (delete losers first so retimes don't collide).
        """
        from ..wizard.i18n import t as _t

        # Default-on safety net: fsck before scanning. Catches the half-
        # applied states left behind by a Ctrl+C in a previous repair (or
        # any other write op): orphan manifests, dangling rows, stray
        # .tmp files, manifest-vs-index ref-count desync. We run fsck
        # with repair=True even in dry-run mode for the OUTER call —
        # fsck's repairs are themselves safe and idempotent, and a clean
        # vault is a precondition for the dry-run report being accurate.
        if fsck_first:
            _emit(progress_cb, ProgressEvent(
                kind="phase_start", phase="repair_pre_fsck",
                label=_t("phase.repair_scan.reading", count=0),  # placeholder, fsck has its own progress
            ))
            try:
                self.fsck(repair=True)
            except Exception as e:
                _emit(progress_cb, ProgressEvent(
                    kind="warning", phase="repair_pre_fsck",
                    label=f"fsck failed (continuing): {e}",
                ))
            _emit(progress_cb, ProgressEvent(
                kind="phase_done", phase="repair_pre_fsck",
            ))

        report = RepairReport()
        all_snaps = self.list()
        report.scanned = len(all_snaps)

        _emit(progress_cb, ProgressEvent(
            kind="phase_start", phase="repair_scan",
            label=_t("phase.repair_scan.reading", count=len(all_snaps)),
            total=len(all_snaps),
        ))

        # Per-snapshot state: (snap, current_ts_ms, target_ts_ms_or_None,
        #                      recovered_lp_or_None)
        # recovered_lp is set when we pulled LastPlayed out of the file pool
        # (instead of finding it pre-recorded in the manifest header). On
        # apply we'll persist it back to the manifest so future runs see
        # the field directly.
        plans: list[tuple[ChunkSnapshot, int, int | None, int | None]] = []
        # Snapshots whose manifest header disagrees with the index row
        # (timestamp_ms or label) — typically the result of a Ctrl+C
        # between manifest write and SQL update in a previous repair.
        # These need an index-only reconciliation; the manifest is
        # authoritative because it's the durable per-snapshot file.
        index_lag: list[tuple[str, int, str]] = []   # (id, manifest_ts_ms, manifest_label)
        for i, snap in enumerate(all_snaps, 1):
            try:
                manifest = read_manifest(snap.manifest_path)
            except Exception as e:
                report.unreadable.append((snap.id, str(e)))
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="repair_scan",
                    current=i, total=len(all_snaps),
                ))
                continue
            current_ts_ms = manifest.header.timestamp_ms
            manifest_label = manifest.header.label or ""

            # Detect index-vs-manifest desync. snap.timestamp comes from the
            # index; manifest.header.timestamp_ms is the durable truth.
            index_ts_ms = int(snap.timestamp.timestamp() * 1000)
            index_label = snap.label or ""
            if index_ts_ms != current_ts_ms or index_label != manifest_label:
                index_lag.append((snap.id, current_ts_ms, manifest_label))

            lp_ms = manifest.header.last_played_ms
            recovered: int | None = None
            if not lp_ms:
                # Pre-fix-era manifest: try to pull level.dat out of the
                # file pool and read LastPlayed there. The bytes are
                # content-addressed and immutable, so this works as well
                # now as it did at ingest time.
                recovered = self._recover_last_played_from_pool(manifest)
                if recovered:
                    lp_ms = recovered
                    report.recovered_from_pool += 1
            if not lp_ms:
                report.no_last_played.append((snap.id, snap.label or ""))
                plans.append((snap, current_ts_ms, None, None))
            else:
                plans.append((snap, current_ts_ms, lp_ms, recovered))
            _emit(progress_cb, ProgressEvent(
                kind="phase_progress", phase="repair_scan",
                current=i, total=len(all_snaps),
            ))
        report.index_lag_to_reconcile = len(index_lag)

        # Group by (world_name, target_ts_ms) for those that have a target.
        groups: dict[tuple[str, int], list[tuple[ChunkSnapshot, int]]] = {}
        for snap, cur_ts, target, _rec in plans:
            if target is None:
                continue
            groups.setdefault((snap.world_name, target), []).append((snap, cur_ts))

        # Decide winner per group, mark losers for deletion.
        delete_ids: set[str] = set()
        for (world_name, target_ts), members in groups.items():
            if len(members) <= 1:
                continue
            # Prefer the one whose current ts already matches target (i.e.,
            # it's correctly placed); ties broken by id-string sort for
            # determinism.
            members_sorted = sorted(
                members,
                key=lambda m: (m[1] != target_ts, m[0].id),
            )
            winner = members_sorted[0][0]
            losers = [m[0] for m in members_sorted[1:]]
            report.duplicate_groups.append(DuplicateGroup(
                world_name=world_name,
                target_ts_ms=target_ts,
                winner_id=winner.id,
                loser_ids=[l.id for l in losers],
            ))
            for loser in losers:
                delete_ids.add(loser.id)
                report.to_delete.append((loser.id, loser.label or ""))

        # Compute retime plan for surviving snapshots.
        recovered_for: dict[str, int] = {}    # snap_id -> recovered_lp_ms
        for snap, cur_ts, target, recovered in plans:
            if snap.id in delete_ids or target is None:
                continue
            if cur_ts == target:
                # Already correctly placed. If we recovered LP from the pool,
                # still queue a manifest update so the field gets persisted
                # — otherwise the next repair run does the recovery again.
                if recovered is not None:
                    recovered_for[snap.id] = recovered
                report.already_correct += 1
                continue
            old_label = snap.label or ""
            new_label = self._maybe_retitle_label(
                old_label, snap.world_name, cur_ts, target,
            )
            report.to_retime.append(RetimePlan(
                snap_id=snap.id,
                world_name=snap.world_name,
                old_ts_ms=cur_ts,
                new_ts_ms=target,
                old_label=old_label,
                new_label=new_label,
            ))
            if recovered is not None:
                recovered_for[snap.id] = recovered

        _emit(progress_cb, ProgressEvent(
            kind="phase_done", phase="repair_scan",
            current=len(all_snaps), total=len(all_snaps),
        ))

        if dry_run:
            return report

        # Apply: delete losers FIRST so retimes don't run into label/ts
        # collisions with snapshots that are about to disappear anyway.
        _emit(progress_cb, ProgressEvent(
            kind="phase_start", phase="repair_apply",
            label=_t("phase.repair_apply.label",
                     dels=len(report.to_delete),
                     retimes=len(report.to_retime)),
            total=len(report.to_delete) + len(report.to_retime),
        ))
        applied = 0
        for snap_id, _label in report.to_delete:
            try:
                self.delete(snap_id)
                report.deleted.append(snap_id)
            except Exception as e:
                report.errors.append(("delete", snap_id, str(e)))
            applied += 1
            _emit(progress_cb, ProgressEvent(
                kind="phase_progress", phase="repair_apply",
                current=applied,
            ))
        # Hold one IndexDB connection across the whole apply loop instead
        # of opening/closing per snapshot — for vaults with hundreds of
        # snapshots, the connection setup cost dominated.
        retime_plan_ids = {p.snap_id for p in report.to_retime}
        with IndexDB(self.index_path) as index:
            for plan in report.to_retime:
                try:
                    self._apply_retime_plan(
                        plan, recovered_for.get(plan.snap_id), index,
                    )
                    report.retimed.append(plan.snap_id)
                except Exception as e:
                    report.errors.append(("retime", plan.snap_id, str(e)))
                applied += 1
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="repair_apply",
                    current=applied,
                ))
            # Already-correct snapshots whose LP we recovered: persist it
            # so subsequent repair runs are scan-only (no re-pull from pool).
            for snap_id, lp in recovered_for.items():
                if snap_id in retime_plan_ids:
                    continue
                try:
                    self._persist_last_played_ms_with_index(snap_id, lp, index)
                except Exception as e:
                    report.errors.append(("persist_lp", snap_id, str(e)))
            # Reconcile index-lag (snapshots whose index row was left stale
            # by a Ctrl+C between manifest write and SQL update in a prior
            # repair). Manifest is authoritative; index is just a cache.
            for snap_id, manifest_ts_ms, manifest_label in index_lag:
                if snap_id in retime_plan_ids:
                    continue   # _apply_retime_plan already wrote the index
                try:
                    with index._conn:
                        index._conn.execute(
                            "UPDATE snapshots SET timestamp_ms = ?, label = ? "
                            "WHERE id = ?",
                            (manifest_ts_ms, manifest_label, snap_id),
                        )
                    report.index_lag_reconciled += 1
                except Exception as e:
                    report.errors.append(("reconcile_index", snap_id, str(e)))
        _emit(progress_cb, ProgressEvent(
            kind="phase_done", phase="repair_apply",
            current=applied,
        ))

        report.applied = True
        return report

    def migrate_mca_files_to_chunks(
        self, *,
        dry_run: bool = True,
        fsck_first: bool = True,
        progress_cb: ProgressCallback = None,
    ) -> "MigrateMcaReport":
        """Retroactively chunk-dedupe MCA files that were stored as
        whole-files in older snapshots.

        Snapshots taken before :func:`enumerate_region_dirs` recognised
        ``entities/`` and ``poi/`` as region-style dirs put those .mca
        files into ``manifest.files`` (whole-file dedup, sha256-keyed).
        That works but misses chunk-level dedup, so every save stored a
        full new copy of files that change every tick (mob movement,
        villager pathfinding) — easily GB of waste over a long timeline.

        This tool walks every snapshot's manifest, finds file entries
        whose basename is ``r.X.Z.mca``, pulls the bytes out of the file
        pool, parses them as a region, hashes the chunks, writes new
        chunks to the chunk pool, and rewrites the manifest:

        * Each migrated entry moves from ``manifest.files`` to
          ``manifest.dimensions[<derived_dim_key>]``.
        * The dim_key is derived from the file's relative path:
          ``entities/r.0.0.mca`` → ``entities``;
          ``world_nether/poi/r.0.0.mca`` → ``world_nether/poi``.
        * Ref counts: file refs -1 (whole-file blobs become eligible for
          gc); chunk refs +1 per newly-referenced chunk hash.

        Existing chunk-deduped dimensions (e.g. ``region``) are
        untouched. Snapshots whose entire ``files`` list lacks any
        ``r.X.Z.mca`` entry are noops.

        With ``dry_run=True`` (default), nothing is modified — only a
        plan is produced. ``dry_run=False`` applies the plan.
        """
        from ..mca.region import Region, MCAError, EXTERNAL_FLAG
        from ..mca.hasher import hash_chunk
        from pathlib import PurePosixPath

        # Default-on safety: fsck first, repair=True. Catches any pending
        # half-applied state from prior interrupted writes so the migration
        # plan is built against a consistent vault.
        if fsck_first:
            try:
                self.fsck(repair=True)
            except Exception as e:
                _emit(progress_cb, ProgressEvent(
                    kind="warning", phase="migrate_mca_pre_fsck",
                    label=f"fsck failed (continuing): {e}",
                ))

        report = MigrateMcaReport()
        all_snaps = self.list()
        report.scanned = len(all_snaps)

        _emit(progress_cb, ProgressEvent(
            kind="phase_start", phase="migrate_mca_scan",
            label=f"reading {len(all_snaps)} manifests",
            total=len(all_snaps),
        ))

        # Per-snapshot plan: list of (FileRecord, dim_key, rx, rz)
        plans: dict[str, list[tuple]] = {}     # snap_id -> [(file_rec, dim_key, rx, rz, region_bytes_sha)]
        for i, snap in enumerate(all_snaps, 1):
            try:
                manifest = read_manifest(snap.manifest_path)
            except Exception as e:
                report.unreadable.append((snap.id, str(e)))
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="migrate_mca_scan",
                    current=i, total=len(all_snaps),
                ))
                continue

            entries: list[tuple] = []
            for fr in manifest.files:
                pp = PurePosixPath(fr.relative_path)
                # parse "r.X.Z.mca" pattern
                parts = pp.name.split(".")
                if len(parts) != 4 or parts[0] != "r" or parts[3] not in ("mca", "mcr"):
                    continue
                try:
                    rx, rz = int(parts[1]), int(parts[2])
                except ValueError:
                    continue
                # dim_key = parent directory path; "" if at root
                parent_posix = "/".join(pp.parts[:-1])
                if not parent_posix:
                    continue   # bare r.X.Z.mca at world root; skip (unusual)
                entries.append((fr, parent_posix, rx, rz))
            if entries:
                plans[snap.id] = entries
                report.snapshots_with_mca_files += 1
                report.mca_files_total += len(entries)
            _emit(progress_cb, ProgressEvent(
                kind="phase_progress", phase="migrate_mca_scan",
                current=i, total=len(all_snaps),
            ))

        _emit(progress_cb, ProgressEvent(
            kind="phase_done", phase="migrate_mca_scan",
            current=len(all_snaps), total=len(all_snaps),
        ))

        if dry_run or not plans:
            return report

        # Apply: for each snapshot's plan, parse + hash + rewrite manifest
        snaps_by_id = {s.id: s for s in all_snaps}
        _emit(progress_cb, ProgressEvent(
            kind="phase_start", phase="migrate_mca_apply",
            label=f"migrating {report.mca_files_total} files across "
                  f"{report.snapshots_with_mca_files} snapshots",
            total=report.mca_files_total,
        ))
        applied = 0
        with IndexDB(self.index_path) as index:
            for snap_id, entries in plans.items():
                snap = snaps_by_id[snap_id]
                try:
                    manifest = read_manifest(snap.manifest_path)
                except Exception as e:
                    report.errors.append(("read_manifest", snap_id, str(e)))
                    applied += len(entries)
                    continue

                # New dim records to add to this manifest, grouped by dim_key
                new_dims: dict[str, list[RegionRecord]] = {}
                old_file_shas_to_remove: list[bytes] = []
                new_chunk_hashes: list[bytes] = []   # ref-count delta input

                for fr, dim_key, rx, rz in entries:
                    blob = self.chunks.read_file(fr.sha256)
                    if blob is None:
                        report.errors.append((
                            "read_blob", snap_id,
                            f"file pool missing {fr.sha256.hex()[:16]} "
                            f"({fr.relative_path})",
                        ))
                        applied += 1
                        continue
                    try:
                        region = Region.from_bytes(blob, rx=rx, rz=rz,
                                                   source=fr.relative_path)
                        chunks_iter = list(region.iter_chunks())
                    except MCAError as e:
                        report.errors.append((
                            "parse_mca", snap_id,
                            f"{fr.relative_path}: {e}",
                        ))
                        applied += 1
                        continue

                    rec = RegionRecord(rx=rx, rz=rz, chunks=[])
                    for chunk in chunks_iter:
                        if chunk.external:
                            # entities/poi MCAs effectively never use external
                            # chunks (they're metadata, not block payloads),
                            # but be safe: skip the migration of this region
                            # if any external chunk appears.
                            report.skipped_external += 1
                            rec = None
                            break
                        payload = chunk.payload
                        h = hash_chunk(chunk)
                        masked = chunk.compression & ~EXTERNAL_FLAG
                        chunk_blob = bytes([masked]) + payload
                        if not self.chunks.has_chunk(h):
                            self.chunks.store_chunk(h, chunk_blob)
                        rec.chunks.append(ChunkRecord(
                            cx=chunk.cx, cz=chunk.cz,
                            compression=chunk.compression,
                            timestamp=chunk.timestamp,
                            content_hash=h,
                        ))
                        new_chunk_hashes.append(h)
                    if rec is None:
                        applied += 1
                        continue

                    new_dims.setdefault(dim_key, []).append(rec)
                    old_file_shas_to_remove.append(fr.sha256)
                    report.files_migrated += 1
                    report.chunks_added += len(rec.chunks)

                    applied += 1
                    _emit(progress_cb, ProgressEvent(
                        kind="phase_progress", phase="migrate_mca_apply",
                        label=f"{snap.short_id}: {fr.relative_path}",
                        current=applied,
                    ))

                # Commit the manifest rewrite + all SQL updates as one
                # atomic operation. Order matters: chunks were already
                # written to the pool above (atomic, idempotent). Now write
                # the manifest (also atomic), then a SINGLE SQL transaction
                # bumps chunk refs + dec file refs + updates the snapshot
                # row's counters. If killed:
                #   - between manifest write and SQL: manifest is new, index
                #     is stale, but chunks are on disk + referenced by the
                #     manifest. fsck (with the manifest-aware reconciler)
                #     detects the desync and adds missing chunk presence /
                #     ref bumps from manifest contents. Safe.
                #   - inside the SQL txn: rollback takes care of it.
                try:
                    # Merge new dim records into the existing manifest.
                    for dim_key, recs in new_dims.items():
                        manifest.dimensions.setdefault(dim_key, []).extend(recs)
                    removed_set = set(old_file_shas_to_remove)
                    manifest.files = [
                        f for f in manifest.files if f.sha256 not in removed_set
                    ]
                    write_manifest(snap.manifest_path, manifest)

                    new_chunk_count = sum(
                        len(r.chunks)
                        for rs in manifest.dimensions.values()
                        for r in rs
                    )
                    new_file_count = len(manifest.files)

                    # Single SQL transaction: presence rows + chunk refs +
                    # file refs + snapshot counters. All-or-nothing.
                    index._conn.execute("BEGIN")
                    try:
                        if new_chunk_hashes:
                            unique = list({h for h in new_chunk_hashes})
                            index._conn.executemany(
                                "INSERT OR IGNORE INTO chunks (content_hash) "
                                "VALUES (?)",
                                [(h,) for h in unique],
                            )
                            for h in new_chunk_hashes:
                                index._conn.execute(
                                    "INSERT INTO chunks (content_hash, ref_count) "
                                    "VALUES (?, 1) "
                                    "ON CONFLICT(content_hash) DO UPDATE "
                                    "SET ref_count = ref_count + 1",
                                    (h,),
                                )
                        if old_file_shas_to_remove:
                            for sha in old_file_shas_to_remove:
                                index._conn.execute(
                                    "INSERT INTO files (content_hash, ref_count) "
                                    "VALUES (?, -1) "
                                    "ON CONFLICT(content_hash) DO UPDATE "
                                    "SET ref_count = ref_count - 1",
                                    (sha,),
                                )
                        index._conn.execute(
                            "UPDATE snapshots SET chunk_count = ?, "
                            "file_count = ? WHERE id = ?",
                            (new_chunk_count, new_file_count, snap_id),
                        )
                        index._conn.execute("COMMIT")
                    except Exception:
                        index._conn.execute("ROLLBACK")
                        raise
                    report.snapshots_rewritten += 1
                except Exception as e:
                    report.errors.append(("commit", snap_id, str(e)))

        _emit(progress_cb, ProgressEvent(
            kind="phase_done", phase="migrate_mca_apply",
            current=applied, total=report.mca_files_total,
        ))

        report.applied = True
        return report

    def _recover_last_played_from_pool(
        self, manifest: "Manifest",
    ) -> int | None:
        """Pull a snapshot's level.dat out of the file pool and read its
        ``Data.LastPlayed`` field — without needing the source archive.

        Solves the upgrade case: snapshots written by chunkvault versions
        before level.dat-as-authoritative-ts was added have
        ``manifest.header.last_played_ms == 0`` even though the snapshot
        itself includes the level.dat file in its non-region files list.
        That file's bytes are content-addressed in ``files/`` and
        unchanged since ingest, so we can reconstruct the
        ``LastPlayed`` value at any later time.

        Returns the recovered Unix-epoch ms, or None if no level.dat is
        in this snapshot's files (or none parseable). Doesn't touch
        ``level.dat_old`` — that's MC's backup-of-backup, may be stale.
        """
        import gzip
        from pathlib import PurePosixPath
        from ..mca.nbt_lite import find_last_played

        # Collect every level.dat the manifest references; prefer ones
        # at top level / 'world/' over deeper paths (Bukkit's `world/`
        # is the canonical save).
        candidates: list[tuple[int, "FileRecord"]] = []
        for f in manifest.files:
            name = PurePosixPath(f.relative_path).name
            if name != "level.dat":
                continue
            depth = f.relative_path.count("/")
            # Sort key: depth (shallower first), then path itself for
            # determinism. world/level.dat (depth 1) wins over
            # world_nether/level.dat (depth 1, same depth) by alphabetic
            # — fine, they share the same LastPlayed in practice anyway.
            candidates.append((depth, f))
        candidates.sort(key=lambda x: (x[0], x[1].relative_path))

        for _, fr in candidates:
            blob = self.chunks.read_file(fr.sha256)
            if blob is None:
                continue
            try:
                nbt = gzip.decompress(blob)
            except Exception:
                continue
            lp = find_last_played(nbt)
            if lp is not None and lp > 0:
                return lp
        return None

    def _maybe_retitle_label(
        self, old_label: str, world_name: str,
        old_ts_ms: int, new_ts_ms: int,
    ) -> str:
        """If ``old_label`` looks like the ingest-generated
        ``<world>-<YYYY-MM-DD-HH-MM-SS>`` pattern with the OLD timestamp,
        return a new label with the NEW timestamp. Otherwise return
        ``old_label`` unchanged — user-chosen labels are not auto-renamed.
        """
        if not old_label:
            return old_label
        old_ts = datetime.fromtimestamp(old_ts_ms / 1000, tz=timezone.utc)
        expected = f"{world_name}-{old_ts.strftime('%Y-%m-%d-%H-%M-%S')}"
        if old_label != expected:
            return old_label
        new_ts = datetime.fromtimestamp(new_ts_ms / 1000, tz=timezone.utc)
        return f"{world_name}-{new_ts.strftime('%Y-%m-%d-%H-%M-%S')}"

    def _persist_last_played_ms(self, snap_id: str, lp_ms: int) -> None:
        """Write a recovered ``last_played_ms`` into the manifest header so
        future repair runs see it pre-recorded. Convenience wrapper that
        opens its own IndexDB; prefer the ``_with_index`` variant inside
        a loop so the connection isn't recreated per call."""
        with IndexDB(self.index_path) as index:
            self._persist_last_played_ms_with_index(snap_id, lp_ms, index)

    def _persist_last_played_ms_with_index(
        self, snap_id: str, lp_ms: int, index: "IndexDB",
    ) -> None:
        snap = self.get(snap_id)
        if snap is None:
            raise ChunkRepoError(f"snapshot vanished mid-repair: {snap_id}")
        manifest = read_manifest(snap.manifest_path)
        manifest.header.last_played_ms = lp_ms
        write_manifest(snap.manifest_path, manifest)

    def _apply_retime_plan(
        self, plan: "RetimePlan", recovered_lp: int | None,
        index: "IndexDB",
    ) -> None:
        """Apply timestamp + label + recovered-LP changes to one snapshot
        in a SINGLE manifest read+write, instead of three separate cycles.

        For vaults with multi-hundred-MB manifests, doing one I/O pass
        instead of three is a 3x throughput win on apply. The previous
        implementation called retime_snapshot, _rename_snapshot_label, and
        _persist_last_played_ms in sequence — each opened the manifest,
        edited a field, wrote it back, opened a SQLite connection.
        """
        snap = self.get(plan.snap_id)
        if snap is None:
            raise ChunkRepoError(
                f"snapshot vanished mid-repair: {plan.snap_id}"
            )
        if not snap.manifest_path.is_file():
            raise ChunkRepoError(
                f"manifest missing for {snap.short_id}: {snap.manifest_path}"
            )

        manifest = read_manifest(snap.manifest_path)
        old_ts_ms = manifest.header.timestamp_ms
        new_ts_ms = plan.new_ts_ms

        # Collision check: refuse if another snapshot already sits at this
        # exact (label, timestamp). Same logic as retime_snapshot — we just
        # also need to consider the new label, since repair may rename.
        check_label = plan.new_label or plan.old_label
        existing_id = index.find_snapshot_by_label_and_timestamp(
            check_label, new_ts_ms,
        )
        if existing_id is not None and existing_id != plan.snap_id:
            raise ChunkRepoError(
                f"refusing retime: {existing_id[:12]} already at "
                f"label={check_label!r} timestamp_ms={new_ts_ms}"
            )

        first_retime = manifest.header.original_timestamp_ms == 0
        new_original = (
            old_ts_ms if first_retime else manifest.header.original_timestamp_ms
        )

        # Apply all changes in memory
        manifest.header.timestamp_ms = new_ts_ms
        manifest.header.original_timestamp_ms = new_original
        if plan.new_label != plan.old_label:
            manifest.header.label = plan.new_label
        if recovered_lp is not None:
            manifest.header.last_played_ms = recovered_lp

        # Single atomic write of the manifest
        write_manifest(snap.manifest_path, manifest)

        # Index updates: one SQL transaction so a Ctrl+C between the ts
        # update and the label update can't leave them desynced. Either
        # both apply or neither does. (Manifest is already on disk; if
        # this txn fails or is interrupted, the next repair detects the
        # manifest-vs-index mismatch via the index_lag scan and reconciles.)
        with index._conn:
            if first_retime:
                index._conn.execute(
                    "UPDATE snapshots SET timestamp_ms = ?, "
                    "original_timestamp_ms = ? WHERE id = ?",
                    (new_ts_ms, new_original, plan.snap_id),
                )
            else:
                index._conn.execute(
                    "UPDATE snapshots SET timestamp_ms = ? WHERE id = ?",
                    (new_ts_ms, plan.snap_id),
                )
            if plan.new_label != plan.old_label:
                index._conn.execute(
                    "UPDATE snapshots SET label = ? WHERE id = ?",
                    (plan.new_label, plan.snap_id),
                )

    def _rename_snapshot_label(self, snap_id: str, new_label: str) -> None:
        """Persist a label rename to both the manifest header and the index
        row. Run AFTER ``retime_snapshot`` (which already touches the
        manifest, so this is a small follow-up write)."""
        snap = self.get(snap_id)
        if snap is None:
            raise ChunkRepoError(f"snapshot vanished mid-repair: {snap_id}")
        manifest = read_manifest(snap.manifest_path)
        manifest.header.label = new_label
        write_manifest(snap.manifest_path, manifest)
        with IndexDB(self.index_path) as index:
            index.update_snapshot_label(snap_id, new_label)

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
