"""Tests for the analytics-friendly snapshot file access API:
list_snapshot_files / iter_snapshot_files / read_snapshot_file.
"""
from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.store.repo import ChunkRepoError

from tests._fixtures import ChunkSpec, write_region_file
from tests.store.test_repo import _make_level_dat


def _world_with_extras(tmp_path: Path) -> Path:
    """Build a world with level.dat + playerdata + a datapack."""
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(
        _make_level_dat("1.20.4", 3700, last_played_ms=1_700_000_000_000),
    )
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"chunk-A"),
    ])
    pd = world / "playerdata"
    pd.mkdir()
    (pd / "alice-uuid.dat").write_bytes(b"alice-nbt-bytes")
    (pd / "bob-uuid.dat").write_bytes(b"bob-nbt-bytes")
    (pd / "carol-uuid.dat").write_bytes(b"carol-nbt-bytes")
    dp = world / "datapacks"
    dp.mkdir()
    (dp / "vanilla.zip").write_bytes(b"PK\x03\x04fake-zip-bytes")
    (dp / "custom.zip").write_bytes(b"PK\x03\x04another-zip")
    return world


def test_list_snapshot_files_no_filter(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world_with_extras(tmp_path)
    snap = repo.snapshot(world, label="x", verify_roundtrip=False)
    entries = repo.list_snapshot_files(snap)
    paths = {p for p, _ in entries}
    assert "level.dat" in paths
    assert "playerdata/alice-uuid.dat" in paths
    assert "datapacks/vanilla.zip" in paths


def test_list_snapshot_files_prefix_filter(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world_with_extras(tmp_path)
    snap = repo.snapshot(world, label="x", verify_roundtrip=False)
    entries = repo.list_snapshot_files(snap, prefix="playerdata/")
    paths = sorted(p for p, _ in entries)
    assert paths == [
        "playerdata/alice-uuid.dat",
        "playerdata/bob-uuid.dat",
        "playerdata/carol-uuid.dat",
    ]
    # sha256 is a 32-byte digest
    for _, sha in entries:
        assert isinstance(sha, bytes) and len(sha) == 32


def test_list_snapshot_files_suffix_filter(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world_with_extras(tmp_path)
    snap = repo.snapshot(world, label="x", verify_roundtrip=False)
    entries = repo.list_snapshot_files(snap, suffix=".zip")
    assert all(p.endswith(".zip") for p, _ in entries)
    assert len(entries) == 2


def test_list_snapshot_files_combined_filters(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world_with_extras(tmp_path)
    snap = repo.snapshot(world, label="x", verify_roundtrip=False)
    entries = repo.list_snapshot_files(
        snap, prefix="playerdata/", suffix=".dat",
    )
    assert len(entries) == 3


def test_iter_snapshot_files_yields_bytes(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world_with_extras(tmp_path)
    snap = repo.snapshot(world, label="x", verify_roundtrip=False)
    pairs = list(repo.iter_snapshot_files(snap, prefix="playerdata/"))
    assert len(pairs) == 3
    by_path = {p: data for p, data in pairs}
    assert by_path["playerdata/alice-uuid.dat"] == b"alice-nbt-bytes"
    assert by_path["playerdata/bob-uuid.dat"] == b"bob-nbt-bytes"


def test_read_snapshot_file_exact_path(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world_with_extras(tmp_path)
    snap = repo.snapshot(world, label="x", verify_roundtrip=False)
    data = repo.read_snapshot_file(snap, "playerdata/alice-uuid.dat")
    assert data == b"alice-nbt-bytes"


def test_read_snapshot_file_returns_none_for_missing(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world_with_extras(tmp_path)
    snap = repo.snapshot(world, label="x", verify_roundtrip=False)
    assert repo.read_snapshot_file(snap, "no/such/file.txt") is None


def test_accepts_snapshot_id_or_label(tmp_path: Path):
    """All three methods should accept either a ChunkSnapshot object or
    a short_id / label string."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world_with_extras(tmp_path)
    snap = repo.snapshot(world, label="my-label", verify_roundtrip=False)

    # By short_id
    by_short = repo.list_snapshot_files(snap.short_id)
    # By label
    by_label = repo.list_snapshot_files("my-label")
    # By object
    by_obj = repo.list_snapshot_files(snap)

    assert len(by_short) == len(by_label) == len(by_obj)


def test_unknown_snapshot_raises(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    with pytest.raises(ChunkRepoError):
        repo.list_snapshot_files("does-not-exist")
    with pytest.raises(ChunkRepoError):
        list(repo.iter_snapshot_files("does-not-exist"))
    with pytest.raises(ChunkRepoError):
        repo.read_snapshot_file("does-not-exist", "anything")


def test_realistic_nbt_workflow(tmp_path: Path):
    """End-to-end: gzip a level.dat-style payload, snapshot, read it back
    via the API, decompress, confirm bytes match. Mimics the real
    analytics use case."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(
        _make_level_dat("1.20.4", 3700, last_played_ms=1_700_000_000_000),
    )
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"x"),
    ])
    # Add a gzipped synthetic playerdata
    pd_dir = world / "playerdata"
    pd_dir.mkdir()
    fake_player_nbt = b"\\x0a\\x00\\x00fake-player-compound\\x00"
    (pd_dir / "test.dat").write_bytes(gzip.compress(fake_player_nbt))

    snap = repo.snapshot(world, label="x", verify_roundtrip=False)

    raw = repo.read_snapshot_file(snap, "playerdata/test.dat")
    assert raw is not None
    decompressed = gzip.decompress(raw)
    assert decompressed == fake_player_nbt
