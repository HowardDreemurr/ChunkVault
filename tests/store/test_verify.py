from __future__ import annotations

from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo

from tests._fixtures import ChunkSpec, write_mcc, write_region_file


def _build_world(root: Path, name: str = "world") -> Path:
    world = root / name
    world.mkdir(parents=True, exist_ok=True)
    (world / "level.dat").write_bytes(b"placeholder")
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"chunk-A"),
        ChunkSpec(1, 0, 1, 2, b"chunk-B"),
    ])
    return world


def _first_chunk_blob(repo: ChunkSnapshotRepo) -> Path:
    for p in (repo.repo_path / "chunks").rglob("*"):
        if p.is_file() and ".tmp." not in p.name:
            return p
    raise AssertionError("no chunk blobs found")


def _first_file_blob(repo: ChunkSnapshotRepo) -> Path:
    for p in (repo.repo_path / "files").rglob("*"):
        if p.is_file() and ".tmp." not in p.name:
            return p
    raise AssertionError("no file blobs found")


# ---- happy path ------------------------------------------------------------

def test_verify_clean_repo_passes(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path)
    repo.snapshot(world, label="v1")

    report = repo.verify()
    assert report.corrupt_chunks == 0
    assert report.corrupt_files == 0
    assert report.missing_referenced == 0
    assert report.missing_manifests == 0
    assert report.orphan_blobs == 0
    assert report.ok_chunks >= 2  # 2 chunks in the fixture
    assert report.ok_files >= 1   # level.dat


def test_verify_returns_zero_for_empty_repo(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    report = repo.verify()
    assert report == report.__class__()  # all zeros


# ---- corruption detection --------------------------------------------------

def test_verify_detects_tampered_chunk(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path)
    repo.snapshot(world, label="v1")

    # Corrupt one chunk by appending bytes
    target = _first_chunk_blob(repo)
    target.write_bytes(target.read_bytes() + b"GARBAGE")

    report = repo.verify()
    assert report.corrupt_chunks == 1


def test_verify_detects_tampered_file(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path)
    repo.snapshot(world, label="v1")

    target = _first_file_blob(repo)
    target.write_bytes(b"completely different")

    report = repo.verify()
    assert report.corrupt_files == 1


def test_verify_repair_removes_corrupt_blobs(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path)
    repo.snapshot(world, label="v1")

    target = _first_chunk_blob(repo)
    target.write_bytes(b"corrupted")
    assert target.exists()

    report = repo.verify(repair=True)
    assert report.repaired == 1
    assert not target.exists()


# ---- missing references ----------------------------------------------------

def test_verify_detects_missing_referenced_chunk(tmp_path: Path):
    """If a manifest references a chunk that's been deleted from disk, verify
    must surface it."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path)
    repo.snapshot(world, label="v1")

    # Delete a chunk blob
    target = _first_chunk_blob(repo)
    target.unlink()

    report = repo.verify()
    assert report.missing_referenced == 1


# ---- orphan detection ------------------------------------------------------

def test_verify_reports_orphan_blobs_after_delete(tmp_path: Path):
    """A snapshot delete leaves the chunk blobs in place (no ref counting in
    base verify), so they show up as orphans until gc."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path)
    snap = repo.snapshot(world, label="doomed")

    blobs_before = sum(1 for p in (repo.repo_path / "chunks").rglob("*")
                       if p.is_file())
    blobs_before += sum(1 for p in (repo.repo_path / "files").rglob("*")
                        if p.is_file())
    repo.delete(snap)
    report = repo.verify()
    assert report.orphan_blobs == blobs_before
    # And those orphans aren't counted as missing-referenced (no manifest
    # references them anymore)
    assert report.missing_referenced == 0


# ---- round-trip integrity --------------------------------------------------

def test_chunk_blob_format_includes_compression_byte(tmp_path: Path):
    """The blob must be (masked_compression || payload), so blake2b of the
    file content equals the path-encoded hash."""
    import hashlib
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _build_world(tmp_path)
    repo.snapshot(world)
    target = _first_chunk_blob(repo)
    expected_hex = (
        target.parent.parent.name + target.parent.name + target.name
    )
    expected = bytes.fromhex(expected_hex)
    actual = hashlib.blake2b(target.read_bytes(), digest_size=16).digest()
    assert actual == expected


def test_external_chunk_blob_format(tmp_path: Path):
    """External chunks store (masked_compression || mcc_payload)."""
    import hashlib
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "ew"
    world.mkdir()
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(cx=3, cz=5, timestamp=1, compression=0x82, payload=b""),
    ])
    write_mcc(world, "region", 3, 5, b"external content " * 20)
    repo.snapshot(world)

    # Find the chunk blob and verify its hash
    target = _first_chunk_blob(repo)
    expected_hex = (
        target.parent.parent.name + target.parent.name + target.name
    )
    expected = bytes.fromhex(expected_hex)
    actual = hashlib.blake2b(target.read_bytes(), digest_size=16).digest()
    assert actual == expected
    # Restore and confirm mcc is preserved
    dest = tmp_path / "rest"
    snap = repo.list()[0]
    repo.restore(snap, dest)
    assert (dest / "region" / "c.3.5.mcc").read_bytes() == b"external content " * 20
