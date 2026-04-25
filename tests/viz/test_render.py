"""Tests for the chunk → tile renderer (chunkvault.viz.render)."""
from __future__ import annotations

import struct
import zlib

import pytest

from chunkvault.mca.nbt_lite import (
    TAG_BYTE, TAG_COMPOUND, TAG_END, TAG_INT, TAG_LIST, TAG_LONG_ARRAY,
    TAG_STRING,
)
from chunkvault.viz.colors import AIR_RGB, color_for_block
from chunkvault.viz.render import (
    RENDER_MODES, RenderError, TILE_BYTES,
    render_chunk_blob, render_chunk_nbt,
)


# ---- minimal NBT chunk builders (1.18+ format) -----------------------------

def _str_payload(s: str) -> bytes:
    b = s.encode("utf-8")
    return len(b).to_bytes(2, "big") + b


def _named(tag_type: int, name: str, payload: bytes) -> bytes:
    n = name.encode("utf-8")
    return bytes([tag_type]) + len(n).to_bytes(2, "big") + n + payload


def _build_compound(*children_bytes: bytes) -> bytes:
    """Wrap raw child payloads in a TAG_Compound body (caller adds outer
    type+name bytes if nesting it inside a parent)."""
    body = bytearray()
    for c in children_bytes:
        body += c
    body.append(TAG_END)
    return bytes(body)


def _build_list(elt_type: int, *elt_payloads: bytes) -> bytes:
    """A TAG_List body: 1 byte elt type, 4 bytes len, then payloads inline."""
    body = bytearray()
    body.append(elt_type)
    body += len(elt_payloads).to_bytes(4, "big", signed=True)
    for p in elt_payloads:
        body += p
    return bytes(body)


def _palette_entry(block_name: str) -> bytes:
    """One palette compound: TAG_String 'Name' = block_name."""
    return _build_compound(_named(TAG_STRING, "Name", _str_payload(block_name)))


def _build_section_uniform(section_y: int, block_name: str) -> bytes:
    """Section with palette of size 1 — no 'data' field needed.

    1.18+ omits ``data`` when the entire 4096-block volume is one block.
    """
    palette = _build_list(TAG_COMPOUND, _palette_entry(block_name))
    block_states = _build_compound(_named(TAG_LIST, "palette", palette))
    return _build_compound(
        _named(TAG_BYTE, "Y", section_y.to_bytes(1, "big", signed=True)),
        _named(TAG_COMPOUND, "block_states", block_states),
    )


def _pack_no_spanning(values: list[int], bits: int) -> list[int]:
    """Pack ``values`` into longs at ``bits`` bits each, no spanning (1.16+)."""
    per_long = 64 // bits
    out: list[int] = []
    for chunk_start in range(0, len(values), per_long):
        word = 0
        for i, v in enumerate(values[chunk_start:chunk_start + per_long]):
            word |= (v & ((1 << bits) - 1)) << (i * bits)
        out.append(word)
    return out


def _signed_long(u64: int) -> int:
    """Convert unsigned 64-bit to Python signed int for storage."""
    return u64 - (1 << 64) if u64 >= (1 << 63) else u64


def _build_section_packed(
    section_y: int, palette_names: list[str], indices_4096: list[int],
) -> bytes:
    """Section with multi-entry palette + packed data (1.16+ no-spanning)."""
    bits = max(4, max(1, (len(palette_names) - 1).bit_length()))
    packed_unsigned = _pack_no_spanning(indices_4096, bits)
    packed_signed = [_signed_long(w) for w in packed_unsigned]
    data_payload = (
        len(packed_signed).to_bytes(4, "big", signed=True)
        + b"".join(v.to_bytes(8, "big", signed=True) for v in packed_signed)
    )
    palette_entries = [_palette_entry(n) for n in palette_names]
    palette = _build_list(TAG_COMPOUND, *palette_entries)
    block_states = _build_compound(
        _named(TAG_LIST, "palette", palette),
        _named(TAG_LONG_ARRAY, "data", data_payload),
    )
    return _build_compound(
        _named(TAG_BYTE, "Y", section_y.to_bytes(1, "big", signed=True)),
        _named(TAG_COMPOUND, "block_states", block_states),
    )


