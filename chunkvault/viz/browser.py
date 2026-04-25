"""Local HTTP server + Leaflet HTML for browsing a vault visually.

Started by ``chunkvault browse <repo>``. Pure stdlib (``http.server``)
so there's no extra dependency. Routes:

* ``GET /``                              — the SPA HTML (Leaflet from CDN)
* ``GET /api/snapshots``                 — JSON list of every snapshot's
                                            metadata + which (dim, mode)
                                            sidecars exist on disk
* ``GET /thumbnails/<snap_id>/<file>``   — serves the per-dim PNG sidecars
                                            written by snapshot/backfill

The frontend is a single-file HTML/JS app: snapshot dropdown, dim/mode
picker, and a Leaflet map showing the current sidecar as an image
overlay. ``L.CRS.Simple`` is used so the image displays at pixel-for-
pixel resolution — no map projection. Pan/zoom + a built-in coord
display (block coords, offset by sidecar bounds) gives an "explore the
vault" experience without any restore.

Strictly a debug/preview tool, not production: single-threaded HTTP,
no auth, binds to localhost by default. Don't expose to the internet.
"""
from __future__ import annotations

import http.server
import json
import socketserver
import urllib.parse
from pathlib import Path

from ..store import ChunkSnapshotRepo


def serve(repo_path: Path | str, *, host: str = "127.0.0.1",
          port: int = 8765) -> None:
    """Start the browser HTTP server (blocks until Ctrl-C)."""
    repo = ChunkSnapshotRepo(repo_path)
    if not repo.is_initialized():
        raise RuntimeError(f"repo not initialized: {repo_path}")
    handler = _make_handler(repo)
    # Reuse-address so successive starts after Ctrl-C don't sleep on TIME_WAIT.
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((host, port), handler) as httpd:
        print(f"chunkvault browse — serving {repo.repo_path}")
        print(f"open http://{host}:{port}/ in your browser  (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopping.")


