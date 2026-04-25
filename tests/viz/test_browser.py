"""Tests for the Leaflet vault-browser HTTP server."""
from __future__ import annotations

import http.client
import json
import socketserver
import threading
import time
from pathlib import Path

import pytest

from chunkvault.store import ChunkSnapshotRepo
from chunkvault.viz.browser import _list_snapshots, _make_handler, _png_size

from tests._fixtures import ChunkSpec, write_region_file
from tests.store.test_repo import _make_level_dat
from tests.viz.test_snapshot_render import _seed_world_with_real_chunk


@pytest.fixture
def served_repo(tmp_path: Path):
    """Spin up a real HTTP server backed by a fresh repo with one snapshot."""
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    world = _seed_world_with_real_chunk(tmp_path)
    snap = repo.snapshot(world, label="t", verify_roundtrip=False)

    handler_cls = _make_handler(repo)
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Tiny pause so the listener is fully bound before tests fire requests.
    time.sleep(0.05)
    yield repo, snap, port
    server.shutdown()
    server.server_close()


def _get(port: int, path: str) -> tuple[int, bytes, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    headers = dict(resp.getheaders())
    conn.close()
    return resp.status, body, headers


def test_root_returns_html(served_repo):
    repo, snap, port = served_repo
    status, body, headers = _get(port, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"chunkvault browse" in body
    assert b"leaflet" in body.lower()


def test_api_snapshots_lists_snapshot(served_repo):
    repo, snap, port = served_repo
    status, body, headers = _get(port, "/api/snapshots")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    data = json.loads(body)
    assert isinstance(data, list)
    assert any(s["id"] == snap.id for s in data)
    entry = next(s for s in data if s["id"] == snap.id)
    assert entry["label"] == "t"
    assert "sidecars" in entry
    assert any(sc["dim_mode"] == "region-topdown" for sc in entry["sidecars"])


def test_thumbnail_route_serves_png(served_repo):
    repo, snap, port = served_repo
    status, body, headers = _get(
        port, f"/thumbnails/{snap.id}/region-topdown.png",
    )
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    # PNG magic bytes
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


def test_thumbnail_route_404_on_missing(served_repo):
    repo, snap, port = served_repo
    status, _, _ = _get(port, f"/thumbnails/{snap.id}/nonexistent.png")
    assert status == 404


def test_thumbnail_route_blocks_path_traversal(served_repo):
    repo, snap, port = served_repo
    # Try to escape thumbnails/ via ../ — should be blocked
    status, _, _ = _get(port, "/thumbnails/../index.sqlite")
    assert status in (403, 404)


def test_unknown_route_returns_404(served_repo):
    repo, snap, port = served_repo
    status, _, _ = _get(port, "/totally/made/up")
    assert status == 404


# ---- helpers ---------------------------------------------------------------

def test_png_size_reads_ihdr(tmp_path: Path):
    # Build a tiny valid PNG via Pillow
    from PIL import Image
    p = tmp_path / "small.png"
    Image.new("RGB", (47, 23), (10, 20, 30)).save(p, "PNG")
    assert _png_size(p) == (47, 23)


def test_png_size_returns_none_on_garbage(tmp_path: Path):
    p = tmp_path / "bad.png"
    p.write_bytes(b"definitely not a png")
    assert _png_size(p) is None


def test_list_snapshots_for_empty_repo(tmp_path: Path):
    repo = ChunkSnapshotRepo(tmp_path / "repo")
    repo.init()
    assert _list_snapshots(repo, repo.repo_path / "thumbnails") == []
