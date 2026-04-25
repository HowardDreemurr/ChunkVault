"""Block palette extractor tests.

We build NBT by hand to cover both chunk layouts without needing a real MC
world. The helper ``_compound_payload`` builds a nested-compound payload
(without its outer tag-type+name prefix) so we can embed it as a list element.
"""
from __future__ import annotations

import zlib
from pathlib import Path

from chunkvault.mca.nbt_lite import (
    TAG_COMPOUND,
    TAG_END,
    TAG_INT,
    TAG_LIST,
    TAG_STRING,
    build_nbt_compound,
)
from chunkvault.mca.region import RawChunk, Region
from chunkvault.mca.semantic import chunk_block_palette

from tests._fixtures import ChunkSpec, build_mca


def _str_payload(s: str) -> bytes:
    b = s.encode("utf-8")
    return len(b).to_bytes(2, "big") + b


def _int_payload(v: int) -> bytes:
    return v.to_bytes(4, "big", signed=True)


def _compound_body(children: list[tuple[int, str, bytes]]) -> bytes:
    """Encode just the BODY of a compound (children + TAG_END), no outer
    tag/name prefix. Used as a list element."""
    out = bytearray()
    for tag_type, name, payload in children:
        out.append(tag_type)
        n = name.encode("utf-8")
        out += len(n).to_bytes(2, "big")
        out += n
        out += payload
    out.append(TAG_END)
    return bytes(out)


def _list_payload(elt_type: int, elements: list[bytes]) -> bytes:
    """Encode a TAG_List payload: element type + count + elements."""
    out = bytearray()
    out.append(elt_type)
    out += len(elements).to_bytes(4, "big", signed=True)
    for e in elements:
        out += e
    return bytes(out)


def _make_chunk(nbt: bytes) -> tuple[Region, RawChunk]:
    data = build_mca([
        ChunkSpec(cx=0, cz=0, timestamp=0, compression=2, payload=zlib.compress(nbt)),
    ])
    region = Region.from_bytes(data, rx=0, rz=0)
    (chunk,) = list(region.iter_chunks())
    return region, chunk


# ---- 1.18+ flat layout (sections[i].block_states.palette) ------------------

def test_palette_flat_post_1_18():
    """Root → sections (list) → each has block_states.palette[]."""
    # One palette entry: {Name: "minecraft:stone"}
    stone_entry = _compound_body([
        (TAG_STRING, "Name", _str_payload("minecraft:stone")),
    ])
    dirt_entry = _compound_body([
        (TAG_STRING, "Name", _str_payload("minecraft:dirt")),
    ])
    # block_states compound (embedded in section compound body)
    block_states_body = bytearray()
    # Child: palette (TAG_List of TAG_Compound)
    block_states_body.append(TAG_LIST)
    name = b"palette"
    block_states_body += len(name).to_bytes(2, "big") + name
    block_states_body += _list_payload(TAG_COMPOUND, [stone_entry, dirt_entry])
    block_states_body.append(TAG_END)  # end of block_states

    # Section compound body
    section_body = bytearray()
    section_body.append(TAG_COMPOUND)
    nm = b"block_states"
    section_body += len(nm).to_bytes(2, "big") + nm
    section_body += bytes(block_states_body)
    section_body.append(TAG_END)  # end of section

    # Root: sections list containing one section compound
    root_children = []
    root_list = _list_payload(TAG_COMPOUND, [bytes(section_body)])
    root_children.append((TAG_LIST, "sections", root_list))
    root_children.append((TAG_INT, "DataVersion", _int_payload(3700)))
    nbt = build_nbt_compound("", root_children)

    region, chunk = _make_chunk(nbt)
    palette = chunk_block_palette(region, chunk)
    assert palette == frozenset({"minecraft:stone", "minecraft:dirt"})


# ---- 1.13-1.17 nested layout (Level.Sections[i].Palette) --------------------

