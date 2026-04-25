from __future__ import annotations

import gzip
import zlib

import pytest

from chunkvault.mca.nbt_lite import (
    NBTError,
    TAG_COMPOUND,
    TAG_END,
    TAG_INT,
    TAG_LONG,
    TAG_STRING,
    build_nbt_compound,
    decompress_chunk_payload,
    find_data_version,
    find_last_played,
)


def _int_payload(value: int) -> bytes:
    return value.to_bytes(4, "big", signed=True)


def _string_payload(value: str) -> bytes:
    b = value.encode("utf-8")
    return len(b).to_bytes(2, "big") + b


# ---- decompress_chunk_payload ----------------------------------------------

def test_decompress_zlib():
    raw = b"hello world"
    assert decompress_chunk_payload(2, zlib.compress(raw)) == raw


def test_decompress_gzip():
    raw = b"hello gzip"
    assert decompress_chunk_payload(1, gzip.compress(raw)) == raw


def test_decompress_uncompressed():
    assert decompress_chunk_payload(3, b"plain") == b"plain"


def test_decompress_external_flag_masked():
    """0x80 bit should be masked — external zlib still dispatches to zlib."""
    raw = b"external zlib"
    assert decompress_chunk_payload(0x82, zlib.compress(raw)) == raw


def test_decompress_lz4_raises():
    with pytest.raises(NotImplementedError):
        decompress_chunk_payload(4, b"anything")


def test_decompress_unknown_raises():
    with pytest.raises(NBTError):
        decompress_chunk_payload(99, b"x")


# ---- find_data_version ------------------------------------------------------

def test_data_version_at_root():
    """Post-1.18 flat layout: DataVersion is a direct child of the root compound."""
    nbt = build_nbt_compound("", [
        (TAG_INT, "DataVersion", _int_payload(3700)),
        (TAG_STRING, "Status", _string_payload("full")),
    ])
    assert find_data_version(nbt) == 3700


def test_data_version_in_level_compound():
    """Pre-1.18 layout: DataVersion nested one level deep, typically in `Level`."""
    level_children = [
        (TAG_INT, "DataVersion", _int_payload(2230)),
        (TAG_INT, "xPos", _int_payload(0)),
    ]
    inner = build_nbt_compound("Level", level_children)
    # The inner itself begins with its own TAG_COMPOUND byte — for nesting we
    # want it inlined as a child. Strip that leading byte and name when we
    # reassemble at root level.
    # build_nbt_compound emits: TAG_COMPOUND | name | children | TAG_END
    # To nest, the OUTER root treats `inner` as a single (tag, name, payload)
    # triple. But our helper doesn't split that. Build manually instead.
    root = bytearray()
    root.append(TAG_COMPOUND)
    root += b"\x00\x00"  # empty root name

    # Child 1: Level compound. Layout: tag_type | name_len | name | payload
    root.append(TAG_COMPOUND)
    root += b"\x00\x05" + b"Level"
    # Inline the Level compound's body (excl. leading compound-tag+name) by
    # encoding the children directly
    for tag_type, child_name, payload in level_children:
        root.append(tag_type)
        n = child_name.encode("utf-8")
        root += len(n).to_bytes(2, "big")
        root += n
        root += payload
    root.append(TAG_END)  # end of Level

    root.append(TAG_END)  # end of root
    assert find_data_version(bytes(root)) == 2230


def test_data_version_absent_returns_none():
    nbt = build_nbt_compound("", [
        (TAG_STRING, "Status", _string_payload("full")),
    ])
    assert find_data_version(nbt) is None


def test_root_level_wins_over_nested():
    """When both root and nested compounds have DataVersion, root wins."""
    root = bytearray()
    root.append(TAG_COMPOUND)
    root += b"\x00\x00"
    # Root DataVersion = 3000
    root.append(TAG_INT)
    root += b"\x00\x0B" + b"DataVersion"
    root += _int_payload(3000)
    # Nested Level with DataVersion = 1000
    root.append(TAG_COMPOUND)
    root += b"\x00\x05" + b"Level"
    root.append(TAG_INT)
    root += b"\x00\x0B" + b"DataVersion"
    root += _int_payload(1000)
    root.append(TAG_END)
    root.append(TAG_END)
    assert find_data_version(bytes(root)) == 3000


def test_malformed_nbt_returns_none():
    """Truncated NBT: return None gracefully, never raise."""
    assert find_data_version(b"\x0A\x00") is None
    assert find_data_version(b"") is None
    assert find_data_version(b"\xFF\xFF\xFF") is None


def test_non_compound_root_returns_none():
    """If root tag isn't a compound, we can't proceed."""
    assert find_data_version(b"\x03fake non-compound") is None


def _long_payload(value: int) -> bytes:
    return value.to_bytes(8, "big", signed=True)


# ---- find_last_played ------------------------------------------------------

def test_last_played_inside_data_compound():
    """Real layout: level.dat root → Data compound → LastPlayed (TAG_Long ms)."""
    last_played_ms = 1_700_000_000_000
    root = bytearray()
    root.append(TAG_COMPOUND)
    root += b"\x00\x00"  # empty root name
    root.append(TAG_COMPOUND)
    root += b"\x00\x04" + b"Data"
    root.append(TAG_LONG)
    root += b"\x00\x0A" + b"LastPlayed"
    root += _long_payload(last_played_ms)
    root.append(TAG_END)  # end of Data
    root.append(TAG_END)  # end of root
    assert find_last_played(bytes(root)) == last_played_ms


def test_last_played_at_root():
    """Some modded worlds put LastPlayed at root; we still find it."""
    root = bytearray()
    root.append(TAG_COMPOUND)
    root += b"\x00\x00"
    root.append(TAG_LONG)
    root += b"\x00\x0A" + b"LastPlayed"
    root += _long_payload(42)
    root.append(TAG_END)
    assert find_last_played(bytes(root)) == 42


def test_last_played_absent_returns_none():
    nbt = build_nbt_compound("", [
        (TAG_INT, "DataVersion", _int_payload(3700)),
    ])
    assert find_last_played(nbt) is None


def test_last_played_malformed_returns_none():
    assert find_last_played(b"") is None
    assert find_last_played(b"\xFF\xFF") is None


def test_round_trip_compressed_chunk():
    """Full pipeline: build NBT → zlib compress → decompress → extract."""
    nbt = build_nbt_compound("", [
        (TAG_INT, "DataVersion", _int_payload(3465)),
    ])
    compressed = zlib.compress(nbt)
    decompressed = decompress_chunk_payload(2, compressed)
    assert find_data_version(decompressed) == 3465