def _make_handler(repo: ChunkSnapshotRepo):
    """Build a request handler bound to a specific repo."""
    thumbnails_root = repo.repo_path / "thumbnails"

    class Handler(http.server.BaseHTTPRequestHandler):
        # Quiet the default "GET / 200" log spam — the user already
        # sees activity via their browser. Override to nothing.
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/":
                return self._send_html(_PAGE_HTML)
            if path == "/api/snapshots":
                return self._send_json(_list_snapshots(repo, thumbnails_root))
            if path.startswith("/thumbnails/"):
                return self._serve_thumbnail(path[len("/thumbnails/"):])
            return self._send(404, "text/plain", b"not found")

        def _serve_thumbnail(self, rel: str):
            # Path-traversal sanitization: resolve and assert containment
            target = (thumbnails_root / rel).resolve()
            try:
                target.relative_to(thumbnails_root.resolve())
            except ValueError:
                return self._send(403, "text/plain", b"forbidden")
            if not target.is_file():
                return self._send(404, "text/plain", b"not found")
            data = target.read_bytes()
            return self._send(200, "image/png", data)

        def _send_html(self, html: str):
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))

        def _send_json(self, obj):
            payload = json.dumps(obj).encode("utf-8")
            self._send(200, "application/json", payload)

        def _send(self, code: int, ctype: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _list_snapshots(repo: ChunkSnapshotRepo, thumbs_root: Path) -> list[dict]:
    """JSON serializable listing of all snapshots + which sidecars they have.

    Each entry: ``{id, label, timestamp, world_name, mc_version, sidecars: [
    {filename, dim_mode, size_px: [w, h]}]}``. Frontend uses ``sidecars`` to
    populate the dim/mode picker per snapshot.
    """
    out: list[dict] = []
    for snap in repo.list():
        snap_dir = thumbs_root / snap.id
        sidecars = []
        if snap_dir.is_dir():
            for png in sorted(snap_dir.glob("*.png")):
                # Filename pattern: <safe_dim>-<mode>.png. Split off mode from
                # the right; everything else is the dim key (with -'s instead
                # of /'s — frontend doesn't need to undo that).
                stem = png.stem
                dim_mode = stem
                size_px = _png_size(png)
                sidecars.append({
                    "filename": png.name,
                    "dim_mode": dim_mode,
                    "size_px": size_px,
                })
        out.append({
            "id": snap.id,
            "short_id": snap.short_id,
            "label": snap.label,
            "timestamp": snap.timestamp.isoformat(),
            "world_name": snap.world_name,
            "mc_version": snap.mc_version,
            "sidecars": sidecars,
        })
    return out


def _png_size(path: Path) -> tuple[int, int] | None:
    """Read a PNG's pixel dimensions from its IHDR chunk (no Pillow needed)."""
    try:
        with open(path, "rb") as f:
            sig = f.read(8)
            if sig != b"\x89PNG\r\n\x1a\n":
                return None
            # IHDR is the first chunk: 4 length + 4 type + 13 data + 4 crc
            f.read(4)               # length
            if f.read(4) != b"IHDR":
                return None
            w = int.from_bytes(f.read(4), "big")
            h = int.from_bytes(f.read(4), "big")
            return (w, h)
    except OSError:
        return None


# ---- single-file SPA HTML (Leaflet from unpkg) ----------------------------

_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>chunkvault browse</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
      crossorigin="">
<style>
  html, body { margin: 0; padding: 0; height: 100%; font-family: ui-monospace, Menlo, Consolas, monospace; background: #111; color: #eee; }
  #wrap { display: grid; grid-template-rows: auto 1fr; height: 100%; }
  #ctrl { padding: 10px 14px; background: #1a1a1f; border-bottom: 1px solid #2a2a32; display: flex; flex-wrap: wrap; gap: 14px; align-items: center; }
  #ctrl label { font-size: 12px; color: #aaa; margin-right: 4px; }
  #ctrl select, #ctrl input[type=range] { background: #232329; color: #eee; border: 1px solid #3a3a44; padding: 4px 6px; font: inherit; }
  #ctrl input[type=range] { width: 280px; }
  #snapinfo { font-size: 12px; color: #8af; margin-left: auto; }
  #map { background: #0c0c10; }
  .leaflet-container { background: #0c0c10; }
</style>
</head>
<body>
<div id="wrap">
  <div id="ctrl">
    <label for="snap">snapshot</label>
    <input id="snap" type="range" min="0" max="0" step="1" value="0">
    <select id="snap_sel"></select>
    <label for="dim_sel">layer</label>
    <select id="dim_sel"></select>
    <span id="snapinfo"></span>
  </div>
  <div id="map"></div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
        crossorigin=""></script>
<script>
  const map = L.map('map', {
    crs: L.CRS.Simple,
    minZoom: -6,
    maxZoom: 4,
    zoomSnap: 0.25,
    attributionControl: false,
  });
  let overlay = null;
  let snapshots = [];

  async function init() {
    const r = await fetch('/api/snapshots');
    snapshots = await r.json();
    const slider = document.getElementById('snap');
    const sel    = document.getElementById('snap_sel');
    if (snapshots.length === 0) {
      document.getElementById('snapinfo').textContent =
        'No snapshots in this vault yet.';
      return;
    }
    // Slider: 0 = oldest, len-1 = newest. API returns newest first; reverse for slider.
    snapshots.reverse();
    slider.max = snapshots.length - 1;
    slider.value = snapshots.length - 1;
    snapshots.forEach((s, i) => {
      const opt = document.createElement('option');
      opt.value = i;
      opt.textContent = `${s.timestamp.substring(0, 19)}  ${s.label || s.short_id}`;
      sel.appendChild(opt);
    });
    sel.value = snapshots.length - 1;

    slider.addEventListener('input', () => {
      sel.value = slider.value;
      onSnapChanged();
    });
    sel.addEventListener('change', () => {
      slider.value = sel.value;
      onSnapChanged();
    });
    document.getElementById('dim_sel').addEventListener('change', renderOverlay);
    onSnapChanged();
  }

  function onSnapChanged() {
    const snap = snapshots[+document.getElementById('snap').value];
    const dimSel = document.getElementById('dim_sel');
    // Repopulate dim picker, preserving selection if same dim_mode exists.
    const prev = dimSel.value;
    dimSel.innerHTML = '';
    snap.sidecars.forEach(sc => {
      const opt = document.createElement('option');
      opt.value = sc.dim_mode;
      opt.textContent = sc.dim_mode;
      dimSel.appendChild(opt);
    });
    if ([...dimSel.options].some(o => o.value === prev)) dimSel.value = prev;
    document.getElementById('snapinfo').textContent =
      `${snap.world_name || '?'} · mc ${snap.mc_version || '?'} · ${snap.short_id}`;
    renderOverlay();
  }

  function renderOverlay() {
    const snap = snapshots[+document.getElementById('snap').value];
    const dim_mode = document.getElementById('dim_sel').value;
    const sc = snap.sidecars.find(s => s.dim_mode === dim_mode);
    if (!sc) {
      if (overlay) { map.removeLayer(overlay); overlay = null; }
      return;
    }
    const url = `/thumbnails/${snap.id}/${encodeURIComponent(sc.filename)}`;
    const [w, h] = sc.size_px || [256, 256];
    // CRS.Simple bounds: y is inverted in screen-coords; use [[h,0],[0,w]]
    const bounds = [[h, 0], [0, w]];
    if (overlay) map.removeLayer(overlay);
    overlay = L.imageOverlay(url, bounds, { interactive: false }).addTo(map);
    // Re-fit only on first load or when bounds change drastically.
    if (!map._chunkvault_fitted) {
      map.fitBounds(bounds);
      map._chunkvault_fitted = true;
    }
  }

  init().catch(e => {
    document.getElementById('snapinfo').textContent = 'error: ' + e.message;
  });
</script>
</body>
</html>
"""
