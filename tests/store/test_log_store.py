from __future__ import annotations

from pathlib import Path

import pytest

from chunkvault.store.log_store import LogStore


def _h32(b: int) -> bytes:
    return bytes([b]) * 32


def test_store_and_read_roundtrip(tmp_path: Path):
    store = LogStore(tmp_path)
    store.init()
    sha = _h32(0xAB)
    assert store.has_log(sha) is False
    assert store.store_log(sha, b"log line\nlog line 2") is True
    assert store.has_log(sha) is True
    assert store.read_log(sha) == b"log line\nlog line 2"


def test_store_dedupes(tmp_path: Path):
    store = LogStore(tmp_path)
    store.init()
    sha = _h32(0xCD)
    assert store.store_log(sha, b"first content") is True
    assert store.store_log(sha, b"first content") is False
    assert store.read_log(sha) == b"first content"


def test_read_missing_returns_none(tmp_path: Path):
    store = LogStore(tmp_path)
    store.init()
    assert store.read_log(_h32(0)) is None


def test_path_layout(tmp_path: Path):
    store = LogStore(tmp_path)
    store.init()
    sha = bytes.fromhex("abcdef" + "00" * 29)
    store.store_log(sha, b"x")
    assert (tmp_path / "logs" / "ab" / "cd" / ("ef" + "00" * 29)).is_file()


def test_init_idempotent(tmp_path: Path):
    store = LogStore(tmp_path)
    store.init()
    store.init()
    assert store.logs_dir.is_dir()


def test_short_sha_rejected(tmp_path: Path):
    store = LogStore(tmp_path)
    store.init()
    with pytest.raises(ValueError):
        store.store_log(b"\x00", b"x")
