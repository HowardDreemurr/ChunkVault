"""Reference counting + fast gc behavior."""
from __future__ import annotations

from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.store.index import IndexDB

from tests._fixtures import ChunkSpec, write_region_file


def _build_world(root: Path, name: str, *, payload: bytes = b"x") -> Path:
    world = root / name
    world.mkdir(parents=True, exist_ok=True)
    (world / "level.dat").write_bytes(b"placeholder")
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, payload),
    ])
    return world


def _count_chunk_blobs(repo: ChunkSnapshotRepo) -> int:
    return sum(1 for p in (repo.repo_path / "chunks").rglob("*") if p.is_file())


def _ref_count(repo: ChunkSnapshotRepo, h: bytes) -> int:
    with IndexDB(repo.index_path) as idx:
        return idx.chunk_ref_count(h)


def _all_chunk_hashes(repo: ChunkSnapshotRepo) -> list[bytes]:
    """Return every chunk hash currently on disk."""
    out = []
    for path in (repo.repo_path / "chunks").rglob("*"):
        if path.is_file() and ".tmp." not in path.name:
            hex_str = path.parent.parent.name + path.parent.name + path.name
            try:
                out.append(bytes.fromhex(hex_str))
            except ValueError:
                pass
    return out


# ---- snapshot increments refs ---------------------------------------------

def test_snapshot_increments_chunk_refs(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path, "w", payload=b"v1")
    repo.snapshot(world, label="a")
    hashes = _all_chunk_hashes(repo)
    for h in hashes:
        assert _ref_count(repo, h) == 1


def test_two_snapshots_with_overlap_share_refs(tmp_path: Path):
    """Snapshots that include the same chunk should both contribute a ref."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path, "w", payload=b"v1")
    repo.snapshot(world, label="a")
    repo.snapshot(world, label="b")  # identical → all chunks shared

    for h in _all_chunk_hashes(repo):
        assert _ref_count(repo, h) == 2


def test_snapshot_with_changed_chunk_increments_only_new_chunks(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path, "w", payload=b"v1")
    repo.snapshot(world, label="a")
    refs_after_a = {h: _ref_count(repo, h) for h in _all_chunk_hashes(repo)}

    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"v2-different"),
    ])
    repo.snapshot(world, label="b")
    refs_after_b = {h: _ref_count(repo, h) for h in _all_chunk_hashes(repo)}

    # The pre-existing chunk's ref stays at 1 (it's only in snapshot a; b uses
    # the new chunk). The new chunk's ref is 1 (only in b).
    assert sum(refs_after_b.values()) == sum(refs_after_a.values()) + 1


# ---- delete decrements ----------------------------------------------------

def test_delete_decrements_refs(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path, "w", payload=b"v1")
    snap_a = repo.snapshot(world, label="a")
    snap_b = repo.snapshot(world, label="b")
    h = _all_chunk_hashes(repo)[0]
    assert _ref_count(repo, h) == 2
    repo.delete(snap_a)
    assert _ref_count(repo, h) == 1
    repo.delete(snap_b)
    assert _ref_count(repo, h) == 0


def test_delete_then_gc_removes_zero_ref_chunks(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path, "w", payload=b"v1")
    snap = repo.snapshot(world, label="solo")
    chunks_before = _count_chunk_blobs(repo)
    assert chunks_before > 0

    repo.delete(snap)
    # Still on disk before gc
    assert _count_chunk_blobs(repo) == chunks_before

    rc, rf = repo.gc()
    assert rc == chunks_before
    assert _count_chunk_blobs(repo) == 0


def test_gc_preserves_chunks_still_referenced_elsewhere(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path, "w", payload=b"shared-content")
    snap_a = repo.snapshot(world, label="keep")
    snap_b = repo.snapshot(world, label="doomed")  # same content → shared chunks
    chunks_total = _count_chunk_blobs(repo)
    assert chunks_total > 0

    repo.delete(snap_b)
    rc, rf = repo.gc()
    # Nothing reclaimed — every chunk is still referenced by snap_a
    assert rc == 0
    assert _count_chunk_blobs(repo) == chunks_total

    # Now deleting snap_a too should let gc reclaim everything
    repo.delete(snap_a)
    rc, rf = repo.gc()
    assert rc == chunks_total
    assert _count_chunk_blobs(repo) == 0


# ---- bootstrap from legacy state ------------------------------------------

def test_bootstrap_runs_when_index_lacks_ref_counts(tmp_path: Path):
    """Simulate a legacy repo: snapshots exist but ref_counts are all 0."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path, "w", payload=b"v1")
    snap = repo.snapshot(world, label="legacy")

    # Reset all ref counts to simulate pre-ref-counting state
    with IndexDB(repo.index_path) as idx:
        with idx._conn:
            idx._conn.execute("UPDATE chunks SET ref_count = 0")
            idx._conn.execute("UPDATE files SET ref_count = 0")
        assert idx.needs_ref_bootstrap() is True

    # Run gc — must NOT delete the legacy snapshot's blobs (bootstrap kicks in)
    chunks_before = _count_chunk_blobs(repo)
    rc, rf = repo.gc()
    chunks_after = _count_chunk_blobs(repo)
    assert chunks_after == chunks_before
    assert rc == 0
    assert rf == 0

    # And refs are now populated
    for h in _all_chunk_hashes(repo):
        assert _ref_count(repo, h) == 1


def test_bootstrap_idempotent(tmp_path: Path):
    """Running gc twice must not double-count refs."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path, "w", payload=b"v1")
    repo.snapshot(world, label="a")
    repo.gc()
    refs_first = {h: _ref_count(repo, h) for h in _all_chunk_hashes(repo)}
    repo.gc()
    refs_second = {h: _ref_count(repo, h) for h in _all_chunk_hashes(repo)}
    assert refs_first == refs_second


# ---- file ref counting ----------------------------------------------------

def test_file_refs_track_independently(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path, "w")
    snap_a = repo.snapshot(world, label="a")
    snap_b = repo.snapshot(world, label="b")
    with IndexDB(repo.index_path) as idx:
        # level.dat has ref_count=2 (referenced by both)
        cur = idx._conn.execute("SELECT content_hash, ref_count FROM files")
        rows = cur.fetchall()
        assert all(rc == 2 for _, rc in rows)


def test_file_gc_after_full_delete(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path, "w")
    snap = repo.snapshot(world, label="x")
    files_before = sum(1 for p in (repo.repo_path / "files").rglob("*")
                       if p.is_file())
    assert files_before > 0
    repo.delete(snap)
    rc, rf = repo.gc()
    assert rf == files_before
    assert sum(1 for p in (repo.repo_path / "files").rglob("*")
               if p.is_file()) == 0