def test_palette_pre_1_18_level_nested():
    """Root → Level (compound) → Sections (list) → each has Palette[]."""
    stone = _compound_body([(TAG_STRING, "Name", _str_payload("minecraft:stone"))])
    iron = _compound_body([(TAG_STRING, "Name", _str_payload("minecraft:iron_ore"))])

    # Section body: Palette list
    section_body = bytearray()
    section_body.append(TAG_LIST)
    n = b"Palette"
    section_body += len(n).to_bytes(2, "big") + n
    section_body += _list_payload(TAG_COMPOUND, [stone, iron])
    section_body.append(TAG_END)

    # Level body: Sections list + DataVersion (int)
    level_body = bytearray()
    level_body.append(TAG_LIST)
    n = b"Sections"
    level_body += len(n).to_bytes(2, "big") + n
    level_body += _list_payload(TAG_COMPOUND, [bytes(section_body)])
    level_body.append(TAG_END)

    # Root with Level child
    root = bytearray()
    root.append(TAG_COMPOUND)
    root += b"\x00\x00"
    root.append(TAG_COMPOUND)
    root += b"\x00\x05" + b"Level"
    root += bytes(level_body)
    root.append(TAG_END)

    region, chunk = _make_chunk(bytes(root))
    palette = chunk_block_palette(region, chunk)
    assert palette == frozenset({"minecraft:stone", "minecraft:iron_ore"})


# ---- multi-section combining ------------------------------------------------

def test_palette_unions_multiple_sections():
    """Each section contributes its own palette; result is the union."""
    stone = _compound_body([(TAG_STRING, "Name", _str_payload("minecraft:stone"))])
    air = _compound_body([(TAG_STRING, "Name", _str_payload("minecraft:air"))])
    dirt = _compound_body([(TAG_STRING, "Name", _str_payload("minecraft:dirt"))])

    def section(palette_entries: list[bytes]) -> bytes:
        inner = bytearray()
        inner.append(TAG_LIST)
        nm = b"palette"
        inner += len(nm).to_bytes(2, "big") + nm
        inner += _list_payload(TAG_COMPOUND, palette_entries)
        inner.append(TAG_END)

        body = bytearray()
        body.append(TAG_COMPOUND)
        nm2 = b"block_states"
        body += len(nm2).to_bytes(2, "big") + nm2
        body += bytes(inner)
        body.append(TAG_END)
        return bytes(body)

    sections_list = _list_payload(TAG_COMPOUND, [
        section([stone, air]),
        section([dirt, air]),  # air appears in both
    ])
    nbt = build_nbt_compound("", [
        (TAG_LIST, "sections", sections_list),
    ])
    region, chunk = _make_chunk(nbt)
    palette = chunk_block_palette(region, chunk)
    assert palette == frozenset(
        {"minecraft:stone", "minecraft:air", "minecraft:dirt"}
    )


# ---- edge cases -------------------------------------------------------------

def test_palette_empty_when_absent():
    """A chunk NBT without any palette list returns an empty set."""
    nbt = build_nbt_compound("", [
        (TAG_INT, "DataVersion", _int_payload(3700)),
    ])
    region, chunk = _make_chunk(nbt)
    assert chunk_block_palette(region, chunk) == frozenset()


def test_palette_empty_for_corrupt_zlib():
    from tests._fixtures import ChunkSpec as CS
    data = build_mca([
        CS(cx=0, cz=0, timestamp=0, compression=2, payload=b"not zlib"),
    ])
    region = Region.from_bytes(data, rx=0, rz=0)
    (chunk,) = list(region.iter_chunks())
    assert chunk_block_palette(region, chunk) == frozenset()


def test_palette_empty_for_lz4_chunk():
    from tests._fixtures import ChunkSpec as CS
    data = build_mca([
        CS(cx=0, cz=0, timestamp=0, compression=4, payload=b"lz4 fake"),
    ])
    region = Region.from_bytes(data, rx=0, rz=0)
    (chunk,) = list(region.iter_chunks())
    assert chunk_block_palette(region, chunk) == frozenset()


def test_palette_ignores_non_palette_lists():
    """Lists not named Palette/palette should be traversed but not collected."""
    some_compound_entry = _compound_body([
        (TAG_STRING, "Name", _str_payload("not_a_block_do_not_collect")),
    ])
    other_list = _list_payload(TAG_COMPOUND, [some_compound_entry])
    nbt = build_nbt_compound("", [
        (TAG_LIST, "Entities", other_list),
    ])
    region, chunk = _make_chunk(nbt)
    assert chunk_block_palette(region, chunk) == frozenset()
