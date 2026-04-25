"""Content-addressed pool for rendered chunk tiles.

Parallel to :class:`~chunkvault.store.chunk_store.ChunkStore` but stores
the small RGB previews produced by :mod:`chunkvault.viz.render`. Same
sharded layout (``XX/YY/<hash>-<mode>.tile``) so the directory tree
stays balanced even with millions of tiles.

Why a separate pool from chunks/:

* Different lifecycle: a chunk blob is what the snapshot points at and
  must persist as long as any snapshot references it; a tile is a
  *render* of that chunk and could in principle be regenerated from
  the chunk blob. Keeping them separate means a future "re-render
  with better color table" can drop the entire tile pool without
  touching chunk data.
* Different content: chunk blobs are per-version compressed NBT;
  tiles are uniform 768-byte raw RGB. Mixing them would muddle
  filesystem caching.

A tile filename embeds its mode (``topdown`` / ``nether_low`` /
``nether_high``) so a single chunk can have multiple cached renders
without path collisions. The (content_hash, mode) → tile relationship
is mirrored in the SQLite index for ref counting + fast presence checks.
"""
from __future__ import annotations

import os
from pathlib import Path

# 768 = 16 * 16 * 3 — the size of every tile we ever store. Hard-checked
# on writes to surface accidental format drift early.
TILE_BYTES = 768


class TileStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.tiles_dir = self.root / "tiles"

    def init(self) -> None:
        self.tiles_dir.mkdir(parents=True, exist_ok=True)

    def has(self, content_hash: bytes, mode: str) -> bool:
        return self._tile_path(content_hash, mode).is_file()

    def store(self, content_hash: bytes, mode: str, rgb_bytes: bytes) -> bool:
        """Store ``rgb_bytes`` under (content_hash, mode).

        Returns True if newly written, False if a tile at that key already
        existed (idempotent re-render is harmless).
        """
        if len(rgb_bytes) != TILE_BYTES:
            raise ValueError(
                f"tile must be exactly {TILE_BYTES} bytes (16x16 RGB), "
                f"got {len(rgb_bytes)}"
            )
        return self._atomic_store(
            self._tile_path(content_hash, mode), rgb_bytes,
        )

    def read(self, content_hash: bytes, mode: str) -> bytes | None:
        try:
            return self._tile_path(content_hash, mode).read_bytes()
        except FileNotFoundError:
            return None

    def delete(self, content_hash: bytes, mode: str) -> bool:
        try:
            self._tile_path(content_hash, mode).unlink()
            return True
        except FileNotFoundError:
            return False

    # ---- internals ---------------------------------------------------------

    def _tile_path(self, h: bytes, mode: str) -> Path:
        if len(h) < 2:
            raise ValueError(f"hash too short for path keying: {h!r}")
        if not mode or "/" in mode or "\\" in mode or ".." in mode:
            raise ValueError(f"unsafe tile mode name: {mode!r}")
        hex_ = h.hex()
        return self.tiles_dir / hex_[:2] / hex_[2:4] / f"{hex_[4:]}-{mode}.tile"

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
