"""Enumerate dimension region-directories within a Minecraft world folder.

Real-world MC server layouts vary wildly:

    <world>/region/                              vanilla overworld
    <world>/DIM-1/region/                        vanilla nether
    <world>/DIM1/region/                         vanilla end
    <world>/dimensions/<ns>/<id>/region/         datapack
    <server>/world/region/                       Bukkit overworld
    <server>/world_nether/region/                Bukkit nether
    <server>/world_the_end/region/               Bukkit end
    <server>/<custom>/region/                    Multiverse / renamed worlds
    <server>/world/region/                       (any name from server.properties level-name)

Rather than hardcode the known layouts, we just look for ANY directory named
``region`` (anywhere within world_root, depth-bounded) that contains
``r.X.Z.mca`` files. The dimension_key is the relative posix path from
world_root to the region dir — uniquely identifies it forever.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..mca.region import parse_region_filename

# Maximum depth (from world_root) at which we'll look for region/ directories.
# Vanilla goes 2-4 levels (world/dimensions/ns/id/region/). 6 is generous.
_MAX_REGION_DEPTH = 6


@dataclass(frozen=True)
class RegionDir:
    dimension_key: str   # relative path with forward slashes, e.g. "DIM-1/region"
    path: Path           # absolute path to the directory containing r.X.Z.mca


def enumerate_region_dirs(world_root: Path | str) -> list[RegionDir]:
    """Find every region directory under ``world_root``, regardless of layout.

    A "region directory" is any directory named ``region`` (depth ≤ 6 from
    world_root) that contains at least one ``r.X.Z.mca`` file. Returns them
    sorted by dimension_key for deterministic iteration.
    """
    root = Path(world_root)
    results: list[RegionDir] = []
    if not root.is_dir():
        return results
    for region_dir in _find_region_dirs(root, _MAX_REGION_DEPTH):
        rel = region_dir.relative_to(root).as_posix()
        results.append(RegionDir(dimension_key=rel, path=region_dir))
    results.sort(key=lambda r: r.dimension_key)
    return results


def _find_region_dirs(root: Path, max_depth: int) -> Iterator[Path]:
    """Yield directories that contain at least one r.X.Z.mca file.

    MC stores region-format data in three known places per world tree:
    ``region/`` (block data), ``entities/`` (1.17+ mob/item data), and
    ``poi/`` (1.14+ village + portal points). Plus per-dimension
    variants (``DIM-1/entities/``, ``world_nether/poi/``, etc.).

    Rather than maintaining a name list — easy to forget a variant when
    Mojang adds a new one, or when a mod introduces its own region-style
    pool — we treat any directory containing ``r.X.Z.mca`` files as a
    region-style dir. That uniformly enables chunk-level dedup across
    all of MC's vanilla region pools and is forward-compatible with
    future format additions.
    """
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            children = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        # Probe THIS dir: does it contain r.X.Z.mca files?
        try:
            has_mca = any(
                child.is_file()
                and parse_region_filename(child) is not None
                for child in children
            )
        except (PermissionError, OSError):
            has_mca = False
        if has_mca:
            yield current
            # A region-style dir doesn't itself contain nested region-style
            # dirs — stop descending.
            continue
        # Otherwise descend into subdirs
        for child in children:
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            if depth + 1 < max_depth:
                stack.append((child, depth + 1))


def iter_region_files(region_dir: Path) -> Iterator[tuple[int, int, Path]]:
    """Yield (rx, rz, path) for every r.X.Z.mca file in a region directory.

    Files whose names don't parse as region coordinates are skipped
    silently — this is defensive against accidental stray files that
    admins sometimes drop in world folders.
    """
    if not region_dir.is_dir():
        return
    for entry in sorted(region_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        coords = parse_region_filename(entry)
        if coords is None:
            continue
        yield coords.rx, coords.rz, entry
