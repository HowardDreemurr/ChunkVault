"""Content-addressed primitive for storing chunk payloads and whole files.

Two parallel pools:

    chunks/   — MC chunk raw payload bytes (zlib-compressed by MC; we store
                them exactly as written to the .mca, no double compression).
                Keyed by ``hash_chunk(...)`` (blake2b-128, 16 bytes).
    files/    — whole non-region files (level.dat, datapack zips, etc).
                Keyed by sha256 of contents.

Both use a 4-hex-char prefix split (``XX/YY/<rest>``) → 65,536 leaf dirs,
which keeps any individual directory below ~1k entries even with 70M+
unique chunks total — comfortably within NTFS / ext4 single-dir limits.

Writes are atomic (temp + rename), so a crash mid-snapshot leaves a
consistent on-disk state: a chunk either exists in full or doesn't exist.
Two snapshots racing on the same hash both write the same content, so the
"loser" of the rename simply overwrites with identical bytes — no harm.
"""
from __future__ import annotations

import os
from pathlib import Path


class ChunkStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.chunks_dir = self.root / "chunks"
        self.files_dir = self.root / "files"

    def init(self) -> None:
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)

    # ---- chunks (raw MC chunk payloads, keyed by 16-byte content hash) -----

    def has_chunk(self, content_hash: bytes) -> bool:
        return self._chunk_path(content_hash).is_file()

    def store_chunk(self, content_hash: bytes, payload: bytes) -> bool:
        """Store ``payload`` under ``content_hash``. Returns True if newly
        written, False if a file at that hash already existed."""
        return self._atomic_store(self._chunk_path(content_hash), payload)

    def read_chunk(self, content_hash: bytes) -> bytes | None:
        path = self._chunk_path(content_hash)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    # ---- whole files (e.g., level.dat) -------------------------------------

    def has_file(self, sha: bytes) -> bool:
        return self._file_path(sha).is_file()

    def store_file(self, sha: bytes, content: bytes) -> bool:
        return self._atomic_store(self._file_path(sha), content)

    def read_file(self, sha: bytes) -> bytes | None:
        path = self._file_path(sha)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    # ---- internals ----------------------------------------------------------

    def _chunk_path(self, h: bytes) -> Path:
        if len(h) < 2:
            raise ValueError(f"hash too short for path keying: {h!r}")
        hex_ = h.hex()
        return self.chunks_dir / hex_[:2] / hex_[2:4] / hex_[4:]

    def _file_path(self, h: bytes) -> Path:
        if len(h) < 2:
            raise ValueError(f"hash too short for path keying: {h!r}")
        hex_ = h.hex()
        return self.files_dir / hex_[:2] / hex_[2:4] / hex_[4:]

    @staticmethod
    def _atomic_store(path: Path, content: bytes) -> bool:
        """Write atomically. Returns True if newly created.

        The tmp filename includes pid + thread id + a 4-byte random suffix.
        ``pid`` alone wasn't enough: two threads in the same process writing
        the same content-hash (deduped chunk content) would collide on
        ``<hash>.tmp.<pid>``, mid-flight overwrites would corrupt the bytes
        that the rename publishes. Adding ``threading.get_ident()`` plus
        random bytes makes per-call tmp names unique across both threads
        and processes, while still being deterministic enough to clean up.
        """
        if path.is_file():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        import threading, os as _os
        suffix = f"{_os.getpid()}.{threading.get_ident()}.{_os.urandom(4).hex()}"
        tmp = path.with_name(f"{path.name}.tmp.{suffix}")
        tmp.write_bytes(content)
        try:
            os.replace(tmp, path)
        except OSError:
            # Replace can fail on Windows if dest opened by another reader,
            # or if a concurrent writer already published the same content.
            # Either way, clean up our tmp and report whether the dest now
            # exists (likely True if a sibling thread/process won the race).
            try:
                tmp.unlink()
            except OSError:
                pass
            return path.is_file()
        return True
