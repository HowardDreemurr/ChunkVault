"""Tests for ``ChunkSnapshotRepo.repair_timestamps`` and the CLI command.

The repair tool fixes the historical bug where ``ingest_archive`` fell
back to ``datetime.now()`` when the archive filename didn't parse as a
6-component timestamp — yielding wrong-timestamped snapshots whose labels
also embedded the wrong time, breaking idempotency and producing
duplicates on re-ingest.

These tests synthesize that broken state directly (rather than
round-tripping through a buggy ingest) so the repair logic can be
exercised without depending on the bug's exact reproduction conditions.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.store.manifest import read_manifest, write_manifest
from chunkvault.store.index import IndexDB

from tests._fixtures import ChunkSpec, write_region_file
from tests.store.test_repo import _make_level_dat


def _seed_world(tmp_path: Path, name: str, *,
                last_played_ms: int) -> Path:
    world = tmp_path / name
    world.mkdir(parents=True)
    (world / "level.dat").write_bytes(
        _make_level_dat("1.20.4", 3700, last_played_ms=last_played_ms),
    )
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, name.encode()),
    ])
    return world


def _corrupt_timestamp_to_now(repo: ChunkSnapshotRepo, snap_id: str,
                              wrong_ms: int) -> None:
    """Simulate the historical bug: snapshot was written with a 'now()'
    timestamp+label even though the manifest's last_played_ms is correct.

    Updates: index row's timestamp_ms + label, and the manifest header's
    timestamp_ms + label. Does NOT touch last_played_ms (that's the
    "fix beacon" the repair will read).
    """
    snap = repo.get(snap_id)
    assert snap is not None
    manifest = read_manifest(snap.manifest_path)
    # Build a label that follows the ingest pattern with the wrong ts
    wrong_dt = datetime.fromtimestamp(wrong_ms / 1000, tz=timezone.utc)
    wrong_label = f"{snap.world_name}-{wrong_dt.strftime('%Y-%m-%d-%H-%M-%S')}"
    manifest.header.timestamp_ms = wrong_ms
    manifest.header.label = wrong_label
    write_manifest(snap.manifest_path, manifest)
    with IndexDB(repo.index_path) as idx:
        idx._conn.execute(
            "UPDATE snapshots SET timestamp_ms = ?, label = ? WHERE id = ?",
            (wrong_ms, wrong_label, snap_id),
        )
        idx._conn.commit()


# ---- dry-run reporting ------------------------------------------------------

def test_repair_dry_run_does_not_modify(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    lp = 1_700_000_000_000
    world = _seed_world(tmp_path, "EX-Server", last_played_ms=lp)
    snap = repo.snapshot(world, label="EX-Server-test", world_name="EX-Server")

    wrong = 1_900_000_000_000
    _corrupt_timestamp_to_now(repo, snap.id, wrong)

    # Snapshot all the state we care about preserving in dry-run
    before_label = repo.get(snap.id).label
    before_ts = repo.get(snap.id).timestamp

    report = repo.repair_timestamps(dry_run=True)
    assert report.scanned == 1
    assert len(report.to_retime) == 1
    assert report.to_retime[0].new_ts_ms == lp
    assert not report.applied

    # Vault state unchanged
    after = repo.get(snap.id)
    assert after.label == before_label
    assert after.timestamp == before_ts


def test_repair_apply_aligns_timestamp_with_last_played(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    lp = 1_700_000_000_000
    world = _seed_world(tmp_path, "EX-Server", last_played_ms=lp)
    snap = repo.snapshot(world, label="EX-Server-orig", world_name="EX-Server")
    _corrupt_timestamp_to_now(repo, snap.id, 1_900_000_000_000)

    report = repo.repair_timestamps(dry_run=False)
    assert report.applied
    assert len(report.retimed) == 1

    fixed = repo.get(snap.id)
    assert int(fixed.timestamp.timestamp() * 1000) == lp


def test_repair_renames_label_when_it_matches_ingest_pattern(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    lp = 1_700_000_000_000  # 2023-11-14-22-13-20 UTC
    world = _seed_world(tmp_path, "EX-Server", last_played_ms=lp)
    snap = repo.snapshot(world, label="ignored", world_name="EX-Server")
    # Rewrite to look like a wrong-time ingest output
    _corrupt_timestamp_to_now(repo, snap.id, 1_900_000_000_000)

    repo.repair_timestamps(dry_run=False)
    fixed = repo.get(snap.id)
    expected_label = (
        f"EX-Server-"
        f"{datetime.fromtimestamp(lp / 1000, tz=timezone.utc).strftime('%Y-%m-%d-%H-%M-%S')}"
    )
    assert fixed.label == expected_label
    # Manifest header agrees
    manifest = read_manifest(fixed.manifest_path)
    assert manifest.header.label == expected_label


def test_repair_preserves_user_chosen_labels(tmp_path: Path):
    """Labels that don't match the ingest pattern (e.g. 'before-raid')
    must NOT be auto-renamed — only the timestamp gets fixed."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    lp = 1_700_000_000_000
    world = _seed_world(tmp_path, "EX-Server", last_played_ms=lp)
    snap = repo.snapshot(world, label="before-raid", world_name="EX-Server")
    # Corrupt only the index/manifest timestamp, NOT the label
    with IndexDB(repo.index_path) as idx:
        idx._conn.execute(
            "UPDATE snapshots SET timestamp_ms = ? WHERE id = ?",
            (1_900_000_000_000, snap.id),
        )
        idx._conn.commit()
    manifest = read_manifest(snap.manifest_path)
    manifest.header.timestamp_ms = 1_900_000_000_000
    write_manifest(snap.manifest_path, manifest)

    repo.repair_timestamps(dry_run=False)
    fixed = repo.get(snap.id)
    assert fixed.label == "before-raid"  # unchanged
    assert int(fixed.timestamp.timestamp() * 1000) == lp


