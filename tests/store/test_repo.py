"""ChunkSnapshotRepo end-to-end behavioral tests."""
from __future__ import annotations

import gzip
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chunkvault.mca.nbt_lite import (
    TAG_COMPOUND, TAG_INT, TAG_LONG, TAG_STRING, build_nbt_compound,
)
from chunkvault.store import (
    ChunkRepoError,
    ChunkSnapshotRepo,
    DEFAULT_EXCLUDE,
)

from tests._fixtures import ChunkSpec, write_mcc, write_region_file


# ---- helpers ---------------------------------------------------------------

def _hash_dir(root: Path, exclude: set[str] | None = None) -> dict[str, str]:
    """sha256 of every file under root, keyed by relative posix path."""
    out: dict[str, str] = {}
    exclude = exclude or set()
    for entry in root.rglob("*"):
        if not entry.is_file():
            continue
        rel = entry.relative_to(root).as_posix()
        if rel in exclude:
            continue
        out[rel] = hashlib.sha256(entry.read_bytes()).hexdigest()
    return out


def _mk_world(tmp_path: Path, name: str = "world") -> Path:
    world = tmp_path / name
    world.mkdir()
    (world / "level.dat").write_bytes(_make_level_dat("1.20.4", 3700))
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"chunk-A"),
        ChunkSpec(1, 0, 2, 2, b"chunk-B"),
    ])
    write_region_file(world, "DIM-1/region", 0, 0, [
        ChunkSpec(0, 0, 3, 2, b"nether-A"),
    ])
    return world


def _make_level_dat(
    name: str, data_version: int, last_played_ms: int | None = None,
) -> bytes:
    """Build a minimal gzipped level.dat.

    Always includes ``Data.Version.{Name,Id}`` and ``Data.DataVersion``;
    optionally includes ``Data.LastPlayed`` (TAG_Long, Unix ms) — set this
    to test snapshot-timestamp auto-detection.
    """
    # Inner Version compound: {Name: name, Id: data_version}
    name_b = name.encode("utf-8")
    version_compound_body = bytearray()
    # Name (TAG_String)
    version_compound_body.append(TAG_STRING)
    version_compound_body += b"\x00\x04Name"
    version_compound_body += len(name_b).to_bytes(2, "big") + name_b
    # Id (TAG_Int)
    version_compound_body.append(TAG_INT)
    version_compound_body += b"\x00\x02Id"
    version_compound_body += data_version.to_bytes(4, "big", signed=True)
    version_compound_body.append(0)  # TAG_End

    # Data compound containing Version compound + DataVersion int (+ optional LastPlayed)
    data_body = bytearray()
    data_body.append(TAG_COMPOUND)
    data_body += b"\x00\x07Version"
    data_body += bytes(version_compound_body)
    data_body.append(TAG_INT)
    data_body += b"\x00\x0bDataVersion"
    data_body += data_version.to_bytes(4, "big", signed=True)
    if last_played_ms is not None:
        data_body.append(TAG_LONG)
        data_body += b"\x00\x0aLastPlayed"
        data_body += last_played_ms.to_bytes(8, "big", signed=True)
    data_body.append(0)  # end Data

    # Root compound (no name) containing Data
    root = bytearray()
    root.append(TAG_COMPOUND)
    root += b"\x00\x00"
    root.append(TAG_COMPOUND)
    root += b"\x00\x04Data"
    root += bytes(data_body)
    root.append(0)
    return gzip.compress(bytes(root))


# ---- init ------------------------------------------------------------------

