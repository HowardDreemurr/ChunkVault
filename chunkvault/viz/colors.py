"""Mojang's official Map Color palette.

Minecraft itself uses this 64-color palette to render in-game maps (the
ones you craft with paper + compass). Mojang publishes the table; we
mirror it here. Each block has a designated map color (its
``defaultMapColor()`` in Mojang source); rendering a chunk to a small
preview tile is then just "for each (x,z), find the top block, look up
its map color".

Why this instead of unmined-style photoreal rendering:

* unmined needs a real on-disk world directory — we'd have to restore
  every snapshot to render it. Killer for TB-scale vaults.
* This table covers every vanilla block and produces output that
  matches the MC in-game map look — players recognize "this is the spawn
  area" instantly because they've seen the same colors on their crafted
  maps.

Reference: https://minecraft.wiki/w/Map_color (the MapColor class in
``net.minecraft.world.level.material.MapColor``).
"""
from __future__ import annotations

# ---- 64 base map colors (id, RGB). brightness=2 (normal) values --------

_BASE_COLORS: tuple[tuple[int, int, int], ...] = (
    (0,   0,   0),    # 0  NONE — air, transparent in-game; black here
    (127, 178, 56),   # 1  GRASS
    (247, 233, 163),  # 2  SAND
    (199, 199, 199),  # 3  WOOL
    (255, 0,   0),    # 4  FIRE — TNT/lava
    (160, 160, 255),  # 5  ICE
    (167, 167, 167),  # 6  METAL — iron block, anvil
    (0,   124, 0),    # 7  PLANT — leaves, plants
    (255, 255, 255),  # 8  SNOW
    (164, 168, 184),  # 9  CLAY
    (151, 109, 77),   # 10 DIRT
    (112, 112, 112),  # 11 STONE
    (64,  64,  255),  # 12 WATER
    (143, 119, 72),   # 13 WOOD — oak planks
    (255, 252, 245),  # 14 QUARTZ — diorite, quartz, end stone
    (216, 127, 51),   # 15 COLOR_ORANGE
    (178, 76,  216),  # 16 COLOR_MAGENTA
    (102, 153, 216),  # 17 COLOR_LIGHT_BLUE
    (229, 229, 51),   # 18 COLOR_YELLOW
    (127, 204, 25),   # 19 COLOR_LIGHT_GREEN
    (242, 127, 165),  # 20 COLOR_PINK
    (76,  76,  76),   # 21 COLOR_GRAY
    (153, 153, 153),  # 22 COLOR_LIGHT_GRAY
    (76,  127, 153),  # 23 COLOR_CYAN
    (127, 63,  178),  # 24 COLOR_PURPLE
    (51,  76,  178),  # 25 COLOR_BLUE
    (102, 76,  51),   # 26 COLOR_BROWN
    (102, 127, 51),   # 27 COLOR_GREEN
    (153, 51,  51),   # 28 COLOR_RED
    (25,  25,  25),   # 29 COLOR_BLACK
    (250, 238, 77),   # 30 GOLD
    (92,  219, 213),  # 31 DIAMOND
    (74,  128, 255),  # 32 LAPIS
    (0,   217, 58),   # 33 EMERALD
    (129, 86,  49),   # 34 PODZOL — spruce planks/log
    (112, 2,   0),    # 35 NETHER — netherrack
    (209, 177, 161),  # 36 TERRACOTTA_WHITE
    (159, 82,  36),   # 37 TERRACOTTA_ORANGE
    (149, 87,  108),  # 38 TERRACOTTA_MAGENTA
    (112, 108, 138),  # 39 TERRACOTTA_LIGHT_BLUE
    (186, 133, 36),   # 40 TERRACOTTA_YELLOW
    (103, 117, 53),   # 41 TERRACOTTA_LIGHT_GREEN
    (160, 77,  78),   # 42 TERRACOTTA_PINK
    (57,  41,  35),   # 43 TERRACOTTA_GRAY
    (135, 107, 98),   # 44 TERRACOTTA_LIGHT_GRAY
    (87,  92,  92),   # 45 TERRACOTTA_CYAN
    (122, 73,  88),   # 46 TERRACOTTA_PURPLE
    (76,  62,  92),   # 47 TERRACOTTA_BLUE
    (76,  50,  35),   # 48 TERRACOTTA_BROWN
    (76,  82,  42),   # 49 TERRACOTTA_GREEN
    (142, 60,  46),   # 50 TERRACOTTA_RED
    (37,  22,  16),   # 51 TERRACOTTA_BLACK
    (189, 48,  49),   # 52 CRIMSON_NYLIUM
    (148, 63,  97),   # 53 CRIMSON_STEM
    (92,  25,  29),   # 54 CRIMSON_HYPHAE
    (22,  126, 134),  # 55 WARPED_NYLIUM
    (58,  142, 140),  # 56 WARPED_STEM
    (86,  44,  62),   # 57 WARPED_HYPHAE
    (20,  180, 133),  # 58 WARPED_WART_BLOCK
    (100, 100, 100),  # 59 DEEPSLATE
    (216, 175, 147),  # 60 RAW_IRON
    (127, 167, 150),  # 61 GLOW_LICHEN
)

