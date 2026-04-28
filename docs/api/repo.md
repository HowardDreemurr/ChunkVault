# `chunkvault.store` — `ChunkSnapshotRepo`

The repo class. Owns the on-disk vault, exposes every snapshot operation.

```python
from chunkvault.store import ChunkSnapshotRepo

repo = ChunkSnapshotRepo("F:/Vaults/EX-Server")
repo.init()                           # idempotent
snap = repo.snapshot("D:/world", label="alpha")
```

---

## Construction

### `ChunkSnapshotRepo(repo_path)`

```python
ChunkSnapshotRepo(repo_path: Path | str)
```

`repo_path` is the vault root. The constructor itself touches no disk; call
`init()` once to materialize the vault layout.

Attributes worth knowing:

| Attribute        | Type           | What it is                          |
| ---              | ---            | ---                                 |
| `repo_path`      | `Path`         | Resolved absolute vault path.       |
| `chunks`         | `ChunkStore`   | Chunk pool + non-region file pool.  |
| `logs`           | `LogStore`     | Log pool (parallel to chunks).      |
| `tiles`          | `TileStore`    | Lazy-rendered map tiles.            |
| `index_path`     | `Path`         | `index.sqlite` location.            |
| `manifests_dir`  | `Path`         | `manifests/<id>.mcbk` directory.    |

### `is_initialized() -> bool`

True iff `index.sqlite` exists.

### `init() -> None`

Create the vault's directory tree and SQLite index. Idempotent.

---

## Taking snapshots

### `snapshot(world_path, label=None, *, ...) -> ChunkSnapshot`

```python
snapshot(
    world_path: Path | str,
    label: str | None = None,
    *,
    timestamp: datetime | None = None,
    allow_live: bool = False,
    exclude: Iterable[str] | None = None,
    world_name: str | None = None,
    progress_cb: ProgressCallback = None,
    verify_roundtrip: bool = True,
    parallelism: int | None = None,
) -> ChunkSnapshot
```

Snapshot a single world. Refuses if the world is held by `session.lock`
unless `allow_live=True`. Refuses if `world_path` doesn't look like a real MC
world (no `level.dat` and no `region/` subdirectory).

**Timestamp resolution:**
1. Explicit `timestamp=` argument wins.
2. Else `level.dat`'s `LastPlayed` field (preferred — stable across
   re-snapshots of an archived save).
3. Else newest region mtime.
4. Else current wall clock.

**Verify round-trip:** by default, after writing the manifest, the snapshot
is restored to a temp dir and byte-compared against the source. `verify=True`
~doubles snapshot time but catches silent corruption (a chunk pack producing
non-deterministic bytes, an `os.replace` racing with antivirus, …). Pass
`verify_roundtrip=False` for trusted bulk ingest.

**Parallelism:** region hashing + tile rendering run in a thread pool.
`None` → `min(cpu_count, 8)`. `1` → fully serial. Workers don't touch SQLite
— only the main thread writes the index.

### `backfill_region_cache(progress_cb=None) -> BackfillStats`

For older vaults snapshotted before the region cache existed: walk every
manifest, populate the per-region cache rows so future snapshots of the
same region get the fast path. Pure addition; never removes anything.

---

## Listing & lookup

### `list() -> list[ChunkSnapshot]`

All snapshots in the vault, newest first.

### `get(id_or_label) -> ChunkSnapshot | None`

Look up by full SHA, short SHA, or label. Returns `None` if not found.

---

## Restoring

### `restore(snapshot, dest, paths=None, *, progress_cb=None) -> None`

```python
restore(
    snapshot: ChunkSnapshot | str,
    dest: Path | str,
    paths: Iterable[str] | None = None,
    *,
    progress_cb: ProgressCallback = None,
) -> None
```

Re-materialize a snapshot to `dest`. `paths` is an optional whitelist
(posix-style relative paths or `dimkey/r.X.Z.mca` strings). Without it, the
whole snapshot is restored.

The restore is **safe to interrupt**: writes go through `os.replace`, so a
partial restore leaves either the previous file or the new one, never a
torn middle.

Emits two phases — `restore_regions`, then `restore_files` — each with a
total + per-item progress so the caller's bar can show real movement on a
TB-scale world that takes hours.

### `delete(snapshot) -> None`

```python
delete(snapshot: ChunkSnapshot | str) -> None
```

Drop the snapshot row, decrement chunk + file ref counts, delete the
manifest. Doesn't remove blobs from the pool — that's `gc()`'s job.

---

## Diff

### `diff_snapshots(snap_a, snap_b) -> WorldDiff`

```python
diff_snapshots(
    snap_a: ChunkSnapshot | str,
    snap_b: ChunkSnapshot | str,
) -> WorldDiff
```

