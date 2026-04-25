"""Optional wrapper around the **unmined** CLI renderer.

unmined (https://unmined.net) is a separate native tool that produces
XYZ-scheme map tiles of a Minecraft world. We don't ship it — we just shell
out to it when the user has it installed.

All tile rendering is optional: the Leaflet HTML map works perfectly well
without a base layer (dark background). When the user has unmined and wants
nicer-looking output, ``render_world_tiles`` produces a tile tree suitable
for feeding to ``render_diff_html(..., base_tiles_url="tiles/{z}/{x}/{y}.png")``.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class TilesError(Exception):
    """unmined was unavailable or its invocation failed."""


UNMINED_EXECUTABLES = ("unmined-cli", "unmined")


def unmined_available() -> str | None:
    """Return the path to the unmined executable, or ``None`` if not on PATH."""
    for name in UNMINED_EXECUTABLES:
        path = shutil.which(name)
        if path:
            return path
    return None


def render_world_tiles(
    world_path: Path | str,
    out_dir: Path | str,
    *,
    dimension: str | None = None,
    zoom_range: tuple[int, int] | None = None,
) -> Path:
    """Render ``world_path`` to an XYZ tile tree under ``out_dir``.

    Requires ``unmined-cli`` on PATH. Raises :class:`TilesError` if it's
    missing or the render fails. Returns the output directory.

    ``dimension`` is passed through to unmined's ``--dimension`` option
    (``overworld`` / ``nether`` / ``the_end`` / custom). ``zoom_range`` is
    ``(min, max)`` zoom levels to emit.
    """
    exe = unmined_available()
    if exe is None:
        raise TilesError(
            "unmined is not installed or not on PATH. "
            "Download from https://unmined.net/cli/ and place the binary on PATH."
        )
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    args = [
        exe, "web", "render",
        f"--world={Path(world_path)}",
        f"--output={out}",
    ]
    if dimension is not None:
        args.append(f"--dimension={dimension}")
    if zoom_range is not None:
        args.append(f"--zoom.min={zoom_range[0]}")
        args.append(f"--zoom.max={zoom_range[1]}")

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise TilesError(
            f"unmined failed (rc={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return out
