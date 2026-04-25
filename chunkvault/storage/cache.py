"""SQLite cache of per-chunk content hashes, keyed by git blob SHAs.

Why this is correct (cache never goes stale):

    A git blob SHA is the SHA of the blob's bytes — it IS the content id.
    So once we know a chunk's hash from a particular set of blob SHAs,
    that mapping is true forever:

      * Internal chunk → identity = (region_blob_sha, cx, cz)
      * External chunk → identity = (region_blob_sha, cx, cz, mcc_blob_sha)

    Both are content-addressed; nothing can change underneath them. We
    populate on first compute, hit forever.

Schema:

    blob_parse        Once a region blob has been parsed, one row per chunk
                      with the external flag and (for internal chunks) the
                      content hash. ``internal_hash`` is NULL for external
                      chunks — those are looked up in ``external_hash``.

    blob_parsed       Marker: this region blob has been fully enumerated.
                      A row's presence in ``blob_parse`` without the marker
                      means a partial / aborted parse — we ignore it.

    external_hash     Combined ``(region_sha, cx, cz, mcc_sha)`` → hash.
                      Populated lazily when an external chunk is processed.

Storage: ``<repo>/chunkvault-cache.sqlite``. Safely deletable any time — will
rebuild lazily.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blob_parse (
    region_sha    TEXT    NOT NULL,
    cx            INTEGER NOT NULL,
    cz            INTEGER NOT NULL,
    external      INTEGER NOT NULL,
    internal_hash BLOB,                  -- NULL when external=1
    PRIMARY KEY (region_sha, cx, cz)
);
CREATE TABLE IF NOT EXISTS blob_parsed (
    region_sha TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS external_hash (
    region_sha   TEXT NOT NULL,
    cx           INTEGER NOT NULL,
    cz           INTEGER NOT NULL,
    mcc_sha      TEXT NOT NULL,
    content_hash BLOB NOT NULL,
    PRIMARY KEY (region_sha, cx, cz, mcc_sha)
);
"""


@dataclass(frozen=True)
class ChunkRecord:
    cx: int
    cz: int
    external: bool
    internal_hash: bytes | None  # None iff external=True


class ChunkHashCache:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(self.path)
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.commit()
        except sqlite3.DatabaseError:
            # Cache file corrupt — nuke and rebuild. Cache loss is annoying
            # but never a correctness problem, since the source of truth is
            # git itself.
            try:
                self._conn.close()
            except Exception:
                pass
            self.path.unlink(missing_ok=True)
            self._conn = sqlite3.connect(self.path)
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def __enter__(self) -> "ChunkHashCache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- blob enumeration ----------------------------------------------------

    def get_blob_chunks(self, region_sha: str) -> list[ChunkRecord] | None:
        """Return the full chunk list for a region blob, or None on miss.

        ``None`` means we've never seen this blob; an empty list means we
        have, and it had zero chunks (a header-only region file).
        """
        row = self._conn.execute(
            "SELECT 1 FROM blob_parsed WHERE region_sha=?", (region_sha,)
        ).fetchone()
        if row is None:
            return None
        return [
            ChunkRecord(cx=cx, cz=cz, external=bool(ext), internal_hash=h)
            for cx, cz, ext, h in self._conn.execute(
                "SELECT cx, cz, external, internal_hash FROM blob_parse "
                "WHERE region_sha=? ORDER BY cz, cx",
                (region_sha,),
            )
        ]

    def store_blob_chunks(
        self, region_sha: str, chunks: list[ChunkRecord],
    ) -> None:
        """Persist a freshly parsed region blob's full chunk list."""
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO blob_parse "
                "(region_sha, cx, cz, external, internal_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (region_sha, c.cx, c.cz, int(c.external), c.internal_hash)
                    for c in chunks
                ],
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO blob_parsed (region_sha) VALUES (?)",
                (region_sha,),
            )

    # ---- external chunk hashes ----------------------------------------------

    def get_external_hash(
        self, region_sha: str, cx: int, cz: int, mcc_sha: str,
    ) -> bytes | None:
        row = self._conn.execute(
            "SELECT content_hash FROM external_hash "
            "WHERE region_sha=? AND cx=? AND cz=? AND mcc_sha=?",
            (region_sha, cx, cz, mcc_sha),
        ).fetchone()
        return row[0] if row else None

    def store_external_hash(
        self, region_sha: str, cx: int, cz: int, mcc_sha: str, h: bytes,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO external_hash "
                "(region_sha, cx, cz, mcc_sha, content_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (region_sha, cx, cz, mcc_sha, h),
            )

    # ---- diagnostics --------------------------------------------------------

    def size(self) -> tuple[int, int, int]:
        """Return (cached blobs, internal chunk rows, external chunk rows)."""
        blobs = self._conn.execute(
            "SELECT COUNT(*) FROM blob_parsed"
        ).fetchone()[0]
        chunks = self._conn.execute(
            "SELECT COUNT(*) FROM blob_parse"
        ).fetchone()[0]
        externals = self._conn.execute(
            "SELECT COUNT(*) FROM external_hash"
        ).fetchone()[0]
        return blobs, chunks, externals
