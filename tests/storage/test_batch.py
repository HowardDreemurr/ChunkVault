from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chunkvault.storage.batch import BatchCatFile
from chunkvault.storage.repo import SnapshotRepo, git_available

from tests._fixtures import ChunkSpec, write_region_file

pytestmark = pytest.mark.skipif(not git_available(), reason="git CLI not on PATH")


def _world(tmp_path: Path, name: str = "world") -> Path:
    world = tmp_path / name
    world.mkdir()
    (world / "level.dat").write_bytes(b"fake level.dat")
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"A"),
    ])
    return world


def test_batch_fetch_returns_blob_bytes(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _world(tmp_path)
    (world / "hello.txt").write_bytes(b"hello batch")
    snap = repo.snapshot(world)
    with BatchCatFile(repo.repo_path) as batch:
        blob = batch.fetch(f"{snap.id}:hello.txt")
    assert blob == b"hello batch"


def test_batch_fetch_missing_returns_none(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _world(tmp_path)
    snap = repo.snapshot(world)
    with BatchCatFile(repo.repo_path) as batch:
        blob = batch.fetch(f"{snap.id}:does/not/exist.mca")
    assert blob is None


def test_batch_many_requests_in_one_subprocess(tmp_path: Path):
    """The value proposition: N fetches share one git process."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _world(tmp_path)
    # Seed many small files
    for i in range(50):
        (world / f"f{i}.txt").write_bytes(f"contents {i}".encode())
    snap = repo.snapshot(world)

    with BatchCatFile(repo.repo_path) as batch:
        blobs = [batch.fetch(f"{snap.id}:f{i}.txt") for i in range(50)]
    for i, b in enumerate(blobs):
        assert b == f"contents {i}".encode()


def test_batch_matches_standalone_cat_file(tmp_path: Path):
    """Equivalence with a fresh `git cat-file blob` — same bytes."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _world(tmp_path)
    (world / "payload.bin").write_bytes(bytes(range(256)) * 3)  # 768 bytes
    snap = repo.snapshot(world)

    # Standalone reference
    import os
    env = os.environ.copy()
    env["GIT_DIR"] = str(repo.repo_path)
    ref = subprocess.run(
        ["git", "cat-file", "blob", f"{snap.id}:payload.bin"],
        capture_output=True, env=env,
    ).stdout

    with BatchCatFile(repo.repo_path) as batch:
        via_batch = batch.fetch(f"{snap.id}:payload.bin")

    assert via_batch == ref
    assert len(via_batch) == 768


def test_batch_exits_cleanly_on_context_leave(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _world(tmp_path)
    snap = repo.snapshot(world)
    batch = BatchCatFile(repo.repo_path)
    with batch:
        batch.fetch(f"{snap.id}:level.dat")
    # After exit, the subprocess should be gone
    assert batch._proc is None


def test_batch_fetch_before_enter_raises(tmp_path: Path):
    batch = BatchCatFile(tmp_path)
    with pytest.raises(RuntimeError):
        batch.fetch("HEAD:any")
