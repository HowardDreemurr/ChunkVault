"""Synthetic MCA byte-string builder for tests.

Lets us exercise the parser without needing a real Minecraft world.
Chunks are packed contiguously after the 8 KB header, in the order given;
the location + timestamp tables are filled in to match.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SECTOR_SIZE = 4096
HEADER_BYTES = SECTOR_SIZE * 2


@dataclass
class ChunkSpec:
    cx: int        # 0..31
    cz: int        # 0..31
    timestamp: int
    compression: int   # raw byte (set 0x80 for external)
    payload: bytes


def build_mca(chunks: list[ChunkSpec]) -> bytes:
    """Assemble a valid .mca byte string from chunk specs."""
    locations = bytearray(SECTOR_SIZE)
    timestamps = bytearray(SECTOR_SIZE)
    body = bytearray()
    next_sector = 2  # header occupies sectors 0 and 1

    for spec in chunks:
        if not (0 <= spec.cx < 32 and 0 <= spec.cz < 32):
            raise ValueError(f"cx/cz out of range: {spec.cx},{spec.cz}")
        idx = spec.cx + spec.cz * 32

        data_len = 1 + len(spec.payload)  # stored in the 4-byte length field
        record = data_len.to_bytes(4, "big") + bytes([spec.compression]) + spec.payload
        pad = (-len(record)) % SECTOR_SIZE
        record_padded = record + b"\x00" * pad
        sector_count = len(record_padded) // SECTOR_SIZE
        if sector_count > 255:
            raise ValueError(
                f"chunk ({spec.cx},{spec.cz}): {sector_count} sectors > 255; "
                f"use the external-chunk flag (0x80) instead"
            )

        locations[idx * 4:idx * 4 + 3] = next_sector.to_bytes(3, "big")
        locations[idx * 4 + 3] = sector_count
        timestamps[idx * 4:idx * 4 + 4] = spec.timestamp.to_bytes(4, "big", signed=False)

        body.extend(record_padded)
        next_sector += sector_count

    return bytes(locations) + bytes(timestamps) + bytes(body)


def write_region_file(
    root: Path,
    dimension_key: str,
    rx: int,
    rz: int,
    chunks: list[ChunkSpec],
) -> Path:
    """Write a single r.X.Z.mca into <root>/<dimension_key>/."""
    region_dir = root.joinpath(*dimension_key.split("/"))
    region_dir.mkdir(parents=True, exist_ok=True)
    path = region_dir / f"r.{rx}.{rz}.mca"
    path.write_bytes(build_mca(chunks))
    return path


def write_mcc(root: Path, dimension_key: str, world_cx: int, world_cz: int,
              payload: bytes) -> Path:
    """Write a c.<cx>.<cz>.mcc file next to a region directory."""
    region_dir = root.joinpath(*dimension_key.split("/"))
    region_dir.mkdir(parents=True, exist_ok=True)
    path = region_dir / f"c.{world_cx}.{world_cz}.mcc"
    path.write_bytes(payload)
    return path
