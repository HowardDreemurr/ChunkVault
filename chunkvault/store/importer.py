"""Import a world from an archive (zip/tar) or a plain directory.

The historical 8 TB problem: most of those snapshots aren't sitting around as
loose world directories — they're zips and tarballs in long-term storage.
``ImportSession`` handles unpacking transparently, locates the actual world
folder inside (the world is sometimes wrapped in an extra ``world/``
directory the user named after the server), and hands a usable path to
``ChunkSnapshotRepo.snapshot``.

Cleans up the extracted tree on context exit.
"""
from __future__ import annotations

import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Iterator


class ImportError_(Exception):
    """An archive couldn't be opened or didn't contain a recognizable world."""


_TAR_SUFFIXES = {
    ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
}


class ImportSession:
    """Context manager that unpacks an archive (or passes a dir through) and
    exposes the contained world directory.

    Use as::

        with ImportSession(Path("world.zip")) as world:
            repo.snapshot(world, label="...")
    """

    def __init__(
        self,
        source: Path | str,
        *,
        world_subpath: str | None = None,
    ):
        self.source = Path(source)
        self._world_subpath = world_subpath
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._world: Path | None = None

    def __enter__(self) -> Path:
        if not self.source.exists():
            raise ImportError_(f"source does not exist: {self.source}")

        if self.source.is_dir():
            # Pass-through: no extraction needed, just locate the world.
            self._world = _locate_world(self.source, self._world_subpath)
            return self._world

        # Archive: extract into a temp dir.
        self._tmp = tempfile.TemporaryDirectory(prefix="chunkvault-import-")
        extract_root = Path(self._tmp.name)
        kind = _detect_archive_kind(self.source)
        if kind == "zip":
            with zipfile.ZipFile(self.source) as zf:
                _safe_extract_zip(zf, extract_root)
        elif kind == "tar":
            with tarfile.open(self.source) as tf:
                _safe_extract_tar(tf, extract_root)
        else:
            raise ImportError_(
                f"unsupported archive format: {self.source.name}. "
                f"Supported: .zip, .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz."
            )

        self._world = _locate_world(extract_root, self._world_subpath)
        return self._world

    def __exit__(self, *exc) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None
        self._world = None

    @property
    def default_label(self) -> str:
        """Sensible default snapshot label derived from source name."""
        name = self.source.name
        for suf in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2",
                    ".txz", ".tar", ".zip"):
            if name.endswith(suf):
                name = name[:-len(suf)]
                break
        return name


# ---- helpers ----------------------------------------------------------------

def _detect_archive_kind(path: Path) -> str | None:
    """Return ``"zip"``, ``"tar"``, or ``None`` based on the filename."""
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip"
    for suf in _TAR_SUFFIXES:
        if name.endswith(suf):
            return "tar"
    return None


def _locate_world(root: Path, subpath: str | None) -> Path:
    """Find the actual world directory inside an extracted tree.

    Strategy:
      * If ``subpath`` is given, use ``root/subpath`` (must exist and contain
        ``level.dat`` or a ``region`` dir).
      * Else, walk up to 2 levels deep looking for a directory containing
        ``level.dat`` OR a ``region`` subdir. First match wins.
    """
    if subpath is not None:
        candidate = root / subpath
        if not candidate.is_dir():
            raise ImportError_(
                f"--world-subpath {subpath!r} does not point to a directory "
                f"in the archive"
            )
        if not _looks_like_world(candidate):
            raise ImportError_(
                f"{candidate} doesn't look like a Minecraft world (no level.dat "
                f"or region/ found)"
            )
        return candidate

    if _looks_like_world(root):
        return root
    # One level deep
    candidates = sorted(p for p in root.iterdir() if p.is_dir())
    for c in candidates:
        if _looks_like_world(c):
            return c
    # Two levels deep
    for c in candidates:
        for cc in sorted(p for p in c.iterdir() if p.is_dir()):
            if _looks_like_world(cc):
                return cc
    raise ImportError_(
        f"could not find a world directory inside {root} "
        f"(no level.dat or region/ within 2 levels). "
        f"Try --world-subpath if your archive has an unusual layout."
    )


def _looks_like_world(path: Path) -> bool:
    """Heuristic: a world dir has level.dat or a region/ subdirectory."""
    return (path / "level.dat").is_file() or (path / "region").is_dir()


# Path-traversal-safe extraction helpers.

def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    for name in zf.namelist():
        target = dest / name
        try:
            target.resolve().relative_to(dest.resolve())
        except ValueError:
            raise ImportError_(f"unsafe zip entry: {name!r}") from None
    zf.extractall(dest)


def _safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    # Python 3.12+ tarfile has a built-in 'data' filter
    tf.extractall(dest, filter="data")


def iter_archives(directory: Path | str) -> Iterator[Path]:
    """Yield every plausible archive file under ``directory``.

    Useful for batch-importing a whole shelf of historical backups.
    """
    root = Path(directory)
    for entry in sorted(root.rglob("*")):
        if entry.is_file() and _detect_archive_kind(entry) is not None:
            yield entry
