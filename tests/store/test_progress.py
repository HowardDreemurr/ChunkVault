"""Progress callback hook tests for snapshot()."""
from __future__ import annotations

from pathlib import Path

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.store.progress import ProgressEvent

from tests._fixtures import ChunkSpec, write_region_file


def _world(root: Path) -> Path:
    world = root / "w"
    world.mkdir()
    (world / "level.dat").write_bytes(b"placeholder")
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"a"),
    ])
    write_region_file(world, "region", 1, 0, [
        ChunkSpec(0, 0, 1, 2, b"b"),
    ])
    return world


def test_snapshot_emits_phase_events(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)

    events: list[ProgressEvent] = []
    repo.snapshot(world, label="probe", progress_cb=events.append)

    kinds = [e.kind for e in events]
    # Should at least have: phase_start regions, phase_progress regions×N,
    # phase_done regions, phase_start files, ..., phase_done files, finish.
    assert "phase_start" in kinds
    assert "phase_progress" in kinds
    assert "phase_done" in kinds
    assert kinds[-1] == "finish"


def test_progress_total_matches_actual_regions(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    events: list[ProgressEvent] = []
    repo.snapshot(world, progress_cb=events.append)
    region_starts = [e for e in events
                     if e.kind == "phase_start" and e.phase == "regions"]
    assert len(region_starts) == 1
    assert region_starts[0].total == 2  # we created 2 region files


def test_finish_event_contains_summary(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    events: list[ProgressEvent] = []
    # Disable verify so the LAST event is the snapshot's finish, not the
    # round-trip's. We cover the verify event sequence in test_roundtrip.
    repo.snapshot(world, label="x", progress_cb=events.append,
                  verify_roundtrip=False)
    finish = events[-1]
    assert finish.kind == "finish"
    assert finish.detail["new_chunks"] >= 1
    assert finish.detail["chunk_count"] >= 2
    assert "snapshot_id" in finish.detail


def test_callback_none_is_safe_noop(tmp_path: Path):
    """Default progress_cb=None must add no overhead and never raise."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    repo.snapshot(world, progress_cb=None)


def test_misbehaving_callback_does_not_break_snapshot(tmp_path: Path):
    """A callback that raises must not corrupt the snapshot — the repo's
    integrity is more important than UI."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)

    def crashy(_e):
        raise RuntimeError("UI is on fire")

    snap = repo.snapshot(world, label="resilient", progress_cb=crashy)
    assert snap.label == "resilient"
    assert snap.manifest_path.is_file()


def test_world_name_override(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _world(tmp_path)
    snap = repo.snapshot(world, label="x", world_name="EX-Server")
    assert snap.world_name == "EX-Server"
    # And it shows up in subsequent list / world-keyed lookups
    listed = repo.list()
    assert any(s.world_name == "EX-Server" for s in listed)
