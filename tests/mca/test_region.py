from __future__ import annotations

from pathlib import Path

import pytest

from chunkvault.mca.region import (
    HEADER_BYTES,
    MCACorruptHeaderError,
    MCAError,
    MCATruncatedError,
    Region,
    parse_region_filename,
)
from tests._fixtures import ChunkSpec, build_mca


def _write_region(tmp_path: Path, rx: int, rz: int, data: bytes) -> Path:
    p = tmp_path / f"r.{rx}.{rz}.mca"
    p.write_bytes(data)
    return p


def test_parse_region_filename_valid():
    assert parse_region_filename(Path("r.0.0.mca")).rx == 0
    assert parse_region_filename(Path("r.-3.12.mca")).rz == 12
    assert parse_region_filename(Path("r.0.0.mcr")) is not None


def test_parse_region_filename_invalid():
    assert parse_region_filename(Path("notaregion.mca")) is None
    assert parse_region_filename(Path("r.0.mca")) is None
    assert parse_region_filename(Path("r.foo.0.mca")) is None
    assert parse_region_filename(Path("r.0.0.txt")) is None


def test_region_rejects_non_region_filename(tmp_path):
    p = tmp_path / "not_a_region.mca"
    p.write_bytes(b"\x00" * HEADER_BYTES)
    with pytest.raises(MCAError):
        Region(p)


def test_region_coords_parsed():
    assert Region.__init__  # sanity
    p = Path("r.-3.12.mca")
    # Can't open a nonexistent file, so just check parse_region_filename
    assert parse_region_filename(p).rx == -3
    assert parse_region_filename(p).rz == 12


def test_empty_file_yields_no_chunks(tmp_path):
    p = _write_region(tmp_path, 0, 0, b"")
    region = Region(p)
    assert list(region.iter_chunks()) == []


def test_zero_filled_header_yields_no_chunks(tmp_path):
    p = _write_region(tmp_path, 0, 0, b"\x00" * HEADER_BYTES)
    region = Region(p)
    assert list(region.iter_chunks()) == []


def test_single_chunk_roundtrip(tmp_path):
    payload = b"hello world" * 17
    data = build_mca([
        ChunkSpec(cx=3, cz=5, timestamp=0xAABBCCDD, compression=2, payload=payload),
    ])
    p = _write_region(tmp_path, 0, 0, data)
    region = Region(p)
    chunks = list(region.iter_chunks())
    assert len(chunks) == 1
    c = chunks[0]
    assert (c.cx, c.cz) == (3, 5)
    assert c.timestamp == 0xAABBCCDD
    assert c.compression == 2
    assert c.compression_type == 2
    assert c.external is False
    assert c.payload == payload


def test_multiple_chunks_varied_sizes(tmp_path):
    specs = [
        ChunkSpec(0, 0, 1000, 2, b"a" * 100),
        ChunkSpec(1, 0, 2000, 2, b"b" * 5000),     # > 1 sector
        ChunkSpec(31, 31, 3000, 2, b"c" * 20000),  # several sectors
    ]
    data = build_mca(specs)
    p = _write_region(tmp_path, 0, 0, data)
    region = Region(p)
    chunks = {(c.cx, c.cz): c for c in region.iter_chunks()}
    assert set(chunks.keys()) == {(0, 0), (1, 0), (31, 31)}
    for spec in specs:
        c = chunks[(spec.cx, spec.cz)]
        assert c.payload == spec.payload
        assert c.timestamp == spec.timestamp


def test_external_chunk_flag_detected(tmp_path):
    data = build_mca([
        ChunkSpec(cx=10, cz=10, timestamp=555, compression=0x82, payload=b""),
    ])
    p = _write_region(tmp_path, 0, 0, data)
    region = Region(p)
    chunks = list(region.iter_chunks())
    assert len(chunks) == 1
    c = chunks[0]
    assert c.external is True
    assert c.compression == 0x82
    assert c.compression_type == 2
    assert c.payload == b""


def test_get_chunk_returns_none_for_absent(tmp_path):
    data = build_mca([
        ChunkSpec(cx=3, cz=5, timestamp=1, compression=2, payload=b"x"),
    ])
    p = _write_region(tmp_path, 0, 0, data)
    region = Region(p)
    assert region.get_chunk(0, 0) is None
    assert region.get_chunk(3, 5) is not None


