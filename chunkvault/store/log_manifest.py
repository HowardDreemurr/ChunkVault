"""JSON manifest format for log snapshots.

Logs are far less numerous than world chunks (typically a few dozen files
per server per backup), so a small JSON manifest is fine — no need for
the binary packing we use for chunk manifests. Easy to inspect, edit, or
extend with new fields later.

Schema:

    {
      "id":            "<32-hex uuid>",
      "label":         "2025-04-25-12-34-56" | null,
      "timestamp_ms":  1714049696000,
      "source_path":   "/path/to/2025-04-25-12-34-56.zip" | null,
      "servers": {
        "EX-Server": [
          {
            "path":   "logs/server.log",
            "sha256": "abcd...",
            "size":   12345,
            "mtime_ms": 1714049696000
          },
          ...
        ],
        "CR-Server": [...]
      }
    }
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class LogManifestError(Exception):
    """A log manifest blob was malformed or had unexpected shape."""


@dataclass(frozen=True)
class LogFileRecord:
    relative_path: str   # posix-style under the server root
    sha256: bytes        # 32 bytes
    size: int
    mtime_ms: int


@dataclass
class LogManifest:
    id: str
    label: str | None
    timestamp_ms: int
    source_path: str | None
    servers: dict[str, list[LogFileRecord]] = field(default_factory=dict)


def write_log_manifest(out_path: Path | str, manifest: LogManifest) -> int:
    payload = {
        "id": manifest.id,
        "label": manifest.label,
        "timestamp_ms": manifest.timestamp_ms,
        "source_path": manifest.source_path,
        "servers": {
            server_name: [
                {
                    "path": rec.relative_path,
                    "sha256": rec.sha256.hex(),
                    "size": rec.size,
                    "mtime_ms": rec.mtime_ms,
                }
                for rec in files
            ]
            for server_name, files in manifest.servers.items()
        },
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    Path(out_path).write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def read_log_manifest(path: Path | str) -> LogManifest:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        raise LogManifestError(f"could not read log manifest {path}: {e}") from e

    try:
        servers: dict[str, list[LogFileRecord]] = {}
        for server_name, files in payload.get("servers", {}).items():
            recs = []
            for f in files:
                sha = bytes.fromhex(f["sha256"])
                if len(sha) != 32:
                    raise LogManifestError(
                        f"sha256 not 32 bytes: {f['sha256']!r}"
                    )
                recs.append(LogFileRecord(
                    relative_path=f["path"],
                    sha256=sha,
                    size=int(f["size"]),
                    mtime_ms=int(f.get("mtime_ms", 0)),
                ))
            servers[server_name] = recs
        return LogManifest(
            id=payload["id"],
            label=payload.get("label"),
            timestamp_ms=int(payload["timestamp_ms"]),
            source_path=payload.get("source_path"),
            servers=servers,
        )
    except (KeyError, ValueError, TypeError) as e:
        raise LogManifestError(f"malformed log manifest: {e}") from e
