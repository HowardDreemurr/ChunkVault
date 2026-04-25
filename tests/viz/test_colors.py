"""Tests for the Mojang map color table + block→color lookup."""
from __future__ import annotations

from chunkvault.viz.colors import (
    AIR_RGB,
    UNMAPPED_RGB,
    color_for_block,
    color_for_id,
    is_air,
)


def test_known_overworld_blocks_have_distinct_colors():
    grass = color_for_block("minecraft:grass_block")
    stone = color_for_block("minecraft:stone")
    water = color_for_block("minecraft:water")
    sand = color_for_block("minecraft:sand")
    assert grass != stone != water != sand
    # Spot-check: grass should be greenish
    assert grass[1] > grass[0] and grass[1] > grass[2]


def test_air_blocks_render_as_air():
    assert color_for_block("minecraft:air") == AIR_RGB
    assert color_for_block("minecraft:cave_air") == AIR_RGB
    assert color_for_block("minecraft:void_air") == AIR_RGB


def test_unknown_block_falls_back_to_magenta():
    """Unmapped blocks should pop visually so we notice gaps in the table."""
    assert color_for_block("modded:something_we_dont_know") == UNMAPPED_RGB
    assert color_for_block("minecraft:fictional_block") == UNMAPPED_RGB


def test_nether_blocks_have_red_tint():
    """Nether terrain colors lean red — netherrack, nether bricks, magma."""
    netherrack = color_for_block("minecraft:netherrack")
    assert netherrack[0] > netherrack[1]
    assert netherrack[0] > netherrack[2]


def test_end_blocks_off_white():
    end = color_for_block("minecraft:end_stone")
    # end_stone uses QUARTZ map color — near-white
    assert min(end) > 200


def test_wood_variants_distinguishable():
    """Different wood types map to different colors."""
    oak = color_for_block("minecraft:oak_planks")
    spruce = color_for_block("minecraft:spruce_planks")
    birch = color_for_block("minecraft:birch_planks")
    acacia = color_for_block("minecraft:acacia_planks")
    assert len({oak, spruce, birch, acacia}) == 4


def test_dye_color_consistency():
    """Same dye color across block types should map to similar colors.

    e.g. red_wool, red_concrete, red_stained_glass all use red dye color.
    """
    wool_red = color_for_block("minecraft:red_wool")
    concrete_red = color_for_block("minecraft:red_concrete")
    glass_red = color_for_block("minecraft:red_stained_glass")
    # All should be reddish (R > G, R > B)
    for c in (wool_red, concrete_red, glass_red):
        assert c[0] > c[1] and c[0] > c[2]


def test_color_for_id_returns_tuple():
    grass = color_for_id(1)
    assert grass == (127, 178, 56)
    # Out of range falls back to UNMAPPED
    assert color_for_id(999) == UNMAPPED_RGB


def test_is_air_helper():
    assert is_air("minecraft:air")
    assert is_air("minecraft:cave_air")
    assert is_air("minecraft:void_air")
    assert not is_air("minecraft:grass_block")
    assert not is_air("modded:thing")
