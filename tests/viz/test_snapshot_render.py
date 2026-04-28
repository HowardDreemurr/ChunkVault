"""Tests for snapshot integration of the tile renderer + backfill command."""
from __future__ import annotations

import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.viz.snapshot_render import (
    ensure_tiles_for_manifest,
    modes_for_dim,
    write_snapshot_sidecars,
)
from chunkvault.viz.colors import color_for_block

from tests._fixtures import ChunkSpec, write_region_file
from tests.store.test_repo import _make_level_dat
from tests.viz.test_render import _build_chunk_1_18, _build_section_uniform


# ---- helpers ---------------------------------------------------------------

def _grass_chunk_payload() -> bytes:
    """A single chunk's NBT, zlib-compressed, that renders to all-grass."""
    nbt = _build_chunk_1_18([_build_section_uniform(0, "minecraft:grass_block")])
    return zlib.compress(nbt)


def _seed_world_with_real_chunk(tmp_path: Path, name: str = "world") -> Path:
    """Build a world dir whose .mca contains a real renderable chunk."""
    world = tmp_path / name
    world.mkdir(parents=True)
    (world / "level.dat").write_bytes(
        _make_level_dat("1.20.4", 3700, last_played_ms=1_000_000),
    )
    payload = _grass_chunk_payload()
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, payload),  # compression=2 (zlib)
    ])
    return world


# ---- modes_for_dim mapping -------------------------------------------------

def test_overworld_dim_uses_topdown():
    assert modes_for_dim("region") == ("topdown",)


def test_nether_dim_uses_two_altitude_modes():
    assert modes_for_dim("DIM-1/region") == ("nether_low", "nether_high")
    assert modes_for_dim("DIM-1") == ("nether_low", "nether_high")


def test_end_dim_uses_topdown():
    assert modes_for_dim("DIM1/region") == ("topdown",)


def test_unknown_dim_falls_back_to_topdown():
    assert modes_for_dim("custom_dim/region") == ("topdown",)


# ---- snapshot integration --------------------------------------------------

def test_snapshot_populates_tile_pool(tmp_path: Path):
    """A real-NBT chunk gets rendered into the tile pool during snapshot."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_real_chunk(tmp_path)
    snap = repo.snapshot(world, label="x", verify_roundtrip=False)

    # The chunk's tile should be in the pool under "topdown"
    from chunkvault.store.manifest import read_manifest
    manifest = read_manifest(snap.manifest_path)
    chunk_hash = manifest.dimensions["region"][0].chunks[0].content_hash
    assert repo.tiles.has(chunk_hash, "topdown")


def test_snapshot_writes_per_dim_sidecar_png(tmp_path: Path):
    """A PNG sidecar appears under repo/thumbnails/<snap-id>/."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_real_chunk(tmp_path)
    snap = repo.snapshot(world, label="png", verify_roundtrip=False)

    sidecar_dir = repo.repo_path / "thumbnails" / snap.id
    pngs = list(sidecar_dir.glob("*.png"))
    assert len(pngs) >= 1
    assert any(p.name == "region-topdown.png" for p in pngs)


def test_snapshot_does_not_fail_on_unrenderable_chunks(tmp_path: Path):
    """Garbage payloads (existing tests' fixtures) don't break snapshot."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(_make_level_dat("1.20.4", 3700))
    # Non-NBT payload — render attempt will fail but snapshot must succeed
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"not real chunk NBT"),
    ])
    # Should not raise
    snap = repo.snapshot(world, label="bad", verify_roundtrip=False)
    assert snap.id


def test_second_snapshot_skips_cached_tiles(tmp_path: Path):
    """A repeat snapshot of the same world reuses tile pool entries."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_real_chunk(tmp_path)
    repo.snapshot(world, label="first", verify_roundtrip=False)

    # Trigger a second snapshot. ensure_tiles_for_manifest should report
    # that all tiles were cache hits.
    from chunkvault.store.manifest import read_manifest
    snap = repo.snapshot(world, label="second", verify_roundtrip=False,
                         timestamp=__import__("datetime").datetime(
                             2030, 1, 1,
                             tzinfo=__import__("datetime").timezone.utc),
                         allow_live=True)
    manifest = read_manifest(snap.manifest_path)
    stats = ensure_tiles_for_manifest(repo, manifest)
    # Everything was already present, so 0 new renders
    assert stats.tiles_rendered == 0
    assert stats.tiles_skipped_cached >= 1


