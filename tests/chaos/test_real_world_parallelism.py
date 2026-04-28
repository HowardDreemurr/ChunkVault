"""Real-world chaos test for the parallel snapshot path.

Skipped by default — needs an actual MC world directory which is too big
to ship in the test fixture. Provide one via the ``CHUNKVAULT_TEST_WORLD``
env var pointing at a real ``world/`` (e.g. the user's
``D:/mc_1_21_10_server/world``) and re-run::

    set CHUNKVAULT_TEST_WORLD=D:/mc_1_21_10_server/world
    python -m pytest tests/chaos/ -v

The test snapshots the same world under multiple parallelism levels
(1, 2, 4, 8) and verifies:

  - the chunk pool's content is byte-identical across runs
  - every snapshot's manifest records the same chunk hashes per region
  - restore from the parallel-built vault matches the source
    byte-for-byte (compare-directories PASS)

If any parallelism level diverges from serial, we have a real
concurrency bug — and we want to find it on real data, not just on
2-chunk synthetic fixtures.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.store.manifest import read_manifest
from chunkvault.store.repo import DEFAULT_EXCLUDE
from chunkvault.store.roundtrip import compare_directories


_WORLD_ENV = "CHUNKVAULT_TEST_WORLD"


def _world_path() -> Path | None:
    raw = os.environ.get(_WORLD_ENV)
    if not raw:
        return None
    p = Path(raw)
    if not p.is_dir():
        return None
    return p


pytestmark = pytest.mark.skipif(
    _world_path() is None,
    reason=(
        f"set {_WORLD_ENV}=<path-to-world> to run real-fixture chaos tests "
        f"(e.g. {_WORLD_ENV}=D:/mc_1_21_10_server/world)"
    ),
)


def _vault_chunk_set(vault_path: Path) -> set[str]:
    """Return the set of chunk-hash filenames in the chunk pool. Two
    identical-content vaults have identical sets."""
    out: set[str] = set()
    for p in (vault_path / "chunks").rglob("*"):
        if p.is_file() and ".tmp." not in p.name:
            out.add(p.name)
    return out


def _manifest_chunk_hashes(vault_path: Path) -> dict:
    """Per-snapshot, per-(dim_key,rx,rz,cx,cz) chunk content hashes.
    Stable across renamings, useful for cross-run equivalence checks."""
    repo = ChunkSnapshotRepo(vault_path)
    out: dict = {}
    for snap in repo.list():
        m = read_manifest(snap.manifest_path)
        per_snap: dict = {}
        for dim_key, regions in m.dimensions.items():
            for region in regions:
                for c in region.chunks:
                    per_snap[(dim_key, region.rx, region.rz, c.cx, c.cz)] = (
                        c.content_hash
                    )
        out[snap.world_name + ":" + (snap.label or "")] = per_snap
    return out


@pytest.mark.parametrize("parallelism", [1, 2, 4, 8])
def test_real_world_parallel_equivalence(tmp_path: Path, parallelism: int):
    """Same world, different parallelism levels, identical output."""
    world = _world_path()
    assert world is not None    # pytestmark skipif covers the bare case

    # Reference: serial snapshot
    ref_vault = tmp_path / "ref-vault"
    ref_repo = ChunkSnapshotRepo(ref_vault)
    ref_repo.init()
    t0 = time.time()
    ref_repo.snapshot(
        world, label="ref",
        allow_live=True, verify_roundtrip=False,
        parallelism=1,
    )
    t_serial = time.time() - t0

    ref_chunks = _vault_chunk_set(ref_vault)
    ref_manifest = _manifest_chunk_hashes(ref_vault)

    # Test: parallel snapshot
    par_vault = tmp_path / f"par-vault-{parallelism}"
    par_repo = ChunkSnapshotRepo(par_vault)
    par_repo.init()
    t0 = time.time()
    par_repo.snapshot(
        world, label="ref",
        allow_live=True, verify_roundtrip=False,
        parallelism=parallelism,
    )
    t_parallel = time.time() - t0

    par_chunks = _vault_chunk_set(par_vault)
    par_manifest = _manifest_chunk_hashes(par_vault)

    speedup = t_serial / t_parallel if t_parallel > 0 else float("inf")
    print(
        f"\n  parallelism={parallelism}: serial={t_serial:.2f}s "
        f"parallel={t_parallel:.2f}s  speedup={speedup:.2f}x"
    )

    assert ref_chunks == par_chunks, (
        f"chunk pool sets differ at parallelism={parallelism} "
        f"(missing: {ref_chunks - par_chunks}, "
        f"extra: {par_chunks - ref_chunks})"
    )
    assert ref_manifest == par_manifest, (
        f"manifest chunk-hash maps differ at parallelism={parallelism}"
    )


def test_real_world_parallel_restore_identical(tmp_path: Path):
    """Snapshot real world under parallelism=8, restore, compare to source."""
    world = _world_path()
    assert world is not None

    vault = tmp_path / "vault"
    repo = ChunkSnapshotRepo(vault)
    repo.init()
    snap = repo.snapshot(
        world, label="x",
        allow_live=True, verify_roundtrip=False,
        parallelism=8,
    )

    restore = tmp_path / "restored"
    repo.restore(snap, restore)
    report = compare_directories(world, restore, exclude=DEFAULT_EXCLUDE)
    assert report.passed, (
        f"parallel snapshot + restore did not reproduce the source: "
        f"{report.summary()}"
    )


def test_real_world_parallel_verify_equivalent(tmp_path: Path):
    """Build a vault, run verify under serial vs parallel — same result."""
    world = _world_path()
    assert world is not None

    vault = tmp_path / "vault"
    repo = ChunkSnapshotRepo(vault)
    repo.init()
    repo.snapshot(
        world, label="v",
        allow_live=True, verify_roundtrip=False,
        parallelism=4,
    )

    serial = repo.verify(parallelism=1)
    parallel = repo.verify(parallelism=8)
    assert serial.ok_chunks == parallel.ok_chunks
    assert serial.corrupt_chunks == parallel.corrupt_chunks
    assert serial.ok_files == parallel.ok_files
    assert serial.corrupt_files == parallel.corrupt_files
    assert serial.missing_referenced == parallel.missing_referenced
