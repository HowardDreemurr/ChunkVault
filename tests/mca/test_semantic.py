from __future__ import annotations

import gzip
import zlib
from pathlib import Path

from chunkvault.mca.nbt_lite import TAG_INT, build_nbt_compound
from chunkvault.mca.region import RawChunk, Region
from chunkvault.mca.semantic import chunk_data_version, chunk_data_version_at

from tests._fixtures import ChunkSpec, build_mca, write_mcc, write_region_file


def _nbt_with_data_version(value: int) -> bytes:
    return build_nbt_compound("", [
        (TAG_INT, "DataVersion", value.to_bytes(4, "big", signed=True)),
    ])


def _make_region_file(tmp_path: Path, *chunks: ChunkSpec) -> Path:
    data = build_mca(list(chunks))
    path = tmp_path / "r.0.0.mca"
    path.write_bytes(data)
    return path


# ---- zlib / gzip / uncompressed ---------------------------------------------

def test_zlib_chunk_data_version(tmp_path: Path):
    nbt = _nbt_with_data_version(3700)
    path = _make_region_file(tmp_path, ChunkSpec(
        cx=0, cz=0, timestamp=1, compression=2, payload=zlib.compress(nbt),
    ))
    region = Region(path)
    (chunk,) = list(region.iter_chunks())
    assert chunk_data_version(region, chunk) == 3700


def test_gzip_chunk_data_version(tmp_path: Path):
    nbt = _nbt_with_data_version(1976)
    path = _make_region_file(tmp_path, ChunkSpec(
        cx=0, cz=0, timestamp=1, compression=1, payload=gzip.compress(nbt),
    ))
    region = Region(path)
    (chunk,) = list(region.iter_chunks())
    assert chunk_data_version(region, chunk) == 1976


def test_uncompressed_chunk_data_version(tmp_path: Path):
    nbt = _nbt_with_data_version(2586)
    path = _make_region_file(tmp_path, ChunkSpec(
        cx=0, cz=0, timestamp=1, compression=3, payload=nbt,
    ))
    region = Region(path)
    (chunk,) = list(region.iter_chunks())
    assert chunk_data_version(region, chunk) == 2586


# ---- external chunks --------------------------------------------------------

def test_external_chunk_reads_from_mcc(tmp_path: Path):
    nbt = _nbt_with_data_version(3952)
    # Region at (0,0), chunk at world-space (5, 7)
    path = _make_region_file(tmp_path, ChunkSpec(
        cx=5, cz=7, timestamp=1, compression=0x82, payload=b"",
    ))
    # .mcc at world-coords 5,7 (same dir)
    (tmp_path / "c.5.7.mcc").write_bytes(zlib.compress(nbt))
    region = Region(path)
    (chunk,) = list(region.iter_chunks())
    assert chunk_data_version(region, chunk) == 3952


def test_external_chunk_missing_mcc_returns_none(tmp_path: Path):
    path = _make_region_file(tmp_path, ChunkSpec(
        cx=0, cz=0, timestamp=1, compression=0x82, payload=b"",
    ))
    region = Region(path)
    (chunk,) = list(region.iter_chunks())
    assert chunk_data_version(region, chunk) is None


# ---- robustness -------------------------------------------------------------

def test_corrupt_zlib_returns_none(tmp_path: Path):
    path = _make_region_file(tmp_path, ChunkSpec(
        cx=0, cz=0, timestamp=1, compression=2, payload=b"not zlib bytes at all",
    ))
    region = Region(path)
    (chunk,) = list(region.iter_chunks())
    assert chunk_data_version(region, chunk) is None


def test_lz4_returns_none_not_raise(tmp_path: Path):
    """LZ4 isn't supported yet — surface as None, not exception."""
    path = _make_region_file(tmp_path, ChunkSpec(
        cx=0, cz=0, timestamp=1, compression=4, payload=b"lz4 pretend bytes",
    ))
    region = Region(path)
    (chunk,) = list(region.iter_chunks())
    assert chunk_data_version(region, chunk) is None


def test_valid_nbt_but_no_data_version_tag(tmp_path: Path):
    from chunkvault.mca.nbt_lite import TAG_STRING
    nbt = build_nbt_compound("", [
        (TAG_STRING, "Status",
         len("full").to_bytes(2, "big") + b"full"),
    ])
    path = _make_region_file(tmp_path, ChunkSpec(
        cx=0, cz=0, timestamp=1, compression=2, payload=zlib.compress(nbt),
    ))
    region = Region(path)
    (chunk,) = list(region.iter_chunks())
    assert chunk_data_version(region, chunk) is None


# ---- world-space convenience ------------------------------------------------

def test_chunk_data_version_at_world_coords(tmp_path: Path):
    """World layout lookup: put a chunk in region (-1, -1) and query it back."""
    world = tmp_path / "world"
    nbt = _nbt_with_data_version(3700)
    # Region (-1, -1) covers chunks cx ∈ [-32..-1], cz ∈ [-32..-1].
    # Place chunk at local (5, 3) → world (-32+5, -32+3) = (-27, -29).
    write_region_file(world, "region", -1, -1, [
        ChunkSpec(cx=5, cz=3, timestamp=1, compression=2,
                  payload=zlib.compress(nbt)),
    ])
    assert chunk_data_version_at(world, "region", -27, -29) == 3700


def test_chunk_data_version_at_missing_region(tmp_path: Path):
    world = tmp_path / "world"
    world.mkdir()
    assert chunk_data_version_at(world, "region", 0, 0) is None


def test_chunk_data_version_at_absent_chunk(tmp_path: Path):
    """Region exists but the specific chunk slot is empty."""
    world = tmp_path / "world"
    nbt = _nbt_with_data_version(3000)
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(cx=5, cz=5, timestamp=1, compression=2,
                  payload=zlib.compress(nbt)),
    ])
    # Query a different slot
    assert chunk_data_version_at(world, "region", 10, 10) is None


def test_chunk_data_version_at_nether_dimension(tmp_path: Path):
    world = tmp_path / "world"
    nbt = _nbt_with_data_version(2586)
    write_region_file(world, "DIM-1/region", 0, 0, [
        ChunkSpec(cx=0, cz=0, timestamp=1, compression=2,
                  payload=zlib.compress(nbt)),
    ])
    assert chunk_data_version_at(world, "DIM-1/region", 0, 0) == 2586
