"""Self-contained interactive HTML map of a WorldDiff, built on Leaflet.

The output is a single ``.html`` file that pulls Leaflet from unpkg and
inlines the diff data as JSON. No server, no build step — open in any
browser.

Coordinate system is ``L.CRS.Simple`` with 1 unit = 1 chunk. Each changed
chunk is a small colored rectangle; layer controls let you toggle each
dimension independently. Hover for per-chunk details.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..diff.world import WorldDiff

_COLORS = {
    "added": "#28c850",
    "modified": "#f0a028",
    "removed": "#dc3232",
}


def render_diff_html(
    diff: WorldDiff,
    *,
    out_path: Path | str | None = None,
    title: str = "chunkvault diff",
    base_tiles_url: str | None = None,
    base_tiles_attribution: str | None = None,
    base_tiles_min_zoom: int | None = None,
    base_tiles_max_zoom: int | None = None,
    base_tiles_chunks_per_tile: int = 16,
) -> str:
    """Return an HTML string (and optionally write to ``out_path``).

    ``base_tiles_url`` is an optional URL pattern for a ``L.tileLayer`` —
    e.g. ``"./tiles/{z}/{x}/{y}.png"`` if you pre-rendered tiles with unmined
    next to the HTML. The underlying tiles are expected to cover whole
    Minecraft regions (512×512 blocks per tile by default in unmined — that's
    ``chunks_per_tile=32``); adjust ``base_tiles_chunks_per_tile`` if your
    renderer uses a different zoom base.

    Every changed chunk is rendered regardless of dimension. Dimensions are
    separate toggleable overlays, layered over the base tiles.
    """
    by_dim: dict[str, list[dict]] = {}
    for c in diff.changes:
        by_dim.setdefault(c.dimension_key, []).append({
            "cx": c.cx,
            "cz": c.cz,
            "kind": c.kind,
            "old": c.old_hash.hex() if c.old_hash else None,
            "new": c.new_hash.hex() if c.new_hash else None,
        })

    summary = diff.count_by_kind()
    error_count = len(diff.errors)
    data_json = json.dumps(by_dim)
    summary_payload = {
        **summary,
        "errors": error_count,
        "dimensions": sorted(by_dim.keys()),
    }
    # Include snapshot version info if known (chunk-store diffs have it,
    # directory-based diffs don't).
    if diff.old_mc_version or diff.new_mc_version or diff.old_label or diff.new_label:
        summary_payload["versions"] = {
            "old_mc_version": diff.old_mc_version,
            "new_mc_version": diff.new_mc_version,
            "old_data_version": diff.old_data_version,
            "new_data_version": diff.new_data_version,
            "old_label": diff.old_label,
            "new_label": diff.new_label,
            "version_changed": diff.version_changed(),
        }
    summary_json = json.dumps(summary_payload)

    base_tiles_config = {
        "url": base_tiles_url,
        "attribution": base_tiles_attribution,
        "minZoom": base_tiles_min_zoom,
        "maxZoom": base_tiles_max_zoom,
        "chunksPerTile": base_tiles_chunks_per_tile,
    } if base_tiles_url else None

    html = _TEMPLATE.format(
        title=_escape(title),
        data_json=data_json,
        summary_json=summary_json,
        colors_json=json.dumps(_COLORS),
        base_tiles_json=json.dumps(base_tiles_config),
    )

    if out_path is not None:
        Path(out_path).write_text(html, encoding="utf-8")
    return html


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; font-family: monospace; }}
  #map {{ height: 100%; background: #202020; }}
  .info {{
    position: absolute; top: 10px; left: 60px; z-index: 1000;
    background: rgba(0,0,0,0.75); color: #fff;
    padding: 8px 12px; border-radius: 4px; line-height: 1.5;
    font-size: 13px; max-width: 340px;
  }}
  .info h1 {{ margin: 0 0 4px 0; font-size: 14px; }}
  .info .swatch {{
    display: inline-block; width: 10px; height: 10px; margin-right: 4px;
    vertical-align: middle; border: 1px solid #000;
  }}
  .leaflet-tooltip.chunk {{
    background: rgba(0,0,0,0.85); color: #fff; border: none;
    font-family: monospace; font-size: 12px;
  }}
</style>
</head>
<body>
<div id="map"></div>
<div class="info" id="info"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const DATA = {data_json};
const SUMMARY = {summary_json};
const COLORS = {colors_json};
const BASE_TILES = {base_tiles_json};

const mapOpts = {{
  crs: L.CRS.Simple,
  minZoom: -6, maxZoom: 6,
  zoomSnap: 0.25,
}};
if (BASE_TILES && BASE_TILES.minZoom !== null) mapOpts.minZoom = BASE_TILES.minZoom;
if (BASE_TILES && BASE_TILES.maxZoom !== null) mapOpts.maxZoom = BASE_TILES.maxZoom;

const map = L.map('map', mapOpts);
map.setView([0, 0], 0);

// Leaflet CRS.Simple uses [y, x]. Map chunk (cx, cz) to latlng so that
// positive cz points "down" on screen (matches in-game map orientation).
function chunkToLatLng(cx, cz) {{ return [-cz, cx]; }}

// Base-tile support: chunk-space coords × chunksPerTile = tile-space coords,
// which is what the tile-renderer numbered its tiles against.
if (BASE_TILES) {{
  const tileOpts = {{ noWrap: true, tileSize: 256 }};
  if (BASE_TILES.attribution) tileOpts.attribution = BASE_TILES.attribution;
  if (BASE_TILES.minZoom !== null) tileOpts.minZoom = BASE_TILES.minZoom;
  if (BASE_TILES.maxZoom !== null) tileOpts.maxZoom = BASE_TILES.maxZoom;
  L.tileLayer(BASE_TILES.url, tileOpts).addTo(map);
}}

const dimLayers = {{}};
for (const [dim, features] of Object.entries(DATA)) {{
  const layer = L.layerGroup();
  for (const f of features) {{
    const sw = chunkToLatLng(f.cx, f.cz + 1);
    const ne = chunkToLatLng(f.cx + 1, f.cz);
    const rect = L.rectangle([sw, ne], {{
      color: COLORS[f.kind], weight: 0,
      fillColor: COLORS[f.kind], fillOpacity: 0.85,
    }});
    const lines = [
      `<b>${{f.kind}}</b> <span style="opacity:0.7">${{dim}}</span>`,
      `chunk (${{f.cx}}, ${{f.cz}})`,
    ];
    if (f.old) lines.push(`old ${{f.old.slice(0,12)}}…`);
    if (f.new) lines.push(`new ${{f.new.slice(0,12)}}…`);
    rect.bindTooltip(lines.join('<br>'), {{
      direction: 'top', className: 'chunk',
    }});
    rect.addTo(layer);
  }}
  dimLayers[dim] = layer;
  layer.addTo(map);
}}

if (Object.keys(dimLayers).length > 1) {{
  L.control.layers(null, dimLayers, {{ collapsed: false }}).addTo(map);
}}

// Fit to bounds
const allFeatures = Object.values(DATA).flat();
if (allFeatures.length > 0) {{
  const lats = allFeatures.flatMap(f => [-f.cz, -f.cz - 1]);
  const lngs = allFeatures.flatMap(f => [f.cx, f.cx + 1]);
  map.fitBounds([
    [Math.min(...lats) - 1, Math.min(...lngs) - 1],
    [Math.max(...lats) + 1, Math.max(...lngs) + 1],
  ]);
}}

// Info panel
const info = document.getElementById('info');
const parts = [
  `<h1>chunkvault diff</h1>`,
];
if (SUMMARY.versions) {{
  const v = SUMMARY.versions;
  const oldV = v.old_mc_version || (v.old_data_version ? 'DV ' + v.old_data_version : '?');
  const newV = v.new_mc_version || (v.new_data_version ? 'DV ' + v.new_data_version : '?');
  const arrow = v.version_changed ? ' <b>(version changed)</b>' : '';
  parts.push(
    `<div style="margin-bottom:4px;border-bottom:1px solid #444;padding-bottom:4px">`
      + `${{v.old_label || 'old'}} (${{oldV}}) → ${{v.new_label || 'new'}} (${{newV}})${{arrow}}`
    + `</div>`
  );
}}
parts.push(
  `<div><span class="swatch" style="background:${{COLORS.added}}"></span>added: ${{SUMMARY.added}}</div>`,
  `<div><span class="swatch" style="background:${{COLORS.modified}}"></span>modified: ${{SUMMARY.modified}}</div>`,
  `<div><span class="swatch" style="background:${{COLORS.removed}}"></span>removed: ${{SUMMARY.removed}}</div>`,
  `<div>dimensions: ${{SUMMARY.dimensions.length}}</div>`,
);
if (SUMMARY.errors > 0) {{
  parts.push(`<div style="color:#ff8080">errors: ${{SUMMARY.errors}}</div>`);
}}
if (allFeatures.length === 0) {{
  parts.push(`<div style="margin-top:6px;opacity:0.7">no changes</div>`);
}}
info.innerHTML = parts.join('');
</script>
</body>
</html>
"""
