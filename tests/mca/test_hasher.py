from __future__ import annotations

from pathlib import Path

from chunkvault.mca.hasher import HASH_BYTES, hash_chunk, hash_chunk_on_disk
from chunkvault.mca.region import RawChunk, Region

from tests._fixtures import ChunkSpec, build_mca


def _chunk(compression: int = 2, payload: bytes = b"hello") -> RawChunk:
    return RawChunk(
        cx=0, cz=0, timestamp=0,
        compression=compression, payload=payload, external=False,
    )


def test_hash_deterministic():
    assert hash_chunk(_chunk()) == hash_chunk(_chunk())


def test_hash_length():
    assert len(hash_chunk(_chunk())) == HASH_BYTES


def test_hash_distinguishes_payloads():
    assert hash_chunk(_chunk(payload=b"one")) != hash_chunk(_chunk(payload=b"two"))


def test_hash_distinguishes_compression_byte():
    assert hash_chunk(_chunk(compression=2)) != hash_chunk(_chunk(compression=1))


def test_hash_ignores_timestamp_and_coords():
    """Content identity depends on bytes-on-disk, not where the chunk lives."""
    a = RawChunk(cx=0, cz=0, timestamp=100, compression=2, payload=b"x", external=False)
    b = RawChunk(cx=15, cz=7, timestamp=999, compression=2, payload=b"x", external=False)
    assert hash_chunk(a) == hash_chunk(b)


def test_empty_payload_hashes_stably():
    """External chunks store empty payload; make sure that doesn't crash."""
    a = RawChunk(cx=0, cz=0, timestamp=0, compression=0x82, payload=b"", external=True)
    assert len(hash_chunk(a)) == HASH_BYTES


def test_external_flag_bit_masked_so_inline_and_external_hash_same():
    """Same logical content (compression + payload) hashes identical whether
    stored inline or externally — the 0x80 storage flag is not part of identity."""
    inline = RawChunk(cx=0, cz=0, timestamp=0, compression=2, payload=b"body",
                      external=False)
    external = RawChunk(cx=0, cz=0, timestamp=0, compression=0x82, payload=b"",
                        external=True)
    assert hash_chunk(inline) == hash_chunk(external, external_payload=b"body")


def test_external_payload_changes_are_detected():
    a = RawChunk(cx=0, cz=0, timestamp=0, compression=0x82, payload=b"",
                 external=True)
    assert hash_chunk(a, external_payload=b"v1") != hash_chunk(a, external_payload=b"v2")


def test_hash_chunk_on_disk_reads_mcc(tmp_path: Path):
    """External chunks: hash_chunk_on_disk must load the c.X.Z.mcc file."""
    # Build a region with one external chunk at local (3, 5).
    data = build_mca([
        ChunkSpec(cx=3, cz=5, timestamp=0, compression=0x82, payload=b""),
    ])
    region_path = tmp_path / "r.0.0.mca"
    region_path.write_bytes(data)
    # Create the backing .mcc file. For region (0,0), world-space coords equal
    # local coords.
    mcc = tmp_path / "c.3.5.mcc"
    mcc.write_bytes(b"the real payload")

    region = Region(region_path)
    (chunk,) = list(region.iter_chunks())
    # on_disk hash should equal hash_chunk(..., external_payload=mcc contents)
    assert hash_chunk_on_disk(region, chunk) == hash_chunk(
        chunk, external_payload=b"the real payload"
    )


def test_hash_chunk_on_disk_tolerates_missing_mcc(tmp_path: Path):
    """Missing .mcc: don't crash, hash is still stable (it'll just hash empty)."""
    data = build_mca([
        ChunkSpec(cx=0, cz=0, timestamp=0, compression=0x82, payload=b""),
    ])
    region_path = tmp_path / "r.0.0.mca"
    region_path.write_bytes(data)
    region = Region(region_path)
    (chunk,) = list(region.iter_chunks())
    # No .mcc file created. Should still produce a 16-byte hash.
    h = hash_chunk_on_disk(region, chunk)
    assert len(h) == HASH_BYTES
    # And it should match hashing with empty external_payload.
    assert h == hash_chunk(chunk, external_payload=b"")


def test_hash_chunk_on_disk_uses_world_space_coords(tmp_path: Path):
    """For non-(0,0) regions, the .mcc filename uses world-space chunk coords."""
    data = build_mca([
        ChunkSpec(cx=3, cz=5, timestamp=0, compression=0x82, payload=b""),
    ])
    # Region (rx=2, rz=-1) → world chunk (2*32+3, -1*32+5) = (67, -27)
    region_path = tmp_path / "r.2.-1.mca"
    region_path.write_bytes(data)
    (tmp_path / "c.67.-27.mcc").write_bytes(b"correct")
    (tmp_path / "c.3.5.mcc").write_bytes(b"wrong - do not read this")

    region = Region(region_path)
    (chunk,) = list(region.iter_chunks())
    assert hash_chunk_on_disk(region, chunk) == hash_chunk(
        chunk, external_payload=b"correct"
    )
