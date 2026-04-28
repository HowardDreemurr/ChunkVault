"""Tile-pool population + per-snapshot PNG sidecars.

Two entry points:

* :func:`ensure_tiles_for_manifest` — given a (repo, manifest) pair,
  walks every chunk reference and renders any tile that isn't already
  in the pool. Reads chunk NBT from the chunk pool blobs, so it's just
  as usable for backfill (no source world dir needed) as for live
  snapshot integration.
* :func:`write_snapshot_sidecars` — assembles the rendered chunk tiles
  for one snapshot into a per-dimension, per-mode PNG laid out by world
  coords. Tiles are stitched at chunk granularity (16x16 px each), so
  a 1024-region world produces a manageable image — and for the common
  case of small explored worlds it stays small.

Why split: ``ensure_tiles_for_manifest`` is the expensive content-addressed
work (decompress + render every chunk that hasn't been seen before).
``write_snapshot_sidecars`` is cheap (just glue tiles together). They run
in sequence during a normal snapshot, but the backfill command runs them
independently — populate the pool first so any partial backfill leaves
the tile pool ready for any future PNG regeneration.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..mca.nbt_lite import decompress_chunk_payload
from ..mca.region import EXTERNAL_FLAG
from ..store.progress import ProgressCallback, ProgressEvent, _emit
from .colors import AIR_RGB
from .render import RENDER_MODES, RenderError, TILE_BYTES, render_chunk_nbt


# Vanilla dimension keys → which render modes to produce per chunk. Custom
# datapack dims fall through to the default ("topdown" only).
NETHER_DIM_KEYS = ("DIM-1/region", "DIM-1")
END_DIM_KEYS = ("DIM1/region", "DIM1")


# Tunable, used by ensure_tiles_for_manifest. Module-level so tests can
# monkeypatch them; production callers shouldn't need to touch them.
#
# _RENDER_PROGRESS_TICK: emit a phase_progress event every N chunks
#   inside the inner render loop. Without this the rich progress bar
#   sits at 0/N for hours on a multi-million-chunk first-pass render.
#   256 keeps the console responsive without flooding it.
#
# _RENDER_COMMIT_BATCH: persist chunk_renders presence rows every N
#   successful renders. The previous design batched only at end-of-
#   group, so a Ctrl+C at 99% of a 4M-chunk group lost ALL the index
#   entries — the .png files were on disk, but nothing in the index
#   knew that, and the next run re-rendered every one of them. The
#   batch size bounds the blast radius of an interrupt to ~N
#   redundantly-rendered tiles while keeping SQLite write
#   amplification reasonable.
_RENDER_PROGRESS_TICK = 256
_RENDER_COMMIT_BATCH = 1024


def modes_for_dim(dim_key: str) -> tuple[str, ...]:
    """Which render modes apply to a given dimension's region key."""
    # Nether's bedrock ceiling makes a single "top-down" view useless;
    # we render two altitude bands instead.
    if dim_key in NETHER_DIM_KEYS or dim_key.startswith("DIM-1"):
        return ("nether_low", "nether_high")
    return ("topdown",)


@dataclass
class TileRenderStats:
    """Counts returned from :func:`ensure_tiles_for_manifest`."""
    chunks_inspected: int = 0
    tiles_rendered: int = 0
    tiles_skipped_cached: int = 0
    chunks_failed: int = 0


