"""Robustness: atomic writes, fsck recovery, Ctrl-C tolerance."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo, FsckReport
from chunkvault.store.index import IndexDB

from tests._fixtures import ChunkSpec, write_region_file


def _world(root: Path, name: str = "world") -> Path:
    w = root / name
    w.mkdir(parents=True, exist_ok=True)
    (w / "level.dat").write_bytes(b"placeholder")
    write_region_file(w, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"chunk-A"),
        ChunkSpec(1, 0, 1, 2, b"chunk-B"),
    ])
    return w


# ---- atomic manifest write -------------------------------------------------

def test_write_manifest_is_atomic_no_partial_files(tmp_path: Path):
    """Half-written manifest must never appear at the destination path —
    only the .tmp.<pid> sibling exists during write."""
    from chunkvault.store.manifest import (
        write_manifest, Manifest, ManifestHeader,
    )
    m = Manifest(header=ManifestHeader(
        timestamp_ms=0, label="x", world_name="w",
    ))
    out = tmp_path / "snap.mcbk"
    write_manifest(out, m)
    assert out.is_file()
    # No .tmp.* files left
    leftovers = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == []


def test_write_log_manifest_is_atomic(tmp_path: Path):
    from chunkvault.store.log_manifest import (
        write_log_manifest, LogManifest,
    )
    m = LogManifest(id="x", label=None, timestamp_ms=0, source_path=None)
    out = tmp_path / "x.json"
    write_log_manifest(out, m)
    assert out.is_file()
    leftovers = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == []


# ---- fsck on a clean repo --------------------------------------------------

def test_fsck_clean_repo_reports_clean(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    repo.snapshot(world, label="v1", verify_roundtrip=False)
    report = repo.fsck()
    assert report.clean
    assert report.total_issues == 0


# ---- fsck on orphan manifest (snapshot Ctrl-C'd before index commit) ------

def test_fsck_detects_and_removes_orphan_manifest(tmp_path: Path):
    """Simulate: snapshot wrote the manifest then died before adding the
    snapshot row. fsck should delete the orphan manifest."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    snap = repo.snapshot(world, label="real", verify_roundtrip=False)

    # Drop a fake orphan manifest into the dir
    fake_id = uuid.uuid4().hex
    (repo.repo_path / "manifests" / f"{fake_id}.mcbk").write_bytes(b"junk-bytes")

    report = repo.fsck()
    assert not report.clean
    assert any(fake_id in path for path in report.orphan_manifests)
    # The fake one is deleted
    assert not (repo.repo_path / "manifests" / f"{fake_id}.mcbk").is_file()
    # The real snapshot survives
    assert repo.get("real") is not None