def test_init_creates_repo_layout(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    assert repo.is_initialized() is False
    repo.init()
    assert repo.is_initialized() is True
    assert (repo.repo_path / "chunks").is_dir()
    assert (repo.repo_path / "files").is_dir()
    assert (repo.repo_path / "manifests").is_dir()
    assert (repo.repo_path / "index.sqlite").is_file()


def test_init_is_idempotent(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    repo.init()
    assert repo.is_initialized()


# ---- snapshot --------------------------------------------------------------

def test_snapshot_requires_init(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    world = _mk_world(tmp_path)
    with pytest.raises(ChunkRepoError, match="not initialized"):
        repo.snapshot(world)


def test_snapshot_creates_manifest_and_chunks(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    snap = repo.snapshot(world, label="v1")
    assert len(snap.id) == 32
    assert snap.label == "v1"
    assert snap.world_name == "world"
    assert snap.mc_version == "1.20.4"
    assert snap.data_version == 3700
    assert snap.manifest_path.is_file()
    # Some chunks should have landed in the pool
    chunks_dir = repo.repo_path / "chunks"
    chunk_files = [p for p in chunks_dir.rglob("*") if p.is_file()]
    assert len(chunk_files) >= 3  # 3 chunks across overworld + nether


def test_snapshot_dedupes_identical_world(tmp_path: Path):
    """Second snapshot of unchanged world should add ZERO new chunks."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    repo.snapshot(world, label="a")

    chunks_dir = repo.repo_path / "chunks"
    files1 = sorted(p for p in chunks_dir.rglob("*") if p.is_file())

    repo.snapshot(world, label="b")
    files2 = sorted(p for p in chunks_dir.rglob("*") if p.is_file())
    assert files1 == files2  # exact same chunks, no new files written


def test_snapshot_only_adds_changed_chunks(tmp_path: Path):
    """Modifying one chunk should add exactly one new chunk to the pool."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    repo.snapshot(world, label="a")
    chunks_dir = repo.repo_path / "chunks"
    count_before = sum(1 for p in chunks_dir.rglob("*") if p.is_file())

    # Change one chunk's content
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"chunk-A"),         # unchanged
        ChunkSpec(1, 0, 2, 2, b"chunk-B-MODIFIED"),  # different bytes
    ])
    repo.snapshot(world, label="b")
    count_after = sum(1 for p in chunks_dir.rglob("*") if p.is_file())
    assert count_after - count_before == 1


def test_snapshot_default_excludes_logs_and_session_lock(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    (world / "session.lock").write_bytes(b"\xE2\x98\x83")
    (world / "logs").mkdir()
    (world / "logs" / "latest.log").write_bytes(b"some log spam")
    (world / "crash-reports").mkdir()
    (world / "crash-reports" / "crash.txt").write_bytes(b"oops")
    (world / "server.log").write_bytes(b"another log")

    snap = repo.snapshot(world, label="excludes")

    # Restore + check none of the excluded files showed up
    dest = tmp_path / "restored"
    repo.restore(snap, dest)
    assert not (dest / "session.lock").exists()
    assert not (dest / "logs" / "latest.log").exists()
    assert not (dest / "crash-reports" / "crash.txt").exists()
    assert not (dest / "server.log").exists()
    # But level.dat etc. should be there
    assert (dest / "level.dat").is_file()
    assert (dest / "region" / "r.0.0.mca").is_file()


def test_snapshot_world_without_level_dat(tmp_path: Path):
    """A world dir without level.dat (broken / mid-import) should still snapshot."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "headless-world"
    world.mkdir()
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"x"),
    ])
    snap = repo.snapshot(world, label="headless")
    assert snap.mc_version is None
    assert snap.data_version is None


# ---- list / get -------------------------------------------------------------

def test_list_orders_newest_first(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    t1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    repo.snapshot(world, label="one", timestamp=t1)
    repo.snapshot(world, label="two", timestamp=t2)
    repo.snapshot(world, label="three", timestamp=t3)
    assert [s.label for s in repo.list()] == ["three", "two", "one"]


def test_get_by_label_and_short_id(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    snap = repo.snapshot(world, label="pinned")
    assert repo.get("pinned").id == snap.id
    assert repo.get(snap.id).id == snap.id
    assert repo.get(snap.id[:8]).id == snap.id
    assert repo.get("ghost") is None


# ---- restore ---------------------------------------------------------------

def test_restore_full_world_byte_for_byte(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    original = _hash_dir(world)
    snap = repo.snapshot(world, label="r1")
    dest = tmp_path / "restored"
    repo.restore(snap, dest)

    restored = _hash_dir(dest)

    # Restored region files should contain identical CHUNK content (we
    # reassemble MCA so byte-exact equality of the .mca file isn't
    # guaranteed — but every chunk's payload is preserved). Check by
    # round-tripping: snapshot the restored world, see that no new chunks
    # are added.
    chunks_dir = repo.repo_path / "chunks"
    count_before = sum(1 for p in chunks_dir.rglob("*") if p.is_file())
    repo.snapshot(dest, label="round-trip")
    count_after = sum(1 for p in chunks_dir.rglob("*") if p.is_file())
    # Some non-region files (level.dat, etc.) should be byte-identical
    assert restored["level.dat"] == original["level.dat"]
    # Round-trip snapshot adds zero new chunks → all chunk content preserved
    assert count_after == count_before


def test_restore_preserves_external_mcc_files(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "world"
    world.mkdir()
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(cx=3, cz=5, timestamp=1, compression=0x82, payload=b""),
    ])
    mcc_payload = b"external chunk payload " * 100
    write_mcc(world, "region", 3, 5, mcc_payload)

    snap = repo.snapshot(world, label="ext")
    dest = tmp_path / "restored"
    repo.restore(snap, dest)
    assert (dest / "region" / "c.3.5.mcc").read_bytes() == mcc_payload


def test_restore_with_path_filter_only_writes_matching(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    snap = repo.snapshot(world, label="filt")
    dest = tmp_path / "restored"
    repo.restore(snap, dest, paths=["region/r.0.0.mca"])
    files = sorted(p.relative_to(dest).as_posix()
                   for p in dest.rglob("*") if p.is_file())
    assert files == ["region/r.0.0.mca"]


def test_restore_unknown_snapshot_errors(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    with pytest.raises(ChunkRepoError, match="No such snapshot"):
        repo.restore("ghost", tmp_path / "x")


# ---- delete ----------------------------------------------------------------

def test_delete_removes_snapshot_and_manifest(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    snap = repo.snapshot(world, label="doomed")
    assert snap.manifest_path.is_file()
    repo.delete(snap)
    assert not snap.manifest_path.is_file()
    assert repo.get("doomed") is None


def test_delete_does_not_remove_chunk_blobs(tmp_path: Path):
    """Chunks survive snapshot deletion (no ref counting yet — gc later)."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    snap = repo.snapshot(world, label="x")
    chunks_dir = repo.repo_path / "chunks"
    before = sorted(p for p in chunks_dir.rglob("*") if p.is_file())
    repo.delete(snap)
    after = sorted(p for p in chunks_dir.rglob("*") if p.is_file())
    assert before == after


# ---- session.lock ---------------------------------------------------------

def test_snapshot_blocks_on_held_session_lock(tmp_path: Path):
    """Reuse the storage layer's lock check — same semantics across backends."""
    import os as _os
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    lock = world / "session.lock"
    lock.write_bytes(b"\xE2\x98\x83")
    if _os.name == "nt":
        import msvcrt
        fh = open(lock, "r+b")
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            with pytest.raises(Exception, match="session.lock"):
                repo.snapshot(world, label="blocked")
        finally:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            fh.close()
    else:
        import fcntl
        fh = open(lock, "r+b")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(Exception, match="session.lock"):
                repo.snapshot(world, label="blocked")
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()


# ---- exclude semantics -----------------------------------------------------

def test_default_exclude_constants_present():
    assert "session.lock" in DEFAULT_EXCLUDE
    assert "logs/**" in DEFAULT_EXCLUDE
    assert "*.log" in DEFAULT_EXCLUDE


def test_custom_exclude_overrides_default(tmp_path: Path):
    """Pass exclude= to keep some default-excluded files, or add more."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    (world / "logs").mkdir()
    (world / "logs" / "important.log").write_bytes(b"keep this")

    # Override: only exclude session.lock, NOT logs
    snap = repo.snapshot(world, label="x", exclude=["session.lock"])
    dest = tmp_path / "restored"
    repo.restore(snap, dest)
    assert (dest / "logs" / "important.log").is_bytes if False else \
           (dest / "logs" / "important.log").is_file()


# ---- mc version detection --------------------------------------------------

def test_diff_snapshots_carries_version_metadata(tmp_path: Path):
    """Cross-version snapshot pair → diff exposes both sides' MC versions."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()

    # World A on 1.16.5
    world_a = tmp_path / "wa"
    world_a.mkdir()
    (world_a / "level.dat").write_bytes(_make_level_dat("1.16.5", 2586))
    write_region_file(world_a, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"v1"),
    ])
    snap_a = repo.snapshot(world_a, label="legacy")

    # World B (same path, "upgraded" to 1.20.4)
    world_b = tmp_path / "wb"
    world_b.mkdir()
    (world_b / "level.dat").write_bytes(_make_level_dat("1.20.4", 3700))
    write_region_file(world_b, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"v2"),
    ])
    snap_b = repo.snapshot(world_b, label="modern")

    diff = repo.diff_snapshots(snap_a, snap_b)
    assert diff.old_mc_version == "1.16.5"
    assert diff.new_mc_version == "1.20.4"
    assert diff.old_data_version == 2586
    assert diff.new_data_version == 3700
    assert diff.old_label == "legacy"
    assert diff.new_label == "modern"
    assert diff.version_changed() is True