# ---- duplicate dedup --------------------------------------------------------

def test_repair_dedupes_duplicate_snapshots(tmp_path: Path):
    """Two snapshots of the same world+last_played, created with different
    wrong fallback timestamps: repair must keep one and delete the others."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    lp = 1_700_000_000_000
    world = _seed_world(tmp_path, "EX-Server", last_played_ms=lp)

    # Snapshot 1: wrong ts A
    snap1 = repo.snapshot(world, label="EX-Server-a", world_name="EX-Server",
                          timestamp=datetime.fromtimestamp(
                              1_900_000_000, tz=timezone.utc),
                          allow_live=True, verify_roundtrip=False)
    _corrupt_timestamp_to_now(repo, snap1.id, 1_900_000_000_000)

    # Snapshot 2 (different label/ts so it doesn't collide on creation,
    # but represents the same archive: same world_name + same last_played)
    snap2 = repo.snapshot(world, label="EX-Server-b", world_name="EX-Server",
                          timestamp=datetime.fromtimestamp(
                              1_950_000_000, tz=timezone.utc),
                          allow_live=True, verify_roundtrip=False)
    _corrupt_timestamp_to_now(repo, snap2.id, 1_950_000_000_000)

    assert len(repo.list()) == 2

    report = repo.repair_timestamps(dry_run=False)
    assert len(report.duplicate_groups) == 1
    assert len(report.deleted) == 1
    survivors = repo.list()
    assert len(survivors) == 1
    # Survivor's timestamp now matches LastPlayed
    assert int(survivors[0].timestamp.timestamp() * 1000) == lp


def test_repair_keeps_correctly_timestamped_winner(tmp_path: Path):
    """When deduping, prefer the snapshot whose timestamp is already correct
    (== last_played_ms) — others were created later with wrong ts."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    lp = 1_700_000_000_000
    world = _seed_world(tmp_path, "EX-Server", last_played_ms=lp)

    correct_dt = datetime.fromtimestamp(lp / 1000, tz=timezone.utc)
    correct = repo.snapshot(world, label="correct", world_name="EX-Server",
                            timestamp=correct_dt, allow_live=True,
                            verify_roundtrip=False)
    wrong = repo.snapshot(world, label="wrong", world_name="EX-Server",
                          timestamp=datetime.fromtimestamp(
                              1_900_000_000, tz=timezone.utc),
                          allow_live=True, verify_roundtrip=False)
    _corrupt_timestamp_to_now(repo, wrong.id, 1_900_000_000_000)

    repo.repair_timestamps(dry_run=False)
    survivors = repo.list()
    assert len(survivors) == 1
    assert survivors[0].id == correct.id


