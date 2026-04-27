"""Round-trip verification: prove a snapshot can faithfully reproduce its source.

The chunk store is content-addressed and unit-tested, but real-world trust
needs a cheaper-than-full-restore self-check: take a fresh snapshot, restore
it to a temporary directory, then walk the two trees in lockstep and compare:

* every region's set of present chunks
* every chunk's content hash (computed on both sides — chunk-store-aware)
* every non-region file's bytes
* every "extra" file in the restore that wasn't in the source

This catches: a botched restore, a corrupt manifest, missing pool blobs,
silent reassembly bugs (e.g. the chunk-stub format change), or — most
important — the case where the user *thinks* their backup is good but a
deletion+gc actually nuked something it shouldn't have.

By design this is **expensive** (one full restore + a full byte walk).
Run it sparingly: after the first snapshot of a new world, after a major
chunkvault upgrade, periodically as a smoke test. Not on every snapshot.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..mca.hasher import hash_chunk, hash_chunk_on_disk
from ..mca.region import MCAError, Region
from ..world.layout import enumerate_region_dirs, iter_region_files
from .progress import ProgressCallback, ProgressEvent, _emit
from .repo import DEFAULT_EXCLUDE, ChunkRepoError, _matches_any
# Forward reference to avoid circular import at module load.


@dataclass(frozen=True)
class ChunkMismatch:
    dimension_key: str
    rx: int
    rz: int
    cx: int
    cz: int
    kind: str       # "missing_in_restore" | "extra_in_restore" | "hash_mismatch"
    detail: str = ""


@dataclass(frozen=True)
class FileMismatch:
    relative_path: str
    kind: str       # "missing_in_restore" | "extra_in_restore" | "byte_mismatch"
    source_size: int = -1
    restore_size: int = -1


@dataclass
class RoundTripReport:
    # Counts
    chunks_checked: int = 0
    chunks_matching: int = 0
    files_checked: int = 0
    files_matching: int = 0

    # Mismatches
    chunk_mismatches: list[ChunkMismatch] = field(default_factory=list)
    file_mismatches: list[FileMismatch] = field(default_factory=list)
    regions_only_in_source: list[str] = field(default_factory=list)
    regions_only_in_restore: list[str] = field(default_factory=list)

    # Expected divergence (excluded files that intentionally aren't in the snapshot)
    files_excluded_from_snapshot: list[str] = field(default_factory=list)

    # Errors that aborted comparison of some piece (e.g. corrupt MCA on either side)
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            not self.chunk_mismatches
            and not self.file_mismatches
            and not self.regions_only_in_source
            and not self.regions_only_in_restore
        )

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"{verdict}  "
            f"chunks: {self.chunks_matching}/{self.chunks_checked}, "
            f"files: {self.files_matching}/{self.files_checked}, "
            f"chunk-mismatches: {len(self.chunk_mismatches)}, "
            f"file-mismatches: {len(self.file_mismatches)}, "
            f"missing-regions: {len(self.regions_only_in_source)}, "
            f"extra-regions: {len(self.regions_only_in_restore)}, "
            f"excluded: {len(self.files_excluded_from_snapshot)}"
        )


def compare_directories(
    a: Path | str,
    b: Path | str,
    *,
    exclude: Iterable[str] | None = None,
    progress_cb: ProgressCallback = None,
) -> RoundTripReport:
    """Byte-level comparison of two world-shaped directory trees.

    Reuses the round-trip comparator (region-by-region chunk hash compare +
    non-region byte-equality compare), but skips the snapshot/restore step.
    Intended for ad-hoc audits — e.g. "I restored a snapshot here, and the
    archive's contents are extracted there; do they match?".

    The report's ``regions_only_in_source`` / ``regions_only_in_restore``
    name the LEFT (``a``) side as ``source`` and the RIGHT (``b``) side as
    ``restore`` for compatibility with the existing :class:`RoundTripReport`
    fields. ``exclude`` is matched against paths in ``a`` only (mirrors the
    snapshot semantics — if a file is excluded from ``a`` and absent in
    ``b``, that's "expected", not a mismatch).
    """
    a_path = Path(a)
    b_path = Path(b)
    if not a_path.is_dir():
        raise ChunkRepoError(f"left side is not a directory: {a_path}")
    if not b_path.is_dir():
        raise ChunkRepoError(f"right side is not a directory: {b_path}")

    exclude_patterns = tuple(exclude) if exclude is not None else ()
    report = RoundTripReport()
    _compare_regions(a_path, b_path, report, progress_cb)
    _compare_files(a_path, b_path, exclude_patterns, report, progress_cb)
    _emit(progress_cb, ProgressEvent(
        kind="finish", phase="compare_directories",
        label=report.summary(),
    ))
    return report


def verify_roundtrip(
    repo: "ChunkSnapshotRepo",
    snapshot,
    original_world: Path | str,
    *,
    exclude: Iterable[str] | None = None,
    progress_cb: ProgressCallback = None,
    keep_restore: Path | None = None,
) -> RoundTripReport:
    """Restore ``snapshot`` to a temp dir, then compare against ``original_world``.

    ``exclude`` should match the same patterns the snapshot used (defaults
    to :data:`DEFAULT_EXCLUDE`); files matching them are reported as
    "expected exclusion" rather than as missing.

    ``keep_restore`` (optional path): keep the restored copy here instead
    of cleaning it up — useful for forensics if the report flags a problem.
    """
    from .repo import ChunkSnapshotRepo  # noqa: F401  — for type hint
    original = Path(original_world)
    if not original.is_dir():
        raise ChunkRepoError(f"original world is not a directory: {original}")

    exclude_patterns = tuple(exclude) if exclude is not None else DEFAULT_EXCLUDE

    if keep_restore is not None:
        restore_path = Path(keep_restore)
        restore_path.mkdir(parents=True, exist_ok=True)
        return _do_compare(
            repo, snapshot, original, restore_path,
            exclude_patterns, progress_cb,
        )

    with tempfile.TemporaryDirectory(prefix="chunkvault-rt-") as td:
        return _do_compare(
            repo, snapshot, original, Path(td),
            exclude_patterns, progress_cb,
        )


# ---- internals --------------------------------------------------------------

def _do_compare(
    repo, snapshot, original: Path, restore_path: Path,
    exclude_patterns: tuple[str, ...],
    progress_cb: ProgressCallback,
) -> RoundTripReport:
    # Pass the callback INTO restore — it emits restore_regions /
    # restore_files phases with per-item progress. Without this the
    # roundtrip step is a silent multi-hour stretch on TB-scale worlds.
    repo.restore(snapshot, restore_path, progress_cb=progress_cb)

    report = RoundTripReport()
    _compare_regions(original, restore_path, report, progress_cb)
    _compare_files(original, restore_path, exclude_patterns, report, progress_cb)
    _emit(progress_cb, ProgressEvent(
        kind="finish", phase="roundtrip",
        label=report.summary(),
    ))
    return report


def _compare_regions(
    source: Path, restore: Path, report: RoundTripReport,
    progress_cb: ProgressCallback,
) -> None:
    source_regions = _enumerate_regions(source)
    restore_regions = _enumerate_regions(restore)

    only_source = sorted(set(source_regions) - set(restore_regions))
    only_restore = sorted(set(restore_regions) - set(source_regions))
    for key in only_source:
        report.regions_only_in_source.append(_key_str(key))
    for key in only_restore:
        report.regions_only_in_restore.append(_key_str(key))

    shared = sorted(set(source_regions) & set(restore_regions))
    _emit(progress_cb, ProgressEvent(
        kind="phase_start", phase="compare_regions",
        label="comparing chunks", total=len(shared),
    ))
    for i, key in enumerate(shared, 1):
        src_path = source_regions[key]
        rst_path = restore_regions[key]
        _compare_one_region(key, src_path, rst_path, report)
        _emit(progress_cb, ProgressEvent(
            kind="phase_progress", phase="compare_regions",
            label=_key_str(key), current=i, total=len(shared),
        ))
    _emit(progress_cb, ProgressEvent(
        kind="phase_done", phase="compare_regions",
        current=len(shared), total=len(shared),
    ))


def _compare_one_region(
    key: tuple[str, int, int],
    source_path: Path, restore_path: Path,
    report: RoundTripReport,
) -> None:
    dim_key, rx, rz = key
    try:
        src_region = Region(source_path)
        rst_region = Region(restore_path)
    except MCAError as e:
        report.errors.append(f"{_key_str(key)}: open failed — {e}")
        return

    try:
        src_chunks = {(c.cx, c.cz): c for c in src_region.iter_chunks()}
    except MCAError as e:
        report.errors.append(f"{_key_str(key)} (source): parse failed — {e}")
        return
    try:
        rst_chunks = {(c.cx, c.cz): c for c in rst_region.iter_chunks()}
    except MCAError as e:
        report.errors.append(f"{_key_str(key)} (restore): parse failed — {e}")
        return

    for local in sorted(set(src_chunks) | set(rst_chunks)):
        cx, cz = local
        report.chunks_checked += 1
        in_src = local in src_chunks
        in_rst = local in rst_chunks
        if in_src and not in_rst:
            report.chunk_mismatches.append(ChunkMismatch(
                dimension_key=dim_key, rx=rx, rz=rz,
                cx=cx, cz=cz, kind="missing_in_restore",
            ))
            continue
        if in_rst and not in_src:
            report.chunk_mismatches.append(ChunkMismatch(
                dimension_key=dim_key, rx=rx, rz=rz,
                cx=cx, cz=cz, kind="extra_in_restore",
            ))
            continue
        # Both present — compare content
        try:
            src_hash = hash_chunk_on_disk(src_region, src_chunks[local])
            rst_hash = hash_chunk_on_disk(rst_region, rst_chunks[local])
        except Exception as e:
            report.errors.append(
                f"{_key_str(key)} chunk ({cx},{cz}): hash failed — {e}"
            )
            continue
        if src_hash == rst_hash:
            report.chunks_matching += 1
        else:
            report.chunk_mismatches.append(ChunkMismatch(
                dimension_key=dim_key, rx=rx, rz=rz,
                cx=cx, cz=cz, kind="hash_mismatch",
                detail=f"src={src_hash.hex()[:12]} rst={rst_hash.hex()[:12]}",
            ))


def _compare_files(
    source: Path, restore: Path,
    exclude_patterns: tuple[str, ...],
    report: RoundTripReport,
    progress_cb: ProgressCallback,
) -> None:
    region_paths_src = {p for p in _all_region_paths(source)}
    region_paths_rst = {p for p in _all_region_paths(restore)}

    src_files: dict[str, Path] = {}
    for entry in source.rglob("*"):
        if not entry.is_file():
            continue
        if entry in region_paths_src:
            continue
        rel = entry.relative_to(source).as_posix()
        # Also skip .mcc files — they're handled as part of region chunks
        if rel.split("/")[-1].startswith("c.") and rel.endswith(".mcc"):
            continue
        src_files[rel] = entry

    rst_files: dict[str, Path] = {}
    for entry in restore.rglob("*"):
        if not entry.is_file():
            continue
        if entry in region_paths_rst:
            continue
        rel = entry.relative_to(restore).as_posix()
        if rel.split("/")[-1].startswith("c.") and rel.endswith(".mcc"):
            continue
        rst_files[rel] = entry

    _emit(progress_cb, ProgressEvent(
        kind="phase_start", phase="compare_files",
        label="comparing non-region files",
        total=len(set(src_files) | set(rst_files)),
    ))

    counter = 0
    for rel in sorted(set(src_files) | set(rst_files)):
        counter += 1
        in_src = rel in src_files
        in_rst = rel in rst_files
        if in_src and not in_rst:
            if _matches_any(rel, exclude_patterns):
                report.files_excluded_from_snapshot.append(rel)
            else:
                report.file_mismatches.append(FileMismatch(
                    relative_path=rel, kind="missing_in_restore",
                    source_size=src_files[rel].stat().st_size,
                ))
        elif in_rst and not in_src:
            report.file_mismatches.append(FileMismatch(
                relative_path=rel, kind="extra_in_restore",
                restore_size=rst_files[rel].stat().st_size,
            ))
        else:
            report.files_checked += 1
            if _file_bytes_equal(src_files[rel], rst_files[rel]):
                report.files_matching += 1
            else:
                report.file_mismatches.append(FileMismatch(
                    relative_path=rel, kind="byte_mismatch",
                    source_size=src_files[rel].stat().st_size,
                    restore_size=rst_files[rel].stat().st_size,
                ))
        _emit(progress_cb, ProgressEvent(
            kind="phase_progress", phase="compare_files",
            label=rel, current=counter,
            total=len(set(src_files) | set(rst_files)),
        ))
    _emit(progress_cb, ProgressEvent(
        kind="phase_done", phase="compare_files",
        current=counter, total=counter,
    ))


# ---- helpers ----------------------------------------------------------------

def _enumerate_regions(world: Path) -> dict[tuple[str, int, int], Path]:
    out: dict[tuple[str, int, int], Path] = {}
    for region_dir in enumerate_region_dirs(world):
        for rx, rz, region_path in iter_region_files(region_dir.path):
            out[(region_dir.dimension_key, rx, rz)] = region_path
    return out


def _all_region_paths(world: Path) -> Iterable[Path]:
    for region_dir in enumerate_region_dirs(world):
        for _, _, p in iter_region_files(region_dir.path):
            yield p


def _file_bytes_equal(a: Path, b: Path, chunk_size: int = 1 << 16) -> bool:
    """Compare files efficiently: short-circuit on size, then stream-hash."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        ha = hashlib.blake2b(digest_size=16)
        hb = hashlib.blake2b(digest_size=16)
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ba = fa.read(chunk_size)
                bb = fb.read(chunk_size)
                if not ba and not bb:
                    return True
                if ba != bb:
                    return False
                ha.update(ba); hb.update(bb)
    except OSError:
        return False


def _key_str(key: tuple[str, int, int]) -> str:
    dim, rx, rz = key
    return f"{dim}/r.{rx}.{rz}.mca"
