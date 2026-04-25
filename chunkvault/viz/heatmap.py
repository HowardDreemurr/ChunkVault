"""PNG heatmap of per-chunk changes within one dimension.

Each chunk becomes an NxN colored square (N = scale). Colors:
    added    → green
    removed  → red
    modified → orange
    background (no change) → dark grey

Image bounds auto-fit the set of changed chunks. Unchanged chunks in the
middle of a changed region still render as background, so a sparse change
pattern shows up clearly.
"""
from __future__ import annotations

import io
from pathlib import Path

from ..diff.world import WorldDiff

COLOR_BG = (32, 32, 32)
COLOR_ADDED = (40, 200, 80)
COLOR_MODIFIED = (240, 160, 40)
COLOR_REMOVED = (220, 50, 50)

_KIND_COLOR = {
    "added": COLOR_ADDED,
    "modified": COLOR_MODIFIED,
    "removed": COLOR_REMOVED,
}


def render_diff_png(
    diff: WorldDiff,
    dimension_key: str,
    *,
    scale: int = 8,
    out_path: Path | str | None = None,
) -> bytes:
    """Render a heatmap PNG of changes within one dimension.

    Returns PNG bytes. If `out_path` is set, also writes them there.

    `scale` controls pixels-per-chunk. Default 8 is a good compromise between
    legibility and image size for multi-thousand-chunk diffs.
    """
    if scale < 1:
        raise ValueError(f"scale must be >= 1, got {scale}")

    # Deferred import so importing chunkvault without Pillow stays cheap.
    from PIL import Image, ImageDraw

    changes = [c for c in diff.changes if c.dimension_key == dimension_key]

    if not changes:
        # Empty diff: produce a minimal but valid PNG so downstream scripts
        # don't have to special-case None.
        img = Image.new("RGB", (scale, scale), COLOR_BG)
        return _finish(img, out_path)

    min_cx = min(c.cx for c in changes)
    max_cx = max(c.cx for c in changes)
    min_cz = min(c.cz for c in changes)
    max_cz = max(c.cz for c in changes)
    width = (max_cx - min_cx + 1) * scale
    height = (max_cz - min_cz + 1) * scale

    img = Image.new("RGB", (width, height), COLOR_BG)
    draw = ImageDraw.Draw(img)
    for c in changes:
        px = (c.cx - min_cx) * scale
        py = (c.cz - min_cz) * scale
        draw.rectangle(
            [px, py, px + scale - 1, py + scale - 1],
            fill=_KIND_COLOR[c.kind],
        )
    return _finish(img, out_path)


def _finish(img, out_path: Path | str | None) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    if out_path is not None:
        Path(out_path).write_bytes(data)
    return data
