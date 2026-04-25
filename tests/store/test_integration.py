"""End-to-end chunk-store integration tests at non-trivial scale.

These tests build a sequence of evolving worlds, snapshot them, then verify:

* dedup works — pool growth equals exactly the number of new chunks each
  snapshot introduces;
* every snapshot's restore reproduces its world's chunk content;
* diff_snapshots agrees with byte-level diff_worlds on the restored output.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chunkvault.diff import diff_worlds
from chunkvault.store import ChunkSnapshotRepo

from tests._fixtures import ChunkSpec, write_mcc, write_region_file


def _count_chunk_blobs(repo: ChunkSnapshotRepo) -> int:
    return sum(1 for p in (repo.repo_path / "chunks").rglob("*") if p.is_file())


def _count_file_blobs(repo: ChunkSnapshotRepo) -> int:
    return sum(1 for p in (repo.repo_path / "files").rglob("*") if p.is_file())


# ---- dedup correctness across multiple incremental snapshots ---------------

def test_five_evolving_snapshots_dedupe_correctly(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()

    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(b"placeholder level.dat")

    # Initial state: 4 chunks
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"static-A"),
        ChunkSpec(1, 0, 2, 2, b"static-B"),
        ChunkSpec(2, 0, 3, 2, b"changing-1"),
        ChunkSpec(3, 0, 4, 2, b"churn"),
    ])
    snap1 = repo.snapshot(world, label="s1")
    chunks_after_1 = _count_chunk_blobs(repo)
    # 4 chunk blobs + 0 file blobs (level.dat is a file blob)
    assert chunks_after_1 == 4

    # Snap 2: change one chunk only
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"static-A"),
        ChunkSpec(1, 0, 2, 2, b"static-B"),
        ChunkSpec(2, 0, 3, 2, b"changing-2"),  # new content
        ChunkSpec(3, 0, 4, 2, b"churn"),
    ])
    snap2 = repo.snapshot(world, label="s2")
    chunks_after_2 = _count_chunk_blobs(repo)
    assert chunks_after_2 == chunks_after_1 + 1

    # Snap 3: change the same chunk back to the original (dedup hit)
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"static-A"),
        ChunkSpec(1, 0, 2, 2, b"static-B"),
        ChunkSpec(2, 0, 3, 2, b"changing-1"),  # back to s1's content
        ChunkSpec(3, 0, 4, 2, b"churn"),
    ])
    snap3 = repo.snapshot(world, label="s3")
    chunks_after_3 = _count_chunk_blobs(repo)
    # No new chunks — we've seen all of s3's content already
    assert chunks_after_3 == chunks_after_2

    # Snap 4: add a brand new chunk
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"static-A"),
        ChunkSpec(1, 0, 2, 2, b"static-B"),
        ChunkSpec(2, 0, 3, 2, b"changing-1"),
        ChunkSpec(3, 0, 4, 2, b"churn"),
        ChunkSpec(10, 10, 5, 2, b"explored-new"),
    ])
    snap4 = repo.snapshot(world, label="s4")
    chunks_after_4 = _count_chunk_blobs(repo)
    assert chunks_after_4 == chunks_after_3 + 1

    # Snap 5: remove a chunk (still no new blobs to store)
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"static-A"),
        ChunkSpec(1, 0, 2, 2, b"static-B"),
        ChunkSpec(2, 0, 3, 2, b"changing-1"),
        ChunkSpec(10, 10, 5, 2, b"explored-new"),
    ])
    snap5 = repo.snapshot(world, label="s5")
    chunks_after_5 = _count_chunk_blobs(repo)
    assert chunks_after_5 == chunks_after_4

    # All snapshots should round-trip
    for snap in (snap1, snap2, snap3, snap4, snap5):
        dest = tmp_path / f"restore-{snap.label}"
        repo.restore(snap, dest)
        # Re-snapshotting the restored world adds nothing new (proves chunk
        # content is preserved end-to-end)
        before = _count_chunk_blobs(repo)
        repo.snapshot(dest, label=f"verify-{snap.label}")
        after = _count_chunk_blobs(repo)
        assert before == after, (
            f"restore of {snap.label} did not round-trip cleanly"
        )


# ---- diff_snapshots agrees with diff_worlds on restored output -------------

def test_diff_snapshots_matches_diff_worlds(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "world"
    world.mkdir()
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"v1-a"),
        ChunkSpec(1, 0, 1, 2, b"v1-b"),
        ChunkSpec(2, 2, 1, 2, b"v1-c"),
    ])
    write_region_file(world, "DIM-1/region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"nether-1"),
    ])
    snap_a = repo.snapshot(world, label="a")

    # Modify, add, and remove chunks across snaps
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"v1-a"),       # unchanged
        ChunkSpec(1, 0, 1, 2, b"v2-b"),       # modified
        ChunkSpec(7, 7, 1, 2, b"v2-new"),     # added (replaces removed v1-c)
    ])
    snap_b = repo.snapshot(world, label="b")

    # Manifest-only diff
    fast_diff = repo.diff_snapshots(snap_a, snap_b)

    # Restore both into temp dirs and run a byte-level diff for comparison
    rest_a = tmp_path / "rest-a"
    rest_b = tmp_path / "rest-b"
    repo.restore(snap_a, rest_a)
    repo.restore(snap_b, rest_b)
    bytes_diff = diff_worlds(rest_a, rest_b)

    def keyed(d):
        return sorted(
            (c.dimension_key, c.cx, c.cz, c.kind) for c in d.changes
        )
    assert keyed(fast_diff) == keyed(bytes_diff)


# ---- file dedup ------------------------------------------------------------

def test_non_region_files_dedupe_across_snapshots(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(b"unchanging level dat content")
    (world / "datapacks").mkdir()
    (world / "datapacks" / "pack.zip").write_bytes(b"zip data " * 1000)
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"x"),
    ])

    repo.snapshot(world, label="a")
    files_after_1 = _count_file_blobs(repo)
    assert files_after_1 == 2  # level.dat + pack.zip

    repo.snapshot(world, label="b")  # nothing changed
    files_after_2 = _count_file_blobs(repo)
    assert files_after_2 == files_after_1


# ---- gc reclaims unreachable chunks ----------------------------------------

def test_gc_reclaims_only_unreachable(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "world"
    world.mkdir()
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"keep-me-1"),
        ChunkSpec(1, 0, 1, 2, b"keep-me-2"),
    ])
    snap_keep = repo.snapshot(world, label="keep")
    chunks_kept_count = _count_chunk_blobs(repo)
    assert chunks_kept_count == 2

    # Create a snapshot with extra unique chunks, then delete it
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"keep-me-1"),
        ChunkSpec(1, 0, 1, 2, b"keep-me-2"),
        ChunkSpec(5, 5, 1, 2, b"doomed-unique-1"),
        ChunkSpec(6, 5, 1, 2, b"doomed-unique-2"),
    ])
    snap_doomed = repo.snapshot(world, label="doomed")
    assert _count_chunk_blobs(repo) == 4
    repo.delete(snap_doomed)
    # Pre-gc: doomed chunks still on disk
    assert _count_chunk_blobs(repo) == 4

    removed_chunks, removed_files = repo.gc()
    assert removed_chunks == 2
    assert _count_chunk_blobs(repo) == 2
    # The "keep" snapshot still restores fine after gc
    dest = tmp_path / "after-gc"
    repo.restore(snap_keep, dest)
    assert (dest / "region" / "r.0.0.mca").is_file()


# ---- external chunk roundtrip across many snapshots ------------------------

def test_external_chunks_roundtrip_across_evolution(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "world"
    world.mkdir()
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(cx=3, cz=5, timestamp=1, compression=0x82, payload=b""),
    ])
    write_mcc(world, "region", 3, 5, b"v1 of mcc")
    snap1 = repo.snapshot(world, label="ext-1")

    # mcc changes only
    write_mcc(world, "region", 3, 5, b"v2 of mcc with more bytes")
    snap2 = repo.snapshot(world, label="ext-2")

    # Restore each and verify mcc bytes survive
    rest1 = tmp_path / "rest-1"
    rest2 = tmp_path / "rest-2"
    repo.restore(snap1, rest1)
    repo.restore(snap2, rest2)
    assert (rest1 / "region" / "c.3.5.mcc").read_bytes() == b"v1 of mcc"
    assert (rest2 / "region" / "c.3.5.mcc").read_bytes() == b"v2 of mcc with more bytes"

    # diff between them captures the change
    diff = repo.diff_snapshots(snap1, snap2)
    assert len(diff.changes) == 1
    assert diff.changes[0].kind == "modified"
    assert (diff.changes[0].cx, diff.changes[0].cz) == (3, 5)
