"""Environment detection — pure functions, no UI, fully testable.

The wizard's first job is to look around and figure out what the user
probably wants to point us at:

* an existing chunk-store / git-store repo
* directories full of timestamped archive files

We surface this as a simple summary the wizard layer can render. All
detection is best-effort and silent on failure — if a path is unreadable
we just skip it.
"""
from __future__ import annotations

import os
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..store.importer import _detect_archive_kind


# ---- repo detection ---------------------------------------------------------

@dataclass(frozen=True)
class RepoSummary:
    path: Path
    kind: str                  # "chunk", "git", or "unknown"
    snapshot_count: int = 0
    log_snapshot_count: int = 0
    chunk_blob_count: int = 0
    log_blob_count: int = 0
    on_disk_bytes: int = 0


def summarize_repo(path: Path | str) -> RepoSummary | None:
    """Inspect ``path`` and return a summary if it looks like a chunkvault repo.

    Counts come from the SQLite index, not from filesystem walks.
    Walking ``chunks/`` recursively on a multi-million-chunk vault used to
    hang the wizard for minutes (or OOM-kill it on Windows) every time
    ``detect_environment`` ran — which is on launch AND after every
    operation. Index counts are O(1) per table.

    ``on_disk_bytes`` here is **metadata only** — index.sqlite + manifests/
    bytes — NOT the real vault size. The chunk/file/log pools dominate
    actual disk usage but rglob'ing them every wizard launch took minutes
    on multi-million-blob vaults. The wizard relabels this column as
    "metadata" to avoid the misleading "17 GB on a 557 GB vault" surprise;
    callers who need true size should call `du`/equivalent themselves.
    """
    p = Path(path)
    if not p.is_dir():
        return None
    kind = _detect_repo_kind(p)
    if kind == "unknown":
        return None
    snap_count = log_count = chunk_blobs = log_blobs = 0
    on_disk_bytes = 0
    try:
        if kind == "chunk":
            from ..store import ChunkSnapshotRepo
            from ..store.index import IndexDB
            repo = ChunkSnapshotRepo(p)
            if repo.is_initialized():
                # Use direct SQL — list() materializes full snapshot
                # objects which is overkill for a count.
                with IndexDB(repo.index_path) as idx:
                    cur = idx._conn.execute(
                        "SELECT COUNT(*) FROM snapshots",
                    )
                    snap_count = cur.fetchone()[0]
                    cur = idx._conn.execute(
                        "SELECT COUNT(*) FROM log_snapshots",
                    )
                    log_count = cur.fetchone()[0]
                    cur = idx._conn.execute(
                        "SELECT COUNT(*) FROM chunks",
                    )
                    chunk_blobs = cur.fetchone()[0]
                    # log_files presence row count
                    try:
                        cur = idx._conn.execute(
                            "SELECT COUNT(*) FROM log_files",
                        )
                        log_blobs = cur.fetchone()[0]
                    except Exception:
                        log_blobs = 0
            # On-disk size: stat just the index.sqlite + manifests/ —
            # the chunks/ pool is the elephant we deliberately don't
            # walk here.
            try:
                idx_path = p / "index.sqlite"
                if idx_path.is_file():
                    on_disk_bytes += idx_path.stat().st_size
                manifests_dir = p / "manifests"
                if manifests_dir.is_dir():
                    for child in manifests_dir.iterdir():
                        try:
                            on_disk_bytes += child.stat().st_size
                        except OSError:
                            pass
            except OSError:
                pass
        elif kind == "git":
            from ..storage import SnapshotRepo
            repo = SnapshotRepo(p)
            if repo.is_initialized():
                snap_count = len(repo.list())
    except Exception:
        pass
    return RepoSummary(
        path=p, kind=kind,
        snapshot_count=snap_count,
        log_snapshot_count=log_count,
        chunk_blob_count=chunk_blobs,
        log_blob_count=log_blobs,
        on_disk_bytes=on_disk_bytes,
    )


def _detect_repo_kind(p: Path) -> str:
    if (p / "index.sqlite").is_file() and (p / "manifests").is_dir():
        return "chunk"
    if (p / "HEAD").is_file() and (p / "objects").is_dir() and (p / "refs").is_dir():
        return "git"
    return "unknown"


def _count_files_under(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())


