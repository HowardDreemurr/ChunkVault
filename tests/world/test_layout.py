from __future__ import annotations

from pathlib import Path

from chunkvault.world.layout import enumerate_region_dirs, iter_region_files

from tests._fixtures import ChunkSpec, write_region_file


def _mk(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _seed_region(world: Path, dim_key: str) -> None:
    """Drop a single valid r.0.0.mca into world/<dim_key>/.

    enumerate_region_dirs only counts a region/ dir if it contains real
    region files, so test fixtures need a real (parseable) .mca, not just
    an empty directory.
    """
    write_region_file(world, dim_key, 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"x"),
    ])


def test_enumerate_missing_world_root(tmp_path: Path):
    assert enumerate_region_dirs(tmp_path / "does_not_exist") == []


def test_enumerate_empty_world(tmp_path: Path):
    world = tmp_path / "world"
    world.mkdir()
    assert enumerate_region_dirs(world) == []


def test_enumerate_empty_region_dir_is_ignored(tmp_path: Path):
    """A directory named region/ but empty of .mca files isn't a region dir."""
    world = tmp_path / "world"
    _mk(world / "region")
    assert enumerate_region_dirs(world) == []


def test_enumerate_vanilla_dimensions(tmp_path: Path):
    world = tmp_path / "world"
    _seed_region(world, "region")
    _seed_region(world, "DIM-1/region")
    _seed_region(world, "DIM1/region")
    dirs = enumerate_region_dirs(world)
    keys = [d.dimension_key for d in dirs]
    assert sorted(keys) == ["DIM-1/region", "DIM1/region", "region"]


def test_enumerate_datapack_dimensions(tmp_path: Path):
    world = tmp_path / "world"
    _seed_region(world, "region")
    _seed_region(world, "dimensions/mypack/strange_world/region")
    _seed_region(world, "dimensions/otherpack/another/region")
    dirs = enumerate_region_dirs(world)
    keys = [d.dimension_key for d in dirs]
    assert sorted(keys) == [
        "dimensions/mypack/strange_world/region",
        "dimensions/otherpack/another/region",
        "region",
    ]


def test_enumerate_skips_non_directories(tmp_path: Path):
    world = tmp_path / "world"
    _seed_region(world, "region")
    # A stray file in dimensions/ should not crash us
    (world / "dimensions").mkdir()
    (world / "dimensions" / "README.txt").write_text("hi")
    dirs = enumerate_region_dirs(world)
    assert [d.dimension_key for d in dirs] == ["region"]


def test_enumerate_finds_bukkit_layout(tmp_path: Path):
    """Bukkit puts each dimension at the SERVER root, not under a single world/."""
    server = tmp_path / "EX-Server"
    _seed_region(server, "world/region")
    _seed_region(server, "world_nether/region")
    _seed_region(server, "world_the_end/region")
    keys = [d.dimension_key for d in enumerate_region_dirs(server)]
    assert sorted(keys) == [
        "world/region",
        "world_nether/region",
        "world_the_end/region",
    ]


def test_enumerate_finds_custom_world_name(tmp_path: Path):
    """Multiverse / renamed worlds: world dir can be any name."""
    server = tmp_path / "EX-Server"
    _seed_region(server, "survival/region")
    _seed_region(server, "creative/region")
    keys = [d.dimension_key for d in enumerate_region_dirs(server)]
    assert sorted(keys) == ["creative/region", "survival/region"]


def test_iter_region_files_basic(tmp_path: Path):
    rdir = _mk(tmp_path / "region")
    (rdir / "r.0.0.mca").write_bytes(b"")
    (rdir / "r.-3.12.mca").write_bytes(b"")
    (rdir / "session.lock").write_bytes(b"")  # should be skipped
    (rdir / "r.foo.0.mca").write_bytes(b"")   # bad name, skipped
    results = list(iter_region_files(rdir))
    coords = sorted((rx, rz) for rx, rz, _ in results)
    assert coords == [(-3, 12), (0, 0)]


def test_iter_region_files_missing_dir(tmp_path: Path):
    assert list(iter_region_files(tmp_path / "no_such_dir")) == []