def test_render_persists_chunk_renders_incrementally(
    tmp_path: Path, monkeypatch,
):
    """Chaos: render N chunks, simulate Ctrl+C mid-loop after the
    incremental commit fired, then verify already-committed entries
    survived.

    With the old design (commit only at end-of-group) all entries in
    the killed group would be lost. The new design commits every
    _RENDER_COMMIT_BATCH chunks; we set BATCH=1 here so the crash
    after chunk #2 leaves chunks #1 + #2 committed but ALL chunks
    #3+ uncommitted — and the next run only re-renders #3+.
    """
    from chunkvault.store.manifest import read_manifest
    from chunkvault.store.index import IndexDB
    from chunkvault.viz import snapshot_render as sr

    # 3 chunks with DIFFERENT valid payloads so we get 3 unique hashes
    # that all actually render (not get rejected by the parser). Need
    # at least 3 to see the trap fire AFTER 2 successful commits.
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(
        _make_level_dat("1.20.4", 3700, last_played_ms=1_000_000),
    )
    grass = _grass_chunk_payload()
    dirt = zlib.compress(_build_chunk_1_18([
        _build_section_uniform(0, "minecraft:dirt"),
    ]))
    stone = zlib.compress(_build_chunk_1_18([
        _build_section_uniform(0, "minecraft:stone"),
    ]))
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, grass),
        ChunkSpec(1, 0, 1, 2, dirt),
        ChunkSpec(2, 0, 1, 2, stone),
    ])
    snap = repo.snapshot(world, label="x", verify_roundtrip=False)
    manifest = read_manifest(snap.manifest_path)

    # After snapshot, ensure_tiles_for_manifest already ran. Wipe so
    # we can re-run under the chaos harness.
    with IndexDB(repo.index_path) as idx:
        idx._conn.execute("DELETE FROM chunk_renders")
        idx._conn.commit()

    # Tiny commit batch to make incremental persistence visible
    monkeypatch.setattr(sr, "_RENDER_COMMIT_BATCH", 1)

    # Trap IndexDB.add_chunk_renders: let the first call write, then raise
    # on the 2nd. This simulates a crash that strikes AFTER 2 commits
    # already landed (real_add runs before the raise, so the 2nd batch
    # is also persisted before the exception bubbles).
    real_add = IndexDB.add_chunk_renders
    calls = {"n": 0}

    def trap_add(self, items):
        calls["n"] += 1
        real_add(self, items)
        if calls["n"] >= 2:
            raise RuntimeError("simulated Ctrl+C")

    monkeypatch.setattr(IndexDB, "add_chunk_renders", trap_add)

    # ensure_tiles_for_manifest doesn't catch RuntimeError around the
    # commit call (only around the per-chunk render block), so the
    # exception escapes.
    with pytest.raises(RuntimeError, match="simulated"):
        ensure_tiles_for_manifest(repo, manifest)

    # 2 entries committed before the crash (real_add ran in both calls,
    # the second one just raised AFTER writing).
    with IndexDB(repo.index_path) as idx:
        cur = idx._conn.execute("SELECT COUNT(*) FROM chunk_renders")
        committed_after_crash = cur.fetchone()[0]
    assert committed_after_crash == 2, (
        f"expected 2 entries committed before the crash, "
        f"got {committed_after_crash}"
    )

    # Restore add_chunk_renders + reset batch size, re-run.
    monkeypatch.undo()
    stats = ensure_tiles_for_manifest(repo, manifest)
    # The 2 already-committed chunks are cache hits; the remaining
    # uncommitted ones get rendered fresh.
    assert stats.tiles_skipped_cached == 2