Pure-manifest diff — compares chunk content hashes only. Cost is two
manifest reads, not two world reads. The diff has per-chunk
`added` / `removed` / `modified` records you can plot, render, or roll up
into stats.

---

## Verify, gc, fsck

### `verify(*, repair=False, progress_cb=None, parallelism=None) -> VerifyReport`

Walk every blob on disk, recompute its hash, compare against its filename.
Also cross-checks that every manifest's referenced chunks/files actually
exist in the pool. With `repair=True`, corrupt blobs are deleted (will be
regenerated on the next snapshot that needs them).

Emits four phases: `verify_chunks`, `verify_files`, `verify_reachability`,
`verify_orphans`.

`parallelism` defaults to auto. Read+hash is mostly I/O + GIL-releasing
crypto, so threads scale linearly with disk bandwidth on Windows.

### `fsck(*, repair=True, progress_cb=None) -> FsckReport`

Reconcile on-disk state with the index. Catches:

- **Orphan manifests** &nbsp;— manifest file with no index row (snapshot
  interrupted before commit).
- **Dangling rows** &nbsp;— index row pointing at a missing manifest.
- **Stray `*.tmp.<pid>.<tid>` files** &nbsp;— interrupted atomic writes.
- **Manifest-vs-index ref-count desync** &nbsp;— refs missing for chunks
  that manifests still reference.

`repair=False` for dry-run. The CLI's `chunkvault fsck --dry-run` maps
directly to this.

### `gc() -> GCResult`

Delete zero-reference blobs from `chunks/`, `files/`, and `logs/`. Uses an
exact ref-count fast path (O(deleted), not O(N×M)). Run it after `delete`
to actually reclaim disk.

---

## Retiming & repair

### `retime_snapshot(snapshot, new_timestamp) -> ChunkSnapshot`

Reassign one snapshot's timestamp. Atomic per snapshot: rewrites the manifest
header, updates the index row in a single SQL transaction. Refuses if the
new `(label, timestamp)` would collide with an existing snapshot. The first
retime captures the original timestamp into `manifest.original_timestamp_ms`
so the change is auditable.

### `retime_snapshot_from_manifest(snapshot) -> tuple[ChunkSnapshot, str]`

Convenience: retime to the manifest's stored `last_played_ms`. Returns
`(snap, source)` where `source` is `"last_played"` on success or
`"no_last_played"` if the manifest doesn't carry one.

### `repair_timestamps(*, dry_run=True, fsck_first=True, progress_cb=None) -> RepairReport`

Vault-wide: align every snapshot's timestamp+label with its manifest's
`last_played_ms`. Detects and dedupes duplicates created by the historical
fallback-to-now ingest bug.

Default is **dry-run**. See [Repair workflows](../repair.md) for the full
procedure and report-reading guide.

### `migrate_mca_files_to_chunks(*, dry_run=True, fsck_first=True, progress_cb=None) -> MigrateMcaReport`

One-shot: rewrite manifests so `entities/*.mca` and `poi/*.mca` use
chunk-level dedup instead of whole-file dedup. Reads bytes from the file
pool — no source archive needed. Default dry-run.

### `backup_index() -> Path`

Snapshot `index.sqlite` to `index.sqlite.bak` before a risky op. Overwrites
any previous `.bak`; we keep one rollback point, not a history (the chunk
pool itself is immutable; only the index + manifests carry mutable state).

---

## Analytics — read-only file access

These three primitives let you read save data **directly from the pool**
without restoring. See the dedicated [Analytics guide](../analytics.md) for
extended examples.

### `list_snapshot_files(snapshot, *, prefix=None, suffix=None) -> list[(path, sha256)]`

Catalog only. Reads the manifest, returns `(rel_path, sha256_bytes)` tuples
for every non-region file. No blob I/O.

```python
players = repo.list_snapshot_files(snap, prefix="playerdata/", suffix=".dat")
print(f"{len(players)} player files")
```

### `iter_snapshot_files(snapshot, *, prefix=None, suffix=None) -> Iterator[(path, bytes)]`

Streaming. Yields one `(path, content_bytes)` at a time. Safe for thousands
of files — bytes are loaded one file at a time, not all up-front.

`content_bytes` is the **raw on-disk bytes as MC wrote them**. NBT files
(`level.dat`, `playerdata/*.dat`, `data/*.dat`) are gzip-compressed; call
`gzip.decompress(data)` before parsing the NBT.

```python
import gzip
for path, data in repo.iter_snapshot_files(snap, prefix="playerdata/"):
    nbt = gzip.decompress(data)
    # ... parse with nbtlib
```

### `read_snapshot_file(snapshot, relative_path) -> bytes | None`

Random access. Returns raw bytes or `None` if no file with that path is in
the snapshot.

```python
raw = repo.read_snapshot_file(snap, "level.dat")
nbt = gzip.decompress(raw)
```

All three accept either a `ChunkSnapshot`, a short SHA, or a label.