# Fallback for unmapped blocks: bright magenta (Minecraft's missing-texture
# color). Easy to spot when reviewing tiles, prompts you to extend the
# block table below.
UNMAPPED_RGB: tuple[int, int, int] = (255, 0, 255)

# Air / void. Render as slightly-off-black so plain "missing" tiles are
# distinguishable from genuine air-only chunks.
AIR_RGB: tuple[int, int, int] = (12, 12, 16)


def color_for_id(map_color_id: int) -> tuple[int, int, int]:
    """Look up a base map color by Mojang's color ID."""
    if 0 <= map_color_id < len(_BASE_COLORS):
        return _BASE_COLORS[map_color_id]
    return UNMAPPED_RGB


# ---- block name → map color id --------------------------------------------
#
# Sourced from the MapColor enum in Mojang source. We hand-pick the ones
# that actually surface in chunk top-block scans (terrain blocks + common
# player-built blocks). Anything missing falls through to UNMAPPED_RGB so
# the rendered tile shows magenta noise — easy to grep for.

_NETHER_RED = 35
_DIRT = 10
_STONE = 11
_PLANT = 7
_WOOD = 13
_PODZOL = 34
_QUARTZ = 14
_SAND = 2
_GRASS = 1
_WATER = 12
_FIRE = 4
_SNOW = 8
_ICE = 5
_METAL = 6
_CLAY = 9
_GOLD = 30
_DIAMOND = 31
_LAPIS = 32
_EMERALD = 33
_DEEPSLATE = 59
_RAW_IRON = 60

_DYE_COLORS = {
    "white": 8,            # actually WOOL=3 for white_wool, but snow=8 looks right
    "orange": 15,
    "magenta": 16,
    "light_blue": 17,
    "yellow": 18,
    "lime": 19,
    "pink": 20,
    "gray": 21,
    "light_gray": 22,
    "cyan": 23,
    "purple": 24,
    "blue": 25,
    "brown": 26,
    "green": 27,
    "red": 28,
    "black": 29,
}

_TERRACOTTA_COLORS = {
    "white": 36, "orange": 37, "magenta": 38, "light_blue": 39,
    "yellow": 40, "lime": 41, "pink": 42, "gray": 43,
    "light_gray": 44, "cyan": 45, "purple": 46, "blue": 47,
    "brown": 48, "green": 49, "red": 50, "black": 51,
}


