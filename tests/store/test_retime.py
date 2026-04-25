"""Tests for ChunkSnapshotRepo.retime_snapshot + the CLI ``retime`` command.

Retime reassigns a snapshot's timeline position without re-ingesting. It
must:
  - update both the manifest header and the SQLite index row atomically
  - preserve the *first* timestamp the snapshot ever had via
    ``original_timestamp_ms`` (audit trail)
  - refuse collisions: two snapshots with the same label can't share a ts
  - leave chunks/files untouched (content-addressed; not affected by ts)
  - write a backup of the index before any mutation
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.store.repo import ChunkRepoError
from chunkvault.store.manifest import read_manifest

from tests._fixtures import ChunkSpec, write_region_file
from tests.store.test_repo import _make_level_dat


def _seed_world(tmp_path: Path, name: str = "world", *,
                last_played_ms: int | None = None) -> Path:
    world = tmp_path / name
    world.mkdir(parents=True)
    (world / "level.dat").write_bytes(
        _make_level_dat("1.20.4", 3700, last_played_ms=last_played_ms),
    )
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"x"),
    ])
    return world


# ---- core retime behavior --------------------------------------------------

def test_retime_changes_timestamp_in_both_manifest_and_index(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world(tmp_path, last_played_ms=1_700_000_000_000)
    snap = repo.snapshot(world, label="orig")

    new_ts = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    updated = repo.retime_snapshot(snap, new_ts)

    assert updated.timestamp == new_ts
    # Manifest reflects the new timestamp
    manifest = read_manifest(updated.manifest_path)
    assert manifest.header.timestamp_ms == int(new_ts.timestamp() * 1000)
    # And the index list confirms it
    snaps = repo.list()
    assert snaps[0].timestamp == new_ts


def test_retime_seeds_original_timestamp_on_first_call(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world(tmp_path, last_played_ms=1_000_000)
    snap = repo.snapshot(world, label="orig")
    pre_ts_ms = int(snap.timestamp.timestamp() * 1000)

    repo.retime_snapshot(snap, datetime(2024, 6, 15, tzinfo=timezone.utc))
    manifest = read_manifest(snap.manifest_path)
    assert manifest.header.original_timestamp_ms == pre_ts_ms


def test_retime_preserves_first_original_across_subsequent_retimes(tmp_path: Path):
    """Audit trail must always answer 'where was this snapshot born', not
    'where was it last'."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world(tmp_path, last_played_ms=1_000_000)
    snap = repo.snapshot(world, label="orig")
    first_ts_ms = int(snap.timestamp.timestamp() * 1000)

    repo.retime_snapshot(snap, datetime(2024, 1, 1, tzinfo=timezone.utc))
    repo.retime_snapshot(snap, datetime(2024, 6, 1, tzinfo=timezone.utc))
    repo.retime_snapshot(snap, datetime(2025, 1, 1, tzinfo=timezone.utc))

    manifest = read_manifest(snap.manifest_path)
    assert manifest.header.original_timestamp_ms == first_ts_ms

    # Same in the index
    with sqlite3.connect(repo.index_path) as conn:
        row = conn.execute(
            "SELECT original_timestamp_ms FROM snapshots WHERE id = ?",
            (snap.id,),
        ).fetchone()
    assert row[0] == first_ts_ms


