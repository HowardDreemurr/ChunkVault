"""Render a chunk's NBT to a 16x16 RGB tile (768 bytes raw).

Three rendering modes:

* ``topdown`` — overworld + end view. For each (x,z), use the chunk's
  WORLD_SURFACE heightmap to jump straight to the highest non-air block
  and color it with that block's Mojang map color.
* ``nether_low`` — nether view at "where overworld portals usually land"
  altitude. For each (x,z), scan downward from y=80 looking for the
  first non-air block; that's what an explorer near portal level
  would see from above.
* ``nether_high`` — nether view at the upper crust just below the
  bedrock ceiling. Same scan but starting from y=125. Captures the
  mountains / nylium plateaus that rise toward the ceiling.

Both nether modes are needed because the bedrock ceiling makes a single
"top-block" view useless (you'd see only bedrock everywhere). Splitting
the column into two altitude bands gives a usable preview of each layer.

Output is **raw RGB bytes** (768 = 16*16*3) so the tile pool can store
it without per-tile zlib overhead. Compression deferred to filesystem
level if anything.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from ..mca.nbt_lite import (
    NBTError,
    TAG_BYTE, TAG_BYTE_ARRAY, TAG_COMPOUND, TAG_DOUBLE, TAG_END,
    TAG_FLOAT, TAG_INT, TAG_INT_ARRAY, TAG_LIST, TAG_LONG,
    TAG_LONG_ARRAY, TAG_SHORT, TAG_STRING,
)
from .colors import AIR_RGB, color_for_block, is_air

TILE_BYTES = 16 * 16 * 3   # 768
SECTION_HEIGHT = 16

# Mode → (start_y, scan_strategy). For "heightmap" we jump directly via
# the WORLD_SURFACE heightmap; for "scan_below_y" we sweep downward from
# the cap y looking for the first non-air block.
RENDER_MODES = {
    "topdown":     {"strategy": "heightmap", "y_cap": None},
    "nether_low":  {"strategy": "scan_down", "y_cap": 80},
    "nether_high": {"strategy": "scan_down", "y_cap": 125},
}

# DataVersion thresholds for format quirks.
# 1.16 = DataVersion 2566 ⇒ block_states + heightmap stop spanning long
# boundaries (each long holds whole values only).
DATA_VERSION_1_16 = 2566
# 1.18 = DataVersion 2825 ⇒ chunk root flattened (no Level wrapper),
# sections at root, palette nested in block_states sub-compound.
DATA_VERSION_1_18 = 2825


class RenderError(Exception):
    """The chunk NBT was unparseable or used a format we can't render."""


# ---- public entry point ----------------------------------------------------

def render_chunk_nbt(nbt_bytes: bytes, mode: str = "topdown") -> bytes:
    """Render an already-decompressed chunk NBT to 768 raw RGB bytes.

    Returns a tile of all-AIR pixels if the chunk has no parseable data
    or no non-air blocks in the requested altitude band — empty chunks
    happen at world edges and aren't an error.
    """
    if mode not in RENDER_MODES:
        raise RenderError(f"unknown render mode: {mode!r}")
    try:
        chunk = _parse_chunk(nbt_bytes)
    except (NBTError, ValueError, IndexError, UnicodeDecodeError) as e:
        raise RenderError(f"chunk NBT parse failed: {e}") from e

    out = bytearray(TILE_BYTES)
    cfg = RENDER_MODES[mode]
    air_r, air_g, air_b = AIR_RGB
    for i in range(0, TILE_BYTES, 3):
        out[i] = air_r
        out[i + 1] = air_g
        out[i + 2] = air_b

    if not chunk.sections:
        return bytes(out)

    if cfg["strategy"] == "heightmap" and chunk.heightmap is not None:
        for x in range(16):
            for z in range(16):
                y_top = chunk.heightmap_at(x, z) - 1
                if y_top < chunk.min_y:
                    continue
                color = chunk.block_color_at(x, y_top, z)
                _write_pixel(out, x, z, color)
    else:
        # scan_down — used by both nether modes AND by topdown when the
        # heightmap is missing (corrupt chunk, or test fixtures that skip it).
        # For topdown, cap from the highest section we actually saw.
        if cfg["strategy"] == "heightmap":
            y_cap = max(chunk.sections) * SECTION_HEIGHT + (SECTION_HEIGHT - 1)
        else:
            y_cap = cfg["y_cap"]
        for x in range(16):
            for z in range(16):
                color = None
                for y in range(y_cap, chunk.min_y - 1, -1):
                    name = chunk.block_name_at(x, y, z)
                    if name is None or is_air(name):
                        continue
                    color = color_for_block(name)
                    break
                if color is not None:
                    _write_pixel(out, x, z, color)
    return bytes(out)


