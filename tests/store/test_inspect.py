"""Archive preview tests — peek inside zips without extracting."""
from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chunkvault.store.inspect import preview_archive

from tests._fixtures import ChunkSpec, write_region_file


def _make_server(root: Path, name: str, *, dims=("region",), n_logs=2):
    s = root / name
    s.mkdir(parents=True, exist_ok=True)
    (s / "level.dat").write_bytes(b"placeholder level.dat content")
    for dim in dims:
        write_region_file(s / "world", dim, 0, 0, [
            ChunkSpec(0, 0, 1, 2, b"x"),
        ])
    (s / "logs").mkdir()
    for i in range(n_logs):
        (s / "logs" / f"server-{i}.log").write_bytes(b"log line\n" * 50)
    (s / "crash-reports").mkdir()
    (s / "crash-reports" / "crash.txt").write_bytes(b"oops")
    (s / "server.properties").write_bytes(b"max-players=10")
    return s


def _zip_dir(zip_path: Path, src: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in src.rglob("*"):
            if entry.is_file():
                zf.write(entry, entry.relative_to(src).as_posix())


# ---- happy path ------------------------------------------------------------

def test_preview_zip_finds_servers_and_counts(tmp_path: Path):
    src = tmp_path / "src"
    _make_server(src, "EX-Server", dims=("region", "DIM-1/region"), n_logs=3)
    _make_server(src, "CR-Server", dims=("region",), n_logs=1)
    archive = tmp_path / "2025-04-25-12-34-56.zip"
    _zip_dir(archive, src)

    p = preview_archive(archive)
    assert p.error is None
    assert p.timestamp == datetime(2025, 4, 25, 12, 34, 56, tzinfo=timezone.utc)
    assert {s.name for s in p.servers} == {"EX-Server", "CR-Server"}

    by_name = {s.name: s for s in p.servers}
    ex = by_name["EX-Server"]
    assert ex.region_files == 2          # one in region/, one in DIM-1/region/
    assert "region" in ex.dimensions
    assert "DIM-1/region" in ex.dimensions
    assert ex.log_files == 3
    assert ex.crash_report_files == 1
    assert ex.has_level_dat is True
    assert ex.estimated_world_bytes > 0
    assert ex.estimated_log_bytes > 0

    cr = by_name["CR-Server"]
    assert cr.region_files == 1
    assert cr.log_files == 1


def test_preview_directory_passthrough(tmp_path: Path):
    src = tmp_path / "raw-src"
    _make_server(src, "EX-Server")
    p = preview_archive(src)
    assert p.error is None
    assert len(p.servers) == 1


def test_preview_missing_archive_returns_error(tmp_path: Path):
    p = preview_archive(tmp_path / "nope.zip")
    assert p.error is not None
    assert "does not exist" in p.error


def test_preview_corrupt_zip_returns_error(tmp_path: Path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip file")
    p = preview_archive(bad)
    assert p.error is not None


def test_preview_unsupported_format(tmp_path: Path):
    weird = tmp_path / "x.7z"
    weird.write_bytes(b"")
    p = preview_archive(weird)
    assert p.error is not None
    assert "unsupported" in p.error.lower()


def test_preview_other_top_level_entries_listed(tmp_path: Path):
    """Top-level files/folders that aren't servers should be reported separately."""
    src = tmp_path / "src"
    src.mkdir()
    _make_server(src, "EX-Server")
    (src / "readme.txt").write_text("hello")
    (src / "configs").mkdir()
    (src / "configs" / "junk.txt").write_text("x")
    archive = tmp_path / "x.zip"
    _zip_dir(archive, src)

    p = preview_archive(archive)
    assert "EX-Server" in {s.name for s in p.servers}
    # readme.txt and configs/ are not servers
    assert "readme.txt" in p.other_top_level
    assert "configs" in p.other_top_level


def test_preview_empty_archive(tmp_path: Path):
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        pass
    p = preview_archive(archive)
    assert p.error is None
    assert p.servers == []


def test_preview_doesnt_extract(tmp_path: Path):
    """Sanity: preview must not write any files anywhere."""
    src = tmp_path / "src"
    _make_server(src, "EX-Server")
    archive = tmp_path / "x.zip"
    _zip_dir(archive, src)

    files_before = sorted(tmp_path.rglob("*"))
    preview_archive(archive)
    files_after = sorted(tmp_path.rglob("*"))
    assert files_before == files_after