def _dir_size(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


# ---- source archive detection ----------------------------------------------

@dataclass(frozen=True)
class SourcePathSummary:
    path: Path
    archive_count: int
    total_bytes: int
    samples: list[str] = field(default_factory=list)  # first few archive names


def scan_for_archives(
    root: Path | str,
    *,
    max_samples: int = 5,
    max_depth: int = 3,
    max_files: int = 100_000,
) -> SourcePathSummary | None:
    """Find archives under ``root``, **depth-bounded** to avoid drive-root blow-up.

    Default depth of 3 catches typical backup layouts (e.g. ``backups/2024/08/x.zip``)
    without descending into every program-data tree. Skips well-known noise
    directories (Windows, AppData, node_modules, …). ``max_files`` is a final
    safety brake — if we somehow walk past that many entries we stop and
    return what we have.
    """
    p = Path(root)
    if not p.is_dir():
        return None
    archives: list[Path] = []
    total = 0
    files_seen = 0
    try:
        for entry in _walk_bounded(p, max_depth):
            files_seen += 1
            if files_seen > max_files:
                break
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            if _detect_archive_kind(entry) is None:
                continue
            archives.append(entry)
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    except (PermissionError, OSError):
        pass
    if not archives:
        return None
    archives.sort(key=lambda a: a.name)
    return SourcePathSummary(
        path=p,
        archive_count=len(archives),
        total_bytes=total,
        samples=[a.name for a in archives[:max_samples]],
    )


_SKIP_DIRS: set[str] = {
    # Windows system + appdata
    "Windows", "WindowsApps", "Program Files", "Program Files (x86)",
    "ProgramData", "$Recycle.Bin", "System Volume Information",
    "AppData", "Local Settings", "Recovery",
    # Cross-platform dev / cache noise
    ".Trash", ".cache", "node_modules", ".git", "__pycache__",
    "venv", ".venv", "env", ".env", "site-packages",
    ".gradle", ".m2", ".npm", ".yarn", ".cargo",
}


def _walk_bounded(root: Path, max_depth: int):
    """Depth-bounded directory walk. Skips ``_SKIP_DIRS`` aggressively."""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            for child in current.iterdir():
                yield child
                try:
                    if (depth + 1 < max_depth
                            and child.is_dir()
                            and child.name not in _SKIP_DIRS
                            and not child.name.startswith(".")):
                        stack.append((child, depth + 1))
                except OSError:
                    continue
        except (PermissionError, OSError):
            continue


def default_paths_to_scan() -> list[Path]:
    """Conservative defaults: cwd only.

    The previous version walked every drive root which on Windows means
    iterating multi-TB volumes — catastrophic. We now scan only the
    current working directory and let the user explicitly add more paths
    via the wizard prompt.
    """
    return [Path.cwd().resolve()]


# ---- combined environment summary ------------------------------------------

@dataclass(frozen=True)
class EnvironmentSummary:
    repos: list[RepoSummary]
    source_paths: list[SourcePathSummary]


def detect_environment(
    *,
    repo_candidates: list[Path] | None = None,
    source_candidates: list[Path] | None = None,
) -> EnvironmentSummary:
    """Survey the local machine for repos and archive directories.

    Order of precedence (deduped by resolved path):
    1. Explicit ``repo_candidates`` / ``source_candidates`` (tests, CLI).
    2. User-registered paths from ``~/.chunkvault/config.json``
       (``chunkvault repo add``, ``chunkvault source add``).
    3. Conventional defaults: cwd + a few near-cwd names.

    Without (2) the wizard could never find a vault on a user's `F:\\`
    drive when launched from `C:\\Users\\HAOYA`. The registry persists
    locations across runs.
    """
    from . import config as _cfg

    if repo_candidates is None:
        repo_paths = [r.path for r in _cfg.list_repos()]
        repo_paths.extend(_default_repo_candidates())
    else:
        repo_paths = list(repo_candidates)

    if source_candidates is None:
        source_paths = [s.path for s in _cfg.list_source_paths()]
        source_paths.extend(default_paths_to_scan())
    else:
        source_paths = list(source_candidates)

    repos: list[RepoSummary] = []
    seen_repo: set[Path] = set()
    for p in repo_paths:
        summary = summarize_repo(p)
        if summary is None:
            continue
        if summary.path in seen_repo:
            continue
        seen_repo.add(summary.path)
        repos.append(summary)

    sources: list[SourcePathSummary] = []
    seen_src: set[Path] = set()
    for p in source_paths:
        summary = scan_for_archives(p)
        if summary is None:
            continue
        if summary.path in seen_src:
            continue
        seen_src.add(summary.path)
        sources.append(summary)

    return EnvironmentSummary(repos=repos, source_paths=sources)


def _default_repo_candidates() -> list[Path]:
    """Look for an existing repo near where the user is likely working.

    We don't crawl the whole filesystem — just check a few conventional
    spots: cwd, cwd/backup-repo, ~/backup-repo, and similar.
    """
    candidates: list[Path] = []
    cwd = Path.cwd()
    candidates.append(cwd)
    for name in ("backup-repo", "chunkvault-repo", "chunkvault"):
        candidates.append(cwd / name)
    home = Path.home()
    for name in ("backup-repo", "chunkvault-repo"):
        candidates.append(home / name)
    return candidates
