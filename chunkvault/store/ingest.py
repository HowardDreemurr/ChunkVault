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


def discover_servers(extracted_root: Path) -> list[tuple[str, Path]]:
    """Find ``(server_name, server_root)`` for each top-level dir that has world/.

    Falls back to a single anonymous server if the archive's contents look
    like a bare world (no nested server folders).
    """
    if not extracted_root.is_dir():
        return []
    servers: list[tuple[str, Path]] = []
    for entry in sorted(extracted_root.iterdir()):
        if entry.is_dir() and (entry / "world").is_dir():
            servers.append((entry.name, entry))
    if servers:
        return servers
    # Fallback: maybe the archive root IS a server (has world/ at root)
    if (extracted_root / "world").is_dir():
        return [(extracted_root.name or "world", extracted_root)]
    return []


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
        servers = discover_servers(extracted)
        if not servers:
            raise ImportError(
                f"{archive_path.name}: no server folders detected "
                f"(looked for top-level dirs containing 'world')"
            )

        all_log_files: dict[str, list[tuple[str, bytes]]] = {}

        for i, (server_name, server_root) in enumerate(servers, 1):
            world_path = server_root / "world"
            label = f"{server_name}-{ts_label}"
            _emit(progress_cb, ProgressEvent(
                kind="phase_start", phase="server_world",
                label=server_name, current=i, total=len(servers),
            ))
            try:
                snap = repo.snapshot(
                    world_path,
                    label=label,
                    timestamp=ts,
                    allow_live=True,         # archives aren't live worlds
                    verify_roundtrip=verify_roundtrip,
                    world_name=server_name,
                    progress_cb=progress_cb,
                )
                result.snapshots.append(snap)
                result.server_names.append(server_name)
            except Exception as e:
                result.skipped_servers.append((server_name, str(e)))
                _emit(progress_cb, ProgressEvent(
                    kind="error", phase="server_world",
                    label=server_name, detail={"error": str(e)},
                ))
                continue

            if not skip_logs:
                files = collect_log_files(server_root)
                if files:
                    all_log_files[server_name] = files

        if not skip_logs and all_log_files:
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
            "log_snapshot": result.log_snapshot.id if result.log_snapshot else None,
        },
    ))
    return result
