"""Minimal NBT reader — decompress a chunk payload and extract DataVersion.

This is NOT a general-purpose NBT library (use amulet-nbt / nbtlib for that).
It exists only to answer "what MC version wrote this chunk?" cheaply, without
pulling in the full amulet stack. Walking the tree is lazy and stops the
moment DataVersion is found.

Compression schemes supported:
    1  gzip
    2  zlib        (the overwhelming default)
    3  uncompressed
    4  lz4         → NotImplementedError (raise, not silent None)
"""
from __future__ import annotations

import gzip
import io
import zlib
from typing import BinaryIO

from .region import EXTERNAL_FLAG

# NBT tag type constants
TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12


class NBTError(Exception):
    """NBT bytes were not parseable."""


def decompress_chunk_payload(compression: int, payload: bytes) -> bytes:
    """Decompress the raw payload bytes read from a chunk record.

    ``compression`` is the on-disk compression byte, which may have 0x80
    set for external chunks — that bit is masked off here before dispatch.
    """
    scheme = compression & ~EXTERNAL_FLAG
    if scheme == 2:
        return zlib.decompress(payload)
    if scheme == 1:
        return gzip.decompress(payload)
    if scheme == 3:
        return payload
    if scheme == 4:
        raise NotImplementedError("LZ4 chunk compression not supported")
    raise NBTError(f"unknown chunk compression scheme: {scheme}")


def find_version_info(nbt_bytes: bytes) -> tuple[str | None, int | None]:
    """Locate ``Data.Version.{Name,Id}`` in a decompressed level.dat NBT.

    Returns ``(name, data_version)`` — either may be None if missing or the
    NBT is malformed. Doesn't raise.
    """
    try:
        f = io.BytesIO(nbt_bytes)
        root_type = _read(f, 1)[0]
        if root_type != TAG_COMPOUND:
            return None, None
        _read_string(f)  # root name (usually empty)
        return _scan_version_in_compound(f, depth_left=3)
    except (NBTError, ValueError, IndexError, UnicodeDecodeError):
        return None, None


def _scan_version_in_compound(
    f: BinaryIO, depth_left: int,
) -> tuple[str | None, int | None]:
    """Walk a compound looking for a Version sub-compound (name, id)."""
    name_out: str | None = None
    id_out: int | None = None
    while True:
        tag_type = _read(f, 1)[0]
        if tag_type == TAG_END:
            return name_out, id_out
        name = _read_string(f)
        # Look for Version compound at this level
        if tag_type == TAG_COMPOUND and name == "Version":
            n, i = _read_version_fields(f)
            if name_out is None:
                name_out = n
            if id_out is None:
                id_out = i
            continue
        # Pre-1.8: DataVersion may be at root
        if tag_type == TAG_INT and name == "DataVersion" and id_out is None:
            id_out = _read_int(f)
            continue
        if tag_type == TAG_COMPOUND and depth_left > 0:
            n, i = _scan_version_in_compound(f, depth_left - 1)
            if name_out is None:
                name_out = n
            if id_out is None:
                id_out = i
            continue
        _skip_payload(f, tag_type)


def _read_version_fields(f: BinaryIO) -> tuple[str | None, int | None]:
    """Read a Version compound, extract Name + Id."""
    name_out: str | None = None
    id_out: int | None = None
    while True:
        tag_type = _read(f, 1)[0]
        if tag_type == TAG_END:
            return name_out, id_out
        field_name = _read_string(f)
        if tag_type == TAG_STRING and field_name == "Name":
            length = _read_ushort(f)
            name_out = _read(f, length).decode("utf-8", errors="replace")
        elif tag_type == TAG_INT and field_name == "Id":
            id_out = _read_int(f)
        else:
            _skip_payload(f, tag_type)


def find_last_played(nbt_bytes: bytes) -> int | None:
    """Locate ``Data.LastPlayed`` in a decompressed level.dat NBT.

    Returns Unix-epoch milliseconds (TAG_Long) or None if absent. Doesn't
    raise. ``LastPlayed`` is set by Minecraft every time the world is saved
    — usually within seconds of the actual backup time, making it a far
    better default snapshot timestamp than ``datetime.now()`` for any
    world that wasn't taken at the moment ``chunkvault snapshot`` ran.
    """
    try:
        f = io.BytesIO(nbt_bytes)
        root_type = _read(f, 1)[0]
        if root_type != TAG_COMPOUND:
            return None
        _read_string(f)  # root name (usually empty)
        return _scan_last_played_in_compound(f, depth_left=3)
    except (NBTError, ValueError, IndexError, UnicodeDecodeError):
        return None


