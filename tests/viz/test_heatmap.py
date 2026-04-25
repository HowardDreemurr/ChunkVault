from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from chunkvault.diff.world import ChunkDiff, WorldDiff
from chunkvault.viz.heatmap import (
    COLOR_ADDED,
    COLOR_BG,
    COLOR_MODIFIED,
    COLOR_REMOVED,
    render_diff_png,
)


def _diff(*changes: ChunkDiff) -> WorldDiff:
    return WorldDiff(
        old_root=Path("/nonexistent/a"),
        new_root=Path("/nonexistent/b"),
        changes=list(changes),
    )


def _chunk(cx: int, cz: int, kind: str, dim: str = "region") -> ChunkDiff:
    return ChunkDiff(
        dimension_key=dim,
        rx=cx // 32, rz=cz // 32,
        cx=cx, cz=cz,
        kind=kind,
        old_hash=b"\x00" * 16 if kind != "added" else None,
        new_hash=b"\xff" * 16 if kind != "removed" else None,
    )


def _load(png_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


# --- empty case --------------------------------------------------------------

def test_empty_diff_produces_valid_png():
    png = render_diff_png(_diff(), "region", scale=8)
    img = _load(png)
    assert img.size == (8, 8)
    # Single pixel at (0,0) should be the background color.
    assert img.getpixel((0, 0)) == COLOR_BG


def test_empty_diff_for_missing_dimension():
    """If the requested dimension has no changes, we still get a valid PNG."""
    d = _diff(_chunk(5, 5, "added", dim="DIM-1/region"))
    png = render_diff_png(d, "region", scale=4)
    img = _load(png)
    assert img.size == (4, 4)


# --- color per kind ----------------------------------------------------------

def test_colors_match_kinds():
    d = _diff(
        _chunk(0, 0, "added"),
        _chunk(1, 0, "modified"),
        _chunk(2, 0, "removed"),
    )
    png = render_diff_png(d, "region", scale=4)
    img = _load(png)
    assert img.size == (12, 4)  # 3 chunks × 4 px
    # Sample center-of-square for each chunk
    assert img.getpixel((2, 2)) == COLOR_ADDED
    assert img.getpixel((6, 2)) == COLOR_MODIFIED
    assert img.getpixel((10, 2)) == COLOR_REMOVED


# --- bounds + negative coords ------------------------------------------------

def test_negative_coordinates_are_translated_into_canvas():
    d = _diff(
        _chunk(-5, -3, "added"),
        _chunk(2, 4, "removed"),
    )
    png = render_diff_png(d, "region", scale=2)
    img = _load(png)
    # cx range [-5, 2] → 8 columns; cz range [-3, 4] → 8 rows. Scale 2 → 16×16
    assert img.size == (16, 16)
    # (-5, -3) → offset (0, 0). Center-of-square for scale 2 is (1, 1).
    assert img.getpixel((1, 1)) == COLOR_ADDED
    # (2, 4) → offset (7, 7) * 2 = (14, 14). Center (15, 15).
    assert img.getpixel((15, 15)) == COLOR_REMOVED


# --- dimension filter --------------------------------------------------------

def test_dimension_filter_excludes_other_dims():
    d = _diff(
        _chunk(0, 0, "added", dim="region"),
        _chunk(100, 100, "added", dim="DIM-1/region"),
    )
    png = render_diff_png(d, "region", scale=4)
    img = _load(png)
    # Only the 'region' change should influence bounds → 4×4 canvas.
    assert img.size == (4, 4)
    assert img.getpixel((2, 2)) == COLOR_ADDED


# --- scale parameter ---------------------------------------------------------

def test_scale_respected():
    d = _diff(_chunk(0, 0, "added"))
    for s in (1, 4, 16, 32):
        png = render_diff_png(d, "region", scale=s)
        img = _load(png)
        assert img.size == (s, s)


def test_scale_must_be_positive():
    d = _diff(_chunk(0, 0, "added"))
    with pytest.raises(ValueError):
        render_diff_png(d, "region", scale=0)


# --- out_path writes the file ------------------------------------------------

def test_out_path_writes_file(tmp_path: Path):
    d = _diff(_chunk(0, 0, "added"))
    out = tmp_path / "heat.png"
    returned = render_diff_png(d, "region", scale=4, out_path=out)
    assert out.exists()
    assert out.read_bytes() == returned
    # Verify it's a valid PNG
    img = Image.open(out)
    img.verify()
