"""Content-addressed pool for log + auxiliary files.

Conceptually parallel to the world ``files/`` pool but kept separate because:

* logs are operationally distinct (you grep them, you don't restore them
  alongside world files);
* keeping them in their own subtree makes ``rm -rf logs/`` a clean nuclear
  option if a user only wants to keep world history;
* the code-paths that handle log ingest never touch the world chunk pool,
  reducing the blast radius of a bug in either system.

Layout: ``logs/XX/YY/<rest-of-sha256-hex>`` — same pattern as the chunks
pool, sized to keep any single directory under ~1k entries even at
multi-million-file scale.
"""
from __future__ import annotations

import os
from pathlib import Path


class LogStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.logs_dir = self.root / "logs"

    def init(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def has_log(self, sha: bytes) -> bool:
        return self._log_path(sha).is_file()

    def store_log(self, sha: bytes, content: bytes) -> bool:
        """Atomic write. Returns True if newly created, False if existed."""
        return self._atomic_store(self._log_path(sha), content)

    def read_log(self, sha: bytes) -> bytes | None:
        path = self._log_path(sha)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def _log_path(self, sha: bytes) -> Path:
        if len(sha) < 2:
            raise ValueError(f"sha too short for path keying: {sha!r}")
        hex_ = sha.hex()
        return self.logs_dir / hex_[:2] / hex_[2:4] / hex_[4:]

    @staticmethod
    def _atomic_store(path: Path, content: bytes) -> bool:
        if path.is_file():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_bytes(content)
        try:
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            return path.is_file()
        return True
