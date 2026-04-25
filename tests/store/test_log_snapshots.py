"""End-to-end tests for the log-snapshot subsystem on ChunkSnapshotRepo.

The log subsystem is intentionally separate from the world snapshot path —
we verify here that:
* add_log_snapshot is independent (doesn't touch chunks/files pools)
* dedup works across log snapshots (same content stored once)
* extract round-trips bytes
* delete + gc reclaims orphaned log blobs
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chunkvault.store import ChunkRepoError, ChunkSnapshotRepo
from chunkvault.store.index import IndexDB


def _setup(tmp_path: Path) -> ChunkSnapshotRepo:
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    return repo


def _count_logs(repo: ChunkSnapshotRepo) -> int:
    return sum(1 for p in (repo.repo_path / "logs").rglob("*") if p.is_file())


def _ref_count(repo: ChunkSnapshotRepo, sha: bytes) -> int:
    with IndexDB(repo.index_path) as idx:
        return idx.log_ref_count(sha)


# ---- basic add / list / get ------------------------------------------------

def test_add_log_snapshot_creates_files(tmp_path: Path):
    repo = _setup(tmp_path)
    snap = repo.add_log_snapshot({
        "EX-Server": [
            ("logs/server.log", b"server log line\n"),
            ("logs/2024.log.gz", b"\x1f\x8bgz-stub"),
        ],
        "CR-Server": [
            ("logs/server.log", b"different content"),
        ],
    }, label="2025-04-25-12-34-56")

    assert snap.server_count == 2
    assert snap.file_count == 3
    assert snap.label == "2025-04-25-12-34-56"
    assert snap.manifest_path.is_file()
    assert _count_logs(repo) == 3  # all three contents are unique


def test_add_log_snapshot_dedupes_identical_content(tmp_path: Path):
    repo = _setup(tmp_path)
    repo.add_log_snapshot({
        "EX-Server": [
            ("logs/server.log", b"shared log content"),
        ],
        "CR-Server": [
            ("logs/server.log", b"shared log content"),  # same bytes
        ],
    }, label="dedup-test")
    # Only one unique content → one blob on disk
    assert _count_logs(repo) == 1


def test_log_snapshots_dedupe_across_ingests(tmp_path: Path):
    repo = _setup(tmp_path)
    repo.add_log_snapshot({
        "EX-Server": [("logs/server.log", b"a stable log file")],
    }, label="day-1")
    blobs_after_1 = _count_logs(repo)
    repo.add_log_snapshot({
        "EX-Server": [("logs/server.log", b"a stable log file")],
    }, label="day-2")
    blobs_after_2 = _count_logs(repo)
    assert blobs_after_1 == blobs_after_2  # same content → no new blob
    # But ref count went up
    sha = hashlib.sha256(b"a stable log file").digest()
    assert _ref_count(repo, sha) == 2


def test_list_log_snapshots_orders_newest_first(tmp_path: Path):
    repo = _setup(tmp_path)
    t1 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2025, 1, 2, tzinfo=timezone.utc)
    t3 = datetime(2025, 1, 3, tzinfo=timezone.utc)
    repo.add_log_snapshot({"S": [("a.log", b"a")]}, label="one", timestamp=t1)
    repo.add_log_snapshot({"S": [("a.log", b"a")]}, label="two", timestamp=t2)
    repo.add_log_snapshot({"S": [("a.log", b"a")]}, label="three", timestamp=t3)
    labels = [s.label for s in repo.list_log_snapshots()]
    assert labels == ["three", "two", "one"]


def test_get_log_snapshot_by_label_or_id(tmp_path: Path):
    repo = _setup(tmp_path)
    snap = repo.add_log_snapshot(
        {"S": [("x", b"x")]}, label="findable",
    )
    assert repo.get_log_snapshot("findable").id == snap.id
    assert repo.get_log_snapshot(snap.id).id == snap.id
    assert repo.get_log_snapshot(snap.id[:8]).id == snap.id
    assert repo.get_log_snapshot("ghost") is None


# ---- extract --------------------------------------------------------------

def test_extract_logs_writes_flat_tree(tmp_path: Path):
    repo = _setup(tmp_path)
    snap = repo.add_log_snapshot({
        "EX-Server": [
            ("logs/server.log", b"ex-content"),
            ("crash-reports/c.txt", b"crash-content"),
        ],
        "CR-Server": [
            ("logs/server.log", b"cr-content"),
        ],
    }, label="extract-me")

    dest = tmp_path / "extracted"
    written = repo.extract_logs(snap, dest)
    assert written == 3
    assert (dest / "EX-Server" / "logs" / "server.log").read_bytes() == b"ex-content"
    assert (dest / "EX-Server" / "crash-reports" / "c.txt").read_bytes() == b"crash-content"
    assert (dest / "CR-Server" / "logs" / "server.log").read_bytes() == b"cr-content"


def test_extract_logs_with_server_filter(tmp_path: Path):
    repo = _setup(tmp_path)
    snap = repo.add_log_snapshot({
        "EX-Server": [("a.log", b"ex")],
        "CR-Server": [("a.log", b"cr")],
    }, label="filter-me")
    dest = tmp_path / "only-ex"
    written = repo.extract_logs(snap, dest, server="EX-Server")
    assert written == 1
    assert (dest / "EX-Server" / "a.log").is_file()
    assert not (dest / "CR-Server").exists()


def test_extract_unknown_log_snapshot_errors(tmp_path: Path):
    repo = _setup(tmp_path)
    with pytest.raises(ChunkRepoError, match="No such log snapshot"):
        repo.extract_logs("ghost", tmp_path / "x")


# ---- delete + gc ----------------------------------------------------------

def test_delete_log_snapshot_decrements_refs(tmp_path: Path):
    repo = _setup(tmp_path)
    snap_a = repo.add_log_snapshot({"S": [("a.log", b"shared")]}, label="a")
    snap_b = repo.add_log_snapshot({"S": [("a.log", b"shared")]}, label="b")
    sha = hashlib.sha256(b"shared").digest()
    assert _ref_count(repo, sha) == 2

    repo.delete_log_snapshot(snap_a)
    assert _ref_count(repo, sha) == 1
    repo.delete_log_snapshot(snap_b)
    assert _ref_count(repo, sha) == 0


def test_gc_reclaims_zero_ref_log_blobs(tmp_path: Path):
    repo = _setup(tmp_path)
    snap = repo.add_log_snapshot({"S": [("a.log", b"will-be-orphaned")]},
                                 label="doomed")
    blobs_before = _count_logs(repo)
    assert blobs_before > 0

    repo.delete_log_snapshot(snap)
    # Still on disk before gc
    assert _count_logs(repo) == blobs_before

    result = repo.gc()
    assert result.logs == blobs_before
    assert _count_logs(repo) == 0


def test_gc_keeps_logs_still_referenced(tmp_path: Path):
    repo = _setup(tmp_path)
    snap_a = repo.add_log_snapshot({"S": [("a.log", b"shared")]}, label="keep")
    snap_b = repo.add_log_snapshot({"S": [("a.log", b"shared")]}, label="doomed")
    blobs_before = _count_logs(repo)

    repo.delete_log_snapshot(snap_b)
    result = repo.gc()
    assert result.logs == 0  # still referenced by snap_a
    assert _count_logs(repo) == blobs_before


# ---- world & log subsystems are isolated ----------------------------------

def test_world_and_log_snapshots_are_independent(tmp_path: Path):
    """Adding log snapshots must not touch the chunk / world-files pools,
    and vice versa."""
    from tests._fixtures import ChunkSpec, write_region_file
    repo = _setup(tmp_path)

    # World snapshot — touches chunks/ and files/
    world = tmp_path / "w"
    world.mkdir()
    (world / "level.dat").write_bytes(b"placeholder")
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"chunk"),
    ])
    repo.snapshot(world, label="world-only")
    chunk_blobs = sum(1 for p in (repo.repo_path / "chunks").rglob("*") if p.is_file())
    file_blobs = sum(1 for p in (repo.repo_path / "files").rglob("*") if p.is_file())
    log_blobs_before = _count_logs(repo)

    # Log snapshot — should NOT change chunk/file counts
    repo.add_log_snapshot({"S": [("a.log", b"log content")]}, label="log-only")
    chunk_blobs_2 = sum(1 for p in (repo.repo_path / "chunks").rglob("*") if p.is_file())
    file_blobs_2 = sum(1 for p in (repo.repo_path / "files").rglob("*") if p.is_file())
    log_blobs_after = _count_logs(repo)

    assert chunk_blobs_2 == chunk_blobs
    assert file_blobs_2 == file_blobs
    assert log_blobs_after == log_blobs_before + 1
