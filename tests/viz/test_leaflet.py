from __future__ import annotations

import json
import re
from pathlib import Path

from chunkvault.diff.world import ChunkDiff, RegionError, WorldDiff
from chunkvault.viz.leaflet import render_diff_html


def _chunk(cx: int, cz: int, kind: str, dim: str = "region") -> ChunkDiff:
    return ChunkDiff(
        dimension_key=dim, rx=cx // 32, rz=cz // 32,
        cx=cx, cz=cz, kind=kind,
        old_hash=b"\x00" * 16 if kind != "added" else None,
        new_hash=b"\xff" * 16 if kind != "removed" else None,
    )


def _diff(*changes: ChunkDiff, errors=None) -> WorldDiff:
    return WorldDiff(
        old_root=Path("/fake/a"), new_root=Path("/fake/b"),
        changes=list(changes), errors=list(errors or []),
    )


def _extract_data_json(html: str) -> dict:
    """Pull the DATA = {...} JSON out of the rendered HTML."""
    match = re.search(r"const DATA = (\{.*?\});", html, re.DOTALL)
    assert match, "DATA JSON not found in HTML"
    return json.loads(match.group(1))


def _extract_summary_json(html: str) -> dict:
    match = re.search(r"const SUMMARY = (\{.*?\});", html, re.DOTALL)
    assert match, "SUMMARY JSON not found in HTML"
    return json.loads(match.group(1))


# ---- structural checks ------------------------------------------------------

def test_empty_diff_still_produces_valid_html():
    html = render_diff_html(_diff())
    assert html.startswith("<!DOCTYPE html>")
    assert "<html" in html
    assert "</html>" in html
    # Leaflet script tag must be present
    assert "leaflet@1.9.4" in html
    # Data should be an empty object
    assert _extract_data_json(html) == {}


def test_html_escapes_title():
    html = render_diff_html(_diff(), title='<script>alert("xss")</script>')
    assert "&lt;script&gt;" in html
    assert "<script>alert" not in html


# ---- payload contents -------------------------------------------------------

def test_data_grouped_by_dimension():
    html = render_diff_html(_diff(
        _chunk(0, 0, "added", dim="region"),
        _chunk(1, 0, "modified", dim="region"),
        _chunk(10, 10, "removed", dim="DIM-1/region"),
    ))
    data = _extract_data_json(html)
    assert set(data.keys()) == {"region", "DIM-1/region"}
    assert len(data["region"]) == 2
    assert len(data["DIM-1/region"]) == 1


def test_chunk_entries_have_expected_fields():
    html = render_diff_html(_diff(_chunk(5, 7, "modified")))
    data = _extract_data_json(html)
    entry = data["region"][0]
    assert entry["cx"] == 5
    assert entry["cz"] == 7
    assert entry["kind"] == "modified"
    assert entry["old"] == "00" * 16
    assert entry["new"] == "ff" * 16


def test_added_chunk_has_null_old_hash():
    html = render_diff_html(_diff(_chunk(0, 0, "added")))
    data = _extract_data_json(html)
    assert data["region"][0]["old"] is None
    assert data["region"][0]["new"] is not None


def test_removed_chunk_has_null_new_hash():
    html = render_diff_html(_diff(_chunk(0, 0, "removed")))
    data = _extract_data_json(html)
    assert data["region"][0]["old"] is not None
    assert data["region"][0]["new"] is None


def test_summary_counts():
    html = render_diff_html(_diff(
        _chunk(0, 0, "added"),
        _chunk(1, 0, "added"),
        _chunk(2, 0, "modified"),
        _chunk(3, 0, "removed"),
    ))
    summary = _extract_summary_json(html)
    assert summary["added"] == 2
    assert summary["modified"] == 1
    assert summary["removed"] == 1
    assert summary["dimensions"] == ["region"]
    assert summary["errors"] == 0


def test_summary_includes_version_info_when_present():
    """When the diff carries old/new MC versions (chunk-store provenance),
    the summary in the HTML should include them."""
    diff = WorldDiff(
        old_root=Path("/x"), new_root=Path("/y"),
        changes=[_chunk(0, 0, "modified")],
        old_mc_version="1.16.5", new_mc_version="1.20.4",
        old_data_version=2586, new_data_version=3700,
        old_label="legacy", new_label="modern",
    )
    html = render_diff_html(diff)
    summary = _extract_summary_json(html)
    assert "versions" in summary
    v = summary["versions"]
    assert v["old_mc_version"] == "1.16.5"
    assert v["new_mc_version"] == "1.20.4"
    assert v["version_changed"] is True
    assert v["old_label"] == "legacy"
    assert v["new_label"] == "modern"


def test_summary_omits_versions_for_directory_diff():
    """A diff with no version metadata (e.g. from diff_worlds on plain dirs)
    must not emit a 'versions' field — keeps the panel clean."""
    diff = WorldDiff(
        old_root=Path("/x"), new_root=Path("/y"),
        changes=[_chunk(0, 0, "modified")],
    )
    html = render_diff_html(diff)
    summary = _extract_summary_json(html)
    assert "versions" not in summary


def test_summary_reports_errors():
    err = RegionError(
        dimension_key="region", rx=0, rz=0,
        side="old", path=Path("/fake/r.0.0.mca"),
        message="corrupt",
    )
    html = render_diff_html(_diff(errors=[err]))
    summary = _extract_summary_json(html)
    assert summary["errors"] == 1


# ---- writing to disk --------------------------------------------------------

def test_base_tiles_absent_no_tilelayer():
    """Without base_tiles_url there should be no L.tileLayer call."""
    html = render_diff_html(_diff(_chunk(0, 0, "added")))
    # The BASE_TILES constant should be present but null
    assert "const BASE_TILES = null" in html


def test_base_tiles_url_embedded():
    html = render_diff_html(
        _diff(_chunk(0, 0, "added")),
        base_tiles_url="./tiles/{z}/{x}/{y}.png",
        base_tiles_attribution="rendered with unmined",
    )
    # The URL must appear in the embedded config
    match = re.search(r"const BASE_TILES = (\{.*?\});", html)
    assert match
    cfg = json.loads(match.group(1))
    assert cfg["url"] == "./tiles/{z}/{x}/{y}.png"
    assert cfg["attribution"] == "rendered with unmined"


def test_base_tiles_with_custom_zoom_range():
    html = render_diff_html(
        _diff(),
        base_tiles_url="https://example.com/{z}/{x}/{y}.png",
        base_tiles_min_zoom=0,
        base_tiles_max_zoom=5,
    )
    match = re.search(r"const BASE_TILES = (\{.*?\});", html)
    cfg = json.loads(match.group(1))
    assert cfg["minZoom"] == 0
    assert cfg["maxZoom"] == 5


def test_out_path_writes_file(tmp_path: Path):
    d = _diff(_chunk(0, 0, "added"))
    out = tmp_path / "map.html"
    returned = render_diff_html(d, out_path=out)
    assert out.exists()
    assert out.read_text(encoding="utf-8") == returned
    # Basic structural sanity on the written file
    content = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content
    assert "Leaflet" in content or "leaflet" in content
