"""Tests for the wizard i18n layer.

Covers locale registry loading, fallback semantics, env-var resolution,
and persistence — behaviour users will rely on when switching languages.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chunkvault.wizard import i18n as i18n_mod


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Redirect ~/.chunkvault/config.json to a per-test path so tests
    don't stomp on the real user's config (or each other's)."""
    cfg_dir = tmp_path / "_chunkvault"
    monkeypatch.setattr(i18n_mod, "_CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(i18n_mod, "_CONFIG_PATH", cfg_dir / "config.json")
    # Reset locale state per test
    monkeypatch.setattr(i18n_mod, "_current", "en")


def test_all_locales_loaded():
    """Every JSON in locales/ should appear in the registry."""
    tags = {L.tag for L in i18n_mod.list_locales()}
    # The ones we ship today — the test should be updated when adding more.
    expected_subset = {"en", "zh-CN", "zh-TW", "ja", "ko", "de", "fr", "es", "ru"}
    assert expected_subset.issubset(tags), (
        f"missing tags: {expected_subset - tags}"
    )


def test_each_locale_carries_native_name():
    for L in i18n_mod.list_locales():
        assert L.native_name, f"{L.tag} has no native_name"


def test_t_falls_back_to_english_on_missing_key():
    i18n_mod.set_locale("zh-CN", persist=False)
    # A key that exists in en but might or might not in zh-CN; either way
    # the result should be a non-empty string and never the literal key.
    assert i18n_mod.t("menu.repair_ts") not in ("", "menu.repair_ts")


def test_t_returns_key_when_no_translation_anywhere():
    """Unknown keys should surface as the key itself (loud bug)."""
    assert i18n_mod.t("nonsense.totally.fake.key") == "nonsense.totally.fake.key"


def test_t_format_substitution():
    i18n_mod.set_locale("en", persist=False)
    s = i18n_mod.t("prompt.create_repo", path="/tmp/foo")
    assert "/tmp/foo" in s


def test_set_locale_persists_to_config(tmp_path):
    saved = i18n_mod.set_locale("zh-CN")
    assert saved is not None and saved.is_file()
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["locale"] == "zh-CN"


def test_set_locale_rejects_unknown_tag():
    with pytest.raises(ValueError):
        i18n_mod.set_locale("xx-YY")


def test_normalize_tag_handles_posix_locale_strings():
    # zh_CN.UTF-8 -> zh-CN (registered)
    assert i18n_mod._normalize_tag("zh_CN.UTF-8") == "zh-CN"
    # case insensitive
    assert i18n_mod._normalize_tag("ZH-cn") == "zh-CN"
    # primary-only fallback to first matching subtag
    norm = i18n_mod._normalize_tag("zh")
    assert norm in ("zh-CN", "zh-TW")
    # totally unknown
    assert i18n_mod._normalize_tag("klingon") is None


def test_env_var_overrides_config(monkeypatch, tmp_path):
    cfg = i18n_mod._CONFIG_PATH
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"locale": "ja"}), encoding="utf-8")
    monkeypatch.setenv("CHUNKVAULT_LANG", "de")
    assert i18n_mod._resolve_initial_locale() == "de"


def test_zh_cn_translation_for_repair_ts_is_chinese():
    i18n_mod.set_locale("zh-CN", persist=False)
    assert i18n_mod.t("menu.repair_ts") == "修复时间戳"


def test_zh_tw_uses_traditional_characters():
    """zh-TW must use traditional characters, not just simplified.
    Smoke test: the menu label includes 修復 (traditional) not 修复."""
    i18n_mod.set_locale("zh-TW", persist=False)
    label = i18n_mod.t("menu.repair_ts")
    assert "修復" in label
    assert "修复" not in label
