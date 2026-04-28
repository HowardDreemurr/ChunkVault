"""Tests for the user-config registry (repos + source paths)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from chunkvault.wizard import config as cfg


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Each test gets its own ~/.chunkvault/config.json so they don't
    stomp on the real user's settings or each other's."""
    cfg_dir = tmp_path / "_chunkvault"
    monkeypatch.setattr(cfg, "_CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(cfg, "_CONFIG_PATH", cfg_dir / "config.json")


def test_empty_when_no_config(tmp_path):
    assert cfg.list_repos() == []
    assert cfg.list_source_paths() == []


def test_add_then_list_repo(tmp_path):
    target = tmp_path / "vault"
    target.mkdir()
    assert cfg.add_repo(target, label="main") is True
    repos = cfg.list_repos()
    assert len(repos) == 1
    assert repos[0].path == target.resolve()
    assert repos[0].label == "main"


def test_add_repo_idempotent(tmp_path):
    target = tmp_path / "vault"
    target.mkdir()
    assert cfg.add_repo(target) is True
    assert cfg.add_repo(target) is False   # already there
    assert len(cfg.list_repos()) == 1


def test_add_repo_updates_label_if_changed(tmp_path):
    target = tmp_path / "vault"
    target.mkdir()
    cfg.add_repo(target, label="old")
    cfg.add_repo(target, label="new")
    repos = cfg.list_repos()
    assert len(repos) == 1
    assert repos[0].label == "new"


def test_remove_repo(tmp_path):
    target = tmp_path / "vault"
    target.mkdir()
    cfg.add_repo(target)
    assert cfg.remove_repo(target) is True
    assert cfg.list_repos() == []


def test_remove_unregistered_repo_returns_false(tmp_path):
    assert cfg.remove_repo(tmp_path / "nope") is False


def test_source_paths_independent_from_repos(tmp_path):
    repo_dir = tmp_path / "vault"
    src_dir = tmp_path / "backups"
    repo_dir.mkdir()
    src_dir.mkdir()
    cfg.add_repo(repo_dir)
    cfg.add_source_path(src_dir)
    assert len(cfg.list_repos()) == 1
    assert len(cfg.list_source_paths()) == 1


def test_config_file_preserves_unrelated_keys(tmp_path):
    """The file is shared with i18n.py — config.py must not clobber its keys."""
    cfg._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg._CONFIG_PATH.write_text(
        json.dumps({"locale": "zh-CN", "future_field": "preserve me"}),
        encoding="utf-8",
    )
    cfg.add_repo(tmp_path / "vault")
    data = json.loads(cfg._CONFIG_PATH.read_text(encoding="utf-8"))
    assert data["locale"] == "zh-CN"
    assert data["future_field"] == "preserve me"
    assert "repos" in data


def test_corrupt_config_doesnt_crash(tmp_path):
    cfg._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg._CONFIG_PATH.write_text("not valid json {{{", encoding="utf-8")
    # Reading should return empty
    assert cfg.list_repos() == []
    assert cfg.list_source_paths() == []
    # Adding should still work — overwrites the corrupt file
    target = tmp_path / "vault"
    target.mkdir()
    cfg.add_repo(target)
    assert len(cfg.list_repos()) == 1


def test_detect_environment_uses_registered_repos(tmp_path, monkeypatch):
    """The bug this fixes: wizard couldn't see your vault on F:\\ when
    launched from C:\\ because auto-detection only looked at cwd."""
    from chunkvault.wizard import detect

    # Register a vault somewhere completely outside cwd's reach
    far_away = tmp_path / "far" / "BackupVault"
    far_away.mkdir(parents=True)
    # Make it look like a real chunkvault vault
    (far_away / "chunks").mkdir()
    (far_away / "files").mkdir()
    (far_away / "manifests").mkdir()
    (far_away / "logs").mkdir()
    (far_away / "log-snapshots").mkdir()
    import sqlite3
    conn = sqlite3.connect(far_away / "index.sqlite")
    conn.execute("CREATE TABLE snapshots (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    cfg.add_repo(far_away)

    # detect_environment should pick it up via the registry, not just cwd
    monkeypatch.chdir(tmp_path)   # cwd isn't far_away
    env = detect.detect_environment()
    repo_paths = {r.path for r in env.repos}
    assert far_away.resolve() in repo_paths


# ---- CLI integration -------------------------------------------------------

def test_cli_repo_add_list_remove(tmp_path, monkeypatch, capsys):
    import subprocess, sys
    cfg_dir = tmp_path / "_chunkvault"
    target = tmp_path / "vault"
    target.mkdir()
    # Point HOME at tmp so the CLI subprocess writes to our isolated config
    env = {**__import__("os").environ, "USERPROFILE": str(tmp_path),
           "HOME": str(tmp_path)}

    proc = subprocess.run(
        [sys.executable, "-m", "chunkvault", "repo", "add",
         str(target), "--label", "test-vault"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "added" in proc.stdout

    proc = subprocess.run(
        [sys.executable, "-m", "chunkvault", "repo", "list"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert str(target.resolve()) in proc.stdout
    assert "test-vault" in proc.stdout

    proc = subprocess.run(
        [sys.executable, "-m", "chunkvault", "repo", "remove", str(target)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert "removed" in proc.stdout
