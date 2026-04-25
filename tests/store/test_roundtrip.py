"""Round-trip verification tests — proves snapshot+restore reproduces source."""
from __future__ import annotations

from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.store.repo import RoundTripVerificationError
from chunkvault.store.roundtrip import RoundTripReport, verify_roundtrip

from tests._fixtures import ChunkSpec, write_mcc, write_region_file


def _world(root: Path, name: str = "world") -> Path:
    w = root / name
    w.mkdir(parents=True, exist_ok=True)
    (w / "level.dat").write_bytes(b"placeholder level.dat")
    write_region_file(w, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"chunk-A"),
        ChunkSpec(1, 0, 1, 2, b"chunk-B"),
        ChunkSpec(2, 2, 1, 2, b"chunk-C"),
    ])
    write_region_file(w, "DIM-1/region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"nether-1"),
    ])
    return w


# ---- happy path -------------------------------------------------------------

def test_snapshot_default_runs_roundtrip(tmp_path: Path):
    """Verify is opt-out: by default, snapshot does the round-trip check."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    snap = repo.snapshot(world, label="auto-verified")
    # If we got here, the implicit verify passed.
    assert snap.label == "auto-verified"


def test_explicit_verify_passes_for_clean_snapshot(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    snap = repo.snapshot(world, label="x", verify_roundtrip=False)
    report = verify_roundtrip(repo, snap, world)
    assert report.passed
    assert report.chunks_checked == 4
    assert report.chunks_matching == 4
    assert report.chunk_mismatches == []
    assert report.file_mismatches == []
    assert report.files_checked >= 1   # level.dat


def test_external_chunks_round_trip_intact(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "ew"
    world.mkdir()
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(cx=3, cz=5, timestamp=1, compression=0x82, payload=b""),
    ])
    write_mcc(world, "region", 3, 5, b"external content " * 50)
    snap = repo.snapshot(world, verify_roundtrip=False)
    report = verify_roundtrip(repo, snap, world)
    assert report.passed
    assert report.chunks_matching == 1


# ---- excluded files don't count as mismatches -------------------------------

def test_default_excluded_files_reported_separately(tmp_path: Path):
    """session.lock / logs/ are excluded from snapshot — verify must report
    them as expected exclusions, not failures."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    (world / "session.lock").write_bytes(b"\xE2\x98\x83")
    (world / "logs").mkdir()
    (world / "logs" / "latest.log").write_bytes(b"some log line\n")

    snap = repo.snapshot(world, verify_roundtrip=False)
    report = verify_roundtrip(repo, snap, world)
    assert report.passed
    assert "session.lock" in report.files_excluded_from_snapshot
    assert "logs/latest.log" in report.files_excluded_from_snapshot


# ---- failure detection ------------------------------------------------------

def test_verify_detects_corrupt_blob_in_pool(tmp_path: Path):
    """If a chunk blob is corrupted between snapshot and verify, the
    restored region won't reproduce the original — verify must catch it."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    snap = repo.snapshot(world, verify_roundtrip=False)
    # Corrupt one chunk on disk
    chunks_dir = repo.repo_path / "chunks"
    target = next(p for p in chunks_dir.rglob("*")
                  if p.is_file() and ".tmp." not in p.name)
    target.write_bytes(target.read_bytes() + b"GARBAGE")

    report = verify_roundtrip(repo, snap, world)
    assert report.passed is False
    # Either the restore raised (chunk-blob bytes unmatch hash → write wrong
    # bytes back into reassembled region → chunk hash on rstored side differs)
    # or it surfaced as a hash_mismatch in the report.
    assert any(m.kind == "hash_mismatch" for m in report.chunk_mismatches) or \
           report.errors


def test_verify_detects_missing_referenced_blob(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    snap = repo.snapshot(world, verify_roundtrip=False)
    # Delete a chunk blob entirely
    chunks_dir = repo.repo_path / "chunks"
    target = next(p for p in chunks_dir.rglob("*")
                  if p.is_file() and ".tmp." not in p.name)
    target.unlink()

    # Restore should raise inside verify_roundtrip (chunk missing in pool).
    with pytest.raises(Exception):
        verify_roundtrip(repo, snap, world)


# ---- failure surfaces from snapshot() -------------------------------------

def test_snapshot_raises_RoundTripVerificationError_on_failure(tmp_path: Path):
    """If verify-after-snapshot detects a divergence, snapshot() must raise
    a structured error carrying both the snapshot and the report. The
    snapshot is intentionally NOT auto-rolled-back."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    # Snapshot once (verify off) so we have a snap to corrupt against.
    initial = repo.snapshot(world, verify_roundtrip=False)
    chunks_dir = repo.repo_path / "chunks"
    # Corrupt a chunk in the pool that the next snapshot will reuse via dedup
    target = next(p for p in chunks_dir.rglob("*")
                  if p.is_file() and ".tmp." not in p.name)
    target.write_bytes(b"corrupted bytes that won't hash correctly")

    # Take a new snapshot of the same world — verify should fail because
    # restoring the new snapshot uses the (now-corrupted) blob.
    with pytest.raises(RoundTripVerificationError) as excinfo:
        repo.snapshot(world, label="will-fail", verify_roundtrip=True)
    err = excinfo.value
    assert err.snapshot.label == "will-fail"
    # And the snapshot row is still in the index — user can investigate
    assert repo.get("will-fail") is not None


# ---- opt-out works ----------------------------------------------------------

def test_no_verify_skips_check(tmp_path: Path):
    """With verify_roundtrip=False, snapshot succeeds even if the pool is
    pre-corrupted — caller is opting OUT of the safety check."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    repo.snapshot(world, label="first", verify_roundtrip=False)
    # Corrupt the pool
    chunks_dir = repo.repo_path / "chunks"
    target = next(p for p in chunks_dir.rglob("*")
                  if p.is_file() and ".tmp." not in p.name)
    target.write_bytes(b"corrupted")
    # With verify off, snapshot completes without raising
    snap2 = repo.snapshot(world, label="second", verify_roundtrip=False)
    assert snap2.label == "second"


# ---- the report itself ------------------------------------------------------

def test_report_summary_structure():
    r = RoundTripReport()
    r.chunks_checked = 100
    r.chunks_matching = 100
    r.files_checked = 10
    r.files_matching = 10
    assert "PASS" in r.summary()
    r.chunk_mismatches.append(
        __import__("chunkvault.store.roundtrip", fromlist=["ChunkMismatch"])
        .ChunkMismatch(dimension_key="region", rx=0, rz=0, cx=0, cz=0,
                       kind="hash_mismatch")
    )
    assert "FAIL" in r.summary()
