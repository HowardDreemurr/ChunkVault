"""Read-only parser for Minecraft Anvil .mca region files.

Format reference: https://minecraft.wiki/w/Region_file_format

Layout:
    bytes 0..4095      — location table (1024 × 4 bytes: 3-byte sector offset + 1-byte sector count)
    bytes 4096..8191   — timestamp table (1024 × 4 bytes: big-endian unix timestamp)
    bytes 8192..       — chunk data, sector-aligned (4096 bytes per sector)

Each chunk record is:
    4 bytes big-endian length  (counts the compression byte but NOT itself)
    1 byte compression type    (1=gzip, 2=zlib, 3=none, 4=lz4; 0x80 bit = external .mcc)
    length-1 bytes payload     (raw — we do NOT decompress here)
    padding to next 4096-byte sector

This module is intentionally decompression-free: higher layers hash the raw
bytes and only decompress when semantic analysis (amulet) is requested.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

SECTOR_SIZE = 4096
HEADER_SECTORS = 2
HEADER_BYTES = SECTOR_SIZE * HEADER_SECTORS
ENTRIES_PER_TABLE = 1024
EXTERNAL_FLAG = 0x80

COMPRESSION_GZIP = 1
COMPRESSION_ZLIB = 2
COMPRESSION_NONE = 3
COMPRESSION_LZ4 = 4


class MCAError(Exception):
    """Base class for any MCA parsing problem."""


class MCATruncatedError(MCAError):
    """The file is shorter than what its header claims."""


class MCACorruptHeaderError(MCAError):
    """The location table references an impossible sector range."""


@dataclass(frozen=True)
class RegionCoords:
    """Region coordinates parsed from r.<rx>.<rz>.mca."""
    rx: int
    rz: int


@dataclass(frozen=True)
class RawChunk:
    """A chunk exactly as stored on disk — no decompression, no NBT parse.

    `cx` / `cz` are region-local (0..31). World-space chunk coordinates are
    `region.coords.rx * 32 + cx` and `region.coords.rz * 32 + cz`.
    """
    cx: int
    cz: int
    timestamp: int
    compression: int   # raw byte from disk (may have 0x80 set)
    payload: bytes     # empty if external
    external: bool     # True → payload lives in c.<cx>.<cz>.mcc

    @property
    def compression_type(self) -> int:
        """Compression scheme id with the external-flag bit stripped."""
        return self.compression & ~EXTERNAL_FLAG


def parse_region_filename(path: Path) -> RegionCoords | None:
    """Return RegionCoords for r.X.Z.mca / r.X.Z.mcr, or None if not a region name."""
    parts = path.name.split(".")
    if len(parts) != 4 or parts[0] != "r" or parts[3] not in ("mca", "mcr"):
        return None
    try:
        return RegionCoords(int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def pack_region(chunks: list["PackedChunk"]) -> bytes:
    """Assemble valid .mca bytes from per-chunk records.

    Each chunk is packed sector-by-sector after the 8 KB header in the order
    given (the order doesn't affect game behavior). The location and timestamp
    tables are populated to match. External chunks should pass ``payload=b""``;
    their compression byte must have the 0x80 bit set, and the caller is
    responsible for writing the corresponding ``c.X.Z.mcc`` file separately.
    """
    locations = bytearray(SECTOR_SIZE)
    timestamps = bytearray(SECTOR_SIZE)
    body = bytearray()
    next_sector = HEADER_SECTORS

    for c in chunks:
        if not (0 <= c.cx < 32 and 0 <= c.cz < 32):
            raise ValueError(f"chunk coord out of range: ({c.cx},{c.cz})")
        idx = c.cx + c.cz * 32

        data_len = 1 + len(c.payload)  # the 4-byte length field's value
        record = data_len.to_bytes(4, "big") + bytes([c.compression]) + c.payload
        pad = (-len(record)) % SECTOR_SIZE
        record_padded = record + b"\x00" * pad
        sector_count = len(record_padded) // SECTOR_SIZE
        if sector_count > 255:
            raise ValueError(
                f"chunk ({c.cx},{c.cz}): {sector_count} sectors > 255 limit; "
                f"use external-chunk flag (0x80) and write a c.X.Z.mcc file"
            )

        locations[idx * 4:idx * 4 + 3] = next_sector.to_bytes(3, "big")
        locations[idx * 4 + 3] = sector_count
        timestamps[idx * 4:idx * 4 + 4] = c.timestamp.to_bytes(
            4, "big", signed=False,
        )
        body.extend(record_padded)
        next_sector += sector_count

    return bytes(locations) + bytes(timestamps) + bytes(body)


@dataclass(frozen=True)
class PackedChunk:
    """Inputs to :func:`pack_region` — same shape as the on-disk record."""
    cx: int
    cz: int
    timestamp: int
    compression: int   # raw byte (set 0x80 bit for external)
    payload: bytes


class Region:
    """Read-only accessor for a single .mca region file.

    Can be constructed from a filesystem path (:meth:`__init__`) or from raw
    bytes (:meth:`from_bytes`). Either way the data is held in memory — a
    region file is at most ~32 MB, and keeping it buffered makes random
    access trivial and plays nicely with git-blob sourced content.
    """

    path: Path | None

    def __init__(self, path: Path | str):
        self.path = Path(path)
        coords = parse_region_filename(self.path)
        if coords is None:
            raise MCAError(
                f"Filename {self.path.name!r} is not a region file (expected r.X.Z.mca)"
            )
        self.coords = coords
        self._data = self.path.read_bytes()
        self._source = self.path.name

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        rx: int,
        rz: int,
        *,
        source: str | None = None,
    ) -> "Region":
        """Wrap a region file's raw bytes — e.g. from ``git cat-file blob``.

        ``source`` is used only in error messages; defaults to ``r.<rx>.<rz>.mca``.
        """
        self = cls.__new__(cls)
        self.path = None
        self.coords = RegionCoords(rx, rz)
        self._data = data
        self._source = source or f"r.{rx}.{rz}.mca"
        return self

    def _data_size(self) -> int:
        return len(self._data)

    def _load_header(self) -> bytes:
        size = self._data_size()
        if size == 0:
            # MC sometimes leaves zero-byte region files. Treat as "no chunks."
            return b"\x00" * HEADER_BYTES
        if size < HEADER_BYTES:
            raise MCATruncatedError(
                f"{self._source}: expected >= {HEADER_BYTES} header bytes, got {size}"
            )
        return self._data[:HEADER_BYTES]

    def _location(self, cx: int, cz: int) -> tuple[int, int]:
        """Return (sector_offset, sector_count). (0, 0) == chunk not present."""
        header = self._load_header()
        idx = (cx & 31) + (cz & 31) * 32
        entry = header[idx * 4:(idx + 1) * 4]
        sector_offset = int.from_bytes(entry[:3], "big")
        sector_count = entry[3]
        return sector_offset, sector_count

    def _timestamp(self, cx: int, cz: int) -> int:
        header = self._load_header()
        idx = (cx & 31) + (cz & 31) * 32
        base = SECTOR_SIZE + idx * 4
        return int.from_bytes(header[base:base + 4], "big")

    def iter_chunks(self) -> Iterator[RawChunk]:
        """Yield every present chunk in the region, in location-table order."""
        self._load_header()  # early error surface
        file_size = self._data_size()
        if file_size == 0:
            return
        for idx in range(ENTRIES_PER_TABLE):
            cx = idx % 32
            cz = idx // 32
            offset, count = self._location(cx, cz)
            if offset == 0 and count == 0:
                continue
            yield self._read_chunk_at(cx, cz, offset, file_size)

    def get_chunk(self, cx: int, cz: int) -> RawChunk | None:
        """Fetch a single chunk by local (cx, cz), or None if absent."""
        cx &= 31
        cz &= 31
        offset, count = self._location(cx, cz)
        if offset == 0 and count == 0:
            return None
        return self._read_chunk_at(cx, cz, offset, self._data_size())

    def _read_chunk_at(
        self,
        cx: int,
        cz: int,
        sector_offset: int,
        file_size: int,
    ) -> RawChunk:
        if sector_offset < HEADER_SECTORS:
            raise MCACorruptHeaderError(
                f"{self._source}: chunk ({cx},{cz}) sector_offset={sector_offset} "
                f"overlaps the header"
            )
        byte_offset = sector_offset * SECTOR_SIZE
        if byte_offset + 5 > file_size:
            raise MCATruncatedError(
                f"{self._source}: chunk ({cx},{cz}) points to byte {byte_offset}, "
                f"past EOF ({file_size})"
            )
        length = int.from_bytes(self._data[byte_offset:byte_offset + 4], "big")
        if length < 1:
            raise MCAError(
                f"{self._source}: chunk ({cx},{cz}) has impossible length={length}"
            )
        compression = self._data[byte_offset + 4]
        external = bool(compression & EXTERNAL_FLAG)
        payload_len = length - 1

        if byte_offset + 4 + length > file_size:
            raise MCATruncatedError(
                f"{self._source}: chunk ({cx},{cz}) claims {length} bytes but only "
                f"{file_size - byte_offset - 4} remain in file"
            )

        if external:
            # External chunks write a stub (length=1, compression with 0x80 set)
            # and keep the real payload in c.<cx>.<cz>.mcc.
            payload = b""
        else:
            payload_start = byte_offset + 5
            payload = self._data[payload_start:payload_start + payload_len]
            if len(payload) < payload_len:
                raise MCATruncatedError(
                    f"{self._source}: short read on chunk ({cx},{cz}) payload "
                    f"(got {len(payload)}/{payload_len})"
                )

        return RawChunk(
            cx=cx,
            cz=cz,
            timestamp=self._timestamp(cx, cz),
            compression=compression,
            payload=payload,
            external=external,
        )
