"""Binary manifest format for chunk-store snapshots.

Each manifest is a single zlib-compressed blob describing the entire state
of a snapshotted world: its dimensions, the regions within each, and the
chunks within each region. Per-chunk it stores the small metadata needed to
rebuild the on-disk MCA layout (cx, cz, compression byte, timestamp, content
hash, optionally external mcc hash).

We use a custom binary format (no msgpack/cbor dependencies) because:

1. Manifests are big — for a 100 GB world ~250 MB before compression — and
   stdlib JSON / msgpack pay a lot in overhead at that scale.
2. The structure is rigidly nested (snapshot → dim → region → chunks), so a
   schema-less format buys nothing.
3. ``struct`` + ``zlib`` are zero-cost and present everywhere.

Layout (all multi-byte integers big-endian):

    magic              4  bytes  ``b"MCBK"``
    format_version     1  byte   currently 2 (1 still readable)
    body_len           4  bytes  uncompressed body length (sanity check)
    body               variable  zlib-compressed body

Body (uncompressed):

    unix_ms            8  bytes  snapshot timestamp (unix epoch ms)
    label              str       see _read_str
    world_name         str
    mc_version         str       "" if unknown
    data_version       4  bytes  signed int; 0 if unknown
    n_dims             varint
    for each dim:
        dim_key        str       e.g. "region", "DIM-1/region"
        n_regions      varint
        for each region:
            rx, rz     2 × i32
            n_chunks   2  bytes  uint16 (max 1024 per region)
            for each chunk:
                cx, cz         2 × u8
                compression    u8
                timestamp      4  bytes i32
                content_hash   16 bytes
                if external (compression & 0x80):
                    mcc_hash   16 bytes
    n_files            varint   plain non-region files (level.dat etc.)
    for each file:
        relative_path  str       posix path under world root
        sha256         32 bytes

    --- v2 only (appended) ---
    last_played_ms     8  bytes  level.dat's Data.LastPlayed (0 if unknown)
    original_ts_ms     8  bytes  pre-retime timestamp (0 if never retimed)

V1 manifests omit the trailing v2 block; the reader treats missing bytes
as zeros so old vaults open seamlessly. New writes always emit v2.
"""
from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

MAGIC = b"MCBK"
FORMAT_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)
EXTERNAL_FLAG = 0x80


class ManifestError(Exception):
    """A manifest blob was malformed or used an unsupported format version."""


@dataclass(frozen=True)
class ChunkRecord:
    cx: int
    cz: int
    compression: int      # raw on-disk compression byte (may have 0x80 set)
    timestamp: int
    content_hash: bytes   # 16 bytes — the key under which the chunk's bytes
                          # live in the chunks/ pool. For internal chunks this
                          # is the inline payload's bytes; for external chunks
                          # it's the .mcc file's bytes. Either way, on restore
                          # we look up chunks/<content_hash> and write those
                          # bytes to the right place (inline or .mcc).

    @property
    def external(self) -> bool:
        return bool(self.compression & EXTERNAL_FLAG)


@dataclass
class RegionRecord:
    rx: int
    rz: int
    chunks: list[ChunkRecord] = field(default_factory=list)


@dataclass(frozen=True)
class FileRecord:
    relative_path: str    # posix-style under the world root
    sha256: bytes         # 32 bytes


@dataclass
class ManifestHeader:
    timestamp_ms: int
    label: str | None
    world_name: str
    mc_version: str = ""           # "" if unknown
    data_version: int = 0          # 0 if unknown
    last_played_ms: int = 0        # level.dat Data.LastPlayed; 0 if unknown
    original_timestamp_ms: int = 0 # pre-retime timestamp; 0 if never retimed


@dataclass
class Manifest:
    header: ManifestHeader
    dimensions: dict[str, list[RegionRecord]] = field(default_factory=dict)
    files: list[FileRecord] = field(default_factory=list)


# ---- writer -----------------------------------------------------------------

def write_manifest(out_path: Path | str, manifest: Manifest) -> int:
    """Serialize ``manifest`` to ``out_path`` atomically.

    Writes to a sibling temp file first, then renames into place. A Ctrl-C
    or kill mid-write leaves either the previous version (if any) or
    nothing — never a half-written file that would later be misread as
    a complete manifest.
    """
    body = _encode_body(manifest)
    compressed = zlib.compress(body, level=6)
    out = bytearray()
    out += MAGIC
    out.append(FORMAT_VERSION)
    out += struct.pack(">I", len(body))
    out += compressed

    out_path = Path(out_path)
    tmp = out_path.with_name(f"{out_path.name}.tmp.{os.getpid()}")
    tmp.write_bytes(bytes(out))
    try:
        os.replace(tmp, out_path)        # atomic on every supported OS
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return len(out)


