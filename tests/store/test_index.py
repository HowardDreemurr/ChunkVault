from __future__ import annotations

from pathlib import Path

import pytest

from chunkvault.store.index import IndexDB, SnapshotRow


def _row(id="abc", label="v1", world_name="smp", ts=1, **kw) -> SnapshotRow:
    base = dict(
        id=id, label=label, world_name=world_name, timestamp_ms=ts,
        manifest_path=f"manifests/{id}.mcbk",
        mc_version="1.20.4", data_version=3700,
        chunk_count=100, region_count=10, file_count=5,
        new_chunk_count=20, new_file_count=2,
    )
    base.update(kw)
    return SnapshotRow(**base)


# ---- snapshot CRUD ----------------------------------------------------------

def test_add_and_list(tmp_path: Path):
    with IndexDB(tmp_path / "idx.sqlite") as db:
        db.add_snapshot(_row(id="a", ts=10))
        db.add_snapshot(_row(id="b", ts=20))
        db.add_snapshot(_row(id="c", ts=15))
        ids = [s.id for s in db.list_snapshots()]
    # Newest first
    assert ids == ["b", "c", "a"]


def test_get_by_id_and_label(tmp_path: Path):
    with IndexDB(tmp_path / "idx.sqlite") as db:
        db.add_snapshot(_row(id="abc123def", label="pinned"))
        assert db.get_snapshot("abc123def").id == "abc123def"
        assert db.get_snapshot("pinned").id == "abc123def"
        assert db.get_snapshot("abc12").id == "abc123def"  # prefix
        assert db.get_snapshot("ghost") is None


def test_get_by_label_returns_most_recent_when_collision(tmp_path: Path):
    """Two snapshots can share a label; get() returns the newest."""
    with IndexDB(tmp_path / "idx.sqlite") as db:
        db.add_snapshot(_row(id="old", label="dup", ts=1))
        db.add_snapshot(_row(id="new", label="dup", ts=2))
        assert db.get_snapshot("dup").id == "new"


def test_remove_snapshot(tmp_path: Path):
    with IndexDB(tmp_path / "idx.sqlite") as db:
        db.add_snapshot(_row(id="x"))
        assert db.remove_snapshot("x") is True
        assert db.remove_snapshot("x") is False  # already gone
        assert db.get_snapshot("x") is None


def test_latest_for_world(tmp_path: Path):
    with IndexDB(tmp_path / "idx.sqlite") as db:
        db.add_snapshot(_row(id="a", world_name="earth", ts=1))
        db.add_snapshot(_row(id="b", world_name="earth", ts=3))
        db.add_snapshot(_row(id="c", world_name="mars", ts=2))
        assert db.latest_for_world("earth").id == "b"
        assert db.latest_for_world("mars").id == "c"
        assert db.latest_for_world("venus") is None


# ---- hash presence ----------------------------------------------------------

def test_chunk_presence(tmp_path: Path):
    with IndexDB(tmp_path / "idx.sqlite") as db:
        h = b"\xab" * 16
        assert db.has_chunk(h) is False
        db.add_chunks([h])
        assert db.has_chunk(h) is True


def test_bulk_chunk_presence_query(tmp_path: Path):
    with IndexDB(tmp_path / "idx.sqlite") as db:
        present_hashes = [bytes([i]) * 16 for i in range(100)]
        absent_hashes = [bytes([200 + i]) * 16 for i in range(50)]
        db.add_chunks(present_hashes)
        result = db.has_chunks_bulk(present_hashes + absent_hashes)
        assert result == set(present_hashes)


def test_bulk_chunk_presence_handles_large_batch(tmp_path: Path):
    """Crosses SQLite's 999-parameter limit — must paginate internally."""
    with IndexDB(tmp_path / "idx.sqlite") as db:
        hashes = [bytes([i % 256]) * 15 + bytes([i // 256]) for i in range(2000)]
        db.add_chunks(hashes)
        result = db.has_chunks_bulk(hashes)
        assert len(result) == 2000


def test_chunk_dedup_on_add(tmp_path: Path):
    """Adding the same hash twice doesn't error or duplicate."""
    with IndexDB(tmp_path / "idx.sqlite") as db:
        h = b"\xab" * 16
        db.add_chunks([h, h, h])
        assert db.counts()[1] == 1


def test_file_presence_independent_of_chunks(tmp_path: Path):
    with IndexDB(tmp_path / "idx.sqlite") as db:
        h = b"\xcd" * 32
        db.add_chunks([h])
        assert db.has_file(h) is False
        db.add_files([h])
        assert db.has_file(h) is True


# ---- persistence ------------------------------------------------------------

def test_persists_across_open_close(tmp_path: Path):
    db_path = tmp_path / "idx.sqlite"
    with IndexDB(db_path) as db:
        db.add_snapshot(_row(id="x"))
        db.add_chunks([b"\x01" * 16])
        db.add_files([b"\x02" * 32])
    with IndexDB(db_path) as db:
        assert db.get_snapshot("x") is not None
        assert db.has_chunk(b"\x01" * 16)
        assert db.has_file(b"\x02" * 32)


# ---- counts -----------------------------------------------------------------

def test_counts_reflect_state(tmp_path: Path):
    with IndexDB(tmp_path / "idx.sqlite") as db:
        assert db.counts() == (0, 0, 0)
        db.add_snapshot(_row(id="a"))
        db.add_snapshot(_row(id="b"))
        db.add_chunks([b"\x01" * 16, b"\x02" * 16])
        db.add_files([b"\x03" * 32])
        assert db.counts() == (2, 2, 1)