# ---- guard cases ------------------------------------------------------------

def test_repair_recovers_last_played_from_file_pool(tmp_path: Path):
    """The killer feature: even when a snapshot's manifest has
    last_played_ms=0 (because it was taken by an older chunkvault that
    didn't read level.dat at ingest time), the level.dat itself is in
    the file pool — content-addressed, immutable. Repair pulls it out,
    parses LastPlayed, and uses that. No source archive needed."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    lp_in_level_dat = 1_700_000_000_000   # 2023-11-14
    world = _seed_world(tmp_path, "EX-Server", last_played_ms=lp_in_level_dat)
    snap = repo.snapshot(world, label="x", world_name="EX-Server")

    # Simulate a pre-fix-era manifest: nuke last_played_ms in the header
    # while keeping the level.dat *file* in the pool intact.
    manifest = read_manifest(snap.manifest_path)
    assert manifest.header.last_played_ms == lp_in_level_dat   # baseline
    manifest.header.last_played_ms = 0
    write_manifest(snap.manifest_path, manifest)

    # Also corrupt the snapshot's ts/label to look like a now()-fallback
    # ingest produced it.
    _corrupt_timestamp_to_now(repo, snap.id, 1_900_000_000_000)
    # _corrupt rewrote the manifest, restoring last_played_ms to 0 again
    # (it preserves the rest of the header). Re-zero in case.
    manifest = read_manifest(snap.manifest_path)
    manifest.header.last_played_ms = 0
    write_manifest(snap.manifest_path, manifest)

    # Dry-run should report recovered_from_pool=1 and a retime plan
    dry = repo.repair_timestamps(dry_run=True)
    assert dry.recovered_from_pool == 1
    assert len(dry.to_retime) == 1
    assert dry.to_retime[0].new_ts_ms == lp_in_level_dat

    # Apply: snapshot gets retimed AND the manifest's last_played_ms gets
    # persisted so future runs don't have to re-pull from the pool.
    result = repo.repair_timestamps(dry_run=False)
    assert result.applied
    assert result.recovered_from_pool == 1
    fixed = repo.get(snap.id)
    assert int(fixed.timestamp.timestamp() * 1000) == lp_in_level_dat
    fixed_manifest = read_manifest(fixed.manifest_path)
    assert fixed_manifest.header.last_played_ms == lp_in_level_dat

    # A second run should now find it pre-recorded — no recovery needed.
    second = repo.repair_timestamps(dry_run=True)
    assert second.recovered_from_pool == 0
    assert second.already_correct >= 1


def test_repair_recovery_handles_corrupt_level_dat(tmp_path: Path):
    """If the level.dat blob in the pool is unreadable (e.g. truncated),
    recovery falls through to no_last_played — never crashes."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world(tmp_path, "EX-Server", last_played_ms=1_700_000_000_000)
    snap = repo.snapshot(world, label="x", world_name="EX-Server")

    manifest = read_manifest(snap.manifest_path)
    manifest.header.last_played_ms = 0
    write_manifest(snap.manifest_path, manifest)

    # Corrupt the level.dat blob in the pool
    for f in manifest.files:
        if f.relative_path.endswith("level.dat"):
            blob_path = repo.chunks._file_path(f.sha256)
            blob_path.write_bytes(b"not-gzip")
            break

    report = repo.repair_timestamps(dry_run=True)
    assert report.recovered_from_pool == 0
    assert any(sid == snap.id for sid, _ in report.no_last_played)