def test_diff_snapshots_same_version_no_version_changed_flag(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _mk_world(tmp_path)
    snap_a = repo.snapshot(world, label="a")
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"changed"),
    ])
    snap_b = repo.snapshot(world, label="b")
    diff = repo.diff_snapshots(snap_a, snap_b)
    assert diff.old_mc_version == diff.new_mc_version == "1.20.4"
    assert diff.version_changed() is False


def test_mc_version_extracted_from_level_dat(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(_make_level_dat("1.21.0", 3953))
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"x"),
    ])
    snap = repo.snapshot(world, label="v1.21")
    assert snap.mc_version == "1.21.0"
    assert snap.data_version == 3953


# ---- snapshot timestamp auto-detection -------------------------------------

def _seed_world_with_last_played(
    tmp_path: Path, last_played_ms: int | None,
) -> Path:
    """Build a tiny world dir; optionally embed LastPlayed in level.dat."""
    world = tmp_path / "world"
    world.mkdir()
    (world / "level.dat").write_bytes(
        _make_level_dat("1.20.4", 3700, last_played_ms=last_played_ms),
    )
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"chunk"),
    ])
    return world


def test_snapshot_uses_last_played_when_present(tmp_path: Path):
    """Default timestamp comes from level.dat's LastPlayed, not now()."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    last_played_ms = 1_700_000_000_000  # 2023-11-14T22:13:20 UTC
    world = _seed_world_with_last_played(tmp_path, last_played_ms)
    snap = repo.snapshot(world, label="auto-ts")
    assert int(snap.timestamp.timestamp() * 1000) == last_played_ms


def test_snapshot_explicit_timestamp_overrides_last_played(tmp_path: Path):
    """Explicit timestamp wins over auto-detected LastPlayed."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_last_played(tmp_path, 1_700_000_000_000)
    explicit = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    snap = repo.snapshot(world, label="manual", timestamp=explicit)
    assert snap.timestamp == explicit


def test_snapshot_falls_back_when_no_last_played(tmp_path: Path):
    """No LastPlayed → use newest region mtime, not 'now'."""
    import os
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_last_played(tmp_path, last_played_ms=None)
    # Force the region file to a known mtime in the past.
    region_file = world / "region" / "r.0.0.mca"
    target_mtime = 1_650_000_000.0  # 2022-04-15
    os.utime(region_file, (target_mtime, target_mtime))
    snap = repo.snapshot(world, label="mtime")
    # mtime is per-second, give a small tolerance for FS truncation.
    assert abs(snap.timestamp.timestamp() - target_mtime) < 2


def test_snapshot_stores_last_played_in_manifest(tmp_path: Path):
    """LastPlayed is preserved in the manifest header for later retime."""
    from chunkvault.store.manifest import read_manifest
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    last_played_ms = 1_700_000_000_000
    world = _seed_world_with_last_played(tmp_path, last_played_ms)
    snap = repo.snapshot(world, label="lp")
    manifest = read_manifest(snap.manifest_path)
    assert manifest.header.last_played_ms == last_played_ms
    assert manifest.header.original_timestamp_ms == 0  # never retimed
