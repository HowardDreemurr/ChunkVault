"""World-level chunk diff.

`diff_worlds(old, new)` walks both world trees, matches region files by
(dimension, rx, rz), and classifies every chunk as added / removed /
modified based on its content hash.

Corruption in a single region is captured as a `RegionError` in the result,
not raised — a single broken file should never prevent us from diffing the
rest of the world.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Literal

from ..mca.hasher import hash_chunk_on_disk
from ..mca.region import MCAError, RawChunk, Region
from ..world.layout import RegionDir, enumerate_region_dirs, iter_region_files

ChunkKind = Literal["added", "removed", "modified"]


@dataclass(frozen=True)
class ChunkDiff:
    dimension_key: str
    rx: int
    rz: int
    cx: int          # world-space chunk x (rx * 32 + local_cx)
    cz: int          # world-space chunk z
    kind: ChunkKind
    old_hash: bytes | None
    new_hash: bytes | None


@dataclass(frozen=True)
class RegionError:
    """A region file on either side failed to parse; we record it and move on."""
    dimension_key: str
    rx: int
    rz: int
    side: Literal["old", "new"]
    path: Path
    message: str


@dataclass
class WorldDiff:
    old_root: Path
    new_root: Path
    changes: list[ChunkDiff] = field(default_factory=list)
    errors: list[RegionError] = field(default_factory=list)
    # Optional metadata, populated when the diff comes from a snapshot store
    # that has version info available. Stays None for directory-based diffs.
    old_mc_version: str | None = None
    new_mc_version: str | None = None
    old_data_version: int | None = None
    new_data_version: int | None = None
    old_label: str | None = None
    new_label: str | None = None

    def count_by_kind(self) -> dict[str, int]:
        counts = {"added": 0, "removed": 0, "modified": 0}
        for c in self.changes:
            counts[c.kind] += 1
        return counts

    def by_dimension(self) -> dict[str, list[ChunkDiff]]:
        out: dict[str, list[ChunkDiff]] = {}
        for c in self.changes:
            out.setdefault(c.dimension_key, []).append(c)
        return out

    def version_changed(self) -> bool:
        """True if the two snapshots' DataVersions are both known and differ."""
        return (
            self.old_data_version is not None
            and self.new_data_version is not None
            and self.old_data_version != self.new_data_version
        )


def diff_worlds(old: Path | str, new: Path | str) -> WorldDiff:
    old_root = Path(old)
    new_root = Path(new)
    result = WorldDiff(old_root=old_root, new_root=new_root)

    old_dims = {rd.dimension_key: rd for rd in enumerate_region_dirs(old_root)}
    new_dims = {rd.dimension_key: rd for rd in enumerate_region_dirs(new_root)}

    for key in sorted(set(old_dims) | set(new_dims)):
        _diff_dimension(
            key,
            old_dims.get(key),
            new_dims.get(key),
            result,
        )
    return result


def _diff_dimension(
    key: str,
    old_dir: RegionDir | None,
    new_dir: RegionDir | None,
    result: WorldDiff,
) -> None:
    old_regions: dict[tuple[int, int], Path] = {}
    new_regions: dict[tuple[int, int], Path] = {}
    if old_dir is not None:
        old_regions = {(rx, rz): p for rx, rz, p in iter_region_files(old_dir.path)}
    if new_dir is not None:
        new_regions = {(rx, rz): p for rx, rz, p in iter_region_files(new_dir.path)}

    for coord in sorted(set(old_regions) | set(new_regions)):
        rx, rz = coord
        _diff_region(key, rx, rz, old_regions.get(coord), new_regions.get(coord), result)


def _diff_region(
    dimension_key: str,
    rx: int,
    rz: int,
    old_path: Path | None,
    new_path: Path | None,
    result: WorldDiff,
) -> None:
    old_hashes = _hashes_safe(dimension_key, rx, rz, old_path, "old", result)
    new_hashes = _hashes_safe(dimension_key, rx, rz, new_path, "new", result)
    if old_hashes is None and new_hashes is None:
        return

    old_map = old_hashes or {}
    new_map = new_hashes or {}

    for local in sorted(set(old_map) | set(new_map)):
        old_h = old_map.get(local)
        new_h = new_map.get(local)
        if old_h is None:
            kind: ChunkKind = "added"
        elif new_h is None:
            kind = "removed"
        elif old_h != new_h:
            kind = "modified"
        else:
            continue
        lcx, lcz = local
        result.changes.append(ChunkDiff(
            dimension_key=dimension_key,
            rx=rx, rz=rz,
            cx=rx * 32 + lcx,
            cz=rz * 32 + lcz,
            kind=kind,
            old_hash=old_h,
            new_hash=new_h,
        ))


def _hashes_safe(
    dimension_key: str,
    rx: int,
    rz: int,
    path: Path | None,
    side: Literal["old", "new"],
    result: WorldDiff,
) -> dict[tuple[int, int], bytes] | None:
    """Hash every chunk in a region file. Returns None if the file was
    unreadable (error is recorded in result.errors)."""
    if path is None:
        return None
    try:
        region = Region(path)
        out: dict[tuple[int, int], bytes] = {}
        for chunk in region.iter_chunks():
            out[(chunk.cx, chunk.cz)] = hash_chunk_on_disk(region, chunk)
        return out
    except (MCAError, OSError) as e:
        result.errors.append(RegionError(
            dimension_key=dimension_key,
            rx=rx, rz=rz,
            side=side,
            path=path,
            message=str(e),
        ))
        return None