def test_render_progress_emits_during_inner_loop(tmp_path: Path):
    """Progress events must come out *while* a group is rendering, not just
    at end-of-group. With ~1M chunks per group on real worlds, the per-group
    cadence makes the bar look frozen for hours; the inner-loop emit is the
    fix. Also checks cached chunks count toward `current` so the bar moves
    through warm groups too."""
    from chunkvault.store.manifest import read_manifest

    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_real_chunk(tmp_path)
    snap = repo.snapshot(world, label="prog", verify_roundtrip=False)
    manifest = read_manifest(snap.manifest_path)

    # First call already populated the cache during snapshot(). Now simulate
    # the second call where every tile is a cache hit — `seen` must still
    # advance to total, and at least one phase_progress event must fire.
    events: list = []
    ensure_tiles_for_manifest(
        repo, manifest, progress_cb=events.append,
    )
    progress = [e for e in events if e.kind == "phase_progress"]
    assert progress, "no phase_progress emitted"
    last = progress[-1]
    assert last.total is not None and last.total > 0
    assert last.current == last.total, (
        f"final progress {last.current}/{last.total} did not reach total"
    )


def test_sidecar_png_pixel_matches_block_color(tmp_path: Path):
    """The grass_block in the chunk shows up green in the rendered PNG."""
    from PIL import Image
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_real_chunk(tmp_path)
    snap = repo.snapshot(world, label="green", verify_roundtrip=False)

    png_path = repo.repo_path / "thumbnails" / snap.id / "region-topdown.png"
    assert png_path.is_file()
    img = Image.open(png_path).convert("RGB")
    expected = color_for_block("minecraft:grass_block")
    # Top-left pixel should match grass color
    assert img.getpixel((0, 0)) == expected


# ---- backfill via library ---------------------------------------------------

def test_ensure_tiles_for_manifest_populates_pool(tmp_path: Path):
    """Standalone library entry: pool gets filled even without snapshot path."""
    from chunkvault.store.manifest import read_manifest
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_real_chunk(tmp_path)
    snap = repo.snapshot(world, label="seed", verify_roundtrip=False)

    # Drop the existing tile to simulate a snapshot that never had thumbnails
    manifest = read_manifest(snap.manifest_path)
    chunk_hash = manifest.dimensions["region"][0].chunks[0].content_hash
    repo.tiles.delete(chunk_hash, "topdown")
    # Also clear the index row so ensure_tiles re-renders
    import sqlite3
    with sqlite3.connect(repo.index_path) as conn:
        conn.execute("DELETE FROM chunk_renders WHERE content_hash = ?",
                     (chunk_hash,))

    stats = ensure_tiles_for_manifest(repo, manifest)
    assert stats.tiles_rendered == 1
    assert repo.tiles.has(chunk_hash, "topdown")


def test_write_snapshot_sidecars_returns_paths(tmp_path: Path):
    from chunkvault.store.manifest import read_manifest
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_real_chunk(tmp_path)
    snap = repo.snapshot(world, label="paths", verify_roundtrip=False)
    manifest = read_manifest(snap.manifest_path)

    paths = write_snapshot_sidecars(repo, snap.id, manifest)
    assert len(paths) == 1
    assert paths[0].name == "region-topdown.png"
    assert paths[0].is_file()


# ---- CLI smoke tests --------------------------------------------------------

def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "chunkvault", *args],
        capture_output=True, text=True,
    )


def test_cli_thumbnail_single_snap(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_real_chunk(tmp_path)
    snap = repo.snapshot(world, label="cli", verify_roundtrip=False)

    proc = _run_cli("thumbnail", str(repo.repo_path), snap.id)
    assert proc.returncode == 0, proc.stderr
    assert "rendered=" in proc.stdout
    assert "sidecars=" in proc.stdout


def test_cli_thumbnail_all(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world1 = _seed_world_with_real_chunk(tmp_path / "a")
    world2 = _seed_world_with_real_chunk(tmp_path / "b")
    repo.snapshot(world1, label="s1", verify_roundtrip=False)
    repo.snapshot(world2, label="s2", verify_roundtrip=False)

    proc = _run_cli("thumbnail", str(repo.repo_path), "--all")
    assert proc.returncode == 0, proc.stderr
    # Both snapshots' lines + total summary
    assert proc.stdout.count("sidecars=") == 2
    assert "snapshots=2" in proc.stdout


def test_cli_thumbnail_rejects_missing_args(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    proc = _run_cli("thumbnail", str(repo.repo_path))
    assert proc.returncode == 2
    proc = _run_cli("thumbnail", str(repo.repo_path), "abc", "--all")
    assert proc.returncode == 2
