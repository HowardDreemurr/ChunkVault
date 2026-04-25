from __future__ import annotations

from pathlib import Path

from chunkvault.world.layout import enumerate_region_dirs, iter_region_files


def _mk(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_enumerate_missing_world_root(tmp_path: Path):
    assert enumerate_region_dirs(tmp_path / "does_not_exist") == []


def test_enumerate_empty_world(tmp_path: Path):
    world = tmp_path / "world"
    world.mkdir()
    assert enumerate_region_dirs(world) == []


def test_enumerate_vanilla_dimensions(tmp_path: Path):
    world = tmp_path / "world"
    _mk(world / "region")
    _mk(world / "DIM-1" / "region")
    _mk(world / "DIM1" / "region")
    dirs = enumerate_region_dirs(world)
    keys = [d.dimension_key for d in dirs]
    assert keys == ["region", "DIM-1/region", "DIM1/region"]


def test_enumerate_datapack_dimensions(tmp_path: Path):
    world = tmp_path / "world"
    _mk(world / "region")
    _mk(world / "dimensions" / "mypack" / "strange_world" / "region")
    _mk(world / "dimensions" / "otherpack" / "another" / "region")
    dirs = enumerate_region_dirs(world)
    keys = [d.dimension_key for d in dirs]
    assert keys == [
        "region",
        "dimensions/mypack/strange_world/region",
        "dimensions/otherpack/another/region",
    ]


def test_enumerate_skips_non_directories(tmp_path: Path):
    world = tmp_path / "world"
    _mk(world / "region")
    # A stray file in dimensions/ should not crash us
    (world / "dimensions").mkdir()
    (world / "dimensions" / "README.txt").write_text("hi")
    dirs = enumerate_region_dirs(world)
    assert [d.dimension_key for d in dirs] == ["region"]


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
