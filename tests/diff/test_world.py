from __future__ import annotations

from pathlib import Path

from chunkvault.diff import diff_worlds
from chunkvault.diff.world import ChunkDiff, RegionError

from tests._fixtures import ChunkSpec, write_mcc, write_region_file


# Default "overworld" dimension key
OW = "region"


def _world(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.mkdir()
    return p


# --- identity -----------------------------------------------------------------

def test_identical_worlds_yield_no_changes(tmp_path: Path):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    for root in (a, b):
        write_region_file(root, OW, 0, 0, [
            ChunkSpec(cx=0, cz=0, timestamp=1, compression=2, payload=b"same"),
            ChunkSpec(cx=1, cz=0, timestamp=2, compression=2, payload=b"same2"),
        ])
    result = diff_worlds(a, b)
    assert result.changes == []
    assert result.errors == []
    assert result.count_by_kind() == {"added": 0, "removed": 0, "modified": 0}


def test_empty_worlds(tmp_path: Path):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    result = diff_worlds(a, b)
    assert result.changes == []
    assert result.errors == []


# --- basic change classes ------------------------------------------------------

def test_added_chunk(tmp_path: Path):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    write_region_file(a, OW, 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"same"),
    ])
    write_region_file(b, OW, 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"same"),
        ChunkSpec(5, 5, 2, 2, b"new"),
    ])
    result = diff_worlds(a, b)
    assert len(result.changes) == 1
    c = result.changes[0]
    assert c.kind == "added"
    assert (c.cx, c.cz) == (5, 5)
    assert c.old_hash is None
    assert c.new_hash is not None


def test_removed_chunk(tmp_path: Path):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    write_region_file(a, OW, 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"keep"),
        ChunkSpec(7, 7, 1, 2, b"gone"),
    ])
    write_region_file(b, OW, 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"keep"),
    ])
    result = diff_worlds(a, b)
    assert len(result.changes) == 1
    c = result.changes[0]
    assert c.kind == "removed"
    assert (c.cx, c.cz) == (7, 7)
    assert c.old_hash is not None
    assert c.new_hash is None


def test_modified_chunk(tmp_path: Path):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    write_region_file(a, OW, 0, 0, [
        ChunkSpec(3, 4, 1, 2, b"v1"),
    ])
    write_region_file(b, OW, 0, 0, [
        ChunkSpec(3, 4, 1, 2, b"v2"),
    ])
    result = diff_worlds(a, b)
    assert len(result.changes) == 1
    c = result.changes[0]
    assert c.kind == "modified"
    assert (c.cx, c.cz) == (3, 4)
    assert c.old_hash != c.new_hash
    assert c.old_hash is not None and c.new_hash is not None


# --- region / dimension presence asymmetry ------------------------------------

def test_region_only_in_new_world_all_chunks_added(tmp_path: Path):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    (a / OW).mkdir()                    # exists but empty
    write_region_file(b, OW, 2, 3, [
        ChunkSpec(0, 0, 1, 2, b"x"),
        ChunkSpec(31, 31, 1, 2, b"y"),
    ])
    result = diff_worlds(a, b)
    assert {c.kind for c in result.changes} == {"added"}
    # World-space coords: rx=2, rz=3 → (2*32+0, 3*32+0) and (2*32+31, 3*32+31)
    coords = sorted((c.cx, c.cz) for c in result.changes)
    assert coords == [(64, 96), (95, 127)]


def test_region_only_in_old_world_all_chunks_removed(tmp_path: Path):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    write_region_file(a, OW, -1, -1, [
        ChunkSpec(10, 10, 1, 2, b"x"),
    ])
    (b / OW).mkdir()
    result = diff_worlds(a, b)
    assert len(result.changes) == 1
    c = result.changes[0]
    assert c.kind == "removed"
    assert (c.cx, c.cz) == (-1 * 32 + 10, -1 * 32 + 10)


def test_dimension_only_on_one_side(tmp_path: Path):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    # Only 'b' has nether
    write_region_file(b, "DIM-1/region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"netherstuff"),
    ])
    result = diff_worlds(a, b)
    assert len(result.changes) == 1
    c = result.changes[0]
    assert c.dimension_key == "DIM-1/region"
    assert c.kind == "added"


# --- multi-dimension ----------------------------------------------------------