def _scan_last_played_in_compound(
    f: BinaryIO, depth_left: int,
) -> int | None:
    """Walk a compound looking for a TAG_Long ``LastPlayed`` field.

    Mirrors ``_scan_version_in_compound``: descend into nested compounds
    up to ``depth_left`` so we find ``Data.LastPlayed`` as well as
    root-level placements seen in a few rare modded worlds.
    """
    found: int | None = None
    while True:
        tag_type = _read(f, 1)[0]
        if tag_type == TAG_END:
            return found
        name = _read_string(f)
        if tag_type == TAG_LONG and name == "LastPlayed" and found is None:
            found = int.from_bytes(_read(f, 8), "big", signed=True)
            continue
        if tag_type == TAG_COMPOUND and depth_left > 0:
            nested = _scan_last_played_in_compound(f, depth_left - 1)
            if found is None:
                found = nested
            continue
        _skip_payload(f, tag_type)


def find_data_version(nbt_bytes: bytes, max_depth: int = 2) -> int | None:
    """Return the DataVersion int from a decompressed chunk NBT, or None.

    Pre-1.18 chunks nest DataVersion inside a ``Level`` compound; 1.18+ put
    it at root. ``max_depth=2`` covers both — search the root and one level
    of nested compounds.
    """
    try:
        f = io.BytesIO(nbt_bytes)
        root_type = _read(f, 1)[0]
        if root_type != TAG_COMPOUND:
            return None
        _read_string(f)  # root name (usually empty)
        return _scan_compound(f, max_depth)
    except (NBTError, ValueError, IndexError, UnicodeDecodeError):
        return None


# ---- internals --------------------------------------------------------------

def _read(f: BinaryIO, n: int) -> bytes:
    b = f.read(n)
    if len(b) < n:
        raise NBTError(f"unexpected EOF (wanted {n}, got {len(b)})")
    return b


def _read_int(f: BinaryIO) -> int:
    return int.from_bytes(_read(f, 4), "big", signed=True)


def _read_ushort(f: BinaryIO) -> int:
    return int.from_bytes(_read(f, 2), "big", signed=False)


def _read_string(f: BinaryIO) -> str:
    length = _read_ushort(f)
    return _read(f, length).decode("utf-8", errors="replace")


def _scan_compound(f: BinaryIO, depth_left: int) -> int | None:
    """Walk the current compound. Return the first DataVersion int found
    at any depth ≤ depth_left; consume payloads fully regardless."""
    found: int | None = None
    while True:
        tag_type = _read(f, 1)[0]
        if tag_type == TAG_END:
            return found
        name = _read_string(f)
        if tag_type == TAG_INT and name == "DataVersion" and found is None:
            found = _read_int(f)
        elif tag_type == TAG_COMPOUND and depth_left > 0:
            nested = _scan_compound(f, depth_left - 1)
            if found is None:
                found = nested
        else:
            _skip_payload(f, tag_type)


def _skip_payload(f: BinaryIO, tag_type: int) -> None:
    """Consume and discard the payload of one tag of the given type."""
    if tag_type == TAG_END:
        return
    if tag_type == TAG_BYTE:
        _read(f, 1); return
    if tag_type == TAG_SHORT:
        _read(f, 2); return
    if tag_type == TAG_INT:
        _read(f, 4); return
    if tag_type == TAG_LONG:
        _read(f, 8); return
    if tag_type == TAG_FLOAT:
        _read(f, 4); return
    if tag_type == TAG_DOUBLE:
        _read(f, 8); return
    if tag_type == TAG_BYTE_ARRAY:
        n = _read_int(f); _read(f, n); return
    if tag_type == TAG_STRING:
        n = _read_ushort(f); _read(f, n); return
    if tag_type == TAG_LIST:
        elt_type = _read(f, 1)[0]
        n = _read_int(f)
        for _ in range(max(0, n)):
            _skip_payload(f, elt_type)
        return
    if tag_type == TAG_COMPOUND:
        while True:
            sub_type = _read(f, 1)[0]
            if sub_type == TAG_END:
                return
            _read_string(f)
            _skip_payload(f, sub_type)
    if tag_type == TAG_INT_ARRAY:
        n = _read_int(f); _read(f, n * 4); return
    if tag_type == TAG_LONG_ARRAY:
        n = _read_int(f); _read(f, n * 8); return
    raise NBTError(f"unknown NBT tag type: {tag_type}")


# ---- test helper: minimal NBT builder ---------------------------------------

def build_nbt_compound(name: str, children: list[tuple[int, str, bytes]]) -> bytes:
    """Build a TAG_Compound NBT blob — used only by tests / fixtures.

    ``children`` is a list of (tag_type, name, payload_bytes). Caller encodes
    payloads — this helper just glues everything together.
    """
    out = bytearray()
    out.append(TAG_COMPOUND)
    name_bytes = name.encode("utf-8")
    out += len(name_bytes).to_bytes(2, "big")
    out += name_bytes
    for tag_type, child_name, payload in children:
        out.append(tag_type)
        n = child_name.encode("utf-8")
        out += len(n).to_bytes(2, "big")
        out += n
        out += payload
    out.append(TAG_END)
    return bytes(out)
