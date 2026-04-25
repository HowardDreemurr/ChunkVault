from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.wizard.detect import (
    detect_environment,
    default_paths_to_scan,
    scan_for_archives,
    summarize_repo,
)


# ---- repo detection -------------------------------------------------------

def test_summarize_chunk_repo(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    summary = summarize_repo(repo.repo_path)
    assert summary is not None
    assert summary.kind == "chunk"
    assert summary.snapshot_count == 0


def test_summarize_unknown_directory(tmp_path: Path):
    (tmp_path / "random").mkdir()
    (tmp_path / "random" / "file.txt").write_bytes(b"x")
    assert summarize_repo(tmp_path / "random") is None


def test_summarize_missing_path(tmp_path: Path):
    assert summarize_repo(tmp_path / "does-not-exist") is None


def test_summarize_includes_snapshot_count(tmp_path: Path):
    from tests._fixtures import ChunkSpec, write_region_file
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "w"
    world.mkdir()
    (world / "level.dat").write_bytes(b"x")
    write_region_file(world, "region", 0, 0, [ChunkSpec(0, 0, 1, 2, b"x")])
    repo.snapshot(world, label="x")
    summary = summarize_repo(repo.repo_path)
    assert summary.snapshot_count == 1
    assert summary.chunk_blob_count >= 1


def test_summarize_git_repo(tmp_path: Path):
    """SnapshotRepo (git store) should be detected as kind='git'."""
    from chunkvault.storage import SnapshotRepo
    from chunkvault.storage.repo import git_available
    if not git_available():
        pytest.skip("git not on PATH")
    repo = SnapshotRepo(tmp_path / "git-repo")
    repo.init()
    summary = summarize_repo(repo.repo_path)
    assert summary is not None
    assert summary.kind == "git"


# ---- archive scanning -----------------------------------------------------

def _make_zip(p: Path, name: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(f"{name}.txt", "hello")
    return p


def test_scan_for_archives_finds_zips(tmp_path: Path):
    _make_zip(tmp_path / "a.zip")
    _make_zip(tmp_path / "sub" / "b.tar.gz")  # also matches by extension
    # Actually create a real tar.gz
    import tarfile
    with tarfile.open(tmp_path / "sub" / "b.tar.gz", "w:gz") as tf:
        sample = tmp_path / "sample.txt"
        sample.write_text("x")
        tf.add(sample, arcname="x.txt")

    summary = scan_for_archives(tmp_path)
    assert summary is not None
    assert summary.archive_count == 2
    assert "a.zip" in summary.samples


def test_scan_for_archives_returns_none_when_no_archives(tmp_path: Path):
    (tmp_path / "readme.txt").write_text("not an archive")
    assert scan_for_archives(tmp_path) is None


def test_scan_for_archives_handles_missing_dir(tmp_path: Path):
    assert scan_for_archives(tmp_path / "ghost") is None


def test_scan_for_archives_samples_capped(tmp_path: Path):
    for i in range(20):
        _make_zip(tmp_path / f"a{i:02d}.zip")
    summary = scan_for_archives(tmp_path, max_samples=3)
    assert summary.archive_count == 20
    assert len(summary.samples) == 3


# ---- default paths --------------------------------------------------------

def test_default_paths_includes_cwd():
    paths = default_paths_to_scan()
    assert any(p == Path.cwd().resolve() for p in paths)


def test_default_paths_deduplicated():
    paths = default_paths_to_scan()
    resolved = [p.resolve() for p in paths]
    assert len(resolved) == len(set(resolved))


# ---- combined environment -------------------------------------------------

def test_detect_environment_with_explicit_paths(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    _make_zip(archive_dir / "snap.zip")

    env = detect_environment(
        repo_candidates=[repo.repo_path],
        source_candidates=[archive_dir],
    )
    assert len(env.repos) == 1
    assert env.repos[0].kind == "chunk"
    assert len(env.source_paths) == 1
    assert env.source_paths[0].archive_count == 1


def test_detect_environment_skips_invalid(tmp_path: Path):
    env = detect_environment(
        repo_candidates=[tmp_path / "nope"],
        source_candidates=[tmp_path / "also-nope"],
    )
    assert env.repos == []
    assert env.source_paths == []


def test_detect_environment_dedupes_repos(tmp_path: Path):
    """Same repo path passed twice → reported once."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    env = detect_environment(
        repo_candidates=[repo.repo_path, repo.repo_path],
        source_candidates=[],
    )
    assert len(env.repos) == 1
