"""Semantic layer over raw MCA reads — opt-in, lazy, error-tolerant.

For day-to-day backup / diff we work at the byte/hash level and never
decompress. This module is what you reach for when you want to ask higher-
level questions about a chunk — today just "what MC version wrote it?", but
the same idea extends to block counts, tile-entity surveys, etc.
"""
from __future__ import annotations

from pathlib import Path

import io

from .hasher import external_chunk_path
from .nbt_lite import (
    NBTError,
    TAG_COMPOUND,
    TAG_END,
    TAG_LIST,
    TAG_STRING,
    _read,
    _read_int,
    _read_string,
    _read_ushort,
    _skip_payload,
    decompress_chunk_payload,
    find_data_version,
)
from .region import RawChunk, Region


def chunk_data_version(region: Region, chunk: RawChunk) -> int | None:
    """Return the chunk's ``DataVersion`` (MC revision id) or ``None``.

    ``None`` means any of:
      - decompression failed (corrupt payload / unsupported compression)
      - the NBT didn't contain a DataVersion tag (pre-1.9)
      - external chunk's .mcc file is missing

    Never raises on malformed data; backup tooling should keep going.
    """
    if chunk.external:
        mcc = external_chunk_path(region, chunk)
        if not mcc.exists():
            return None
        try:
            raw = mcc.read_bytes()
        except OSError:
            return None
    else:
        raw = chunk.payload

    try:
        nbt_bytes = decompress_chunk_payload(chunk.compression, raw)
    except (NotImplementedError, Exception):
        return None
    return find_data_version(nbt_bytes)


def chunk_block_palette(region: Region, chunk: RawChunk) -> frozenset[str]:
    """Return the set of unique block names present in a chunk.

    Supports the two modern chunk formats:

    * **1.13 – 1.17 (Caves and Cliffs preview excluded)**: palette lives at
      ``Level.Sections[i].Palette[].Name``.
    * **1.18+**: palette lives at ``sections[i].block_states.palette[].Name``.

    Pre-1.13 chunks stored blocks as numeric IDs with no palette; without an
    amulet-core style ID → name mapping we can't enumerate them, so we return
    an empty set. A corrupt or unsupported chunk also returns empty (never
    raises) — this is an analysis helper, not a correctness-critical path.
    """
    nbt = _decompress(region, chunk)
    if nbt is None:
        return frozenset()
    try:
        return _scan_palette(nbt)
    except (NBTError, ValueError, IndexError, UnicodeDecodeError):
        return frozenset()


def _decompress(region: Region, chunk: RawChunk) -> bytes | None:
    if chunk.external:
        try:
            mcc = external_chunk_path(region, chunk)
        except ValueError:
            return None
        if not mcc.exists():
            return None
        try:
            raw = mcc.read_bytes()
        except OSError:
            return None
    else:
        raw = chunk.payload
    try:
        return decompress_chunk_payload(chunk.compression, raw)
    except Exception:
        return None


def _scan_palette(nbt_bytes: bytes) -> frozenset[str]:
    """Walk the NBT tree collecting every palette-entry Name string."""
    f = io.BytesIO(nbt_bytes)
    root_type = _read(f, 1)[0]
    if root_type != TAG_COMPOUND:
        return frozenset()
    _read_string(f)  # root name
    collected: set[str] = set()
    _walk_compound_for_palette(f, collected, in_palette_context=False)
    return frozenset(collected)


def _walk_compound_for_palette(
    f: io.BytesIO, out: set[str], *, in_palette_context: bool,
) -> None:
    """Traverse a compound, recursing into interesting subtrees.

    ``in_palette_context`` means the current compound is a palette entry,
    so we should capture its ``Name`` string directly.
    """
    while True:
        tag_type = _read(f, 1)[0]
        if tag_type == TAG_END:
            return
        name = _read_string(f)

        if in_palette_context and tag_type == TAG_STRING and name == "Name":
            length = _read_ushort(f)
            out.add(_read(f, length).decode("utf-8", errors="replace"))
            continue

        if tag_type == TAG_LIST:
            elt_type = _read(f, 1)[0]
            count = _read_int(f)
            # Lists named "Palette" (1.13-1.17) or "palette" (1.18+) whose
            # elements are compounds contain the block-type dicts.
            is_palette_list = (
                name in ("Palette", "palette") and elt_type == TAG_COMPOUND
            )
            for _ in range(max(0, count)):
                if is_palette_list:
                    _walk_compound_for_palette(f, out, in_palette_context=True)
                elif elt_type == TAG_COMPOUND:
                    _walk_compound_for_palette(f, out, in_palette_context=False)
                else:
                    _skip_payload(f, elt_type)
            continue

        if tag_type == TAG_COMPOUND:
            _walk_compound_for_palette(f, out, in_palette_context=False)
            continue

        _skip_payload(f, tag_type)


def chunk_data_version_at(
    world_root: Path | str,
    dimension_key: str,
    world_cx: int,
    world_cz: int,
) -> int | None:
    """Look up a chunk's DataVersion by world-space coordinates.

    Convenience for callers that already have world-space coords (e.g. from
    a ``ChunkDiff``) and don't want to manage the ``Region`` object. Returns
    ``None`` if the region file or chunk is absent, or if NBT extraction
    fails for any reason.
    """
    root = Path(world_root)
    rx = world_cx >> 5  # floor div by 32 for negatives
    rz = world_cz >> 5
    lcx = world_cx & 31
    lcz = world_cz & 31
    dim_dir = root.joinpath(*dimension_key.split("/"))
    region_path = dim_dir / f"r.{rx}.{rz}.mca"
    if not region_path.is_file():
        return None
    try:
        region = Region(region_path)
        chunk = region.get_chunk(lcx, lcz)
    except Exception:
        return None
    if chunk is None:
        return None
    return chunk_data_version(region, chunk)
