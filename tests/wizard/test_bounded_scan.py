"""Tests for the depth-bounded archive scan."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from chunkvault.wizard.detect import default_paths_to_scan, scan_for_archives


def _zip(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("x.txt", "hi")


def test_scan_finds_archives_within_depth(tmp_path: Path):
    _zip(tmp_path / "shallow.zip")
    _zip(tmp_path / "a" / "b" / "deep.zip")            # depth 3 (a, b, file)
    _zip(tmp_path / "a" / "b" / "c" / "d" / "tooDeep.zip")  # depth 5
    summary = scan_for_archives(tmp_path, max_depth=3)
    assert summary is not None
    names = set(summary.samples)
    assert "shallow.zip" in names
    assert "deep.zip" in names
    assert "tooDeep.zip" not in names


def test_scan_skips_well_known_noise_dirs(tmp_path: Path):
    """node_modules, AppData, etc. should never be descended into."""
    _zip(tmp_path / "regular.zip")
    _zip(tmp_path / "node_modules" / "should-not-find.zip")
    _zip(tmp_path / "AppData" / "should-not-find.zip")
    _zip(tmp_path / ".git" / "should-not-find.zip")
    summary = scan_for_archives(tmp_path, max_depth=5)
    assert summary is not None
    names = set(summary.samples)
    assert "regular.zip" in names
    assert "should-not-find.zip" not in names


def test_scan_returns_none_for_no_archives(tmp_path: Path):
    (tmp_path / "readme.txt").write_text("not an archive")
    assert scan_for_archives(tmp_path) is None


def test_scan_handles_missing_dir(tmp_path: Path):
    assert scan_for_archives(tmp_path / "ghost") is None


def test_scan_max_files_safety_brake(tmp_path: Path):
    """Even if we set a tiny budget, the scan returns whatever we found
    rather than hanging or raising."""
    for i in range(200):
        (tmp_path / f"f{i}.txt").write_text("x")
    _zip(tmp_path / "a.zip")
    summary = scan_for_archives(tmp_path, max_files=50, max_depth=2)
    # We may or may not have visited a.zip depending on iteration order,
    # but the call must not have raised or hung.
    if summary is not None:
        assert summary.archive_count >= 1


def test_default_paths_is_just_cwd():
    """Critical: defaults must NOT include drive roots (would scan TBs)."""
    paths = default_paths_to_scan()
    assert paths == [Path.cwd().resolve()]
