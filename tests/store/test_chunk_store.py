from __future__ import annotations

from pathlib import Path

import pytest

from chunkvault.store.chunk_store import ChunkStore


def _h(b: int, length: int = 16) -> bytes:
    return bytes([b]) * length


# ---- chunks ----------------------------------------------------------------

def test_store_and_read_chunk_roundtrip(tmp_path: Path):
    store = ChunkStore(tmp_path)
    store.init()
    h = _h(0xAB)
    payload = b"hello world" * 100
    assert store.has_chunk(h) is False
    assert store.store_chunk(h, payload) is True
    assert store.has_chunk(h) is True
    assert store.read_chunk(h) == payload


def test_store_chunk_dedup_returns_false_on_existing(tmp_path: Path):
    store = ChunkStore(tmp_path)
    store.init()
    h = _h(0xCD)
    assert store.store_chunk(h, b"first") is True
    assert store.store_chunk(h, b"first") is False  # already there
    # Original content should be unchanged
    assert store.read_chunk(h) == b"first"


def test_read_missing_chunk_returns_none(tmp_path: Path):
    store = ChunkStore(tmp_path)
    store.init()
    assert store.read_chunk(_h(0)) is None


def test_chunk_path_uses_4hex_prefix_split(tmp_path: Path):
    """chunks/XX/YY/<rest>: avoids any single directory holding >65k files."""
    store = ChunkStore(tmp_path)
    store.init()
    h = bytes.fromhex("abcdef0123456789abcdef0123456789")
    store.store_chunk(h, b"x")
    assert (tmp_path / "chunks" / "ab" / "cd" / "ef0123456789abcdef0123456789").is_file()


# ---- files -----------------------------------------------------------------

def test_store_and_read_file_roundtrip(tmp_path: Path):
    store = ChunkStore(tmp_path)
    store.init()
    h = _h(0xEF, length=32)  # sha256-sized hash
    content = b"level.dat contents " * 50
    assert store.has_file(h) is False
    assert store.store_file(h, content) is True
    assert store.has_file(h) is True
    assert store.read_file(h) == content


def test_chunks_and_files_pools_are_independent(tmp_path: Path):
    """Storing as a chunk does NOT make the same hash visible as a file."""
    store = ChunkStore(tmp_path)
    store.init()
    h = _h(0x77)
    store.store_chunk(h, b"chunk content")
    assert store.has_chunk(h) is True
    assert store.has_file(h) is False
    assert store.read_file(h) is None


def test_atomic_write_leaves_no_temp_on_success(tmp_path: Path):
    """After a clean write, no .tmp.* files should remain in the chunk dir."""
    store = ChunkStore(tmp_path)
    store.init()
    h = bytes.fromhex("00112233445566778899aabbccddeeff")
    store.store_chunk(h, b"clean")
    leaf = tmp_path / "chunks" / "00" / "11"
    leftovers = [p.name for p in leaf.iterdir() if ".tmp." in p.name]
    assert leftovers == []


def test_init_is_idempotent(tmp_path: Path):
    store = ChunkStore(tmp_path)
    store.init()
    store.init()
    assert store.chunks_dir.is_dir()
    assert store.files_dir.is_dir()


def test_short_hash_rejected(tmp_path: Path):
    store = ChunkStore(tmp_path)
    store.init()
    with pytest.raises(ValueError, match="too short"):
        store.store_chunk(b"\x00", b"")


def test_distinct_hashes_with_same_2hex_prefix_share_dir(tmp_path: Path):
    """Different chunks with shared prefix bytes coexist in the same leaf dir."""
    store = ChunkStore(tmp_path)
    store.init()
    h1 = bytes.fromhex("abcd0000000000000000000000000001")
    h2 = bytes.fromhex("abcd0000000000000000000000000002")
    store.store_chunk(h1, b"one")
    store.store_chunk(h2, b"two")
    leaf = tmp_path / "chunks" / "ab" / "cd"
    files = sorted(p.name for p in leaf.iterdir())
    assert len(files) == 2
    assert store.read_chunk(h1) == b"one"
    assert store.read_chunk(h2) == b"two"
