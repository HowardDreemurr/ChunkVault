from __future__ import annotations

import json
from pathlib import Path

import pytest

from chunkvault.store.log_manifest import (
    LogFileRecord,
    LogManifest,
    LogManifestError,
    read_log_manifest,
    write_log_manifest,
)


def _h32(b: int) -> bytes:
    return bytes([b]) * 32


def _make() -> LogManifest:
    return LogManifest(
        id="abcdef0123456789",
        label="2025-04-25-12-34-56",
        timestamp_ms=1_714_049_696_000,
        source_path="/path/to/source.zip",
        servers={
            "EX-Server": [
                LogFileRecord("logs/server.log", _h32(0xAA), 12345, 1714049696000),
                LogFileRecord("crash-reports/c.txt", _h32(0xBB), 999, 0),
            ],
            "CR-Server": [
                LogFileRecord("logs/server.log", _h32(0xCC), 5000, 1714049000000),
            ],
        },
    )


def test_roundtrip_preserves_everything(tmp_path: Path):
    m = _make()
    out = tmp_path / "snap.json"
    write_log_manifest(out, m)
    loaded = read_log_manifest(out)
    assert loaded.id == m.id
    assert loaded.label == m.label
    assert loaded.timestamp_ms == m.timestamp_ms
    assert loaded.source_path == m.source_path
    assert set(loaded.servers) == set(m.servers)
    for srv in m.servers:
        assert loaded.servers[srv] == m.servers[srv]


def test_empty_servers_roundtrip(tmp_path: Path):
    m = LogManifest(id="x", label=None, timestamp_ms=0, source_path=None)
    out = tmp_path / "x.json"
    write_log_manifest(out, m)
    loaded = read_log_manifest(out)
    assert loaded.servers == {}
    assert loaded.label is None
    assert loaded.source_path is None


def test_label_can_be_none(tmp_path: Path):
    m = LogManifest(id="x", label=None, timestamp_ms=1, source_path=None)
    out = tmp_path / "x.json"
    write_log_manifest(out, m)
    assert read_log_manifest(out).label is None


def test_human_readable_json_output(tmp_path: Path):
    """Manifest should be plain JSON the user can grep/edit."""
    m = _make()
    out = tmp_path / "x.json"
    write_log_manifest(out, m)
    text = out.read_text(encoding="utf-8")
    assert "EX-Server" in text
    assert "logs/server.log" in text
    # Indented, sorted keys → reproducible
    data = json.loads(text)
    assert data["id"] == "abcdef0123456789"


def test_bad_sha256_length_raises(tmp_path: Path):
    out = tmp_path / "bad.json"
    out.write_text(json.dumps({
        "id": "x", "label": None, "timestamp_ms": 0, "source_path": None,
        "servers": {"S": [{"path": "x", "sha256": "ab", "size": 1, "mtime_ms": 0}]},
    }))
    with pytest.raises(LogManifestError, match="32 bytes"):
        read_log_manifest(out)


def test_corrupt_json_raises(tmp_path: Path):
    out = tmp_path / "bad.json"
    out.write_text("{ this is not valid json")
    with pytest.raises(LogManifestError):
        read_log_manifest(out)


def test_missing_required_field_raises(tmp_path: Path):
    out = tmp_path / "bad.json"
    out.write_text(json.dumps({"label": None}))  # missing id, timestamp, etc.
    with pytest.raises(LogManifestError):
        read_log_manifest(out)