def ensure_tiles_for_manifest(
    repo,                       # ChunkSnapshotRepo (forward-declared)
    manifest,                   # chunkvault.store.manifest.Manifest
    *,
    progress_cb: ProgressCallback = None,
) -> TileRenderStats:
    """Walk a manifest and render any missing tiles into the tile pool.

    Idempotent: tiles already present in the pool are skipped (cache hit).
    Render failures (corrupt NBT, unknown format) are counted but never
    raise — a single bad chunk shouldn't kill an entire snapshot's
    thumbnail run.
    """
    stats = TileRenderStats()

    # Group all chunk references by (dim_key, mode) so we can do bulk
    # cache lookups per group. For typical worlds this is 1-3 groups.
    by_dim_mode: dict[tuple[str, str], list[bytes]] = {}
    for dim_key, regions in manifest.dimensions.items():
        modes = modes_for_dim(dim_key)
        for region in regions:
            for c in region.chunks:
                stats.chunks_inspected += 1
                for mode in modes:
                    by_dim_mode.setdefault(
                        (dim_key, mode), []
                    ).append(c.content_hash)

    if not by_dim_mode:
        return stats

    total_to_check = sum(len(v) for v in by_dim_mode.values())
    _emit(progress_cb, ProgressEvent(
        kind="phase_start", phase="render_tiles",
        label=f"{total_to_check} (chunk, mode) pairs across "
              f"{len(by_dim_mode)} groups",
        total=total_to_check,
    ))

    # See module-level constants for tuning rationale.
    progress_tick = _RENDER_PROGRESS_TICK
    commit_batch = _RENDER_COMMIT_BATCH

    seen = 0
    with __import__("chunkvault.store.index", fromlist=["IndexDB"]).IndexDB(
        repo.index_path,
    ) as index:
        for (dim_key, mode), hashes in by_dim_mode.items():
            unique_hashes = list({h for h in hashes})
            already = index.has_chunk_renders_bulk(unique_hashes, mode)
            stats.tiles_skipped_cached += len(already)
            missing = [h for h in unique_hashes if h not in already]
            # Skipped-cached count is part of "seen" too — we don't re-render
            # them, but they're still pairs we've decided about. Bump up
            # front so the bar reflects progress through the cache-hit prefix.
            seen += len(already)
            if already:
                _emit(progress_cb, ProgressEvent(
                    kind="phase_progress", phase="render_tiles",
                    label=f"{dim_key}/{mode} (cached)",
                    current=seen, total=total_to_check,
                ))
            # Buffer for incremental chunk_renders commits.
            uncommitted: list[bytes] = []
            for i, h in enumerate(missing, 1):
                blob = repo.chunks.read_chunk(h)
                rendered_ok = False
                if blob is None or len(blob) < 1:
                    stats.chunks_failed += 1
                else:
                    try:
                        nbt = decompress_chunk_payload(
                            blob[0] & ~EXTERNAL_FLAG, blob[1:],
                        )
                        tile = render_chunk_nbt(nbt, mode)
                        repo.tiles.store(h, mode, tile)
                        stats.tiles_rendered += 1
                        rendered_ok = True
                    except (RenderError, Exception):
                        stats.chunks_failed += 1
                if rendered_ok:
                    uncommitted.append(h)
                    if len(uncommitted) >= commit_batch:
                        # Persist in batches so a Ctrl+C only loses up to
                        # commit_batch entries, not the entire group.
                        index.add_chunk_renders(
                            [(uh, mode) for uh in uncommitted],
                        )
                        uncommitted.clear()
                seen += 1
                # Emit during the inner loop so a multi-million-chunk group
                # doesn't appear frozen. Branch is cheap; _emit only runs
                # once per tick.
                if i % progress_tick == 0:
                    _emit(progress_cb, ProgressEvent(
                        kind="phase_progress", phase="render_tiles",
                        label=f"{dim_key}/{mode}",
                        current=seen, total=total_to_check,
                    ))
            # Final flush for the group's tail.
            if uncommitted:
                index.add_chunk_renders([(uh, mode) for uh in uncommitted])
            _emit(progress_cb, ProgressEvent(
                kind="phase_progress", phase="render_tiles",
                label=f"{dim_key}/{mode}",
                current=seen, total=total_to_check,
            ))

    _emit(progress_cb, ProgressEvent(
        kind="phase_done", phase="render_tiles",
        current=seen, total=total_to_check,
        detail={
            "rendered": stats.tiles_rendered,
            "cached": stats.tiles_skipped_cached,
            "failed": stats.chunks_failed,
        },
    ))
    return stats


def write_snapshot_sidecars(
    repo,                   # ChunkSnapshotRepo
    snap_id: str,
    manifest,               # chunkvault.store.manifest.Manifest
    *,
    output_dir: Path | None = None,
    progress_cb: ProgressCallback = None,
) -> list[Path]:
    """Stitch the rendered chunk tiles into per-(dim, mode) PNG sidecars.

    Output goes to ``repo/thumbnails/<snap_id>/<safe_dim>-<mode>.png``
    unless ``output_dir`` is provided. Returns the list of files written.
    Idempotent: re-runs overwrite (cheap, no diff check).

    Each PNG places one chunk tile (16x16 px) at world coordinates;
    bounds auto-fit the explored region so empty worlds don't produce
    a blank giant image.
    """
    from PIL import Image  # already a chunkvault dep via viz.heatmap

    out_root = output_dir or (repo.repo_path / "thumbnails" / snap_id)
    out_root.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for dim_key, regions in manifest.dimensions.items():
        modes = modes_for_dim(dim_key)
        # World-coords range across all chunks in this dim.
        chunk_positions: list[tuple[int, int, bytes]] = []
        for region in regions:
            for c in region.chunks:
                world_cx = region.rx * 32 + c.cx
                world_cz = region.rz * 32 + c.cz
                chunk_positions.append((world_cx, world_cz, c.content_hash))
        if not chunk_positions:
            continue

        cx_values = [p[0] for p in chunk_positions]
        cz_values = [p[1] for p in chunk_positions]
        min_cx, max_cx = min(cx_values), max(cx_values)
        min_cz, max_cz = min(cz_values), max(cz_values)
        width_chunks = max_cx - min_cx + 1
        height_chunks = max_cz - min_cz + 1

        for mode in modes:
            _emit(progress_cb, ProgressEvent(
                kind="phase_start", phase="sidecar_png",
                label=f"{dim_key} {mode} ({width_chunks}x{height_chunks} chunks)",
                total=len(chunk_positions),
            ))
            img = Image.new(
                "RGB",
                (width_chunks * 16, height_chunks * 16),
                AIR_RGB,
            )
            for i, (wcx, wcz, content_hash) in enumerate(chunk_positions, 1):
                tile_bytes = repo.tiles.read(content_hash, mode)
                if tile_bytes is None or len(tile_bytes) != TILE_BYTES:
                    continue
                # Tile is 16x16 RGB raw; convert to PIL Image and paste.
                tile_img = Image.frombytes("RGB", (16, 16), tile_bytes)
                px = (wcx - min_cx) * 16
                py = (wcz - min_cz) * 16
                img.paste(tile_img, (px, py))
                if i % 256 == 0:
                    _emit(progress_cb, ProgressEvent(
                        kind="phase_progress", phase="sidecar_png",
                        label=f"{dim_key} {mode}",
                        current=i, total=len(chunk_positions),
                    ))
            safe_dim = dim_key.replace("/", "-").replace("\\", "-")
            out_path = out_root / f"{safe_dim}-{mode}.png"
            img.save(out_path, "PNG", optimize=True)
            written.append(out_path)
            _emit(progress_cb, ProgressEvent(
                kind="phase_done", phase="sidecar_png",
                current=len(chunk_positions), total=len(chunk_positions),
                detail={"path": str(out_path)},
            ))

    return written
