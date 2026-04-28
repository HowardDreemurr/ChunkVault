# API reference

chunkvault is usable as a library, not just a CLI. The primary entry point is
`ChunkSnapshotRepo`; everything else is supporting types.

```python
from chunkvault.store import ChunkSnapshotRepo

repo = ChunkSnapshotRepo("F:/Vaults/EX-Server")
repo.init()
snap = repo.snapshot("D:/servers/EX-Server/world", label="alpha")
```

## Modules at a glance

| Module                            | What lives here                                       |
| ---                               | ---                                                   |
| [`chunkvault.store`](repo.md)     | `ChunkSnapshotRepo`, `ChunkSnapshot`, `LogSnapshot`, reports. |
| [`chunkvault.store.manifest`](manifest.md) | `Manifest`, `ManifestHeader`, `ChunkRecord`, `FileRecord`, `read_manifest`, `write_manifest`. |
| [`chunkvault.store.ingest`](ingest.md) | `ingest_archive`, `ingest_archive_per_server_vaults`, `preview_archive_servers`. |
| `chunkvault.store.progress`       | `ProgressEvent` and the `ProgressCallback` protocol used by every long-running method. |
| `chunkvault.store.log_manifest`   | `LogManifest`, `LogFileRecord`, `read_log_manifest`. |
| `chunkvault.mca.region`           | Low-level region (`r.X.Z.mca`) packing/unpacking. Reach for it only when implementing custom analyzers. |
| `chunkvault.mca.semantic`         | NBT-aware chunk inspection helpers. |
| `chunkvault.mca.nbt_lite`         | Minimal NBT reader for the fields chunkvault itself needs (LastPlayed, MC version, DataVersion). For broader NBT work install [`nbtlib`](https://pypi.org/project/nbtlib/). |
| `chunkvault.diff.world`           | `WorldDiff`, `ChunkDiff` — the diff result types. |
| `chunkvault.viz.snapshot_render`  | Tile + thumbnail rendering pipeline (`ensure_tiles_for_manifest`). |
| `chunkvault.world.layout`         | World-directory introspection (`find_region_dirs`, dimension key resolution). |
| `chunkvault.wizard`               | Interactive TUI (`flows`, `ui`, `i18n`, `config`). |

## Stable surface

What you can rely on:

- The classes and functions re-exported from `chunkvault.store.__init__`.
- The manifest binary format (read/write functions; the byte format is
  versioned and forward-compatible).
- The progress event kinds (`phase_start` / `phase_progress` / `phase_done`
  / `error` / `finish` and a fixed set of phase names — see [the repo
  reference](repo.md#progress-events)).

What's internal (don't depend on):

- Anything starting with `_`.
- The `chunkvault.storage.*` legacy git-backed package — kept for backward
  compatibility, not actively developed.
- The exact on-disk pool layout (always read/write through `ChunkStore` /
  `LogStore` rather than poking at `chunks/aa/bbcc...` directly).

## Threading & concurrency

- Multiple processes may **read** the same vault concurrently — the chunk
  pool is content-addressed and immutable.
- Only one process may **write** a vault at a time (no multi-writer
  coordination on top of SQLite WAL).
- Within one process, `ChunkSnapshotRepo` is not thread-safe. Each thread
  should construct its own instance.
- Snapshot, verify, and tile-render operations parallelize internally via a
  worker pool. Pass `parallelism=1` for serial; `None` for auto
  (`min(cpu_count, 8)`).
