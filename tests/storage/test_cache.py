from __future__ import annotations

from pathlib import Path

from chunkvault.storage.cache import ChunkHashCache, ChunkRecord


def _h(b: int) -> bytes:
    return bytes([b]) * 16  # fake 16-byte hash


# ---- blob enumeration -------------------------------------------------------

def test_miss_on_unknown_blob(tmp_path: Path):
    with ChunkHashCache(tmp_path / "cache.sqlite") as c:
        assert c.get_blob_chunks("deadbeef") is None


def test_roundtrip_internal_only(tmp_path: Path):
    chunks = [
        ChunkRecord(cx=0, cz=0, external=False, internal_hash=_h(1)),
        ChunkRecord(cx=5, cz=3, external=False, internal_hash=_h(2)),
    ]
    with ChunkHashCache(tmp_path / "cache.sqlite") as c:
        c.store_blob_chunks("abc", chunks)
        got = c.get_blob_chunks("abc")
    assert got is not None
    assert {(r.cx, r.cz, r.internal_hash) for r in got} == {
        (r.cx, r.cz, r.internal_hash) for r in chunks
    }


def test_roundtrip_with_external(tmp_path: Path):
    """External chunks store no internal_hash but their existence is recorded."""
    chunks = [
        ChunkRecord(cx=0, cz=0, external=False, internal_hash=_h(1)),
        ChunkRecord(cx=3, cz=5, external=True, internal_hash=None),
    ]
    with ChunkHashCache(tmp_path / "cache.sqlite") as c:
        c.store_blob_chunks("blob", chunks)
        got = c.get_blob_chunks("blob")
    assert got is not None
    assert len(got) == 2
    by_coord = {(r.cx, r.cz): r for r in got}
    assert by_coord[(0, 0)].external is False
    assert by_coord[(0, 0)].internal_hash == _h(1)
    assert by_coord[(3, 5)].external is True
    assert by_coord[(3, 5)].internal_hash is None


def test_empty_blob_is_distinct_from_miss(tmp_path: Path):
    """A region blob with zero chunks (header-only) returns [] not None."""
    with ChunkHashCache(tmp_path / "cache.sqlite") as c:
        c.store_blob_chunks("empty", [])
        assert c.get_blob_chunks("empty") == []
        assert c.get_blob_chunks("never-seen") is None


def test_persists_across_instances(tmp_path: Path):
    db = tmp_path / "cache.sqlite"
    with ChunkHashCache(db) as c:
        c.store_blob_chunks("sha1", [
            ChunkRecord(cx=1, cz=1, external=False, internal_hash=_h(9)),
        ])
    with ChunkHashCache(db) as c:
        got = c.get_blob_chunks("sha1")
        assert got is not None and got[0].internal_hash == _h(9)


def test_overwriting_store_updates_values(tmp_path: Path):
    with ChunkHashCache(tmp_path / "cache.sqlite") as c:
        c.store_blob_chunks("sha", [
            ChunkRecord(cx=0, cz=0, external=False, internal_hash=_h(1)),
        ])
        c.store_blob_chunks("sha", [
            ChunkRecord(cx=0, cz=0, external=False, internal_hash=_h(7)),
            ChunkRecord(cx=1, cz=0, external=False, internal_hash=_h(8)),
        ])
        got = c.get_blob_chunks("sha")
    assert got is not None
    by_coord = {(r.cx, r.cz): r.internal_hash for r in got}
    assert by_coord == {(0, 0): _h(7), (1, 0): _h(8)}


# ---- external hash table ----------------------------------------------------

def test_external_hash_roundtrip(tmp_path: Path):
    with ChunkHashCache(tmp_path / "cache.sqlite") as c:
        assert c.get_external_hash("rs", 3, 5, "ms") is None
        c.store_external_hash("rs", 3, 5, "ms", _h(42))
        assert c.get_external_hash("rs", 3, 5, "ms") == _h(42)


def test_external_hash_keyed_by_mcc_sha(tmp_path: Path):
    """Two different mcc SHAs at the same chunk slot map to two distinct hashes."""
    with ChunkHashCache(tmp_path / "cache.sqlite") as c:
        c.store_external_hash("rs", 0, 0, "mcc-a", _h(10))
        c.store_external_hash("rs", 0, 0, "mcc-b", _h(20))
        assert c.get_external_hash("rs", 0, 0, "mcc-a") == _h(10)
        assert c.get_external_hash("rs", 0, 0, "mcc-b") == _h(20)


def test_external_hash_persists(tmp_path: Path):
    db = tmp_path / "cache.sqlite"
    with ChunkHashCache(db) as c:
        c.store_external_hash("rs", 0, 0, "mcc", _h(99))
    with ChunkHashCache(db) as c:
        assert c.get_external_hash("rs", 0, 0, "mcc") == _h(99)


# ---- diagnostics ------------------------------------------------------------

def test_size_counters(tmp_path: Path):
    with ChunkHashCache(tmp_path / "cache.sqlite") as c:
        assert c.size() == (0, 0, 0)
        c.store_blob_chunks("a", [
            ChunkRecord(0, 0, False, _h(1)),
            ChunkRecord(1, 0, True, None),
        ])
        c.store_blob_chunks("b", [
            ChunkRecord(0, 0, False, _h(2)),
        ])
        c.store_external_hash("a", 1, 0, "mcc", _h(3))
        assert c.size() == (2, 3, 1)


# ---- corruption resilience --------------------------------------------------

def test_corrupted_db_is_rebuilt(tmp_path: Path):
    """A corrupt SQLite file must not crash the cache — it should be rebuilt."""
    db = tmp_path / "cache.sqlite"
    db.write_bytes(b"this is not a sqlite database " * 100)
    with ChunkHashCache(db) as c:
        # Should work fresh
        assert c.get_blob_chunks("anything") is None
        c.store_blob_chunks("x", [ChunkRecord(0, 0, False, _h(1))])
        got = c.get_blob_chunks("x")
        assert got is not None and got[0].internal_hash == _h(1)