def render_chunk_blob(blob: bytes, mode: str = "topdown") -> bytes:
    """Render from the raw blob format used in the chunk pool.

    Pool blobs are ``[masked_compression_byte] + payload``. We strip the
    leading byte, decompress per its scheme, and feed the result to
    :func:`render_chunk_nbt`.
    """
    if not blob:
        return _blank_tile()
    from ..mca.nbt_lite import decompress_chunk_payload
    try:
        nbt = decompress_chunk_payload(blob[0], blob[1:])
    except Exception as e:
        raise RenderError(f"chunk blob decompress failed: {e}") from e
    return render_chunk_nbt(nbt, mode)


def _write_pixel(out: bytearray, x: int, z: int, rgb: tuple[int, int, int]) -> None:
    # Z-major to match how MCA chunks index columns; output rows = z, cols = x.
    i = (z * 16 + x) * 3
    out[i] = rgb[0]
    out[i + 1] = rgb[1]
    out[i + 2] = rgb[2]


def _blank_tile() -> bytes:
    r, g, b = AIR_RGB
    return bytes((r, g, b)) * 256


# ---- chunk parser ---------------------------------------------------------

@dataclass
class _Section:
    section_y: int                        # int from -4 to 19 (1.18+ world-height range)
    palette: list[str]                    # block names, indexed by packed-data values
    data: list[int] | None                # packed long array, or None for single-block sections
    bits_per_index: int                   # ceil(log2(len(palette))), min 4 (or 1 for 1.18 with size 1)
    no_spanning: bool                     # True for 1.16+, False for older


@dataclass
class _Chunk:
    data_version: int
    sections: dict[int, _Section]
    heightmap: list[int] | None           # WORLD_SURFACE, 256 ints (y values), or None if missing
    min_y: int                             # world bottom, -64 for 1.18+, 0 for older

    def heightmap_at(self, x: int, z: int) -> int:
        if self.heightmap is None:
            return self.min_y
        return self.heightmap[z * 16 + x]

    def block_name_at(self, x: int, y: int, z: int) -> str | None:
        section_y = y >> 4
        sec = self.sections.get(section_y)
        if sec is None:
            return None
        local_y = y & 15
        idx_in_section = local_y * 256 + z * 16 + x
        if sec.data is None:
            # Single-block section (1.18+) — palette has exactly one entry
            palette_idx = 0
        else:
            palette_idx = _unpack_index(
                sec.data, idx_in_section, sec.bits_per_index, sec.no_spanning,
            )
        if palette_idx >= len(sec.palette):
            return None
        return sec.palette[palette_idx]

    def block_color_at(self, x: int, y: int, z: int) -> tuple[int, int, int]:
        name = self.block_name_at(x, y, z)
        if name is None:
            return AIR_RGB
        return color_for_block(name)