def test_full_region_boundary_indices(tmp_path):
    data = build_mca([
        ChunkSpec(cx=31, cz=31, timestamp=42, compression=2, payload=b"border"),
    ])
    p = _write_region(tmp_path, 0, 0, data)
    region = Region(p)
    c = region.get_chunk(31, 31)
    assert c is not None
    assert c.payload == b"border"


def test_truncated_header_raises(tmp_path):
    p = _write_region(tmp_path, 0, 0, b"\x00" * 100)
    region = Region(p)
    with pytest.raises(MCATruncatedError):
        list(region.iter_chunks())


def test_corrupt_offset_past_eof_raises(tmp_path):
    data = bytearray(build_mca([
        ChunkSpec(cx=0, cz=0, timestamp=1, compression=2, payload=b"x"),
    ]))
    data[0:3] = (99999).to_bytes(3, "big")
    p = _write_region(tmp_path, 0, 0, bytes(data))
    region = Region(p)
    with pytest.raises(MCATruncatedError):
        list(region.iter_chunks())


def test_corrupt_offset_overlaps_header_raises(tmp_path):
    data = bytearray(build_mca([
        ChunkSpec(cx=0, cz=0, timestamp=1, compression=2, payload=b"x"),
    ]))
    data[0:3] = (1).to_bytes(3, "big")  # point at timestamp-table sector
    p = _write_region(tmp_path, 0, 0, bytes(data))
    region = Region(p)
    with pytest.raises(MCACorruptHeaderError):
        list(region.iter_chunks())


def test_corrupt_length_exceeding_file_raises(tmp_path):
    """A plausible sector offset but a length that runs past EOF."""
    data = bytearray(build_mca([
        ChunkSpec(cx=0, cz=0, timestamp=1, compression=2, payload=b"x"),
    ]))
    # Chunk record starts at byte offset 8192. Overwrite its length prefix
    # with something much bigger than the file.
    data[8192:8196] = (10_000_000).to_bytes(4, "big")
    p = _write_region(tmp_path, 0, 0, bytes(data))
    region = Region(p)
    with pytest.raises(MCATruncatedError):
        list(region.iter_chunks())


def test_from_bytes_roundtrip():
    """Region.from_bytes parses the same data a path-based Region would."""
    data = build_mca([
        ChunkSpec(cx=3, cz=5, timestamp=1234, compression=2, payload=b"hi"),
        ChunkSpec(cx=0, cz=0, timestamp=999, compression=2, payload=b"bye"),
    ])
    r = Region.from_bytes(data, rx=7, rz=-2)
    assert r.coords.rx == 7
    assert r.coords.rz == -2
    assert r.path is None
    chunks = {(c.cx, c.cz): c for c in r.iter_chunks()}
    assert chunks[(0, 0)].payload == b"bye"
    assert chunks[(3, 5)].payload == b"hi"
    assert chunks[(3, 5)].timestamp == 1234


def test_from_bytes_error_source_label():
    """Errors from a from_bytes Region reference the provided source label."""
    bad = b"\x00" * 100
    r = Region.from_bytes(bad, rx=0, rz=0, source="garbage.mca")
    with pytest.raises(MCATruncatedError, match="garbage.mca"):
        list(r.iter_chunks())


def test_from_bytes_empty():
    r = Region.from_bytes(b"", rx=0, rz=0)
    assert list(r.iter_chunks()) == []


def test_iter_chunks_in_location_table_order(tmp_path):
    """iter_chunks walks the location table, not insertion order."""
    specs = [
        ChunkSpec(cx=5, cz=0, timestamp=1, compression=2, payload=b"fifth"),
        ChunkSpec(cx=0, cz=0, timestamp=2, compression=2, payload=b"first"),
        ChunkSpec(cx=0, cz=1, timestamp=3, compression=2, payload=b"row1"),
    ]
    data = build_mca(specs)
    p = _write_region(tmp_path, 0, 0, data)
    region = Region(p)
    yielded = [(c.cx, c.cz) for c in region.iter_chunks()]
    # Location-table order: (cx + cz*32) ascending
    assert yielded == [(0, 0), (5, 0), (0, 1)]
