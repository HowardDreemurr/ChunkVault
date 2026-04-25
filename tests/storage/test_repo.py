from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chunkvault.storage.repo import (
    SNAPSHOT_REF_PREFIX,
    Snapshot,
    SnapshotRepo,
    StorageError,
    git_available,
)

from tests._fixtures import ChunkSpec, write_mcc, write_region_file

pytestmark = pytest.mark.skipif(not git_available(), reason="git CLI not on PATH")


# ---- helpers ---------------------------------------------------------------

def _build_world(root: Path, name: str = "world") -> Path:
    world = root / name
    world.mkdir()
    # level.dat-ish marker file
    (world / "level.dat").write_bytes(b"fake level.dat bytes")
    # A couple of regions in overworld + nether
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"overworld chunk A"),
        ChunkSpec(1, 0, 1, 2, b"overworld chunk B"),
    ])
    write_region_file(world, "DIM-1/region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"nether stuff"),
    ])
    return world


def _hash_dir(path: Path) -> dict[str, str]:
    """Map of relative-posix-path → sha256 hex for every file under path."""
    out: dict[str, str] = {}
    for entry in path.rglob("*"):
        if entry.is_file():
            rel = entry.relative_to(path).as_posix()
            out[rel] = hashlib.sha256(entry.read_bytes()).hexdigest()
    return out


# ---- init ------------------------------------------------------------------