---

## Logs

Logs and crash reports live in a parallel pool. Log snapshots are independent
of world snapshots.

### `add_log_snapshot(...) -> LogSnapshot`

Used internally by `ingest_archive`; rarely called directly from user code.

### `list_log_snapshots() -> list[LogSnapshot]`

### `get_log_snapshot(id_or_label) -> LogSnapshot | None`

### `extract_logs(snapshot, dest, *, server=None, progress_cb=None) -> None`

Materialize a log snapshot's files to `dest`. Optional `server` filter.

### `delete_log_snapshot(snapshot) -> None`

---

## Data classes

### `ChunkSnapshot`

| Field            | Type        | What                                            |
| ---              | ---         | ---                                             |
| `id`             | `str`       | 32-hex-char stable id.                          |
| `label`          | `str | None`| Friendly name; unique within `(label, ts)`.     |
| `timestamp`      | `datetime`  | UTC. The snapshot's timeline position.          |
| `world_name`     | `str`       | Source dir name, mostly informational.          |
| `mc_version`     | `str | None`| As read from `level.dat`.                       |
| `data_version`   | `int | None`| As read from `level.dat`.                       |
| `manifest_path`  | `Path`      | Absolute path to `manifests/<id>.mcbk`.         |
| `short_id`       | property    | First 12 chars of `id`.                         |

### `LogSnapshot`

| Field            | Type        | What                                            |
| ---              | ---         | ---                                             |
| `id`             | `str`       |                                                 |
| `label`          | `str | None`|                                                 |
| `timestamp`      | `datetime`  |                                                 |
| `source_path`    | `str | None`| The archive this came from, if known.           |
| `server_count`   | `int`       |                                                 |
| `file_count`     | `int`       |                                                 |
| `manifest_path`  | `Path`      | Absolute path to `log-snapshots/<id>.mcbk`.     |

### Reports

- **`VerifyReport`** &nbsp;— bad chunks/files/logs, missing-from-pool sets,
  orphan blobs.
- **`FsckReport`** &nbsp;— orphan manifests, dangling rows, stray temps,
  manifest/index desync, plus `repaired` flag and `total_issues()`.
- **`GCResult`** &nbsp;— `chunks`, `files`, `logs` removal counts.
- **`RepairReport`** &nbsp;— duplicate groups, retime plan, recovered-from-pool
  count, index lag stats. `summary()` returns a one-line human report.
- **`MigrateMcaReport`** &nbsp;— per-snapshot before/after counts, total
  manifests rewritten.
- **`BackfillStats`** &nbsp;— region cache rows added.

### Errors

- `ChunkRepoError` &nbsp;— base class; raised by every repo method on
  consistency failures.
- `RoundTripVerificationError` &nbsp;— subclass; carries the
  failed-snapshot info + a per-file mismatch report. Caught by callers that
  want to keep going (e.g., bulk ingest).

---

## Progress events

Every long-running method accepts `progress_cb: ProgressCallback`, a callable
taking a single `ProgressEvent`:

```python
@dataclass
class ProgressEvent:
    kind: str            # phase_start | phase_progress | phase_done | error | finish
    phase: str           # snapshot_regions | snapshot_files | restore_regions | ...
    label: str = ""      # human-friendly current item
    current: int = 0
    total: int = 0
    detail: dict | None = None
```

Phase names you'll encounter (the set is closed and stable):

| Phase                    | Where                                            |
| ---                      | ---                                              |
| `snapshot_regions`       | hashing region files during `snapshot()`         |
| `snapshot_files`         | hashing non-region files during `snapshot()`     |
| `snapshot_render`        | rendering tiles during `snapshot()`              |
| `verify_chunks`          | re-hashing chunk pool                            |
| `verify_files`           | re-hashing file pool                             |
| `verify_reachability`    | manifest → pool cross-check                      |
| `verify_orphans`         | pool → manifest cross-check                      |
| `restore_regions`        | restoring `.mca` files                           |
| `restore_files`          | restoring non-region files                       |
| `fsck_stray_tmp`         | stray temp file scan                             |
| `fsck_manifests`         | orphan manifest detection                        |
| `fsck_index_rows`        | dangling row detection                           |
| `fsck_refcounts`         | manifest/index ref desync detection              |
| `repair_scan`            | `repair_timestamps` planning                     |
| `repair_apply`           | `repair_timestamps` execution                    |
| `migrate_scan`           | `migrate_mca_files` planning                     |
| `migrate_apply`          | `migrate_mca_files` execution                    |
| `ingest_archive`         | wraps a whole `ingest_archive` call              |
| `server_world`           | one server's world snapshot inside ingest        |
| `server_logs`            | one server's log capture inside ingest           |

Use `rich.progress.Progress` (the wizard does) or just `print` — chunkvault
doesn't care.