def _parse_chunk(nbt_bytes: bytes) -> _Chunk:
    """Walk the chunk NBT and pull out sections + heightmap + DataVersion."""
    parsed = _parse_nbt(nbt_bytes)
    if not isinstance(parsed, dict):
        raise NBTError("chunk root is not a compound")
    data_version = int(parsed.get("DataVersion", 0))

    # 1.18+ flattened the chunk root; 1.13-1.17 wrap everything in "Level".
    if data_version >= DATA_VERSION_1_18:
        root = parsed
        sections_field = root.get("sections", []) or []
        min_y = int(root.get("yPos", -4)) * SECTION_HEIGHT
        if min_y > -64:
            min_y = -64  # 1.18+ extended height
        heightmaps = root.get("Heightmaps") or {}
    else:
        level = parsed.get("Level", {}) or {}
        root = level
        sections_field = level.get("Sections", []) or []
        min_y = 0
        heightmaps = level.get("Heightmaps") or {}

    no_spanning = data_version >= DATA_VERSION_1_16
    sections: dict[int, _Section] = {}
    for sec in sections_field:
        if not isinstance(sec, dict):
            continue
        sec_y = int(sec.get("Y", 0))
        # 1.18+: palette/data nested under block_states
        if "block_states" in sec:
            bs = sec["block_states"] or {}
            palette_raw = bs.get("palette", []) or []
            data_raw = bs.get("data")
        elif "Palette" in sec:
            # 1.13-1.17 flat fields
            palette_raw = sec.get("Palette", []) or []
            data_raw = sec.get("BlockStates")
        else:
            continue

        palette: list[str] = []
        for entry in palette_raw:
            if isinstance(entry, dict):
                name = entry.get("Name")
                palette.append(str(name) if name else "minecraft:air")
            else:
                palette.append("minecraft:air")
        if not palette:
            continue

        if data_raw is None:
            # Section is uniform (whole 4096-block volume = palette[0]).
            sections[sec_y] = _Section(
                section_y=sec_y, palette=palette, data=None,
                bits_per_index=0, no_spanning=no_spanning,
            )
            continue

        data = list(data_raw)
        # bits_per_index = max(4, ceil(log2(palette_size))) for vanilla.
        # 1.18+ allows 1-3 bits when palette is small enough, but in practice
        # the rule is the same and we just compute it.
        size = len(palette)
        bits = max(4, _bit_length(size - 1)) if size > 1 else 1
        sections[sec_y] = _Section(
            section_y=sec_y, palette=palette, data=data,
            bits_per_index=bits, no_spanning=no_spanning,
        )

    heightmap_raw = heightmaps.get("WORLD_SURFACE")
    heightmap = None
    if heightmap_raw is not None:
        try:
            heightmap = _unpack_heightmap(list(heightmap_raw), no_spanning, min_y)
        except (ValueError, IndexError):
            heightmap = None

    return _Chunk(
        data_version=data_version,
        sections=sections,
        heightmap=heightmap,
        min_y=min_y,
    )


def _bit_length(n: int) -> int:
    """Number of bits needed to represent ``n`` (n=0 → 1, n=15 → 4, n=16 → 5)."""
    if n <= 0:
        return 1
    return n.bit_length()


def _unpack_index(
    data: list[int], idx: int, bits: int, no_spanning: bool,
) -> int:
    """Read packed ``idx``-th value from ``data`` (a long array)."""
    if no_spanning:
        # Each long fits floor(64 / bits) values; high bits unused.
        per_long = 64 // bits
        long_idx = idx // per_long
        offset = (idx % per_long) * bits
        if long_idx >= len(data):
            return 0
        # SQLite returns longs as signed Python ints; mask back to unsigned.
        word = data[long_idx] & ((1 << 64) - 1)
        return (word >> offset) & ((1 << bits) - 1)
    # Pre-1.16: bits span across long boundaries.
    bit_idx = idx * bits
    long_idx = bit_idx // 64
    offset = bit_idx % 64
    if long_idx >= len(data):
        return 0
    word = data[long_idx] & ((1 << 64) - 1)
    value = word >> offset
    used = 64 - offset
    if used < bits and long_idx + 1 < len(data):
        next_word = data[long_idx + 1] & ((1 << 64) - 1)
        value |= next_word << used
    return value & ((1 << bits) - 1)


