from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chunkvault.store.manifest import (
    MAGIC,
    ChunkRecord,
    FileRecord,
    Manifest,
    ManifestError,
    ManifestHeader,
    RegionRecord,
    read_manifest,
    write_manifest,
)


def _h16(b: int) -> bytes:
    return bytes([b]) * 16


def _h32(b: int) -> bytes:
    return bytes([b]) * 32


def _make_manifest() -> Manifest:
    return Manifest(
        header=ManifestHeader(
            timestamp_ms=1_700_000_000_000,
            label="before-raid",
            world_name="smp",
            mc_version="1.20.4",
            data_version=3700,
        ),
        dimensions={
            "region": [
                RegionRecord(rx=0, rz=0, chunks=[
                    ChunkRecord(cx=0, cz=0, compression=2, timestamp=12345,
                                content_hash=_h16(1)),
                    ChunkRecord(cx=5, cz=7, compression=0x82, timestamp=99,
                                content_hash=_h16(2)),
                ]),
                RegionRecord(rx=-1, rz=2, chunks=[
                    ChunkRecord(cx=31, cz=31, compression=2, timestamp=0,
                                content_hash=_h16(4)),
                ]),
            ],
            "DIM-1/region": [
                RegionRecord(rx=0, rz=0, chunks=[]),
            ],
        },
        files=[
            FileRecord(relative_path="level.dat", sha256=_h32(0xAB)),
            FileRecord(relative_path="datapacks/foo.zip", sha256=_h32(0xCD)),
        ],
    )


# ---- happy path roundtrip ---------------------------------------------------

def test_roundtrip_preserves_everything(tmp_path: Path):
    original = _make_manifest()
    out = tmp_path / "snap.mcbk"
    write_manifest(out, original)
    loaded = read_manifest(out)

    assert loaded.header == original.header
    assert set(loaded.dimensions) == set(original.dimensions)
    for dim in original.dimensions:
        assert len(loaded.dimensions[dim]) == len(original.dimensions[dim])
        for a, b in zip(loaded.dimensions[dim], original.dimensions[dim]):
            assert a.rx == b.rx and a.rz == b.rz
            assert len(a.chunks) == len(b.chunks)
            for ca, cb in zip(a.chunks, b.chunks):
                assert ca == cb
    assert loaded.files == original.files


def test_external_chunk_marked_external(tmp_path: Path):
    """The external flag is encoded in the on-disk compression byte (0x80 bit)."""
    m = Manifest(
        header=ManifestHeader(timestamp_ms=0, label=None, world_name="w"),
        dimensions={"region": [RegionRecord(rx=0, rz=0, chunks=[
            ChunkRecord(cx=3, cz=5, compression=0x82, timestamp=0,
                        content_hash=_h16(7)),
        ])]},
    )
    out = tmp_path / "ext.mcbk"
    write_manifest(out, m)
    loaded = read_manifest(out)
    chunk = loaded.dimensions["region"][0].chunks[0]
    assert chunk.external is True
    assert chunk.content_hash == _h16(7)


def test_empty_manifest_roundtrip(tmp_path: Path):
    m = Manifest(
        header=ManifestHeader(timestamp_ms=0, label=None, world_name="empty"),
    )
    out = tmp_path / "empty.mcbk"
    write_manifest(out, m)
    loaded = read_manifest(out)
    assert loaded.dimensions == {}
    assert loaded.files == []
    assert loaded.header.world_name == "empty"


def test_label_can_be_none(tmp_path: Path):
    m = Manifest(
        header=ManifestHeader(timestamp_ms=0, label=None, world_name="w"),
    )
    out = tmp_path / "x.mcbk"
    write_manifest(out, m)
    loaded = read_manifest(out)
    assert loaded.header.label is None


# ---- validation -------------------------------------------------------------



def test_chunk_coords_out_of_range_raises(tmp_path: Path):
    m = Manifest(
        header=ManifestHeader(timestamp_ms=0, label=None, world_name="w"),
        dimensions={"region": [RegionRecord(rx=0, rz=0, chunks=[
            ChunkRecord(cx=32, cz=0, compression=2, timestamp=0,
                        content_hash=_h16(1)),
        ])]},
    )
    with pytest.raises(ManifestError, match="out of range"):
        write_manifest(tmp_path / "bad.mcbk", m)


def test_wrong_hash_length_raises(tmp_path: Path):
    m = Manifest(
        header=ManifestHeader(timestamp_ms=0, label=None, world_name="w"),
        dimensions={"region": [RegionRecord(rx=0, rz=0, chunks=[
            ChunkRecord(cx=0, cz=0, compression=2, timestamp=0,
                        content_hash=b"too short"),
        ])]},
    )
    with pytest.raises(ManifestError, match="16 bytes"):
        write_manifest(tmp_path / "bad.mcbk", m)


def test_file_sha_wrong_length_raises(tmp_path: Path):
    m = Manifest(
        header=ManifestHeader(timestamp_ms=0, label=None, world_name="w"),
        files=[FileRecord(relative_path="x", sha256=b"short")],
    )
    with pytest.raises(ManifestError, match="32 bytes"):
        write_manifest(tmp_path / "bad.mcbk", m)


# ---- corruption / version checks --------------------------------------------

def test_bad_magic_raises(tmp_path: Path):
    p = tmp_path / "x.mcbk"
    p.write_bytes(b"XXXX" + b"\x01" + b"\x00\x00\x00\x00" + b"")
    with pytest.raises(ManifestError, match="bad magic"):
        read_manifest(p)


def test_unsupported_version_raises(tmp_path: Path):
    p = tmp_path / "x.mcbk"
    p.write_bytes(MAGIC + bytes([99]) + b"\x00\x00\x00\x00" + b"")
    with pytest.raises(ManifestError, match="unsupported"):
        read_manifest(p)


def test_truncated_file_raises(tmp_path: Path):
    p = tmp_path / "x.mcbk"
    p.write_bytes(b"MC")
    with pytest.raises(ManifestError, match="too short"):
        read_manifest(p)


def test_corrupt_zlib_body_raises(tmp_path: Path):
    p = tmp_path / "x.mcbk"
    p.write_bytes(MAGIC + bytes([1]) + struct.pack(">I", 100) + b"not valid zlib")
    with pytest.raises(ManifestError, match="zlib"):
        read_manifest(p)


# ---- size sanity ------------------------------------------------------------

def test_manifest_compresses_substantially(tmp_path: Path):
    """A manifest with thousands of chunks should compress better than 1:2."""
    m = Manifest(
        header=ManifestHeader(timestamp_ms=0, label=None, world_name="w"),
    )
    chunks = []
    for i in range(1024):
        chunks.append(ChunkRecord(
            cx=i % 32, cz=i // 32, compression=2, timestamp=i,
            content_hash=_h16(i % 256),
        ))
    m.dimensions["region"] = [RegionRecord(rx=0, rz=0, chunks=chunks)]
    out = tmp_path / "big.mcbk"
    written = write_manifest(out, m)
    # 1024 chunks * 23 bytes uncompressed body ≈ 23.5 KB
    # zlib should easily halve repetitive content_hash bytes
    assert written < 23_000
