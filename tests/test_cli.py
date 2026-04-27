from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from chunkvault.cli import main
from chunkvault.storage.repo import git_available

from tests._fixtures import ChunkSpec, write_region_file


def _world(root: Path, name: str, *, payload: bytes = b"content") -> Path:
    w = root / name
    w.mkdir()
    (w / "level.dat").write_bytes(b"fake level.dat")
    write_region_file(w, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, payload),
        ChunkSpec(1, 0, 1, 2, b"second"),
    ])
    return w


# ---- diff ------------------------------------------------------------------

def test_cli_diff_identical_worlds(tmp_path: Path, capsys):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    rc = main(["diff", str(a), str(b)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 chunk changes" in out


def test_cli_diff_with_changes(tmp_path: Path, capsys):
    a = _world(tmp_path, "a", payload=b"v1")
    b = _world(tmp_path, "b", payload=b"v2")
    rc = main(["diff", str(a), str(b)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 chunk changes" in out
    assert "~1" in out  # 1 modified


def test_cli_diff_json_output(tmp_path: Path, capsys):
    a = _world(tmp_path, "a", payload=b"v1")
    b = _world(tmp_path, "b", payload=b"v2")
    json_path = tmp_path / "diff.json"
    rc = main(["diff", str(a), str(b), "--json", str(json_path)])
    assert rc == 0
    assert json_path.exists()
    payload = json.loads(json_path.read_text())
    assert payload["total"] == 1
    assert payload["summary"] == {"added": 0, "modified": 1, "removed": 0}
    assert len(payload["changes"]) == 1
    c = payload["changes"][0]
    assert c["kind"] == "modified"
    assert c["old_hash"] is not None
    assert c["new_hash"] is not None


def test_cli_diff_png_output(tmp_path: Path, capsys):
    a = _world(tmp_path, "a", payload=b"v1")
    b = _world(tmp_path, "b", payload=b"v2")
    png_path = tmp_path / "out.png"
    rc = main([
        "diff", str(a), str(b),
        "--png", "region", str(png_path),
        "--scale", "4",
    ])
    assert rc == 0
    assert png_path.exists()
    img = Image.open(png_path)
    img.verify()


# ---- render ----------------------------------------------------------------

def test_cli_render(tmp_path: Path):
    a = _world(tmp_path, "a", payload=b"v1")
    b = _world(tmp_path, "b", payload=b"v2")
    out = tmp_path / "heat.png"
    rc = main([
        "render", str(a), str(b), "region",
        "-o", str(out), "--scale", "4",
    ])
    assert rc == 0
    assert out.exists()


# ---- storage commands (need git) -------------------------------------------

needs_git = pytest.mark.skipif(not git_available(), reason="git CLI not on PATH")


@needs_git
def test_cli_init_snapshot_list_restore_roundtrip(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    world = _world(tmp_path, "world", payload=b"fresh")

    assert main(["init", "--store=git", str(repo)]) == 0
    assert (repo / "HEAD").is_file()

    assert main(["snapshot", "--store=git", str(repo), str(world), "--label", "v1"]) == 0
    snap_line = capsys.readouterr().out.strip().splitlines()[-1]
    short_id = snap_line.split()[0]
    assert len(short_id) == 12

    assert main(["list", "--store=git", str(repo)]) == 0
    list_out = capsys.readouterr().out
    assert short_id in list_out
    assert "v1" in list_out

    dest = tmp_path / "restored"
    assert main(["restore", "--store=git", str(repo), "v1", str(dest)]) == 0
    assert (dest / "level.dat").read_bytes() == b"fake level.dat"
    assert (dest / "region" / "r.0.0.mca").exists()


@needs_git
def test_cli_restore_single_path(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    world = _world(tmp_path, "world")
    main(["init", "--store=git", str(repo)])
    main(["snapshot", "--store=git", str(repo), str(world), "--label", "x"])
    capsys.readouterr()  # drain

    dest = tmp_path / "partial"
    rc = main([
        "restore", "--store=git", str(repo), "x", str(dest),
        "--path", "region/r.0.0.mca",
    ])
    assert rc == 0
    # Only that one file, nothing else
    files = sorted(p.relative_to(dest).as_posix()
                   for p in dest.rglob("*") if p.is_file())
    assert files == ["region/r.0.0.mca"]


@needs_git
def test_cli_delete(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    world = _world(tmp_path, "world")
    main(["init", "--store=git", str(repo)])
    main(["snapshot", "--store=git", str(repo), str(world), "--label", "doomed"])
    capsys.readouterr()

    assert main(["delete", "--store=git", str(repo), "doomed"]) == 0
    capsys.readouterr()  # drain the "deleted snapshot ..." confirmation line
    main(["list", "--store=git", str(repo)])
    out = capsys.readouterr().out
    # The snapshot should no longer appear in list output
    assert "doomed" not in out
    assert out.strip() == ""


# ---- error paths -----------------------------------------------------------

def test_cli_no_subcommand_invokes_wizard(monkeypatch, capsys):
    """No-subcommand invocation should drop into the wizard."""
    called = {"value": False}

    def fake_run_wizard(console=None):
        called["value"] = True
        return 0

    import chunkvault.wizard
    monkeypatch.setattr(chunkvault.wizard, "run_wizard", fake_run_wizard)
    rc = main([])
    assert rc == 0
    assert called["value"] is True


def test_cli_diff_missing_world(tmp_path: Path, capsys):
    """A missing world directory just has no region dirs → empty diff, rc 0.

    We do NOT error on missing worlds here — enumerate_region_dirs is
    defensive by design. Error-on-missing is something the caller can add.
    """
    rc = main(["diff", str(tmp_path / "nope_a"), str(tmp_path / "nope_b")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 chunk changes" in out


def test_cli_diff_html_output(tmp_path: Path, capsys):
    a = _world(tmp_path, "a", payload=b"v1")
    b = _world(tmp_path, "b", payload=b"v2")
    html_path = tmp_path / "map.html"
    rc = main(["diff", str(a), str(b), "--html", str(html_path)])
    assert rc == 0
    assert html_path.exists()
    content = html_path.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content
    assert "leaflet" in content.lower()


# ---- diff-snaps ------------------------------------------------------------

def test_cli_diff_base_tiles_url_in_html(tmp_path: Path, capsys):
    a = _world(tmp_path, "a", payload=b"v1")
    b = _world(tmp_path, "b", payload=b"v2")
    html_path = tmp_path / "map.html"
    rc = main([
        "diff", str(a), str(b),
        "--html", str(html_path),
        "--base-tiles", "./tiles/{z}/{x}/{y}.png",
        "--base-attribution", "unmined",
    ])
    assert rc == 0
    content = html_path.read_text(encoding="utf-8")
    assert "./tiles/{z}/{x}/{y}.png" in content
    assert "unmined" in content


@needs_git
def test_cli_diff_snaps_fast_matches_slow(tmp_path: Path, capsys):
    """--fast CLI flag should produce the same change set as the default path."""
    repo = tmp_path / "repo"
    world = _world(tmp_path, "world", payload=b"initial")
    main(["init", "--store=git", str(repo)])
    main(["snapshot", "--store=git", str(repo), str(world), "--label", "a"])
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"different"),
        ChunkSpec(1, 0, 1, 2, b"second"),
    ])
    main(["snapshot", "--store=git", str(repo), str(world), "--label", "b"])
    capsys.readouterr()

    slow_json = tmp_path / "slow.json"
    fast_json = tmp_path / "fast.json"
    assert main(["diff-snaps", "--store=git", str(repo), "a", "b",
                 "--json", str(slow_json)]) == 0
    capsys.readouterr()
    assert main(["diff-snaps", "--store=git", str(repo), "a", "b", "--fast",
                 "--json", str(fast_json)]) == 0
    slow_data = json.loads(slow_json.read_text())
    fast_data = json.loads(fast_json.read_text())
    assert slow_data["total"] == fast_data["total"]
    assert slow_data["summary"] == fast_data["summary"]


def test_cli_render_tiles_missing_unmined(tmp_path: Path, capsys):
    """When unmined isn't on PATH, render-tiles should fail with a clear error."""
    from chunkvault.viz.tiles import unmined_available
    if unmined_available() is not None:
        pytest.skip("unmined is installed — skip 'missing' test")
    world = _world(tmp_path, "world")
    with pytest.raises(Exception, match="unmined"):
        main(["render-tiles", str(world), str(tmp_path / "tiles")])


@needs_git
def test_cli_diff_snaps(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    world = _world(tmp_path, "world", payload=b"v1")
    main(["init", "--store=git", str(repo)])
    main(["snapshot", "--store=git", str(repo), str(world), "--label", "a"])
    (world / "region" / "r.0.0.mca").write_bytes(b"different bytes entirely")
    main(["snapshot", "--store=git", str(repo), str(world), "--label", "b"])
    capsys.readouterr()

    rc = main(["diff-snaps", "--store=git", str(repo), "a", "b"])
    out = capsys.readouterr().out
    assert rc == 0
    # We don't hardcode counts (corrupt-looking bytes may yield errors or
    # removed/added entries). Just verify the command ran through.
    assert ".." in out  # the "<a>..<b>:" header


@needs_git
def test_cli_diff_snaps_html_and_json(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    world = _world(tmp_path, "world", payload=b"v1")
    main(["init", "--store=git", str(repo)])
    main(["snapshot", "--store=git", str(repo), str(world), "--label", "a"])
    # Make a clean change that won't corrupt parsing
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"changed"),
        ChunkSpec(1, 0, 1, 2, b"second"),
    ])
    main(["snapshot", "--store=git", str(repo), str(world), "--label", "b"])
    capsys.readouterr()

    json_path = tmp_path / "d.json"
    html_path = tmp_path / "d.html"
    rc = main([
        "diff-snaps", "--store=git", str(repo), "a", "b",
        "--json", str(json_path), "--html", str(html_path),
    ])
    assert rc == 0
    assert json_path.exists()
    payload = json.loads(json_path.read_text())
    assert "snap_a" in payload and "snap_b" in payload
    assert payload["total"] >= 1
    assert html_path.exists()


# ---- gc --------------------------------------------------------------------

@needs_git
def test_cli_gc(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    world = _world(tmp_path, "world")
    main(["init", "--store=git", str(repo)])
    main(["snapshot", "--store=git", str(repo), str(world), "--label", "doomed"])
    main(["delete", "--store=git", str(repo), "doomed"])
    capsys.readouterr()

    rc = main(["gc", "--store=git", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gc complete" in out


# ---- snapshot --allow-live -------------------------------------------------

@needs_git
def test_cli_snapshot_allow_live_flag_accepted(tmp_path: Path, capsys):
    """The --allow-live flag should be accepted and exit cleanly even on a
    perfectly unlocked world (the flag just disables a check)."""
    repo = tmp_path / "repo"
    world = _world(tmp_path, "world")
    main(["init", "--store=git", str(repo)])
    rc = main(["snapshot", "--store=git", str(repo), str(world), "--label", "forced",
               "--allow-live"])
    assert rc == 0


# ---- chunk store via CLI (default backend) --------------------------------

def test_cli_chunk_store_init_snapshot_list_restore_roundtrip(tmp_path: Path, capsys):
    """End-to-end through the chunk-store backend (the default)."""
    repo = tmp_path / "repo"
    world = _world(tmp_path, "world", payload=b"chunkv1")

    assert main(["init", str(repo)]) == 0
    # Chunk-store layout markers
    assert (repo / "chunks").is_dir()
    assert (repo / "files").is_dir()
    assert (repo / "manifests").is_dir()
    assert (repo / "index.sqlite").is_file()

    assert main(["snapshot", str(repo), str(world), "--label", "v1"]) == 0
    capsys.readouterr()

    assert main(["list", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "v1" in out

    dest = tmp_path / "restored"
    assert main(["restore", str(repo), "v1", str(dest)]) == 0
    assert (dest / "level.dat").read_bytes() == b"fake level.dat"
    assert (dest / "region" / "r.0.0.mca").is_file()


def test_cli_chunk_store_diff_snaps(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    world = _world(tmp_path, "world", payload=b"chunkv1")
    main(["init", str(repo)])
    main(["snapshot", str(repo), str(world), "--label", "a"])
    write_region_file(world, "region", 0, 0, [
        ChunkSpec(0, 0, 1, 2, b"changed"),
        ChunkSpec(1, 0, 1, 2, b"second"),
    ])
    main(["snapshot", str(repo), str(world), "--label", "b"])
    capsys.readouterr()

    json_path = tmp_path / "diff.json"
    rc = main(["diff-snaps", str(repo), "a", "b", "--json", str(json_path)])
    assert rc == 0
    data = json.loads(json_path.read_text())
    assert data["total"] >= 1


def _make_multi_server_zip(tmp_path: Path, servers: list[str]) -> Path:
    """Build a zip containing multiple server folders, each with a world dir."""
    import zipfile
    src = tmp_path / "src"
    src.mkdir()
    for name in servers:
        server = src / name
        server.mkdir()
        world = server / "world"
        world.mkdir()
        (world / "level.dat").write_bytes(b"placeholder")
        write_region_file(world, "region", 0, 0, [
            ChunkSpec(0, 0, 1, 2, name.encode()),
        ])
        (server / "logs").mkdir()
        (server / "logs" / "latest.log").write_bytes(f"log of {name}".encode())
    archive = tmp_path / "2025-04-25-12-34-56.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for entry in src.rglob("*"):
            if entry.is_file():
                zf.write(entry, entry.relative_to(src).as_posix())
    return archive


def test_cli_ingest_multi_server_zip(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    archive = _make_multi_server_zip(tmp_path, ["EX-Server", "CR-Server"])
    main(["init", str(repo)])
    rc = main(["ingest", str(repo), str(archive)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "EX-Server" in out
    assert "CR-Server" in out
    assert "log snapshot" in out


def test_cli_logs_list_after_ingest(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    archive = _make_multi_server_zip(tmp_path, ["EX-Server"])
    main(["init", str(repo)])
    main(["ingest", str(repo), str(archive)])
    capsys.readouterr()
    rc = main(["logs-list", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2025-04-25-12-34-56" in out
    assert "servers=1" in out


def test_cli_logs_extract(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    archive = _make_multi_server_zip(tmp_path, ["EX-Server", "CR-Server"])
    main(["init", str(repo)])
    main(["ingest", str(repo), str(archive)])
    capsys.readouterr()
    dest = tmp_path / "extracted"
    rc = main(["logs-extract", str(repo), "2025-04-25-12-34-56", str(dest)])
    assert rc == 0
    assert (dest / "EX-Server" / "logs" / "latest.log").read_bytes() \
        == b"log of EX-Server"
    assert (dest / "CR-Server" / "logs" / "latest.log").read_bytes() \
        == b"log of CR-Server"


def test_cli_logs_extract_with_server_filter(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    archive = _make_multi_server_zip(tmp_path, ["EX-Server", "CR-Server"])
    main(["init", str(repo)])
    main(["ingest", str(repo), str(archive)])
    capsys.readouterr()
    dest = tmp_path / "ex-only"
    main(["logs-extract", str(repo), "2025-04-25-12-34-56", str(dest),
          "--server", "EX-Server"])
    assert (dest / "EX-Server").is_dir()
    assert not (dest / "CR-Server").exists()


def test_cli_logs_delete(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    archive = _make_multi_server_zip(tmp_path, ["EX-Server"])
    main(["init", str(repo)])
    main(["ingest", str(repo), str(archive)])
    capsys.readouterr()
    rc = main(["logs-delete", str(repo), "2025-04-25-12-34-56"])
    assert rc == 0
    capsys.readouterr()
    main(["logs-list", str(repo)])
    out = capsys.readouterr().out
    assert "no log snapshots" in out


def test_cli_chunk_store_gc(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    world = _world(tmp_path, "world")
    main(["init", str(repo)])
    main(["snapshot", str(repo), str(world), "--label", "doomed"])
    main(["delete", str(repo), "doomed"])
    capsys.readouterr()
    rc = main(["gc", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gc complete" in out
    # Reported counts are present
    assert "removed" in out and "chunks" in out


# ---- verify-folders --------------------------------------------------------

def test_cli_verify_folders_pass(tmp_path: Path, capsys):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    rc = main(["verify-folders", str(a), str(b)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_cli_verify_folders_fail_on_diff(tmp_path: Path, capsys):
    a = _world(tmp_path, "a", payload=b"v1")
    b = _world(tmp_path, "b", payload=b"v2")
    rc = main(["verify-folders", str(a), str(b)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL" in captured.out
    # Mismatch preview lands on stderr
    assert "hash_mismatch" in captured.err


def test_cli_verify_folders_writes_detailed_report(tmp_path: Path, capsys):
    a = _world(tmp_path, "a", payload=b"v1")
    b = _world(tmp_path, "b", payload=b"v2")
    (a / "extra-on-left.txt").write_bytes(b"only-here")
    report_path = tmp_path / "report.txt"
    rc = main([
        "verify-folders", str(a), str(b),
        "--report", str(report_path),
    ])
    assert rc == 1
    assert report_path.is_file()
    content = report_path.read_text(encoding="utf-8")
    assert "FAIL" in content
    assert "chunk mismatches" in content
    assert "extra-on-left.txt" in content


def test_cli_verify_folders_exclude_pattern(tmp_path: Path, capsys):
    a = _world(tmp_path, "a")
    b = _world(tmp_path, "b")
    (a / "session.lock").write_bytes(b"x")
    rc = main([
        "verify-folders", str(a), str(b),
        "--exclude", "session.lock",
    ])
    assert rc == 0  # excluded → not a mismatch