def test_repair_skips_snapshots_without_last_played(tmp_path: Path):
    """If the manifest has no LastPlayed, we can't auto-fix — must skip
    cleanly and report it, never delete it."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    # World whose level.dat omits LastPlayed
    world = tmp_path / "no-lp"
    world.mkdir()
    (world / "level.dat").write_bytes(
        _make_level_dat("1.20.4", 3700, last_played_ms=None),
    )
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"x"),
    ])
    snap = repo.snapshot(world, label="orphan", world_name="no-lp")

    report = repo.repair_timestamps(dry_run=False)
    assert len(report.no_last_played) == 1
    assert not report.deleted
    assert not report.retimed
    # Snapshot still present
    assert repo.get(snap.id) is not None


def test_repair_reconciles_index_lag_after_simulated_ctrl_c(tmp_path: Path):
    """Chaos: a previous repair was Ctrl+C'd between manifest write
    (atomic, succeeded) and the SQL update — leaving manifest=new and
    index=old. Subsequent repair must detect the desync and sync the
    index from the manifest (manifest is authoritative)."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    lp = 1_700_000_000_000
    world = _seed_world(tmp_path, "EX-Server", last_played_ms=lp)
    snap = repo.snapshot(world, label="x", world_name="EX-Server")

    # Now simulate that a prior repair wrote the manifest with a NEW
    # ts/label but its index update never landed — a Ctrl+C window.
    new_ts_ms = lp                          # what manifest was set to
    new_label = "EX-Server-2023-11-14-22-13-20"
    m = read_manifest(snap.manifest_path)
    m.header.timestamp_ms = new_ts_ms
    m.header.label = new_label
    write_manifest(snap.manifest_path, m)
    # Index keeps OLD ts and OLD label — pretend SQL update never happened
    old_ts_ms = 1_900_000_000_000
    with IndexDB(repo.index_path) as idx:
        idx._conn.execute(
            "UPDATE snapshots SET timestamp_ms = ?, label = ? WHERE id = ?",
            (old_ts_ms, "EX-Server-2030-03-17-17-46-40", snap.id),
        )
        idx._conn.commit()

    # Now run repair: should detect index lag and reconcile it.
    dry = repo.repair_timestamps(dry_run=True)
    assert dry.index_lag_to_reconcile == 1
    # Manifest's ts already equals last_played_ms, so no retime is needed.
    assert len(dry.to_retime) == 0

    result = repo.repair_timestamps(dry_run=False)
    assert result.applied
    assert result.index_lag_reconciled == 1

    # After reconciliation, index reflects manifest
    fixed = repo.get(snap.id)
    assert int(fixed.timestamp.timestamp() * 1000) == new_ts_ms
    assert fixed.label == new_label


def test_repair_already_correct_is_noop(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    lp = 1_700_000_000_000
    world = _seed_world(tmp_path, "EX-Server", last_played_ms=lp)
    correct_dt = datetime.fromtimestamp(lp / 1000, tz=timezone.utc)
    repo.snapshot(world, label="x", world_name="EX-Server",
                  timestamp=correct_dt)

    report = repo.repair_timestamps(dry_run=False)
    assert report.already_correct == 1
    assert not report.to_retime
    assert not report.deleted


# ---- CLI --------------------------------------------------------------------

def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "chunkvault", *args],
        capture_output=True, text=True,
    )


def test_cli_repair_dry_run_default(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    lp = 1_700_000_000_000
    world = _seed_world(tmp_path, "EX-Server", last_played_ms=lp)
    snap = repo.snapshot(world, label="x", world_name="EX-Server")
    _corrupt_timestamp_to_now(repo, snap.id, 1_900_000_000_000)

    proc = _run_cli("repair-timestamps", str(repo.repo_path))
    assert proc.returncode == 0, proc.stderr
    assert "DRY-RUN" in proc.stdout
    assert "DRY RUN" in proc.stdout
    # Vault unchanged
    after = repo.get(snap.id)
    assert int(after.timestamp.timestamp() * 1000) == 1_900_000_000_000


def test_cli_repair_apply_modifies_vault(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    lp = 1_700_000_000_000
    world = _seed_world(tmp_path, "EX-Server", last_played_ms=lp)
    snap = repo.snapshot(world, label="x", world_name="EX-Server")
    _corrupt_timestamp_to_now(repo, snap.id, 1_900_000_000_000)

    proc = _run_cli("repair-timestamps", str(repo.repo_path), "--apply")
    assert proc.returncode == 0, proc.stderr
    assert "APPLIED" in proc.stdout
    after = repo.get(snap.id)
    assert int(after.timestamp.timestamp() * 1000) == lp
