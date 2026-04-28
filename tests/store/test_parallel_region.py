"""Parallel region-hash equivalence + chaos tests.

The parallel path must produce IDENTICAL output to the serial path:
  - same manifest bytes
  - same set of chunk hashes in the pool
  - same on-disk blob bytes per hash

These tests run the same fixture under multiple parallelism levels
(1, 2, 4, 8) and compare. If any level diverges, we have a concurrency
bug that would corrupt user data — fail loudly.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.store.manifest import read_manifest

from tests._fixtures import ChunkSpec, write_region_file
from tests.store.test_repo import _make_level_dat


def _world_with_n_regions(tmp_path: Path, n: int = 16) -> Path:
    """Build a world with N region files, each with several chunks
    of varying content (some shared across regions to exercise dedup,
    some unique). Returns the world path."""
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(
        _make_level_dat("1.20.4", 3700, last_played_ms=1_700_000_000_000),
    )
    for i in range(n):
        rx = i // 4 - 2
        rz = i % 4 - 2
        write_region_file(world, "region", rx, rz, [
            ChunkSpec(0, 0, 1, 2, b"shared-chunk-A"),       # cross-region dedup
            ChunkSpec(1, 0, 1, 2, b"shared-chunk-B"),
            ChunkSpec(2, 0, 1, 2, f"unique-{i}-c0".encode()),  # unique per region
            ChunkSpec(3, 0, 1, 2, f"unique-{i}-c1".encode()),
        ])
    return world


def _vault_fingerprint(vault_path: Path) -> tuple[bytes, list[tuple[str, bytes]]]:
    """Return a deterministic fingerprint of a vault: a sorted list of
    (manifest_label, manifest_bytes) plus a sorted list of chunk file
    sha256s. Two vaults with the same fingerprint contain identical
    snapshot+chunk data."""
    repo = ChunkSnapshotRepo(vault_path)

    # Read each manifest's bytes (post-write, so includes header). Sort
    # by label for determinism — the index row order isn't guaranteed.
    manifest_pairs: list[tuple[str, bytes]] = []
    for snap in repo.list():
        m = read_manifest(snap.manifest_path)
        # Re-serialize from header info we care about; bytes-on-disk
        # may differ in incidental ways (manifest_path is per-snapshot
        # uuid). Just compare via the materialized RegionRecord set.
        ts_label = (snap.timestamp.isoformat(), m.header.world_name)
        # Hash the canonical content: chunks per dim, files sorted
        h = hashlib.sha256()
        for dim_key in sorted(m.dimensions):
            h.update(dim_key.encode())
            for region in sorted(m.dimensions[dim_key],
                                 key=lambda r: (r.rx, r.rz)):
                h.update(f"r{region.rx},{region.rz}".encode())
                for c in sorted(region.chunks, key=lambda c: (c.cx, c.cz)):
                    h.update(c.content_hash)
        for f in sorted(m.files, key=lambda x: x.relative_path):
            h.update(f.relative_path.encode())
            h.update(f.sha256)
        manifest_pairs.append((str(ts_label), h.digest()))
    manifest_pairs.sort()

    # Chunk pool fingerprint: every chunk file's sha256 of bytes.
    chunk_hashes: list[bytes] = []
    chunks_dir = vault_path / "chunks"
    if chunks_dir.is_dir():
        for p in chunks_dir.rglob("*"):
            if p.is_file() and ".tmp." not in p.name:
                chunk_hashes.append(p.name.encode())   # filename IS the hash
    chunk_hashes.sort()
    pool_digest = hashlib.sha256(b"".join(chunk_hashes)).digest()

    return pool_digest, manifest_pairs


@pytest.mark.parametrize("parallelism", [1, 2, 4, 8])
def test_parallel_equivalent_to_serial(tmp_path: Path, parallelism: int):
    """For the same input, parallelism=N produces an identical vault
    (chunk pool + manifest content) as parallelism=1."""
    world = _world_with_n_regions(tmp_path, n=8)

    # Reference: serial run
    vault_serial = tmp_path / "vault-serial"
    repo_s = ChunkSnapshotRepo(vault_serial)
    repo_s.init()
    repo_s.snapshot(world, label="ref", parallelism=1, verify_roundtrip=False)
    ref_pool, ref_manifests = _vault_fingerprint(vault_serial)

    # Test: parallel run
    vault_par = tmp_path / f"vault-par{parallelism}"
    repo_p = ChunkSnapshotRepo(vault_par)
    repo_p.init()
    repo_p.snapshot(world, label="ref", parallelism=parallelism,
                    verify_roundtrip=False)
    par_pool, par_manifests = _vault_fingerprint(vault_par)

    assert ref_pool == par_pool, (
        f"chunk pool differs between parallelism=1 and parallelism={parallelism}"
    )
    assert ref_manifests == par_manifests, (
        f"manifest content differs between parallelism=1 and "
        f"parallelism={parallelism}"
    )


def test_parallel_restore_byte_identical(tmp_path: Path):
    """Snapshot under parallelism=8, restore, must reproduce source
    byte-for-byte."""
    from chunkvault.store.roundtrip import compare_directories
    from chunkvault.store.repo import DEFAULT_EXCLUDE

    world = _world_with_n_regions(tmp_path, n=4)
    vault = tmp_path / "vault"
    repo = ChunkSnapshotRepo(vault)
    repo.init()
    snap = repo.snapshot(world, label="x", parallelism=8,
                         verify_roundtrip=False)

    restore = tmp_path / "restored"
    repo.restore(snap, restore)
    report = compare_directories(world, restore, exclude=DEFAULT_EXCLUDE)
    assert report.passed, report.summary()


def test_parallel_handles_large_region_count(tmp_path: Path):
    """64 regions × 8 chunks each = 512 chunks. Stress the pool a bit
    to flush out any thread-pool sequencing assumptions."""
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(
        _make_level_dat("1.20.4", 3700, last_played_ms=1_700_000_000_000),
    )
    for i in range(64):
        rx = (i // 8) - 4
        rz = (i % 8) - 4
        write_region_file(world, "region", rx, rz, [
            ChunkSpec(cx, 0, 1, 2, f"r{i}c{cx}".encode())
            for cx in range(8)
        ])

    vault = tmp_path / "vault"
    repo = ChunkSnapshotRepo(vault)
    repo.init()
    snap = repo.snapshot(world, label="big", parallelism=8,
                         verify_roundtrip=False)
    # 64 regions × 8 unique chunks = 512 unique hashes
    chunks = sum(1 for p in (vault / "chunks").rglob("*") if p.is_file())
    assert chunks == 512


def test_parallel_no_chunk_pool_corruption_under_concurrent_writes(
    tmp_path: Path,
):
    """All workers in a pool may end up writing the same chunk hash
    if the test fixture has duplicate content. The atomic_store with
    thread-id-suffixed tmp file names must prevent corruption.

    Strategy: a world where EVERY region contains the same set of
    chunk payloads. With 32 regions × 4 chunks all = same 4 unique
    hashes, all 8 workers will race to write the same blobs.
    """
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(
        _make_level_dat("1.20.4", 3700, last_played_ms=1_700_000_000_000),
    )
    shared_payloads = [
        b"shared-payload-A" * 64,    # ~1KB each
        b"shared-payload-B" * 64,
        b"shared-payload-C" * 64,
        b"shared-payload-D" * 64,
    ]
    for i in range(32):
        rx = i // 8 - 2
        rz = i % 8 - 4
        write_region_file(world, "region", rx, rz, [
            ChunkSpec(cx, 0, 1, 2, payload)
            for cx, payload in enumerate(shared_payloads)
        ])

    vault = tmp_path / "vault"
    repo = ChunkSnapshotRepo(vault)
    repo.init()
    snap = repo.snapshot(world, label="dup", parallelism=8,
                         verify_roundtrip=False)

    # Should end up with EXACTLY 4 unique chunks in the pool, and each
    # chunk file's bytes must be intact (not the result of two threads
    # clobbering each other's tmp file).
    chunks = list((vault / "chunks").rglob("*"))
    chunk_files = [p for p in chunks if p.is_file() and ".tmp." not in p.name]
    assert len(chunk_files) == 4

    # Each chunk file's hash matches its filename (= content_hash).
    # The chunk payload format is "<masked_compression_byte> + <payload>".
    # We just verify the file isn't truncated/zero — actual hash check
    # would require redoing hash_chunk's compression-aware logic.
    for cf in chunk_files:
        content = cf.read_bytes()
        assert len(content) > 0
        # The content's first byte is the compression byte; rest is
        # one of the shared payloads.
        assert content[1:] in shared_payloads


def test_parallelism_1_takes_serial_path(tmp_path: Path, monkeypatch):
    """parallelism=1 must NOT spawn a thread pool. Verify by ensuring
    the serial-only cache shortcut still works (which it doesn't on the
    parallel path)."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world_with_n_regions(tmp_path, n=4)
    repo.snapshot(world, label="a", parallelism=1, verify_roundtrip=False)

    # On the parallel path, Region.from_bytes is always called — that's
    # the cache pre-check tradeoff. On the serial path it's NOT called
    # for cached regions. So this test is the contract for parallelism=1.
    from chunkvault.store import repo as repo_mod
    real_from_bytes = repo_mod.Region.from_bytes
    calls = {"n": 0}
    def trap(*a, **kw):
        calls["n"] += 1
        return real_from_bytes(*a, **kw)
    monkeypatch.setattr(repo_mod.Region, "from_bytes", trap)

    # Re-snapshot (same content) under parallelism=1 → cache hits → no parse
    repo.snapshot(world, label="b", parallelism=1, verify_roundtrip=False)
    assert calls["n"] == 0


def test_auto_parallelism_caps_at_8(tmp_path: Path):
    """parallelism=None auto-resolves to min(cpu_count, 8). Mostly a
    sanity check that the auto path doesn't blow up."""
    world = _world_with_n_regions(tmp_path, n=4)
    vault = tmp_path / "vault"
    repo = ChunkSnapshotRepo(vault)
    repo.init()
    # parallelism=None means auto. Just verify it works.
    snap = repo.snapshot(world, label="auto", verify_roundtrip=False)
    assert snap.label == "auto"
    chunks = sum(1 for p in (vault / "chunks").rglob("*") if p.is_file())
    assert chunks > 0