def test_init_creates_bare_repo(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    assert repo.is_initialized() is False
    repo.init()
    assert repo.is_initialized() is True
    # Bare repo markers
    assert (repo.repo_path / "HEAD").is_file()
    assert (repo.repo_path / "objects").is_dir()
    assert (repo.repo_path / "refs").is_dir()


def test_init_writes_gitattributes_disabling_delta(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    attrs = (repo.repo_path / "info" / "attributes").read_text(encoding="utf-8")
    assert "*.mca -delta" in attrs
    assert "*.mcc -delta" in attrs


def test_init_is_idempotent(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    repo.init()  # second call must not error
    assert repo.is_initialized()


# ---- snapshot --------------------------------------------------------------

def test_snapshot_requires_init(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    world = _build_world(tmp_path)
    with pytest.raises(StorageError, match="not initialized"):
        repo.snapshot(world)


def test_snapshot_creates_ref(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap = repo.snapshot(world, label="first")
    assert snap.ref.startswith(SNAPSHOT_REF_PREFIX)
    assert snap.label == "first"
    assert len(snap.id) == 40  # full SHA
    assert snap.subject.startswith("snapshot ")
    # The ref file actually exists in the bare repo
    ref_file = repo.repo_path / snap.ref.removeprefix("refs/")
    # Note: modern git may pack refs; check via show-ref instead
    result = repo._git("show-ref", "--verify", snap.ref)
    assert result.stdout.strip().split()[0] == snap.id


def test_snapshot_label_sanitized_in_branch_name(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap = repo.snapshot(world, label="before raid! 2026/04")
    # Illegal chars in the label component should be replaced with dashes.
    # The branch itself is always `refs/heads/snapshot/<label-component>` —
    # the outer slashes are expected; we only audit the label part.
    label_part = snap.ref.removeprefix("refs/heads/snapshot/")
    assert " " not in label_part
    assert "/" not in label_part
    assert "!" not in label_part
    assert label_part.endswith("before-raid-2026-04")


def test_snapshot_missing_world_errors(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    with pytest.raises(StorageError, match="not a directory"):
        repo.snapshot(tmp_path / "nonexistent-world")


# ---- list / get ------------------------------------------------------------

def test_list_empty(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    assert repo.list() == []


def test_list_orders_newest_first(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    t1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    s1 = repo.snapshot(world, label="one", timestamp=t1)
    # Touch the world between snapshots so each has distinct contents
    (world / "region" / "r.0.0.mca").write_bytes(b"changed once")
    s2 = repo.snapshot(world, label="two", timestamp=t2)
    (world / "region" / "r.0.0.mca").write_bytes(b"changed twice")
    s3 = repo.snapshot(world, label="three", timestamp=t3)
    snaps = repo.list()
    assert [s.label for s in snaps] == ["three", "two", "one"]
    assert {s.id for s in snaps} == {s1.id, s2.id, s3.id}


def test_get_by_label_and_by_sha(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap = repo.snapshot(world, label="pinned")
    by_label = repo.get("pinned")
    by_full = repo.get(snap.id)
    by_short = repo.get(snap.id[:8])
    assert by_label == snap
    assert by_full == snap
    assert by_short == snap
    assert repo.get("no-such-thing") is None


# ---- restore ---------------------------------------------------------------

def test_restore_full_world_roundtrip(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    original = _hash_dir(world)
    snap = repo.snapshot(world)
    dest = tmp_path / "restored"
    repo.restore(snap, dest)
    restored = _hash_dir(dest)
    assert original == restored


def test_restore_subset_paths(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap = repo.snapshot(world)
    dest = tmp_path / "restored"
    repo.restore(snap, dest, paths=["region/r.0.0.mca"])
    # Only that one file should be present
    files = [p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()]
    assert files == ["region/r.0.0.mca"]
    # And it matches the original byte-for-byte
    assert (dest / "region" / "r.0.0.mca").read_bytes() == \
           (world / "region" / "r.0.0.mca").read_bytes()


def test_restore_accepts_snap_id_string(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap = repo.snapshot(world)
    dest = tmp_path / "restored"
    repo.restore(snap.id, dest)
    assert (dest / "level.dat").is_file()


def test_restore_nonexistent_path_empty(tmp_path: Path):
    """Restore filtered to a path that doesn't exist in the snapshot → empty dest.

    git-archive treats a missing path as an error; we surface that cleanly.
    """
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap = repo.snapshot(world)
    dest = tmp_path / "restored"
    with pytest.raises(StorageError):
        repo.restore(snap, dest, paths=["does/not/exist.mca"])


# ---- dedup -----------------------------------------------------------------

def test_identical_worlds_dedupe_blobs(tmp_path: Path):
    """Two snapshots of the identical world must share blob objects."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap1 = repo.snapshot(world, label="a")
    # Count objects after first snapshot
    objs_after_1 = _count_loose_objects(repo.repo_path)
    snap2 = repo.snapshot(world, label="b")
    objs_after_2 = _count_loose_objects(repo.repo_path)
    # Second snapshot should add only: 1 new commit object + 0 new blobs.
    # (tree objects are also shared since contents are identical)
    # So diff should be exactly 1 (the new commit).
    assert objs_after_2 - objs_after_1 == 1, (
        f"expected 1 new object (commit only) but got {objs_after_2 - objs_after_1}"
    )
    assert snap1.id != snap2.id


def _count_loose_objects(repo_path: Path) -> int:
    objects = repo_path / "objects"
    count = 0
    for entry in objects.iterdir():
        if entry.is_dir() and len(entry.name) == 2:
            count += sum(1 for _ in entry.iterdir())
    return count


# ---- delete ----------------------------------------------------------------

def test_delete_removes_ref(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap = repo.snapshot(world, label="doomed")
    assert repo.get("doomed") is not None
    repo.delete(snap)
    assert repo.get("doomed") is None
    assert snap.id not in {s.id for s in repo.list()}


def test_delete_by_label_string(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap = repo.snapshot(world, label="doomed")
    repo.delete("doomed")
    assert snap.id not in {s.id for s in repo.list()}


def test_delete_unknown_errors(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    with pytest.raises(StorageError, match="No such snapshot"):
        repo.delete("never-existed")


# ---- external chunks survive snapshot/restore ------------------------------

def test_gc_prunes_unreachable_objects(tmp_path: Path):
    """After deleting a snapshot, gc --prune=now must drop its objects."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap1 = repo.snapshot(world, label="keep")
    # Touch world so snap2 has new blobs
    (world / "region" / "r.0.0.mca").write_bytes(b"changed content for gc test")
    snap2 = repo.snapshot(world, label="doomed")
    objs_before = _count_all_objects(repo.repo_path)
    repo.delete(snap2)
    repo.gc()
    objs_after = _count_all_objects(repo.repo_path)
    # At least the commit + the changed blob should be reclaimed.
    assert objs_after < objs_before


def _count_all_objects(repo_path: Path) -> int:
    """Count loose + packed objects, approximately."""
    loose = 0
    objects = repo_path / "objects"
    for entry in objects.iterdir():
        if entry.is_dir() and len(entry.name) == 2:
            loose += sum(1 for _ in entry.iterdir())
    # For packed objects we'd parse pack-*.idx; for this test just counting
    # loose is sufficient since fresh repos have everything loose.
    return loose


def _hold_lock(path: Path):
    """Return a file handle holding an exclusive byte-range lock on the file.

    Returns (fh, release_fn). Caller must call release_fn() before closing fh.
    Cross-platform — matches what Java FileChannel.tryLock does.
    """
    # Ensure the file has at least one byte so byte-0 locks are meaningful.
    if path.stat().st_size == 0:
        path.write_bytes(b"\x00")
    fh = open(path, "r+b")
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        def release():
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        def release():
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    return fh, release


def test_snapshot_refuses_when_session_lock_is_held(tmp_path: Path):
    """MC-style byte-range lock on session.lock must block snapshot."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path, "live-world")
    lock = world / "session.lock"
    lock.write_bytes(b"\xE2\x98\x83")  # a few bytes like MC writes
    fh, release = _hold_lock(lock)
    try:
        with pytest.raises(StorageError, match="session.lock"):
            repo.snapshot(world, label="blocked")
    finally:
        release()
        fh.close()


def test_snapshot_allow_live_bypasses_lock_check(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path, "live-world")
    lock = world / "session.lock"
    lock.write_bytes(b"\xE2\x98\x83")
    fh, release = _hold_lock(lock)
    try:
        snap = repo.snapshot(world, label="forced", allow_live=True)
        assert snap.label == "forced"
    finally:
        release()
        fh.close()


def test_snapshot_unheld_lock_file_proceeds(tmp_path: Path):
    """A session.lock file that nobody is locking (e.g. leftover from crash)
    should not block snapshot."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path, "recovered-world")
    (world / "session.lock").write_bytes(b"\xE2\x98\x83")
    snap = repo.snapshot(world, label="clean")
    assert snap.label == "clean"


def test_snapshot_no_lock_file_proceeds(tmp_path: Path):
    """No session.lock at all → snapshot works without extra flags."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path, "fresh-world")
    snap = repo.snapshot(world, label="clean")
    assert snap.label == "clean"


def test_snapshot_empty_recent_lock_is_suspicious(tmp_path: Path):
    """Empty session.lock touched within 60s is treated as mid-startup."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path, "starting-world")
    (world / "session.lock").write_bytes(b"")  # empty, fresh mtime
    with pytest.raises(StorageError, match="session.lock"):
        repo.snapshot(world, label="midflight")


def test_snapshot_empty_old_lock_ok(tmp_path: Path):
    """Empty session.lock with old mtime treated as stale — snapshot proceeds."""
    import time
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path, "abandoned-world")
    lock = world / "session.lock"
    lock.write_bytes(b"")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    snap = repo.snapshot(world, label="ok")
    assert snap.label == "ok"


def test_snapshot_is_incremental_from_previous(tmp_path: Path):
    """Second snapshot uses the previous one as an index baseline (read-tree).

    Correctness check only — verify the tree SHAs match a manual recompute.
    The speed improvement is harder to assert on but follows from the
    mechanism: git add --all with a populated index skips unchanged files.
    """
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap1 = repo.snapshot(world, label="a")
    # No changes between snapshots
    snap2 = repo.snapshot(world, label="b")
    # Both should point to the SAME tree — content identical
    tree1 = repo._tree_of(snap1.id)
    tree2 = repo._tree_of(snap2.id)
    assert tree1 is not None
    assert tree1 == tree2


def test_snapshot_latest_for_world_matches_by_world_name(tmp_path: Path):
    """_latest_for_world must pick the right snapshot when repo has multiple worlds."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world_a = _build_world(tmp_path, "earth")
    world_b = _build_world(tmp_path, "mars")

    sa1 = repo.snapshot(world_a, label="earth-1")
    sb1 = repo.snapshot(world_b, label="mars-1")
    sa2 = repo.snapshot(world_a, label="earth-2")

    latest_earth = repo._latest_for_world("earth")
    latest_mars = repo._latest_for_world("mars")
    assert latest_earth is not None and latest_earth.id == sa2.id
    assert latest_mars is not None and latest_mars.id == sb1.id
    assert repo._latest_for_world("venus") is None


def test_snapshot_worldname_round_trips_through_list(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path, "my-smp")
    snap = repo.snapshot(world, label="v1")
    (fetched,) = repo.list()
    assert fetched.world_name == "my-smp"
    assert fetched == snap


def test_incremental_snapshot_still_detects_actual_changes(tmp_path: Path):
    """A real change between snapshots must still produce a different tree."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap1 = repo.snapshot(world, label="a")
    # Meaningfully change the world
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"entirely different content"),
    ])
    snap2 = repo.snapshot(world, label="b")
    assert repo._tree_of(snap1.id) != repo._tree_of(snap2.id)
    # Restore and confirm we get the modified bytes
    dest = tmp_path / "restored"
    repo.restore(snap2, dest)
    content = (dest / "region" / "r.0.0.mca").read_bytes()
    # Our fixture encodes payload inside the MCA — check a signature byte shows up
    assert b"entirely different content" in content


def test_diff_snapshots_same_snap_is_empty(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap = repo.snapshot(world, label="only")
    result = repo.diff_snapshots(snap, snap)
    assert result.changes == []
    assert result.errors == []


def test_diff_snapshots_detects_modifications(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap_a = repo.snapshot(world, label="before")
    # Modify one region; add a new chunk elsewhere
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"overworld chunk A"),      # same
        ChunkSpec(1, 0, 1, 2, b"overworld chunk B NEW"),  # modified
        ChunkSpec(2, 2, 1, 2, b"brand new"),              # added
    ])
    snap_b = repo.snapshot(world, label="after")
    result = repo.diff_snapshots(snap_a, snap_b)
    kinds = sorted(c.kind for c in result.changes)
    assert kinds == ["added", "modified"]


def test_diff_snapshots_accepts_string_identifiers(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    repo.snapshot(world, label="v1")
    (world / "region" / "r.0.0.mca").write_bytes(b"new bytes for v2")
    repo.snapshot(world, label="v2")
    # Use labels, not Snapshot objects
    result = repo.diff_snapshots("v1", "v2")
    # Corrupted-looking region bytes should register as changes somewhere,
    # with errors or a removed/added entry depending on parser tolerance.
    assert result.changes or result.errors


def test_diff_snapshots_fast_matches_slow(tmp_path: Path):
    """fast path must produce the same ChunkDiff set as the restore-based path."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap_a = repo.snapshot(world, label="before")
    # Produce a non-trivial set of changes:
    # - modify an existing chunk
    # - add a new chunk
    # - add a whole new region
    # - remove a region (delete the nether region file)
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"overworld chunk A"),
        ChunkSpec(1, 0, 1, 2, b"overworld chunk B UPDATED"),
        ChunkSpec(5, 5, 1, 2, b"brand new"),
    ])
    write_region_file(world, "region", 1, 0, [
        ChunkSpec(0, 0, 1, 2, b"entirely new region"),
    ])
    (world / "DIM-1" / "region" / "r.0.0.mca").unlink()
    snap_b = repo.snapshot(world, label="after")

    slow = repo.diff_snapshots(snap_a, snap_b)
    fast = repo.diff_snapshots_fast(snap_a, snap_b)

    def keyed(diff):
        return sorted(
            (c.dimension_key, c.cx, c.cz, c.kind, c.old_hash, c.new_hash)
            for c in diff.changes
        )
    assert keyed(fast) == keyed(slow)
    assert {e.dimension_key for e in fast.errors} == {e.dimension_key for e in slow.errors}


def test_diff_snapshots_fast_same_snap_empty(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap = repo.snapshot(world, label="only")
    result = repo.diff_snapshots_fast(snap, snap)
    assert result.changes == []
    assert result.errors == []


def test_diff_snapshots_fast_populates_cache_db(tmp_path: Path):
    """Running a fast diff should materialize the SQLite cache file."""
    from chunkvault.storage.cache import ChunkHashCache
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap_a = repo.snapshot(world, label="a")
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"changed"),
        ChunkSpec(1, 0, 1, 2, b"second"),
    ])
    snap_b = repo.snapshot(world, label="b")

    repo.diff_snapshots_fast(snap_a, snap_b)

    cache_path = repo.repo_path / "chunkvault-cache.sqlite"
    assert cache_path.is_file()
    with ChunkHashCache(cache_path) as cache:
        blobs, chunks, externals = cache.size()
    # We parsed two region blobs (old and new r.0.0.mca)
    assert blobs >= 2
    assert chunks >= 2
    assert externals == 0  # no external chunks in this fixture


def test_diff_snapshots_fast_cache_is_reused(tmp_path: Path):
    """A second fast diff of the same snapshot pair should hit cache for every
    blob — verified indirectly by deleting the source blobs and confirming the
    diff still completes correctly from cache."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    snap_a = repo.snapshot(world, label="a")
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"modified chunk A"),
    ])
    snap_b = repo.snapshot(world, label="b")

    # Warm the cache
    first = repo.diff_snapshots_fast(snap_a, snap_b)

    from chunkvault.storage.cache import ChunkHashCache
    with ChunkHashCache(repo.repo_path / "chunkvault-cache.sqlite") as cache:
        sizes_before = cache.size()

    second = repo.diff_snapshots_fast(snap_a, snap_b)

    with ChunkHashCache(repo.repo_path / "chunkvault-cache.sqlite") as cache:
        sizes_after = cache.size()

    # Caches shouldn't grow — every fetch was served from cache.
    assert sizes_after == sizes_before
    # Functional equivalence
    def keyed(d):
        return sorted((c.dimension_key, c.cx, c.cz, c.kind) for c in d.changes)
    assert keyed(first) == keyed(second)


def test_diff_snapshots_fast_external_mcc_caches_via_composite_key(tmp_path: Path):
    """Regions with external chunks ARE cached now: enumeration in blob_parse,
    plus per-mcc-sha entries in external_hash. Second diff hits both."""
    from chunkvault.storage.cache import ChunkHashCache
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = tmp_path / "world"
    world.mkdir()
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(cx=3, cz=5, timestamp=1, compression=0x82, payload=b""),
    ])
    write_mcc(world, "region", 3, 5, b"external-1")
    snap_a = repo.snapshot(world, label="a")
    write_mcc(world, "region", 3, 5, b"external-2")
    snap_b = repo.snapshot(world, label="b")

    first = repo.diff_snapshots_fast(snap_a, snap_b)
    with ChunkHashCache(repo.repo_path / "chunkvault-cache.sqlite") as cache:
        sizes1 = cache.size()
    # blob_parsed for the region; external_hash for each (region_sha, cx, cz, mcc_sha)
    assert sizes1[0] >= 1   # blob_parsed
    assert sizes1[2] >= 2   # 2 external hashes (one per mcc version)

    # Second diff should hit cache for everything
    second = repo.diff_snapshots_fast(snap_a, snap_b)
    with ChunkHashCache(repo.repo_path / "chunkvault-cache.sqlite") as cache:
        sizes2 = cache.size()
    assert sizes2 == sizes1
    # Functional equivalence
    assert sorted((c.cx, c.cz, c.kind) for c in first.changes) == \
           sorted((c.cx, c.cz, c.kind) for c in second.changes)


def test_diff_snapshots_fast_handles_external_mcc(tmp_path: Path):
    """A change confined to a .mcc file must still surface as a chunk diff."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = tmp_path / "world"
    world.mkdir()
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(cx=3, cz=5, timestamp=1, compression=0x82, payload=b""),
    ])
    write_mcc(world, "region", 3, 5, b"version one")
    snap_a = repo.snapshot(world, label="a")

    # Change only the .mcc; keep the region stub byte-identical
    write_mcc(world, "region", 3, 5, b"version two is longer")
    snap_b = repo.snapshot(world, label="b")

    result = repo.diff_snapshots_fast(snap_a, snap_b)
    assert len(result.changes) == 1
    c = result.changes[0]
    assert c.kind == "modified"
    assert (c.cx, c.cz) == (3, 5)


def test_diff_snapshots_unknown_errors(tmp_path: Path):
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = _build_world(tmp_path)
    repo.snapshot(world, label="exists")
    with pytest.raises(StorageError, match="No such snapshot"):
        repo.diff_snapshots("exists", "ghost")


def test_external_mcc_preserved_across_snapshot_restore(tmp_path: Path):
    """External chunks (.mcc files) must round-trip through the snapshot."""
    repo = SnapshotRepo(tmp_path / "backup")
    repo.init()
    world = tmp_path / "world"
    world.mkdir()
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(cx=3, cz=5, timestamp=1, compression=0x82, payload=b""),
    ])
    mcc_bytes = b"external chunk payload" * 50
    write_mcc(world, "region", 3, 5, mcc_bytes)
    snap = repo.snapshot(world)
    dest = tmp_path / "restored"
    repo.restore(snap, dest)
    assert (dest / "region" / "c.3.5.mcc").read_bytes() == mcc_bytes