def _build_block_table() -> dict[str, int]:
    t: dict[str, int] = {}
    # Air-likes — color 0 (treated specially as AIR_RGB by the renderer)
    for name in ("air", "cave_air", "void_air"):
        t[f"minecraft:{name}"] = 0

    # Liquids
    t["minecraft:water"] = _WATER
    t["minecraft:bubble_column"] = _WATER
    t["minecraft:lava"] = _FIRE
    t["minecraft:fire"] = _FIRE
    t["minecraft:soul_fire"] = 17  # cyan-blue tint

    # Topsoil / dirt family
    for name, color in {
        "grass_block": _GRASS, "dirt": _DIRT, "coarse_dirt": _DIRT,
        "rooted_dirt": _DIRT, "dirt_path": _DIRT, "grass_path": _DIRT,
        "podzol": _PODZOL, "mycelium": 24, "mud": _DIRT, "muddy_mangrove_roots": _DIRT,
        "moss_block": _PLANT, "farmland": _DIRT,
    }.items():
        t[f"minecraft:{name}"] = color

    # Stone family
    for name in ("stone", "cobblestone", "mossy_cobblestone", "andesite",
                 "polished_andesite", "tuff", "polished_tuff", "smooth_stone",
                 "stone_bricks", "mossy_stone_bricks", "cracked_stone_bricks",
                 "chiseled_stone_bricks", "infested_stone", "infested_cobblestone",
                 "infested_stone_bricks", "infested_mossy_stone_bricks",
                 "infested_cracked_stone_bricks", "infested_chiseled_stone_bricks",
                 "bedrock"):
        t[f"minecraft:{name}"] = _STONE
    for name in ("granite", "polished_granite"):
        t[f"minecraft:{name}"] = _DIRT
    for name in ("diorite", "polished_diorite", "calcite", "quartz_block",
                 "smooth_quartz", "quartz_pillar", "chiseled_quartz_block",
                 "quartz_bricks", "end_stone", "end_stone_bricks"):
        t[f"minecraft:{name}"] = _QUARTZ

    # Deepslate family (1.17+)
    for name in ("deepslate", "cobbled_deepslate", "polished_deepslate",
                 "deepslate_bricks", "cracked_deepslate_bricks",
                 "deepslate_tiles", "cracked_deepslate_tiles",
                 "chiseled_deepslate", "reinforced_deepslate"):
        t[f"minecraft:{name}"] = _DEEPSLATE

    # Sand / gravel / clay
    for name in ("sand", "sandstone", "smooth_sandstone", "cut_sandstone",
                 "chiseled_sandstone"):
        t[f"minecraft:{name}"] = _SAND
    for name in ("red_sand", "red_sandstone", "smooth_red_sandstone",
                 "cut_red_sandstone", "chiseled_red_sandstone"):
        t[f"minecraft:{name}"] = 15  # orange
    t["minecraft:gravel"] = _STONE
    t["minecraft:clay"] = _CLAY

    # Snow / ice
    for name in ("snow_block", "snow", "powder_snow"):
        t[f"minecraft:{name}"] = _SNOW
    for name in ("ice", "packed_ice", "blue_ice", "frosted_ice"):
        t[f"minecraft:{name}"] = _ICE

    # Ores (mostly host stone color — they show as stone-colored from above)
    for name in ("coal_ore", "iron_ore", "gold_ore", "diamond_ore",
                 "redstone_ore", "lapis_ore", "emerald_ore", "copper_ore",
                 "nether_quartz_ore", "nether_gold_ore"):
        t[f"minecraft:{name}"] = _STONE
    for name in ("deepslate_coal_ore", "deepslate_iron_ore",
                 "deepslate_gold_ore", "deepslate_diamond_ore",
                 "deepslate_redstone_ore", "deepslate_lapis_ore",
                 "deepslate_emerald_ore", "deepslate_copper_ore"):
        t[f"minecraft:{name}"] = _DEEPSLATE
    t["minecraft:ancient_debris"] = _NETHER_RED

    # Mineral blocks
    t["minecraft:iron_block"] = _METAL
    t["minecraft:gold_block"] = _GOLD
    t["minecraft:diamond_block"] = _DIAMOND
    t["minecraft:emerald_block"] = _EMERALD
    t["minecraft:lapis_block"] = _LAPIS
    t["minecraft:redstone_block"] = _FIRE
    t["minecraft:copper_block"] = _RAW_IRON
    t["minecraft:raw_iron_block"] = _RAW_IRON
    t["minecraft:raw_gold_block"] = _GOLD
    t["minecraft:raw_copper_block"] = _RAW_IRON
    t["minecraft:netherite_block"] = 29  # near-black

    # Wood family — log, planks, leaves
    _WOOD_VARIANTS = {
        "oak": _WOOD, "spruce": _PODZOL, "birch": _SAND, "jungle": _PODZOL,
        "acacia": 15, "dark_oak": 26, "mangrove": 50, "cherry": 20,
        "bamboo": 18, "crimson": 53, "warped": 56,
    }
    for wood, color in _WOOD_VARIANTS.items():
        for suffix in ("log", "wood", "planks", "stripped_log", "stripped_wood"):
            t[f"minecraft:{wood}_{suffix}"] = color
        t[f"minecraft:{wood}_leaves"] = _PLANT
    # Crimson/warped use stem instead of log
    t["minecraft:crimson_stem"] = 53
    t["minecraft:crimson_hyphae"] = 54
    t["minecraft:stripped_crimson_stem"] = 53
    t["minecraft:stripped_crimson_hyphae"] = 54
    t["minecraft:warped_stem"] = 56
    t["minecraft:warped_hyphae"] = 57
    t["minecraft:stripped_warped_stem"] = 56
    t["minecraft:stripped_warped_hyphae"] = 57

    # Nether (overworld-equivalent terrain)
    for name in ("netherrack", "nether_bricks", "red_nether_bricks",
                 "cracked_nether_bricks", "chiseled_nether_bricks",
                 "magma_block"):
        t[f"minecraft:{name}"] = _NETHER_RED
    t["minecraft:soul_sand"] = 26
    t["minecraft:soul_soil"] = 26
    t["minecraft:basalt"] = 51
    t["minecraft:smooth_basalt"] = 51
    t["minecraft:polished_basalt"] = 51
    t["minecraft:blackstone"] = 29
    t["minecraft:gilded_blackstone"] = 29
    t["minecraft:polished_blackstone"] = 29
    t["minecraft:polished_blackstone_bricks"] = 29
    t["minecraft:cracked_polished_blackstone_bricks"] = 29
    t["minecraft:chiseled_polished_blackstone"] = 29
    t["minecraft:crimson_nylium"] = 52
    t["minecraft:warped_nylium"] = 55
    t["minecraft:warped_wart_block"] = 58
    t["minecraft:nether_wart_block"] = 28
    t["minecraft:shroomlight"] = 18

    # End
    t["minecraft:purpur_block"] = 16
    t["minecraft:purpur_pillar"] = 16
    t["minecraft:obsidian"] = 29
    t["minecraft:crying_obsidian"] = 24
    t["minecraft:end_rod"] = _QUARTZ
    t["minecraft:chorus_plant"] = 24
    t["minecraft:chorus_flower"] = 24

    # Plants — top-block view often hits these
    for name in ("oak_sapling", "spruce_sapling", "birch_sapling",
                 "jungle_sapling", "acacia_sapling", "dark_oak_sapling",
                 "tall_grass", "fern", "large_fern", "grass", "short_grass",
                 "dandelion", "poppy", "blue_orchid", "allium",
                 "azure_bluet", "red_tulip", "orange_tulip", "white_tulip",
                 "pink_tulip", "oxeye_daisy", "cornflower", "lily_of_the_valley",
                 "wither_rose", "sunflower", "lilac", "rose_bush", "peony",
                 "sweet_berry_bush", "lily_pad", "cactus", "bamboo",
                 "vine", "moss_carpet", "azalea", "flowering_azalea",
                 "small_dripleaf", "big_dripleaf", "spore_blossom",
                 "glow_lichen", "hanging_roots", "mangrove_roots",
                 "mangrove_propagule", "pink_petals"):
        t[f"minecraft:{name}"] = _PLANT
    t["minecraft:glow_lichen"] = 61

    # Crops
    for name in ("wheat", "carrots", "potatoes", "beetroots", "torchflower",
                 "torchflower_crop", "pitcher_plant", "pitcher_crop"):
        t[f"minecraft:{name}"] = _PLANT
    t["minecraft:pumpkin"] = 15        # orange
    t["minecraft:carved_pumpkin"] = 15
    t["minecraft:jack_o_lantern"] = 15
    t["minecraft:melon"] = _PLANT
    t["minecraft:hay_block"] = 18      # yellow

    # Wools / concrete / terracotta / glazed_terracotta — by dye color
    for dye, color in _DYE_COLORS.items():
        # Wool uses WOOL=3 for white, dye-color for the rest
        wool_color = 3 if dye == "white" else color
        t[f"minecraft:{dye}_wool"] = wool_color
        t[f"minecraft:{dye}_carpet"] = wool_color
        t[f"minecraft:{dye}_concrete"] = color
        t[f"minecraft:{dye}_concrete_powder"] = color
        t[f"minecraft:{dye}_stained_glass"] = color
        t[f"minecraft:{dye}_stained_glass_pane"] = color
        t[f"minecraft:{dye}_glazed_terracotta"] = color
        t[f"minecraft:{dye}_candle"] = color
        t[f"minecraft:{dye}_bed"] = color
    for dye, color in _TERRACOTTA_COLORS.items():
        t[f"minecraft:{dye}_terracotta"] = color
    t["minecraft:terracotta"] = 37  # plain terracotta = orange variant

    # Glass + misc
    t["minecraft:glass"] = _QUARTZ
    t["minecraft:glass_pane"] = _QUARTZ
    t["minecraft:tinted_glass"] = 29
    t["minecraft:glowstone"] = 18
    t["minecraft:sea_lantern"] = _QUARTZ
    t["minecraft:prismarine"] = 31
    t["minecraft:dark_prismarine"] = 31
    t["minecraft:prismarine_bricks"] = 31

    # Sponges, slime, honey
    t["minecraft:sponge"] = 18
    t["minecraft:wet_sponge"] = 18
    t["minecraft:slime_block"] = 19
    t["minecraft:honey_block"] = 18
    t["minecraft:honeycomb_block"] = 15

    return t


_BLOCK_TO_COLOR_ID: dict[str, int] = _build_block_table()


def color_for_block(block_name: str) -> tuple[int, int, int]:
    """Return the 8-bit RGB for a block at brightness=2 (normal).

    ``block_name`` should be the namespaced ID from a chunk's palette
    (e.g. ``minecraft:grass_block``). Anything not in the table returns
    :data:`UNMAPPED_RGB` (bright magenta) so missing blocks pop out
    visually — easier than silently rendering them as black.

    Air-likes return :data:`AIR_RGB` (a near-black distinct from missing).
    """
    cid = _BLOCK_TO_COLOR_ID.get(block_name)
    if cid is None:
        return UNMAPPED_RGB
    if cid == 0:
        return AIR_RGB
    return _BASE_COLORS[cid]


def is_air(block_name: str) -> bool:
    """True for air, cave_air, void_air."""
    return block_name in ("minecraft:air", "minecraft:cave_air", "minecraft:void_air")
