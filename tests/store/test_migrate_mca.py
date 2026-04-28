"""Tests for ``ChunkSnapshotRepo.migrate_mca_files_to_chunks`` and CLI.

The tool fixes the historical case where ``entities/*.mca`` and
``poi/*.mca`` were stored as whole-files (single sha256-keyed blob per
file) instead of getting chunk-level dedup. Bytes are already in the
file pool — no source archive needed.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.store.manifest import (
    FileRecord, read_manifest, write_manifest,
)
from chunkvault.store.index import IndexDB
from chunkvault.mca.region import pack_region, PackedChunk

from tests._fixtures import ChunkSpec, write_region_file
from tests.store.test_repo import _make_level_dat


def _seed_world_with_entities(tmp_path: Path) -> Path:
    """Build a synthetic world with region/, entities/, poi/ all populated."""
    world = tmp_path / "world"
    world.mkdir(parents=True)
    (world / "level.dat").write_bytes(
        _make_level_dat("1.21.10", 4556, last_played_ms=1_700_000_000_000),
    )
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"block-data"),
    ])
    write_region_file(world, "entities", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"entity-data"),
    ])
    write_region_file(world, "poi", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"poi-data"),
    ])
    return world


def _move_mca_dim_back_to_files(
    repo: ChunkSnapshotRepo, snap_id: str, dim_key: str,
) -> int:
    """Simulate a pre-fix-era manifest: take a chunk-deduped dim and rewrite
    its records as whole-file entries in manifest.files. Returns the number
    of file entries created."""
    snap = repo.get(snap_id)
    m = read_manifest(snap.manifest_path)
    if dim_key not in m.dimensions:
        return 0
    moved = 0
    for region in m.dimensions[dim_key]:
        packed = [PackedChunk(
            cx=c.cx, cz=c.cz,
            timestamp=c.timestamp, compression=c.compression,
            payload=repo.chunks.read_chunk(c.content_hash)[1:],
        ) for c in region.chunks]
        mca_bytes = pack_region(packed)
        sha = hashlib.sha256(mca_bytes).digest()
        repo.chunks.store_file(sha, mca_bytes)
        with IndexDB(repo.index_path) as idx:
            idx.adjust_file_refs([sha], delta=+1)
        rel = f"{dim_key}/r.{region.rx}.{region.rz}.mca"
        m.files.append(FileRecord(relative_path=rel, sha256=sha))
        moved += 1
    del m.dimensions[dim_key]
    write_manifest(snap.manifest_path, m)
    return moved


def test_fresh_snapshot_chunk_dedupes_all_three_region_pools(tmp_path: Path):
    """New ingest path: entities/ and poi/ should auto-route to dimensions,
    not get whole-file deduped."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_entities(tmp_path)
    snap = repo.snapshot(world, label="fresh", verify_roundtrip=False)
    m = read_manifest(snap.manifest_path)
    assert set(m.dimensions.keys()) == {"region", "entities", "poi"}
    assert sum(1 for f in m.files if f.relative_path.endswith(".mca")) == 0


