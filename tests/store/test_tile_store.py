"""Tests for the content-addressed tile pool."""
from __future__ import annotations

from pathlib import Path

import pytest

from chunkvault.store.tile_store import TILE_BYTES, TileStore


def _h(byte: int) -> bytes:
    """16-byte hash for tests, made of a repeating byte."""
    return bytes([byte]) * 16


def _tile(byte: int = 0xAB) -> bytes:
    return bytes([byte]) * TILE_BYTES


def test_init_creates_tiles_dir(tmp_path: Path):
    store = TileStore(tmp_path / "repo")
    store.init()
    assert (tmp_path / "repo" / "tiles").is_dir()


def test_store_then_read_roundtrip(tmp_path: Path):
    store = TileStore(tmp_path / "repo")
    store.init()
    h, mode = _h(1), "topdown"
    payload = _tile(0xCD)
    assert store.store(h, mode, payload) is True
    assert store.has(h, mode)
    assert store.read(h, mode) == payload


def test_store_is_idempotent(tmp_path: Path):
    """Repeated store of the same (hash, mode) returns False the second time."""
    store = TileStore(tmp_path / "repo")
    store.init()
    h, mode = _h(2), "topdown"
    assert store.store(h, mode, _tile()) is True
    assert store.store(h, mode, _tile()) is False
    assert store.has(h, mode)


def test_different_modes_for_same_hash_are_independent(tmp_path: Path):
    """Same chunk content can have multiple cached renders without collision."""
    store = TileStore(tmp_path / "repo")
    store.init()
    h = _h(3)
    a = _tile(0x11)
    b = _tile(0x22)
    c = _tile(0x33)
    store.store(h, "topdown", a)
    store.store(h, "nether_low", b)
    store.store(h, "nether_high", c)
    assert store.read(h, "topdown") == a
    assert store.read(h, "nether_low") == b
    assert store.read(h, "nether_high") == c


def test_read_missing_returns_none(tmp_path: Path):
    store = TileStore(tmp_path / "repo")
    store.init()
    assert store.read(_h(99), "topdown") is None
    assert not store.has(_h(99), "topdown")


def test_delete_removes_tile(tmp_path: Path):
    store = TileStore(tmp_path / "repo")
    store.init()
    h, mode = _h(4), "topdown"
    store.store(h, mode, _tile())
    assert store.delete(h, mode) is True
    assert not store.has(h, mode)
    # Deleting twice is a safe no-op
    assert store.delete(h, mode) is False


def test_wrong_size_payload_rejected(tmp_path: Path):
    store = TileStore(tmp_path / "repo")
    store.init()
    with pytest.raises(ValueError, match="must be exactly"):
        store.store(_h(5), "topdown", b"too short")
    with pytest.raises(ValueError, match="must be exactly"):
        store.store(_h(5), "topdown", b"x" * (TILE_BYTES + 1))


def test_unsafe_mode_name_rejected(tmp_path: Path):
    """Mode names go into the file path, so reject path-traversal attempts."""
    store = TileStore(tmp_path / "repo")
    store.init()
    payload = _tile()
    for bad in ("../escape", "with/slash", "back\\slash", "..", ""):
        with pytest.raises(ValueError):
            store.store(_h(6), bad, payload)


def test_sharded_layout_keeps_dirs_small(tmp_path: Path):
    """Many tiles spread across XX/YY/ subdirs (no single huge directory)."""
    store = TileStore(tmp_path / "repo")
    store.init()
    # Store 256 distinct hashes
    for i in range(256):
        h = bytes([i, i, i, i] + [0] * 12)
        store.store(h, "topdown", _tile(i))
    # Top-level should have multiple shard directories, not 256 files
    top_entries = list((tmp_path / "repo" / "tiles").iterdir())
    # Each shard dir is named XX (hex). Many distinct first bytes → many dirs.
    assert all(d.is_dir() for d in top_entries)
    assert len(top_entries) > 1
