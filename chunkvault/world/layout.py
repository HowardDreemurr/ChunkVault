"""Enumerate dimension region-directories within a Minecraft world folder.

Vanilla layout:
    <world>/region/             overworld
    <world>/DIM-1/region/       nether
    <world>/DIM1/region/        the end

Datapack / modded layout (added 1.16+):
    <world>/dimensions/<namespace>/<id>/region/

Each `RegionDir.dimension_key` is the directory path relative to the world
root, using forward slashes even on Windows — so the same key identifies
the same dimension across OSes, and it's trivially round-trippable to and
from config or diff output.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..mca.region import parse_region_filename

VANILLA_DIMENSIONS = ("region", "DIM-1/region", "DIM1/region")


@dataclass(frozen=True)
class RegionDir:
    dimension_key: str   # relative path with forward slashes, e.g. "DIM-1/region"
    path: Path           # absolute path to the directory containing r.X.Z.mca


def enumerate_region_dirs(world_root: Path | str) -> list[RegionDir]:
    """Find every region directory inside a world folder.

    Missing directories are silently skipped. Order is deterministic:
    vanilla dimensions in canonical order, then datapack dimensions sorted
    by namespace + id.
    """
    root = Path(world_root)
    results: list[RegionDir] = []

    for rel in VANILLA_DIMENSIONS:
        # Each vanilla rel is already "<forward>/<slash>" form.
        p = root / rel
        if p.is_dir():
            results.append(RegionDir(dimension_key=rel, path=p))

    dims = root / "dimensions"
    if dims.is_dir():
        for ns in sorted(dims.iterdir(), key=lambda p: p.name):
            if not ns.is_dir():
                continue
            for dim_id in sorted(ns.iterdir(), key=lambda p: p.name):
                reg = dim_id / "region"
                if reg.is_dir():
                    key = f"dimensions/{ns.name}/{dim_id.name}/region"
                    results.append(RegionDir(dimension_key=key, path=reg))

    return results


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