def _unpack_heightmap(
    data: list[int], no_spanning: bool, min_y: int,
) -> list[int]:
    """Unpack the 256 9-bit values from a WORLD_SURFACE heightmap.

    Returns a list of 256 ints (z-major). The stored values are heights
    relative to the world bottom; we add ``min_y`` to convert back to
    absolute world-y.
    """
    out: list[int] = [min_y] * 256
    bits = 9
    for i in range(256):
        v = _unpack_index(data, i, bits, no_spanning)
        out[i] = v + min_y
    return out


# ---- minimal generic NBT parser -------------------------------------------
#
# We only ever parse decompressed chunk NBT here; perf is fine. Building
# Python dicts/lists keeps the rendering logic above readable.

def _parse_nbt(nbt_bytes: bytes) -> Any:
    """Parse a complete NBT blob. Returns the root tag's payload."""
    f = io.BytesIO(nbt_bytes)
    root_type = f.read(1)
    if not root_type:
        raise NBTError("empty NBT")
    rt = root_type[0]
    if rt == TAG_END:
        return None
    _read_string(f)  # root name (usually "")
    return _read_payload(f, rt)


def _read_payload(f: io.BytesIO, tag_type: int) -> Any:
    if tag_type == TAG_END:
        return None
    if tag_type == TAG_BYTE:
        return int.from_bytes(_read_n(f, 1), "big", signed=True)
    if tag_type == TAG_SHORT:
        return int.from_bytes(_read_n(f, 2), "big", signed=True)
    if tag_type == TAG_INT:
        return int.from_bytes(_read_n(f, 4), "big", signed=True)
    if tag_type == TAG_LONG:
        return int.from_bytes(_read_n(f, 8), "big", signed=True)
    if tag_type == TAG_FLOAT:
        import struct
        return struct.unpack(">f", _read_n(f, 4))[0]
    if tag_type == TAG_DOUBLE:
        import struct
        return struct.unpack(">d", _read_n(f, 8))[0]
    if tag_type == TAG_BYTE_ARRAY:
        n = int.from_bytes(_read_n(f, 4), "big", signed=True)
        return list(_read_n(f, n))
    if tag_type == TAG_STRING:
        return _read_string(f)
    if tag_type == TAG_LIST:
        elt_type = _read_n(f, 1)[0]
        n = int.from_bytes(_read_n(f, 4), "big", signed=True)
        return [_read_payload(f, elt_type) for _ in range(max(0, n))]
    if tag_type == TAG_COMPOUND:
        out: dict[str, Any] = {}
        while True:
            sub_type_b = f.read(1)
            if not sub_type_b:
                raise NBTError("unexpected EOF in compound")
            sub_type = sub_type_b[0]
            if sub_type == TAG_END:
                return out
            name = _read_string(f)
            out[name] = _read_payload(f, sub_type)
    if tag_type == TAG_INT_ARRAY:
        n = int.from_bytes(_read_n(f, 4), "big", signed=True)
        return [int.from_bytes(_read_n(f, 4), "big", signed=True) for _ in range(n)]
    if tag_type == TAG_LONG_ARRAY:
        n = int.from_bytes(_read_n(f, 4), "big", signed=True)
        return [int.from_bytes(_read_n(f, 8), "big", signed=True) for _ in range(n)]
    raise NBTError(f"unknown NBT tag type: {tag_type}")


def _read_n(f: io.BytesIO, n: int) -> bytes:
    b = f.read(n)
    if len(b) < n:
        raise NBTError(f"unexpected EOF (wanted {n}, got {len(b)})")
    return b


def _read_string(f: io.BytesIO) -> str:
    n = int.from_bytes(_read_n(f, 2), "big", signed=False)
    return _read_n(f, n).decode("utf-8", errors="replace")