def test_fsck_dry_run_does_not_modify(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    repo.snapshot(world, label="real", verify_roundtrip=False)
    fake_id = uuid.uuid4().hex
    fake_path = repo.repo_path / "manifests" / f"{fake_id}.mcbk"
    fake_path.write_bytes(b"junk")

    report = repo.fsck(repair=False)
    assert not report.clean
    # Dry run: file still there
    assert fake_path.is_file()


# ---- fsck on dangling row (manifest deleted out of band) -------------------

def test_fsck_repairs_dangling_row(tmp_path: Path):
    """Simulate: someone rm'd a manifest file. fsck should drop the row
    and reverse its refs."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    snap = repo.snapshot(world, label="doomed", verify_roundtrip=False)
    # Sanity: refs are populated
    with IndexDB(repo.index_path) as idx:
        before = idx.counts()[1]
    assert before > 0

    # Delete the manifest behind the index's back
    snap.manifest_path.unlink()

    report = repo.fsck()
    assert not report.clean
    assert snap.id in report.dangling_rows
    # The row is gone
    assert repo.get("doomed") is None


# ---- fsck cleans up stray temp files ---------------------------------------

def test_fsck_removes_stray_tmp_files(tmp_path: Path):
    """Atomic writes leave .tmp.<pid> files behind on crash. fsck removes them."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    repo.snapshot(world, label="x", verify_roundtrip=False)

    # Drop fake .tmp files in each pool dir
    for pool in (repo.chunks.chunks_dir, repo.chunks.files_dir,
                 repo.logs.logs_dir, repo.manifests_dir,
                 repo.log_manifests_dir):
        if not pool.is_dir():
            continue
        # Walk into one of its leaf dirs (or root if empty)
        leaf = pool
        for child in pool.rglob("*"):
            if child.is_dir():
                leaf = child
                break
        (leaf / "garbage.tmp.99999").write_bytes(b"x")

    report = repo.fsck()
    assert not report.clean
    assert len(report.stray_temp_files) >= 1
    # All cleared
    for pool in (repo.chunks.chunks_dir, repo.chunks.files_dir,
                 repo.logs.logs_dir, repo.manifests_dir,
                 repo.log_manifests_dir):
        leftovers = [p for p in pool.rglob("*") if ".tmp." in p.name]
        assert leftovers == []


# ---- CLI Ctrl-C handling (exit code 130, no traceback) ---------------------

def test_cli_keyboardinterrupt_exits_cleanly(tmp_path: Path, monkeypatch, capsys):
    """SIGINT during a chunkvault command should exit 130 with a friendly
    message, not dump a Python traceback."""
    from chunkvault.cli import main

    def boom(args):
        raise KeyboardInterrupt()

    # Patch a real subcommand to raise KeyboardInterrupt
    monkeypatch.setattr("chunkvault.cli.cmd_init", boom)
    rc = main(["init", str(tmp_path / "repo")])
    captured = capsys.readouterr()
    assert rc == 130
    # Friendly message went to stderr
    assert "interrupted" in captured.err.lower()
    # And no traceback was dumped to stderr
    assert "Traceback" not in captured.err


def test_cli_brokenpipeerror_exits_silently(monkeypatch, capsys):
    """Piping into `head` etc. closes stdout early — should not error."""
    from chunkvault.cli import main

    def boom(args):
        raise BrokenPipeError()

    monkeypatch.setattr("chunkvault.cli.cmd_init", boom)
    rc = main(["init", "x"])
    assert rc == 0


# ---- snapshot UX defense: reject non-world directories ---------------------

def test_snapshot_refuses_non_world_directory(tmp_path: Path):
    """Pointing snapshot() at a directory that isn't a MC world (no
    level.dat, no region/) must error early — not silently store every
    unrelated file in the directory."""
    from chunkvault.store import ChunkRepoError
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    # A folder full of zip files, NOT a MC world
    fake_world = tmp_path / "archives"
    fake_world.mkdir()
    (fake_world / "backup1.zip").write_bytes(b"x")
    (fake_world / "backup2.zip").write_bytes(b"y")
    with pytest.raises(ChunkRepoError, match="doesn't look like a Minecraft world"):
        repo.snapshot(fake_world, label="oops")


def test_snapshot_accepts_world_with_just_level_dat(tmp_path: Path):
    """level.dat alone should be enough to count as a world (e.g. a brand-new
    server that hasn't generated any region files yet)."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "fresh-world"
    world.mkdir()
    (world / "level.dat").write_bytes(b"placeholder")
    snap = repo.snapshot(world, label="fresh", verify_roundtrip=False)
    assert snap.label == "fresh"


def test_snapshot_accepts_world_with_just_region_dir(tmp_path: Path):
    """region/ dir alone should be enough (some servers strip level.dat)."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "regions-only"
    world.mkdir()
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"x"),
    ])
    snap = repo.snapshot(world, label="ro", verify_roundtrip=False)
    assert snap.label == "ro"


# ---- snapshot order: manifest before index commit --------------------------

def test_snapshot_writes_manifest_before_committing_index(tmp_path: Path,
                                                          monkeypatch):
    """Verify the new ordering: manifest must exist on disk before the
    snapshot row appears in the index. This way fsck can detect a partial
    snapshot (manifest exists, no row) versus a full snapshot."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)

    # Patch IndexDB.add_snapshot to crash, simulating Ctrl-C right at the
    # commit point. We expect: manifest written, refs incremented, but no
    # snapshot row.
    real_add = IndexDB.add_snapshot

    def crashing_add(self, row):
        raise KeyboardInterrupt("simulated")
    monkeypatch.setattr(IndexDB, "add_snapshot", crashing_add)

    with pytest.raises(KeyboardInterrupt):
        repo.snapshot(world, label="never-committed", verify_roundtrip=False)

    # Restore normal behavior
    monkeypatch.setattr(IndexDB, "add_snapshot", real_add)

    # The manifest dir should have an orphan
    manifests = list((repo.repo_path / "manifests").glob("*.mcbk"))
    assert len(manifests) >= 1, "expected at least one orphan manifest"
    # The snapshot doesn't show up in list (no row was committed)
    assert repo.get("never-committed") is None

    # fsck cleans it up
    report = repo.fsck()
    assert len(report.orphan_manifests) >= 1