def test_multi_dimension_world(tmp_path: Path):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    # overworld: unchanged
    write_region_file(a, OW, 0, 0, [ChunkSpec(0, 0, 1, 2, b"same")])
    write_region_file(b, OW, 0, 0, [ChunkSpec(0, 0, 1, 2, b"same")])
    # nether: modified
    write_region_file(a, "DIM-1/region", 0, 0, [ChunkSpec(1, 1, 1, 2, b"old")])
    write_region_file(b, "DIM-1/region", 0, 0, [ChunkSpec(1, 1, 1, 2, b"new")])
    # end: added only in b
    write_region_file(b, "DIM1/region", 0, 0, [ChunkSpec(0, 0, 1, 2, b"enderstuff")])

    result = diff_worlds(a, b)
    by_dim = result.by_dimension()
    assert set(by_dim) == {"DIM-1/region", "DIM1/region"}
    assert [c.kind for c in by_dim["DIM-1/region"]] == ["modified"]
    assert [c.kind for c in by_dim["DIM1/region"]] == ["added"]


# --- external .mcc chunks -----------------------------------------------------

def test_external_chunk_mcc_change_detected(tmp_path: Path):
    """If the .mca stub is identical but the .mcc contents differ, we must detect it."""
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    # Both sides: same external-stub region
    for root in (a, b):
        write_region_file(root, OW, 0, 0, [
            ChunkSpec(cx=3, cz=5, timestamp=1, compression=0x82, payload=b""),
        ])
    # Different .mcc payloads
    write_mcc(a, OW, 3, 5, b"version one")
    write_mcc(b, OW, 3, 5, b"version TWO is longer")

    result = diff_worlds(a, b)
    assert len(result.changes) == 1
    assert result.changes[0].kind == "modified"
    assert (result.changes[0].cx, result.changes[0].cz) == (3, 5)


def test_external_chunk_mcc_identical_no_change(tmp_path: Path):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    for root in (a, b):
        write_region_file(root, OW, 0, 0, [
            ChunkSpec(cx=3, cz=5, timestamp=1, compression=0x82, payload=b""),
        ])
        write_mcc(root, OW, 3, 5, b"identical")
    result = diff_worlds(a, b)
    assert result.changes == []


def test_external_vs_inline_same_content_no_change(tmp_path: Path):
    """Storage-mode change (external→inline) with identical content is NOT a diff.

    This is why the hasher masks the 0x80 bit: we don't want to flag a chunk
    as modified just because MC decided to move it inline or out-of-line.
    """
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    # a: inline, with payload b"body"
    write_region_file(a, OW, 0, 0, [
        ChunkSpec(cx=3, cz=5, timestamp=1, compression=2, payload=b"body"),
    ])
    # b: external stub + .mcc holding the same payload
    write_region_file(b, OW, 0, 0, [
        ChunkSpec(cx=3, cz=5, timestamp=1, compression=0x82, payload=b""),
    ])
    write_mcc(b, OW, 3, 5, b"body")

    result = diff_worlds(a, b)
    assert result.changes == []


# --- robustness ---------------------------------------------------------------

def test_corrupt_region_recorded_as_error_not_raised(tmp_path: Path):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    write_region_file(a, OW, 0, 0, [ChunkSpec(0, 0, 1, 2, b"ok")])
    # Corrupt: file too small to be a valid region
    (b / OW).mkdir(parents=True)
    (b / OW / "r.0.0.mca").write_bytes(b"\x00" * 100)

    result = diff_worlds(a, b)
    # Corrupt side was "new"; we should have recorded an error and yielded
    # nothing for that region (old-side hashes alone give "removed" entries).
    assert len(result.errors) == 1
    err = result.errors[0]
    assert err.dimension_key == OW
    assert err.side == "new"
    assert err.rx == 0 and err.rz == 0
    # Corruption should not crash the diff; we should still process other regions


def test_multiple_regions(tmp_path: Path):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    # region (0,0): unchanged
    for root in (a, b):
        write_region_file(root, OW, 0, 0, [ChunkSpec(0, 0, 1, 2, b"x")])
    # region (1,0): modified
    write_region_file(a, OW, 1, 0, [ChunkSpec(0, 0, 1, 2, b"old")])
    write_region_file(b, OW, 1, 0, [ChunkSpec(0, 0, 1, 2, b"new")])
    # region (0,1): only in a
    write_region_file(a, OW, 0, 1, [ChunkSpec(3, 3, 1, 2, b"gone")])
    # region (2,2): only in b
    write_region_file(b, OW, 2, 2, [ChunkSpec(0, 0, 1, 2, b"added")])

    result = diff_worlds(a, b)
    kinds = [(c.kind, c.rx, c.rz) for c in result.changes]
    assert ("modified", 1, 0) in kinds
    assert ("removed", 0, 1) in kinds
    assert ("added", 2, 2) in kinds
    # region (0,0) should produce nothing
    assert all(not (c.rx == 0 and c.rz == 0) for c in result.changes)
    assert result.count_by_kind() == {"modified": 1, "removed": 1, "added": 1}