def test_retime_to_same_timestamp_is_noop(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world(tmp_path, last_played_ms=1_000_000)
    snap = repo.snapshot(world, label="orig")

    mtime_before = snap.manifest_path.stat().st_mtime
    repo.retime_snapshot(snap, snap.timestamp)
    mtime_after = snap.manifest_path.stat().st_mtime
    # Manifest file should not have been rewritten
    assert mtime_after == mtime_before


# ---- collision protection --------------------------------------------------

def test_retime_refuses_label_timestamp_collision(tmp_path: Path):
    """Two snapshots with the same label can't share a (label, timestamp)."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()

    # Two snapshots with the SAME label but different timestamps.
    world1 = _seed_world(tmp_path / "a", last_played_ms=1_000_000)
    snap_a = repo.snapshot(
        world1, label="shared",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    world2 = _seed_world(tmp_path / "b", last_played_ms=2_000_000)
    snap_b = repo.snapshot(
        world2, label="shared",
        timestamp=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )

    # Trying to retime snap_b onto snap_a's exact (label, ts) must fail.
    with pytest.raises(ChunkRepoError, match="refusing retime"):
        repo.retime_snapshot(snap_b, snap_a.timestamp)

    # Both snapshots remain at their original timestamps
    assert {s.timestamp for s in repo.list()} == {snap_a.timestamp, snap_b.timestamp}


# ---- from-manifest convenience --------------------------------------------

def test_retime_from_manifest_uses_last_played(tmp_path: Path):
    """The shortcut method retimes to whatever LastPlayed the manifest carries."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    last_played_ms = 1_700_000_000_000
    world = _seed_world(tmp_path, last_played_ms=last_played_ms)
    # Snapshot with EXPLICIT timestamp so the snapshot ts ≠ LastPlayed.
    snap = repo.snapshot(
        world, label="bad-ts",
        timestamp=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )

    updated, source = repo.retime_snapshot_from_manifest(snap)
    assert source == "last_played"
    assert int(updated.timestamp.timestamp() * 1000) == last_played_ms


def test_retime_from_manifest_skips_when_no_last_played(tmp_path: Path):
    """Manifests without a usable LastPlayed are reported, not raised."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world(tmp_path, last_played_ms=None)
    snap = repo.snapshot(
        world, label="no-lp",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    updated, source = repo.retime_snapshot_from_manifest(snap)
    assert source == "no_last_played"
    # Snap is returned unchanged
    assert updated.timestamp == snap.timestamp


# ---- safety: blobs untouched, index backed up -----------------------------

def test_retime_does_not_touch_chunk_pool(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world(tmp_path, last_played_ms=1_000_000)
    snap = repo.snapshot(world, label="x")

    chunk_blobs_before = sorted(p.name for p in (repo.repo_path / "chunks").rglob("*")
                                 if p.is_file())
    repo.retime_snapshot(snap, datetime(2024, 6, 1, tzinfo=timezone.utc))
    chunk_blobs_after = sorted(p.name for p in (repo.repo_path / "chunks").rglob("*")
                                if p.is_file())
    assert chunk_blobs_before == chunk_blobs_after


def test_index_auto_migrates_pre_retime_schema(tmp_path: Path):
    """Opening a v1 DB (no original_timestamp_ms column) must auto-add it."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    db_path = repo_path / "index.sqlite"
    # Hand-build the OLD schema (no original_timestamp_ms)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE snapshots (
                id              TEXT PRIMARY KEY,
                label           TEXT,
                world_name      TEXT NOT NULL,
                timestamp_ms    INTEGER NOT NULL,
                manifest_path   TEXT NOT NULL,
                mc_version      TEXT,
                data_version    INTEGER,
                chunk_count     INTEGER NOT NULL DEFAULT 0,
                region_count    INTEGER NOT NULL DEFAULT 0,
                file_count      INTEGER NOT NULL DEFAULT 0,
                new_chunk_count INTEGER NOT NULL DEFAULT 0,
                new_file_count  INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(
            "INSERT INTO snapshots (id, label, world_name, timestamp_ms, "
            "manifest_path) VALUES (?,?,?,?,?)",
            ("abc123", "old", "w", 1000, "manifests/abc123.mcbk"),
        )

    # Now open via IndexDB — migration should add the column
    from chunkvault.store.index import IndexDB
    with IndexDB(db_path) as idx:
        rows = idx.list_snapshots()
    assert len(rows) == 1
    assert rows[0].id == "abc123"
    assert rows[0].original_timestamp_ms == 0  # default for migrated rows


def test_backup_index_creates_bak(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    bak = repo.backup_index()
    assert bak.is_file()
    assert bak.name == "index.sqlite.bak"
    # Rough sanity: bak file size matches the live index at backup time
    assert bak.stat().st_size > 0


def test_backup_index_overwrites_existing_bak(tmp_path: Path):
    """We keep ONE rollback point — a fresh backup overwrites the old one."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    repo.backup_index()
    world = _seed_world(tmp_path, last_played_ms=1_000_000)
    repo.snapshot(world, label="grow")
    bak2 = repo.backup_index()
    # The .bak should now reflect the larger live index (1+ snapshot row)
    assert bak2.stat().st_size >= repo.index_path.stat().st_size - 4096


# ---- CLI smoke: dry-run + real retime --------------------------------------

def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "chunkvault", *args],
        capture_output=True, text=True,
    )


def test_cli_retime_dry_run_changes_nothing(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world(tmp_path, last_played_ms=1_000_000)
    snap = repo.snapshot(
        world, label="dry",
        timestamp=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    old_ts = snap.timestamp

    proc = _run_cli("retime", str(repo.repo_path), snap.id,
                    "--from-level-dat", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "would retime" in proc.stdout

    # Re-read: timestamp unchanged
    after = repo.get(snap.id)
    assert after.timestamp == old_ts


def test_cli_retime_all_from_level_dat(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world1 = _seed_world(tmp_path / "a", last_played_ms=1_500_000_000_000)
    world2 = _seed_world(tmp_path / "b", last_played_ms=1_600_000_000_000)
    repo.snapshot(world1, label="s1",
                  timestamp=datetime(2030, 1, 1, tzinfo=timezone.utc))
    repo.snapshot(world2, label="s2",
                  timestamp=datetime(2030, 6, 1, tzinfo=timezone.utc))

    proc = _run_cli("retime", str(repo.repo_path),
                    "--all", "--from-level-dat")
    assert proc.returncode == 0, proc.stderr

    snaps = {s.label: s for s in repo.list()}
    assert int(snaps["s1"].timestamp.timestamp() * 1000) == 1_500_000_000_000
    assert int(snaps["s2"].timestamp.timestamp() * 1000) == 1_600_000_000_000

    # Backup file should exist
    assert (repo.index_path.with_name("index.sqlite.bak")).is_file()


def test_cli_retime_rejects_conflicting_flags(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world(tmp_path, last_played_ms=1_000_000)
    snap = repo.snapshot(world, label="x")

    # --timestamp and --from-level-dat together
    proc = _run_cli("retime", str(repo.repo_path), snap.id,
                    "--timestamp", "2024-01-01T00:00:00",
                    "--from-level-dat")
    assert proc.returncode == 2

    # --all without --from-level-dat
    proc = _run_cli("retime", str(repo.repo_path),
                    "--all", "--timestamp", "2024-01-01T00:00:00")
    assert proc.returncode == 2