def _build_heightmap(per_xz_y: list[int], min_y: int = -64) -> bytes:
    """Pack a 256-element WORLD_SURFACE heightmap (1.16+ no-spanning, 9-bit)."""
    assert len(per_xz_y) == 256
    relative = [y - min_y for y in per_xz_y]
    packed_unsigned = _pack_no_spanning(relative, 9)
    packed_signed = [_signed_long(w) for w in packed_unsigned]
    return (
        len(packed_signed).to_bytes(4, "big", signed=True)
        + b"".join(v.to_bytes(8, "big", signed=True) for v in packed_signed)
    )


def _build_chunk_1_18(
    sections: list[bytes],
    *,
    data_version: int = 3700,
    y_pos: int = -4,
    heightmap: bytes | None = None,
) -> bytes:
    """Build a complete 1.18+ chunk NBT root."""
    children = [
        _named(TAG_INT, "DataVersion",
               data_version.to_bytes(4, "big", signed=True)),
        _named(TAG_INT, "xPos", (0).to_bytes(4, "big", signed=True)),
        _named(TAG_INT, "yPos", y_pos.to_bytes(4, "big", signed=True)),
        _named(TAG_INT, "zPos", (0).to_bytes(4, "big", signed=True)),
    ]
    if heightmap is not None:
        heightmaps_compound = _build_compound(
            _named(TAG_LONG_ARRAY, "WORLD_SURFACE", heightmap),
        )
        children.append(_named(TAG_COMPOUND, "Heightmaps", heightmaps_compound))
    sections_list = _build_list(TAG_COMPOUND, *sections)
    children.append(_named(TAG_LIST, "sections", sections_list))
    body = _build_compound(*children)
    # Root: TAG_Compound + empty name + body
    return bytes([TAG_COMPOUND]) + b"\x00\x00" + body


# ---- actual tests ----------------------------------------------------------

def test_render_returns_correct_size():
    nbt = _build_chunk_1_18([_build_section_uniform(0, "minecraft:grass_block")])
    out = render_chunk_nbt(nbt, "topdown")
    assert len(out) == TILE_BYTES


def test_render_unknown_mode_raises():
    nbt = _build_chunk_1_18([_build_section_uniform(0, "minecraft:stone")])
    with pytest.raises(RenderError, match="unknown render mode"):
        render_chunk_nbt(nbt, "fictional")


def test_render_empty_chunk_returns_air_tile():
    """No sections at all → all-air tile."""
    nbt = _build_chunk_1_18([])
    out = render_chunk_nbt(nbt, "topdown")
    air_r, air_g, air_b = AIR_RGB
    assert out[0] == air_r and out[1] == air_g and out[2] == air_b


def test_topdown_uses_heightmap():
    """Heightmap pointing at section Y=4 (y=64) → block at that y wins."""
    # Section at Y=4 (y=64..79) full of stone; heightmap says top is y=64.
    sections = [_build_section_uniform(4, "minecraft:stone")]
    # Heightmap value = 65 (one above the topmost solid, as MC stores it)
    heightmap = _build_heightmap([65] * 256)
    nbt = _build_chunk_1_18(sections, heightmap=heightmap)
    out = render_chunk_nbt(nbt, "topdown")

    expected = color_for_block("minecraft:stone")
    # Every pixel should be stone-colored
    for i in range(0, TILE_BYTES, 3):
        assert (out[i], out[i + 1], out[i + 2]) == expected, \
            f"pixel {i // 3} mismatch"


def test_topdown_air_tile_when_heightmap_below_floor():
    """If heightmap says everything is below world floor, render all AIR."""
    sections = [_build_section_uniform(4, "minecraft:stone")]
    heightmap = _build_heightmap([-64] * 256)  # all at floor → y_top = -65 < min_y
    nbt = _build_chunk_1_18(sections, heightmap=heightmap)
    out = render_chunk_nbt(nbt, "topdown")
    air_r, air_g, air_b = AIR_RGB
    assert (out[0], out[1], out[2]) == (air_r, air_g, air_b)


