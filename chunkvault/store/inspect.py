"""Cheap archive inspection — peek inside without extracting.

The wizard's "Proceed?" prompt should show the user EXACTLY what's about
to be ingested: which server folders, how many region files per server,
how many logs and crash-reports. We don't need to decompress chunk
payloads for this — just walk the archive's directory listing.

Zip: ``ZipFile.namelist()`` is O(1) per entry, no decompression.
Tar (gz/bz2/xz): iterating members reads through the compression stream
once but never writes any file content.
"""
from __future__ import annotations

import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .importer import _detect_archive_kind
from .ingest import LOG_SUBPATHS, parse_timestamp_from_name

_REGION_NAME_RE = re.compile(r"^r\.-?\d+\.-?\d+\.mc[ar]$")


@dataclass(frozen=True)
class ArchiveServerPreview:
    name: str                          # e.g. "EX-Server"
    region_files: int                  # total .mca/.mcr files across all dims
    dimensions: list[str] = field(default_factory=list)  # ['region', 'DIM-1/region', …]
    log_files: int = 0
    crash_report_files: int = 0
    has_level_dat: bool = False
    estimated_world_bytes: int = 0     # sum of sizes of files under world/
    estimated_log_bytes: int = 0       # sum of sizes of log + crash-report files
    # Diagnostic: top-level dirs/files inside the server folder. Useful when
    # region_files == 0 to see WHY — proxy server? non-standard layout?
    top_level: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ArchivePreview:
    path: Path
    timestamp: datetime | None         # parsed from filename if possible
    total_entries: int                 # count of files inside
    servers: list[ArchiveServerPreview] = field(default_factory=list)
    other_top_level: list[str] = field(default_factory=list)
    error: str | None = None           # set if archive couldn't be opened


def preview_archive(path: Path | str) -> ArchivePreview:
    """Open ``path`` (zip/tar/dir), classify every entry, return a summary.

    Doesn't extract any file bytes — for zips it reads only the central
    directory; for tars it scans member headers; for directories it walks
    with ``rglob``. Safe to call on hundreds of archives sequentially.
    """
    p = Path(path)
    ts = parse_timestamp_from_name(p.name)
    if not p.exists():
        return ArchivePreview(path=p, timestamp=ts, total_entries=0,
                              error=f"path does not exist: {p}")

    try:
        if p.is_dir():
            entries = list(_iter_dir_entries(p))
        else:
            kind = _detect_archive_kind(p)
            if kind == "zip":
                entries = list(_iter_zip_entries(p))
            elif kind == "tar":
                entries = list(_iter_tar_entries(p))
            else:
                return ArchivePreview(
                    path=p, timestamp=ts, total_entries=0,
                    error=f"unsupported format: {p.name}",
                )
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as e:
        return ArchivePreview(path=p, timestamp=ts, total_entries=0,
                              error=f"open failed: {e}")

    return _summarize(p, ts, entries)


# ---- summarization ---------------------------------------------------------

