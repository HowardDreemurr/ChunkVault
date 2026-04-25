"""Ingest core: turn one timestamped multi-server zip into snapshots + logs.

Source format (the user's actual layout):

    2025-04-25-12-34-56.zip           ← timestamp encoded in filename
    └── EX-Server/                    ← one or more server folders
        ├── world/                    ← chunk-store snapshot target
        ├── logs/                     ← captured into log snapshot
        ├── crash-reports/            ← captured into log snapshot
        └── server.properties etc.    ← ignored (configs aren't worth deduping)
    └── CR-Server/
        ├── world/
        ├── logs/
        ...

For each server: snapshot ``world/`` to chunk store with
``label = <server>-<ts>`` and ``world_name = <server>``. Collect the server's
``logs/`` and ``crash-reports/`` into a single log snapshot per archive
(deduplicated globally).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import tarfile
import tempfile
import zipfile

from .importer import ImportError_ as ImportError
from .importer import (
    _detect_archive_kind,
    _safe_extract_tar,
    _safe_extract_zip,
)
from .progress import ProgressCallback, ProgressEvent, _emit
from .repo import ChunkSnapshot, ChunkSnapshotRepo, LogSnapshot

# Match a YYYY-MM-DD-HH-MM-SS prefix anywhere in the filename.
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})")

# Subdirectories within a server folder we capture into the log snapshot.
LOG_SUBPATHS: tuple[str, ...] = ("logs", "crash-reports")


@dataclass
class IngestResult:
    archive: Path
    timestamp: datetime
    label: str
    snapshots: list[ChunkSnapshot] = field(default_factory=list)
    log_snapshot: LogSnapshot | None = None
    server_names: list[str] = field(default_factory=list)
    skipped_servers: list[tuple[str, str]] = field(default_factory=list)
    # Servers we found in the archive that already had a snapshot at this
    # timestamp — skipped silently to make re-ingest idempotent. Each entry
    # is ``(server_name, existing_snapshot_short_id)``.
    already_ingested: list[tuple[str, str]] = field(default_factory=list)


class _extract_or_passthrough:
    """Context manager: yields the archive's extracted root.

    Unlike ImportSession this does NOT try to drill down into a world
    directory — ingest needs the top level to discover server folders
    itself. For directory inputs, yields the directory unchanged.
    """

    def __init__(self, source: Path):
        self.source = source
        self._tmp: tempfile.TemporaryDirectory | None = None

    def __enter__(self) -> Path:
        if self.source.is_dir():
            return self.source
        kind = _detect_archive_kind(self.source)
        if kind is None:
            raise ImportError(
                f"unsupported archive format: {self.source.name}"
            )
        self._tmp = tempfile.TemporaryDirectory(prefix="chunkvault-ingest-")
        out = Path(self._tmp.name)
        if kind == "zip":
            with zipfile.ZipFile(self.source) as zf:
                _safe_extract_zip(zf, out)
        else:
            with tarfile.open(self.source) as tf:
                _safe_extract_tar(tf, out)
        return out

    def __exit__(self, *exc) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


def parse_timestamp_from_name(name: str) -> datetime | None:
    """Extract a YYYY-MM-DD-HH-MM-SS timestamp from a filename if present."""
    m = _TS_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d-%H-%M-%S").replace(
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


@dataclass(frozen=True)
class DiscoveredServer:
    """One server detected in an archive.

    ``server_root`` is the directory containing logs/, crash-reports/, mods/,
    server.properties — the place a Minecraft server runs from. It's the
    parent dir whose name we use as the server label.

    ``world_root`` is the directory we pass to ``repo.snapshot()``. It's the
    tightest path that contains all region/ dirs for this server. For most
    layouts this is one level below server_root (``server/world/``); for
    Bukkit's parallel-worlds layout it equals server_root.
    """
    server_name: str
    server_root: Path
    world_root: Path


def discover_servers(extracted_root: Path) -> list[tuple[str, Path]]:
    """Backwards-compatible API: ``(server_name, world_root)`` per server.

    Use :func:`discover_servers_full` for ``server_root`` (where logs live).
    """
    return [(d.server_name, d.world_root)
            for d in discover_servers_full(extracted_root)]


def discover_servers_full(extracted_root: Path) -> list[DiscoveredServer]:
    """Find every server in ``extracted_root`` with both server_root +
    world_root.

    A "server" is any directory under which we find at least one
    ``region/r.X.Z.mca`` file (depth-bounded). Layout examples:

    * standard: ``archive/EX-Server/world/region/r.X.Z.mca``
                → server_root=``EX-Server``, world_root=``EX-Server/world``
    * datapack: ``archive/EX-Server/world/dimensions/.../region/...``
                → world_root still ``EX-Server/world``
    * Bukkit:   ``archive/EX-Server/{world,world_nether,world_the_end}/region/...``
                → world_root=server_root=``EX-Server`` (parallel worlds)
    * custom:   ``archive/EX-Server/survival/region/...``
                → world_root=``EX-Server/survival``
    * bare:     ``archive/world/region/...`` (no server wrapper)
                → server_root=world_root=``archive``, name=archive name
    * direct:   ``archive/region/...``
                → server_root=world_root=``archive``, name=archive name

    The split lets us snapshot only world data (world_root) while still
    finding logs/ + crash-reports/ at the server level (server_root).
    """
    from ..world.layout import _find_region_dirs

    if not extracted_root.is_dir():
        return []
    out: list[DiscoveredServer] = []
    try:
        children = sorted(extracted_root.iterdir())
    except OSError:
        return []
    for entry in children:
        if not entry.is_dir():
            continue
        region_dirs = list(_find_region_dirs(entry, max_depth=6))
        if not region_dirs:
            continue
        world_root = _common_world_root(entry, region_dirs)
        out.append(DiscoveredServer(
            server_name=entry.name,
            server_root=entry,
            world_root=world_root,
        ))
    if out:
        return out
    # Fallback: archive root IS the server (no nested server folders)
    region_dirs = list(_find_region_dirs(extracted_root, max_depth=6))
    if region_dirs:
        world_root = _common_world_root(extracted_root, region_dirs)
        name = extracted_root.name or "world"
        return [DiscoveredServer(
            server_name=name,
            server_root=extracted_root,
            world_root=world_root,
        )]
    return []


def _common_world_root(server_root: Path, region_dirs: list[Path]) -> Path:
    """Tightest path under (or equal to) server_root that contains every
    region dir.

    For a single region tree (e.g. ``server/world/region/`` and
    ``server/world/DIM-1/region/``) this returns ``server/world``. For
    Bukkit's parallel-worlds layout (``server/world/region``,
    ``server/world_nether/region``) the only shared ancestor is
    ``server`` itself, so that's what we return.
    """
    if not region_dirs:
        return server_root
    # Use os.path.commonpath on the *parents* of region dirs (the dim-root
    # under which `region/` sits), then walk back up so we sit one level
    # above region/ — that's the world root.
    import os
    # Each region dir is `<world>/.../region`; we want the world part.
    # Take the parent of region/ as the candidate; in datapack cases this is
    # `<world>/dimensions/<ns>/<id>`. Find the common prefix across all of
    # them, then ensure the result is at or above server_root.
    candidates = [str(rd.parent) for rd in region_dirs]
    common = os.path.commonpath(candidates) if len(candidates) > 1 else candidates[0]
    common_path = Path(common)
    # Walk up to ensure result is at most server_root (commonpath could
    # already equal a region dir's parent if there's only one, which is fine).
    try:
        common_path.relative_to(server_root)
    except ValueError:
        return server_root
    # If the common path's last component is a "region pattern" component
    # (e.g. `dimensions`), step up to the world dir. Easier rule: the world
    # root is the highest path ≤ common_path that has level.dat, OR
    # common_path itself if no level.dat exists between server_root and it.
    walker = common_path
    while walker != server_root and walker.parent != walker:
        if (walker / "level.dat").is_file():
            return walker
        walker = walker.parent
    if (server_root / "level.dat").is_file():
        return server_root
    # No level.dat found anywhere — return common_path as best guess
    return common_path


def collect_log_files(server_root: Path) -> list[tuple[str, bytes]]:
    """Return ``[(relative_posix_path, content_bytes), ...]`` for a server's
    log + crash-report files."""
    out: list[tuple[str, bytes]] = []
    for subdir in LOG_SUBPATHS:
        root = server_root / subdir
        if not root.is_dir():
            continue
        for entry in root.rglob("*"):
            if entry.is_file():
                rel = entry.relative_to(server_root).as_posix()
                try:
                    out.append((rel, entry.read_bytes()))
                except OSError:
                    pass
    return out


def ingest_archive(
    repo: ChunkSnapshotRepo,
    archive: Path | str,
    *,
    timestamp: datetime | None = None,
    progress_cb: ProgressCallback = None,
    skip_logs: bool = False,
    verify_roundtrip: bool = True,
) -> IngestResult:
    """Ingest one multi-server archive into ``repo``.

    Per top-level server dir, takes a world snapshot (chunk store) and adds
    one log snapshot for the whole archive. Returns metadata about what
    was created. Survives per-server errors: a corrupt EX-Server doesn't
    block CR-Server's snapshot.
    """
    archive_path = Path(archive)
    ts = timestamp or parse_timestamp_from_name(archive_path.name) \
        or datetime.now(timezone.utc)
    ts_label = ts.strftime("%Y-%m-%d-%H-%M-%S")
    result = IngestResult(archive=archive_path, timestamp=ts, label=ts_label)

    _emit(progress_cb, ProgressEvent(
        kind="phase_start", phase="ingest_archive",
        label=archive_path.name,
    ))

    with _extract_or_passthrough(archive_path) as extracted:
        servers = discover_servers_full(extracted)
        if not servers:
            raise ImportError(
                f"{archive_path.name}: no server folders detected "
                f"(looked for any directory containing region/r.X.Z.mca files)"
            )

        all_log_files: dict[str, list[tuple[str, bytes]]] = {}

        for i, server in enumerate(servers, 1):
            label = f"{server.server_name}-{ts_label}"

            # Idempotency: if a snapshot with this exact label already exists,
            # the archive has been ingested before. Skip silently — content-
            # addressed storage means a redundant snapshot wouldn't duplicate
            # any chunks, but it WOULD add a new manifest + index row, so
            # skipping is both faster and avoids visual clutter for the user.
            existing = repo.get(label)
            if existing is not None:
                result.already_ingested.append(
                    (server.server_name, existing.short_id),
                )
                _emit(progress_cb, ProgressEvent(
                    kind="phase_done", phase="server_world",
                    label=f"{server.server_name} (already ingested)",
                    current=i, total=len(servers),
                ))
                continue

            _emit(progress_cb, ProgressEvent(
                kind="phase_start", phase="server_world",
                label=server.server_name, current=i, total=len(servers),
            ))
            try:
                # Pass world_root (the dir containing level.dat + region/),
                # not server_root — keeps mods/, plugins/, server.properties
                # out of the snapshot. world_root is auto-detected so it works
                # for "world", "world_nether", custom names, etc.
                snap = repo.snapshot(
                    server.world_root,
                    label=label,
                    timestamp=ts,
                    allow_live=True,         # archives aren't live worlds
                    verify_roundtrip=verify_roundtrip,
                    world_name=server.server_name,
                    progress_cb=progress_cb,
                )
                result.snapshots.append(snap)
                result.server_names.append(server.server_name)
            except Exception as e:
                result.skipped_servers.append((server.server_name, str(e)))
                _emit(progress_cb, ProgressEvent(
                    kind="error", phase="server_world",
                    label=server.server_name, detail={"error": str(e)},
                ))
                continue

            if not skip_logs:
                # Logs sit at the SERVER root (sibling of world dirs), not
                # inside world_root.
                files = collect_log_files(server.server_root)
                if files:
                    all_log_files[server.server_name] = files

        if not skip_logs and all_log_files:
            # Idempotency: log snapshots are labelled by archive timestamp,
            # so re-ingest of the same archive would create a duplicate row
            # (blobs would dedupe, but rows wouldn't). Skip if present.
            existing_log = repo.get_log_snapshot(ts_label)
            if existing_log is not None:
                result.log_snapshot = existing_log
            else:
                _emit(progress_cb, ProgressEvent(
                    kind="phase_start", phase="logs",
                    label="capturing logs",
                    total=sum(len(v) for v in all_log_files.values()),
                ))
                log_snap = repo.add_log_snapshot(
                    all_log_files,
                    label=ts_label,
                    source_path=archive_path,
                    timestamp=ts,
                )
                result.log_snapshot = log_snap

    _emit(progress_cb, ProgressEvent(
        kind="finish", phase="ingest_archive",
        label=archive_path.name,
        detail={
            "snapshots": len(result.snapshots),
            "skipped": len(result.skipped_servers),
            "already_ingested": len(result.already_ingested),
            "log_snapshot": result.log_snapshot.id if result.log_snapshot else None,
        },
    ))
    return result