def test_nether_low_scans_to_y_80():
    """nether_low mode finds the topmost block at y ≤ 80 by scanning down."""
    cap = RENDER_MODES["nether_low"]["y_cap"]
    assert cap == 80
    # Section Y=4 covers y=64..79. Fill it with netherrack.
    sections = [_build_section_uniform(4, "minecraft:netherrack")]
    nbt = _build_chunk_1_18(sections, y_pos=0)
    out = render_chunk_nbt(nbt, "nether_low")
    expected = color_for_block("minecraft:netherrack")
    assert (out[0], out[1], out[2]) == expected


def test_nether_high_finds_blocks_above_low_cap():
    """A block at y=100 should appear in nether_high but not in nether_low."""
    # Section Y=6 covers y=96..111. Put basalt there.
    sections = [_build_section_uniform(6, "minecraft:basalt")]
    nbt = _build_chunk_1_18(sections, y_pos=0)

    high = render_chunk_nbt(nbt, "nether_high")
    low = render_chunk_nbt(nbt, "nether_low")

    basalt = color_for_block("minecraft:basalt")
    air_r, air_g, air_b = AIR_RGB
    # high captures basalt
    assert (high[0], high[1], high[2]) == basalt
    # low sees nothing solid below y=80
    assert (low[0], low[1], low[2]) == (air_r, air_g, air_b)


def test_packed_section_decodes_per_position():
    """Multi-entry palette + packed data: each (x,z) reads its own block."""
    palette = ["minecraft:air", "minecraft:grass_block", "minecraft:stone"]
    # 4096-block volume: index by (y * 256 + z * 16 + x)
    # Put grass at (x=0, z=0, y=0); stone at (x=15, z=15, y=0); air everywhere else
    indices = [0] * 4096
    indices[0 * 256 + 0 * 16 + 0] = 1   # grass at (0,0,0) within section
    indices[0 * 256 + 15 * 16 + 15] = 2  # stone at (15,15,0)
    sections = [_build_section_packed(4, palette, indices)]
    # Heightmap: y=65 for (0,0) and (15,15), -64 elsewhere → only those have content
    hm = [-64] * 256
    hm[0 * 16 + 0] = 65
    hm[15 * 16 + 15] = 65
    heightmap = _build_heightmap(hm)
    nbt = _build_chunk_1_18(sections, heightmap=heightmap)
    out = render_chunk_nbt(nbt, "topdown")

    grass = color_for_block("minecraft:grass_block")
    stone = color_for_block("minecraft:stone")
    # Pixel (0,0)
    i = 0
    assert (out[i], out[i + 1], out[i + 2]) == grass, "grass corner mismatch"
    # Pixel (15,15) → row 15, col 15 → byte offset (15*16 + 15) * 3 = 765
    j = (15 * 16 + 15) * 3
    assert (out[j], out[j + 1], out[j + 2]) == stone, "stone corner mismatch"


def test_render_chunk_blob_handles_compression():
    """The blob entry point strips the leading compression byte and decompresses."""
    nbt = _build_chunk_1_18([_build_section_uniform(0, "minecraft:grass_block")])
    blob = bytes([2]) + zlib.compress(nbt)  # 2 = zlib
    out = render_chunk_blob(blob, "topdown")
    grass = color_for_block("minecraft:grass_block")
    assert (out[0], out[1], out[2]) == grass


def test_render_chunk_blob_empty_returns_air():
    out = render_chunk_blob(b"", "topdown")
    assert len(out) == TILE_BYTES
    air_r, air_g, air_b = AIR_RGB
    assert (out[0], out[1], out[2]) == (air_r, air_g, air_b)


def test_render_chunk_blob_corrupt_raises():
    with pytest.raises(RenderError):
        render_chunk_blob(bytes([2]) + b"not valid zlib", "topdown")


def test_render_modes_constant_includes_three():
    assert set(RENDER_MODES) == {"topdown", "nether_low", "nether_high"}