def _summarize(
    path: Path, ts: datetime | None,
    entries: list[tuple[str, int]],          # (posix_path, size_bytes)
) -> ArchivePreview:
    """Aggregate raw entries into per-server counts.

    Pattern-based: a region file is anything matching ``*/region/r.X.Z.mca``
    at any depth under the server root. The dimension key is the path
    leading up to (but not including) the .mca filename. This handles
    every layout we've seen in the wild:

    * vanilla:    ``world/region/r.0.0.mca`` → dim ``world/region``
    * vanilla:    ``world/DIM-1/region/r.0.0.mca`` → dim ``world/DIM-1/region``
    * Bukkit:     ``world_nether/region/r.0.0.mca`` → dim ``world_nether/region``
    * Paper:      ``world_the_end/region/r.0.0.mca`` → dim ``world_the_end/region``
    * Multiverse: ``survival/region/r.0.0.mca`` → dim ``survival/region``
    * datapack:   ``world/dimensions/<ns>/<id>/region/r.0.0.mca``

    No more hardcoded "world/" / "DIM*/" assumptions.
    """
    # server_name -> dict of accumulators
    servers: dict[str, dict] = {}
    other_top_level: set[str] = set()

    for posix, size in entries:
        parts = posix.split("/")
        if not parts or parts[0] == "":
            continue
        top = parts[0]
        if top not in servers:
            servers[top] = {
                "region_files": 0, "dimensions": set(),
                "log_files": 0, "crash_report_files": 0,
                "has_level_dat": False,
                "estimated_world_bytes": 0, "estimated_log_bytes": 0,
                "top_level": set(),   # diagnostic: what's under the server root
            }
        s = servers[top]
        if len(parts) >= 2 and parts[1]:
            s["top_level"].add(parts[1])

        rel = parts[1:]
        if not rel:
            continue

        # 1) level.dat — anywhere from root to a few levels deep
        if rel[-1] == "level.dat" and len(rel) <= 3:
            s["has_level_dat"] = True
            s["estimated_world_bytes"] += size
            continue

        # 2) Region file: any path ending in /region/r.X.Z.mca
        if (len(rel) >= 2 and rel[-2] == "region"
                and _REGION_NAME_RE.match(rel[-1])):
            s["region_files"] += 1
            dim_key = "/".join(rel[:-1])   # drop the .mca filename
            s["dimensions"].add(dim_key)
            s["estimated_world_bytes"] += size
            continue

        # 3) External chunk file (.mcc) — count toward world bytes only
        if rel[-1].startswith("c.") and rel[-1].endswith(".mcc"):
            s["estimated_world_bytes"] += size
            continue

        # 4) Logs / crash reports
        if rel[0] == "logs":
            s["log_files"] += 1
            s["estimated_log_bytes"] += size
            continue
        if rel[0] == "crash-reports":
            s["crash_report_files"] += 1
            s["estimated_log_bytes"] += size
            continue
        # Anything else (server.properties, mods/, plugins/, …) → ignored

    # Drop "servers" that don't actually look like one
    real_servers: list[ArchiveServerPreview] = []
    for name in sorted(servers):
        s = servers[name]
        if s["region_files"] == 0 and not s["has_level_dat"] \
                and s["log_files"] == 0 and s["crash_report_files"] == 0:
            other_top_level.add(name)
            continue
        real_servers.append(ArchiveServerPreview(
            name=name,
            region_files=s["region_files"],
            dimensions=sorted(s["dimensions"]),
            log_files=s["log_files"],
            crash_report_files=s["crash_report_files"],
            has_level_dat=s["has_level_dat"],
            estimated_world_bytes=s["estimated_world_bytes"],
            estimated_log_bytes=s["estimated_log_bytes"],
            top_level=sorted(s["top_level"]),
        ))

    return ArchivePreview(
        path=path, timestamp=ts,
        total_entries=len(entries),
        servers=real_servers,
        other_top_level=sorted(other_top_level),
    )


def _classify_world_entry(rel: list[str], size: int, s: dict) -> None:
    """Classify a path that lives inside a server's world directory."""
    if not rel:
        return
    if rel[0] == "region" and len(rel) == 2 and _REGION_NAME_RE.match(rel[1]):
        s["region_files"] += 1
        s["dimensions"].add("region")
        s["estimated_world_bytes"] += size
        return
    if rel[0].startswith("DIM") and len(rel) >= 3 and rel[1] == "region" \
            and _REGION_NAME_RE.match(rel[-1]):
        s["region_files"] += 1
        s["dimensions"].add(f"{rel[0]}/region")
        s["estimated_world_bytes"] += size
        return
    if rel[0] == "dimensions" and len(rel) >= 4 and rel[-2] == "region" \
            and _REGION_NAME_RE.match(rel[-1]):
        # dimensions/<ns>/<id>/region/r.X.Z.mca
        s["region_files"] += 1
        s["dimensions"].add("/".join(rel[:-1]))
        s["estimated_world_bytes"] += size
        return
    # Other world files (level.dat is handled at server-root, but datapacks/
    # etc. live under world/)
    s["estimated_world_bytes"] += size


# ---- entry iteration -------------------------------------------------------

def _iter_zip_entries(path: Path):
    """Yield (posix_path, size) for every file in the zip."""
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            yield info.filename.replace("\\", "/"), info.file_size


def _iter_tar_entries(path: Path):
    with tarfile.open(path) as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            yield member.name.replace("\\", "/"), member.size


def _iter_dir_entries(root: Path):
    for entry in root.rglob("*"):
        if entry.is_file():
            rel = entry.relative_to(root).as_posix()
            try:
                yield rel, entry.stat().st_size
            except OSError:
                yield rel, 0