def test_migration_converts_legacy_mca_files(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_entities(tmp_path)
    snap = repo.snapshot(world, label="legacy", verify_roundtrip=False)

    # Simulate pre-fix-era state: move entities + poi back to files-pool
    moved_e = _move_mca_dim_back_to_files(repo, snap.id, "entities")
    moved_p = _move_mca_dim_back_to_files(repo, snap.id, "poi")
    assert moved_e + moved_p == 2

    # Confirm pre-state
    m_before = read_manifest(snap.manifest_path)
    assert "entities" not in m_before.dimensions
    assert "poi" not in m_before.dimensions
    n_mca_in_files = sum(1 for f in m_before.files if f.relative_path.endswith(".mca"))
    assert n_mca_in_files == 2

    # Dry-run identifies them
    dry = repo.migrate_mca_files_to_chunks(dry_run=True)
    assert dry.mca_files_total == 2
    assert dry.snapshots_with_mca_files == 1
    assert not dry.applied

    # Apply migrates them
    result = repo.migrate_mca_files_to_chunks(dry_run=False)
    assert result.applied
    assert result.files_migrated == 2
    assert result.chunks_added == 2     # one chunk per fixture-region

    # Verify final manifest state
    m_after = read_manifest(snap.manifest_path)
    assert "entities" in m_after.dimensions
    assert "poi" in m_after.dimensions
    assert sum(1 for f in m_after.files if f.relative_path.endswith(".mca")) == 0


def test_migration_preserves_restore_byte_for_byte(tmp_path: Path):
    """The whole point: after migration, restore must produce identical
    output to the original world tree."""
    from chunkvault.store.roundtrip import compare_directories
    from chunkvault.store.repo import DEFAULT_EXCLUDE

    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_entities(tmp_path)
    snap = repo.snapshot(world, label="legacy", verify_roundtrip=False)
    _move_mca_dim_back_to_files(repo, snap.id, "entities")
    _move_mca_dim_back_to_files(repo, snap.id, "poi")

    repo.migrate_mca_files_to_chunks(dry_run=False)

    restored = tmp_path / "restored"
    repo.restore(snap, restored)
    report = compare_directories(world, restored, exclude=DEFAULT_EXCLUDE)
    assert report.passed, report.summary()


def test_migration_is_idempotent(tmp_path: Path):
    """Running migrate twice must not double-process anything."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_entities(tmp_path)
    snap = repo.snapshot(world, label="legacy", verify_roundtrip=False)
    _move_mca_dim_back_to_files(repo, snap.id, "entities")

    r1 = repo.migrate_mca_files_to_chunks(dry_run=False)
    assert r1.files_migrated == 1

    r2 = repo.migrate_mca_files_to_chunks(dry_run=False)
    # Second run finds nothing left to migrate
    assert r2.mca_files_total == 0
    assert r2.files_migrated == 0


def test_migration_handles_missing_pool_blob_gracefully(tmp_path: Path):
    """If a file blob is missing from the pool, error is reported but
    other files still migrate."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_entities(tmp_path)
    snap = repo.snapshot(world, label="legacy", verify_roundtrip=False)
    _move_mca_dim_back_to_files(repo, snap.id, "entities")
    _move_mca_dim_back_to_files(repo, snap.id, "poi")

    # Corrupt: remove one of the file blobs from the pool
    m = read_manifest(snap.manifest_path)
    target = next(f for f in m.files if f.relative_path.startswith("entities/"))
    blob_path = repo.chunks._file_path(target.sha256)
    blob_path.unlink()

    result = repo.migrate_mca_files_to_chunks(dry_run=False)
    assert result.files_migrated == 1   # only the poi file
    assert result.errors                 # entities file errored


# ---- CLI -------------------------------------------------------------------

def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "chunkvault", *args],
        capture_output=True, text=True,
    )


def test_cli_migrate_dry_run_default(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_entities(tmp_path)
    snap = repo.snapshot(world, label="legacy", verify_roundtrip=False)
    _move_mca_dim_back_to_files(repo, snap.id, "entities")

    proc = _run_cli("migrate-mca-files", str(repo.repo_path))
    assert proc.returncode == 0
    assert "DRY-RUN" in proc.stdout
    # Vault unchanged
    m = read_manifest(snap.manifest_path)
    assert sum(1 for f in m.files if f.relative_path.startswith("entities/")) == 1


def test_cli_migrate_apply_modifies(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_entities(tmp_path)
    snap = repo.snapshot(world, label="legacy", verify_roundtrip=False)
    _move_mca_dim_back_to_files(repo, snap.id, "entities")

    proc = _run_cli("migrate-mca-files", str(repo.repo_path), "--apply")
    assert proc.returncode == 0
    assert "APPLIED" in proc.stdout
    m = read_manifest(snap.manifest_path)
    assert "entities" in m.dimensions
