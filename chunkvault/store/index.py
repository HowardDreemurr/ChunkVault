"""SQLite index of snapshots and stored hashes.

Two roles:

1. **Snapshot registry** — one row per snapshot with metadata (label,
   timestamp, world, MC version, manifest path on disk, and per-snapshot
   counters). This is the lookup table the CLI / library uses for
   ``list``, ``get(label)``, etc.

2. **Hash presence cache** — for each chunk hash and file sha already
   stored, a row in ``chunks`` / ``files``. Querying SQLite for "is this
   hash present?" is much faster than ``Path.is_file`` for the millions
   of chunks we'll see, and is *required* for the snapshot fast path
   (decide "store new chunk?" without a filesystem stat per chunk).

Both tables are write-ahead-log mode for safety with potential concurrent
readers (a long restore while a new snapshot is being made, say).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id              TEXT PRIMARY KEY,
    label           TEXT,
    world_name      TEXT NOT NULL,
    timestamp_ms    INTEGER NOT NULL,
    manifest_path   TEXT NOT NULL,
    mc_version      TEXT,
    data_version    INTEGER,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    region_count    INTEGER NOT NULL DEFAULT 0,
    file_count      INTEGER NOT NULL DEFAULT 0,
    new_chunk_count INTEGER NOT NULL DEFAULT 0,
    new_file_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshots_label ON snapshots(label);
CREATE INDEX IF NOT EXISTS idx_snapshots_world ON snapshots(world_name);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts    ON snapshots(timestamp_ms DESC);

CREATE TABLE IF NOT EXISTS chunks (
    content_hash BLOB PRIMARY KEY,
    ref_count    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files (
    content_hash BLOB PRIMARY KEY,
    ref_count    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS log_snapshots (
    id             TEXT PRIMARY KEY,
    label          TEXT,
    timestamp_ms   INTEGER NOT NULL,
    source_path    TEXT,
    manifest_path  TEXT NOT NULL,
    server_count   INTEGER NOT NULL DEFAULT 0,
    file_count     INTEGER NOT NULL DEFAULT 0,
    new_file_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_log_snapshots_label ON log_snapshots(label);
CREATE INDEX IF NOT EXISTS idx_log_snapshots_ts    ON log_snapshots(timestamp_ms DESC);

CREATE TABLE IF NOT EXISTS log_files (
    content_sha BLOB PRIMARY KEY,
    ref_count   INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class SnapshotRow:
    id: str
    label: str | None
    world_name: str
    timestamp_ms: int
    manifest_path: str
    mc_version: str | None
    data_version: int | None
    chunk_count: int
    region_count: int
    file_count: int
    new_chunk_count: int
    new_file_count: int


@dataclass(frozen=True)
class LogSnapshotRow:
    id: str
    label: str | None
    timestamp_ms: int
    source_path: str | None
    manifest_path: str
    server_count: int
    file_count: int
    new_file_count: int


class IndexDB:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_SCHEMA)
        # Migrate older indices that didn't have ref_count yet.
        for table in ("chunks", "files"):
            try:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN ref_count "
                    f"INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # column already exists
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def __enter__(self) -> "IndexDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- snapshot registry --------------------------------------------------

    def add_snapshot(self, row: SnapshotRow) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO snapshots "
                "(id, label, world_name, timestamp_ms, manifest_path, "
                " mc_version, data_version, chunk_count, region_count, "
                " file_count, new_chunk_count, new_file_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (row.id, row.label, row.world_name, row.timestamp_ms,
                 row.manifest_path, row.mc_version, row.data_version,
                 row.chunk_count, row.region_count, row.file_count,
                 row.new_chunk_count, row.new_file_count),
            )

    def remove_snapshot(self, snap_id: str) -> bool:
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM snapshots WHERE id = ?", (snap_id,)
            )
            return cur.rowcount > 0

    def list_snapshots(self) -> list[SnapshotRow]:
        cur = self._conn.execute(
            "SELECT id, label, world_name, timestamp_ms, manifest_path, "
            "mc_version, data_version, chunk_count, region_count, "
            "file_count, new_chunk_count, new_file_count "
            "FROM snapshots ORDER BY timestamp_ms DESC"
        )
        return [SnapshotRow(*row) for row in cur.fetchall()]

    def get_snapshot(self, id_or_label: str) -> SnapshotRow | None:
        # Exact id match first
        cur = self._conn.execute(
            "SELECT id, label, world_name, timestamp_ms, manifest_path, "
            "mc_version, data_version, chunk_count, region_count, "
            "file_count, new_chunk_count, new_file_count "
            "FROM snapshots WHERE id = ?",
            (id_or_label,),
        )
        row = cur.fetchone()
        if row:
            return SnapshotRow(*row)
        # Then label match
        cur = self._conn.execute(
            "SELECT id, label, world_name, timestamp_ms, manifest_path, "
            "mc_version, data_version, chunk_count, region_count, "
            "file_count, new_chunk_count, new_file_count "
            "FROM snapshots WHERE label = ? "
            "ORDER BY timestamp_ms DESC LIMIT 1",
            (id_or_label,),
        )
        row = cur.fetchone()
        if row:
            return SnapshotRow(*row)
        # Finally, prefix match on id
        like = id_or_label + "%"
        cur = self._conn.execute(
            "SELECT id, label, world_name, timestamp_ms, manifest_path, "
            "mc_version, data_version, chunk_count, region_count, "
            "file_count, new_chunk_count, new_file_count "
            "FROM snapshots WHERE id LIKE ? "
            "ORDER BY timestamp_ms DESC LIMIT 1",
            (like,),
        )
        row = cur.fetchone()
        if row:
            return SnapshotRow(*row)
        return None

    def latest_for_world(self, world_name: str) -> SnapshotRow | None:
        cur = self._conn.execute(
            "SELECT id, label, world_name, timestamp_ms, manifest_path, "
            "mc_version, data_version, chunk_count, region_count, "
            "file_count, new_chunk_count, new_file_count "
            "FROM snapshots WHERE world_name = ? "
            "ORDER BY timestamp_ms DESC LIMIT 1",
            (world_name,),
        )
        row = cur.fetchone()
        return SnapshotRow(*row) if row else None

    # ---- hash presence ------------------------------------------------------

    def has_chunk(self, content_hash: bytes) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM chunks WHERE content_hash = ?", (content_hash,)
        )
        return cur.fetchone() is not None

    def has_chunks_bulk(self, hashes: list[bytes]) -> set[bytes]:
        """Return the subset of ``hashes`` already in the chunks table."""
        if not hashes:
            return set()
        # Process in batches to stay under SQLite's parameter limit (999)
        present: set[bytes] = set()
        for i in range(0, len(hashes), 500):
            batch = hashes[i:i + 500]
            placeholders = ",".join("?" * len(batch))
            cur = self._conn.execute(
                f"SELECT content_hash FROM chunks WHERE content_hash IN ({placeholders})",
                batch,
            )
            present.update(row[0] for row in cur.fetchall())
        return present

    def add_chunks(self, hashes: list[bytes]) -> None:
        """Just record presence (ref_count unchanged). Use ``adjust_chunk_refs``
        when the caller is also taking a reference."""
        if not hashes:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO chunks (content_hash) VALUES (?)",
                [(h,) for h in hashes],
            )

    def adjust_chunk_refs(self, hashes: list[bytes], *, delta: int) -> None:
        """Bulk increment (delta>0) or decrement (delta<0) ref counts.

        Inserts missing rows with ref_count=delta when delta>0; for delta<0
        on a missing hash, the operation is a no-op. Used by snapshot()
        (delta=+1 per chunk reference) and delete() (delta=-1).
        """
        if not hashes or delta == 0:
            return
        with self._conn:
            for h in hashes:
                self._conn.execute(
                    "INSERT INTO chunks (content_hash, ref_count) VALUES (?, ?) "
                    "ON CONFLICT(content_hash) DO UPDATE "
                    "SET ref_count = ref_count + ?",
                    (h, max(delta, 0), delta),
                )

    def chunk_ref_count(self, content_hash: bytes) -> int:
        cur = self._conn.execute(
            "SELECT ref_count FROM chunks WHERE content_hash = ?",
            (content_hash,),
        )
        row = cur.fetchone()
        return row[0] if row else 0

    def gc_zero_ref_chunks(self) -> list[bytes]:
        """Return + remove from index every chunk hash with ref_count <= 0."""
        with self._conn:
            cur = self._conn.execute(
                "SELECT content_hash FROM chunks WHERE ref_count <= 0"
            )
            hashes = [row[0] for row in cur.fetchall()]
            self._conn.execute("DELETE FROM chunks WHERE ref_count <= 0")
        return hashes

    def has_file(self, sha: bytes) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM files WHERE content_hash = ?", (sha,)
        )
        return cur.fetchone() is not None

    def add_files(self, shas: list[bytes]) -> None:
        if not shas:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO files (content_hash) VALUES (?)",
                [(s,) for s in shas],
            )

    def adjust_file_refs(self, shas: list[bytes], *, delta: int) -> None:
        if not shas or delta == 0:
            return
        with self._conn:
            for s in shas:
                self._conn.execute(
                    "INSERT INTO files (content_hash, ref_count) VALUES (?, ?) "
                    "ON CONFLICT(content_hash) DO UPDATE "
                    "SET ref_count = ref_count + ?",
                    (s, max(delta, 0), delta),
                )

    def file_ref_count(self, sha: bytes) -> int:
        cur = self._conn.execute(
            "SELECT ref_count FROM files WHERE content_hash = ?", (sha,)
        )
        row = cur.fetchone()
        return row[0] if row else 0

    def gc_zero_ref_files(self) -> list[bytes]:
        with self._conn:
            cur = self._conn.execute(
                "SELECT content_hash FROM files WHERE ref_count <= 0"
            )
            hashes = [row[0] for row in cur.fetchall()]
            self._conn.execute("DELETE FROM files WHERE ref_count <= 0")
        return hashes

    # ---- log snapshots / log files -----------------------------------------

    def add_log_snapshot(self, row: LogSnapshotRow) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO log_snapshots "
                "(id, label, timestamp_ms, source_path, manifest_path, "
                " server_count, file_count, new_file_count) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (row.id, row.label, row.timestamp_ms, row.source_path,
                 row.manifest_path, row.server_count, row.file_count,
                 row.new_file_count),
            )

    def list_log_snapshots(self) -> list[LogSnapshotRow]:
        cur = self._conn.execute(
            "SELECT id, label, timestamp_ms, source_path, manifest_path, "
            "server_count, file_count, new_file_count "
            "FROM log_snapshots ORDER BY timestamp_ms DESC"
        )
        return [LogSnapshotRow(*r) for r in cur.fetchall()]

    def get_log_snapshot(self, id_or_label: str) -> LogSnapshotRow | None:
        for sql, params in [
            ("WHERE id = ?", (id_or_label,)),
            ("WHERE label = ? ORDER BY timestamp_ms DESC LIMIT 1", (id_or_label,)),
            ("WHERE id LIKE ? ORDER BY timestamp_ms DESC LIMIT 1",
             (id_or_label + "%",)),
        ]:
            cur = self._conn.execute(
                "SELECT id, label, timestamp_ms, source_path, manifest_path, "
                "server_count, file_count, new_file_count "
                "FROM log_snapshots " + sql,
                params,
            )
            row = cur.fetchone()
            if row:
                return LogSnapshotRow(*row)
        return None

    def remove_log_snapshot(self, snap_id: str) -> bool:
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM log_snapshots WHERE id = ?", (snap_id,)
            )
            return cur.rowcount > 0

    def has_log(self, sha: bytes) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM log_files WHERE content_sha = ?", (sha,)
        )
        return cur.fetchone() is not None

    def add_logs(self, shas: list[bytes]) -> None:
        if not shas:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO log_files (content_sha) VALUES (?)",
                [(s,) for s in shas],
            )

    def adjust_log_refs(self, shas: list[bytes], *, delta: int) -> None:
        if not shas or delta == 0:
            return
        with self._conn:
            for s in shas:
                self._conn.execute(
                    "INSERT INTO log_files (content_sha, ref_count) VALUES (?, ?) "
                    "ON CONFLICT(content_sha) DO UPDATE "
                    "SET ref_count = ref_count + ?",
                    (s, max(delta, 0), delta),
                )

    def log_ref_count(self, sha: bytes) -> int:
        cur = self._conn.execute(
            "SELECT ref_count FROM log_files WHERE content_sha = ?", (sha,)
        )
        row = cur.fetchone()
        return row[0] if row else 0

    def gc_zero_ref_logs(self) -> list[bytes]:
        with self._conn:
            cur = self._conn.execute(
                "SELECT content_sha FROM log_files WHERE ref_count <= 0"
            )
            shas = [row[0] for row in cur.fetchall()]
            self._conn.execute("DELETE FROM log_files WHERE ref_count <= 0")
        return shas

    def needs_ref_bootstrap(self) -> bool:
        """True if there are snapshots but no nonzero ref counts yet
        (legacy index that predates ref counting)."""
        snap_count = self._conn.execute(
            "SELECT COUNT(*) FROM snapshots"
        ).fetchone()[0]
        if snap_count == 0:
            return False
        nonzero = self._conn.execute(
            "SELECT 1 FROM chunks WHERE ref_count > 0 LIMIT 1"
        ).fetchone()
        if nonzero:
            return False
        nonzero_files = self._conn.execute(
            "SELECT 1 FROM files WHERE ref_count > 0 LIMIT 1"
        ).fetchone()
        return nonzero_files is None

    # ---- diagnostics --------------------------------------------------------

    def counts(self) -> tuple[int, int, int]:
        """Return (snapshots, chunks, files) row counts."""
        snaps = self._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        chunks = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        files = self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        return snaps, chunks, files