def _encode_body(m: Manifest) -> bytes:
    buf = bytearray()
    buf += struct.pack(">Q", m.header.timestamp_ms)
    _write_str(buf, m.header.label or "")
    _write_str(buf, m.header.world_name)
    _write_str(buf, m.header.mc_version)
    buf += struct.pack(">i", m.header.data_version)

    _write_varint(buf, len(m.dimensions))
    for dim_key in sorted(m.dimensions):
        regions = m.dimensions[dim_key]
        _write_str(buf, dim_key)
        _write_varint(buf, len(regions))
        for region in regions:
            buf += struct.pack(">ii", region.rx, region.rz)
            buf += struct.pack(">H", len(region.chunks))
            for c in region.chunks:
                if not (0 <= c.cx < 32 and 0 <= c.cz < 32):
                    raise ManifestError(f"chunk coord out of range: {c.cx},{c.cz}")
                if len(c.content_hash) != 16:
                    raise ManifestError(
                        f"content_hash must be 16 bytes, got {len(c.content_hash)}"
                    )
                buf += struct.pack(
                    ">BBBi", c.cx, c.cz, c.compression, c.timestamp,
                )
                buf += c.content_hash

    _write_varint(buf, len(m.files))
    for f in m.files:
        if len(f.sha256) != 32:
            raise ManifestError(
                f"file sha256 must be 32 bytes: {f.relative_path}"
            )
        _write_str(buf, f.relative_path)
        buf += f.sha256

    # V2 trailer: appended after the files block so V1 readers (which check
    # for trailing data) would simply reject the new format. New readers
    # detect the trailer by remaining bytes.
    buf += struct.pack(">QQ", m.header.last_played_ms, m.header.original_timestamp_ms)
    return bytes(buf)


# ---- reader -----------------------------------------------------------------

def read_manifest(path: Path | str) -> Manifest:
    raw = Path(path).read_bytes()
    if len(raw) < len(MAGIC) + 1 + 4:
        raise ManifestError(f"manifest too short: {len(raw)} bytes")
    if raw[:4] != MAGIC:
        raise ManifestError(f"bad magic: {raw[:4]!r}")
    version = raw[4]
    if version not in SUPPORTED_VERSIONS:
        raise ManifestError(
            f"unsupported manifest version {version} "
            f"(supported: {SUPPORTED_VERSIONS})"
        )
    body_len = struct.unpack(">I", raw[5:9])[0]
    try:
        body = zlib.decompress(raw[9:])
    except zlib.error as e:
        raise ManifestError(f"manifest body zlib decompress failed: {e}") from e
    if len(body) != body_len:
        raise ManifestError(
            f"body length mismatch: header says {body_len}, decompressed {len(body)}"
        )
    return _decode_body(body, version)


def _decode_body(body: bytes, version: int) -> Manifest:
    cursor = _Cursor(body)
    timestamp_ms = struct.unpack(">Q", cursor.read(8))[0]
    label = _read_str(cursor) or None
    world_name = _read_str(cursor)
    mc_version = _read_str(cursor)
    data_version = struct.unpack(">i", cursor.read(4))[0]
    header = ManifestHeader(
        timestamp_ms=timestamp_ms,
        label=label,
        world_name=world_name,
        mc_version=mc_version,
        data_version=data_version,
    )
    manifest = Manifest(header=header)

    n_dims = _read_varint(cursor)
    for _ in range(n_dims):
        dim_key = _read_str(cursor)
        n_regions = _read_varint(cursor)
        regions: list[RegionRecord] = []
        for _ in range(n_regions):
            rx, rz = struct.unpack(">ii", cursor.read(8))
            n_chunks = struct.unpack(">H", cursor.read(2))[0]
            chunks: list[ChunkRecord] = []
            for _ in range(n_chunks):
                cx, cz, compression, timestamp = struct.unpack(
                    ">BBBi", cursor.read(7)
                )
                content_hash = cursor.read(16)
                chunks.append(ChunkRecord(
                    cx=cx, cz=cz, compression=compression,
                    timestamp=timestamp, content_hash=content_hash,
                ))
            regions.append(RegionRecord(rx=rx, rz=rz, chunks=chunks))
        manifest.dimensions[dim_key] = regions

    n_files = _read_varint(cursor)
    for _ in range(n_files):
        rel = _read_str(cursor)
        sha = cursor.read(32)
        manifest.files.append(FileRecord(relative_path=rel, sha256=sha))

    # V2 trailer: last_played_ms (8) + original_timestamp_ms (8). V1 stops
    # here. If the trailer is partially missing on a v2 manifest, treat it
    # as zeros — not worth aborting the load over a single missing field.
    if version >= 2 and cursor.remaining() >= 16:
        header.last_played_ms, header.original_timestamp_ms = struct.unpack(
            ">QQ", cursor.read(16),
        )
    elif cursor.remaining() != 0:
        raise ManifestError(
            f"trailing data: {cursor.remaining()} bytes after manifest body"
        )
    return manifest


# ---- low-level encoding helpers ---------------------------------------------

class _Cursor:
    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, n: int) -> bytes:
        end = self._pos + n
        if end > len(self._data):
            raise ManifestError(
                f"unexpected EOF: wanted {n} bytes at pos {self._pos}, "
                f"only {len(self._data) - self._pos} left"
            )
        b = self._data[self._pos:end]
        self._pos = end
        return b

    def remaining(self) -> int:
        return len(self._data) - self._pos


def _write_varint(buf: bytearray, value: int) -> None:
    if value < 0:
        raise ValueError(f"varint must be non-negative: {value}")
    while value >= 0x80:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value)


def _read_varint(c: _Cursor) -> int:
    result = 0
    shift = 0
    while True:
        b = c.read(1)[0]
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result
        shift += 7
        if shift > 63:
            raise ManifestError("varint too long")


def _write_str(buf: bytearray, s: str) -> None:
    encoded = s.encode("utf-8")
    _write_varint(buf, len(encoded))
    buf += encoded


def _read_str(c: _Cursor) -> str:
    n = _read_varint(c)
    return c.read(n).decode("utf-8", errors="replace")
