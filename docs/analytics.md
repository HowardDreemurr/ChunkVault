# Snapshot analytics — read save data without restoring

This guide is for users who want to write custom Python on top of
chunkvault: stats, dashboards, ad-hoc queries across many historical
snapshots. The point: chunkvault already has every `level.dat`,
`playerdata/*.dat`, `data/scoreboard.dat`, datapack zip, etc. stored
as plain bytes in its content-addressed file pool. You can read those
bytes directly — no need to extract anything to disk.

The chunk pool is content-addressed, so cross-snapshot reads of the
same underlying file are essentially free: 100 snapshots that all
contain the same `playerdata/abc-uuid.dat` share one blob in
`files/`. Iterating "every player's position across every snapshot
in 2024" is bound by manifest-walk cost, not by physical I/O.

This API surface lives on `ChunkSnapshotRepo`:

```python
from chunkvault.store import ChunkSnapshotRepo

repo = ChunkSnapshotRepo(r"F:\Vaults\EX-Server")
```

---

## Three primitives

### `list_snapshot_files(snapshot, *, prefix=None, suffix=None) -> list[(path, sha256)]`

Catalog only. Reads the snapshot's manifest once, returns a list of
`(relative_path, sha256_bytes)` tuples for every non-region file
matching the filters. No blob I/O.

Use it when you want to count, list, or pre-plan reads.

```python
snap = repo.get("EX-Server-2024-01-15-12-00-00")

# How many players?
players = repo.list_snapshot_files(snap, prefix="playerdata/", suffix=".dat")
print(f"{len(players)} player files")

# Which datapacks were active?
for path, _sha in repo.list_snapshot_files(snap, prefix="datapacks/"):
    print(path)
```

### `iter_snapshot_files(snapshot, *, prefix=None, suffix=None) -> Iterator[(path, bytes)]`

Streaming. For each matching file, yields `(relative_path,
content_bytes)`. Loads one file at a time — safe for thousands of
files.

`content_bytes` is the **raw on-disk bytes as Minecraft wrote them**.
Most NBT files (`level.dat`, `playerdata/*.dat`, `data/*.dat`) are
gzip-compressed; you need to `gzip.decompress(data)` before parsing
NBT.

```python
import gzip

for path, data in repo.iter_snapshot_files(snap, prefix="playerdata/"):
    nbt_bytes = gzip.decompress(data)
    # parse NBT...
```

### `read_snapshot_file(snapshot, relative_path) -> bytes | None`

Random access. Returns the file's raw bytes by exact relative path,
or `None` if no file with that path exists in the snapshot.

```python
level_dat = repo.read_snapshot_file(snap, "level.dat")
if level_dat is None:
    raise RuntimeError("snapshot has no level.dat (shouldn't happen)")
nbt = gzip.decompress(level_dat)
```

All three methods accept either a `ChunkSnapshot` object, its
short_id, or its label.

---

## Worked example: count players + list their positions

This walks every snapshot in a vault, decompresses each player's NBT,
extracts the `Pos` tag, and prints `(label, player_uuid, x, y, z)`.

```python
import gzip
from chunkvault.store import ChunkSnapshotRepo
from chunkvault.mca.nbt_lite import find_data_version  # generic NBT helpers

# A small NBT walker — chunkvault.mca.nbt_lite has helpers but if you
# want richer parsing, install nbtlib (`pip install nbtlib`) and use
# nbtlib.File.parse() on the decompressed bytes.
import struct

def _read_nbt_compound_find(data: bytes, target_name: str) -> bytes | None:
    """Tiny NBT walker that returns the payload bytes for a top-level
    Data.<target_name> field. Real code should use nbtlib."""
    # ... omitted; use nbtlib for real work
    pass


repo = ChunkSnapshotRepo(r"F:\Vaults\EX-Server")
for snap in repo.list():
    label = snap.label or snap.short_id
    for path, data in repo.iter_snapshot_files(snap, prefix="playerdata/"):
        nbt = gzip.decompress(data)
        # Use nbtlib (or any NBT lib) to parse `nbt` and read the Pos
        # TAG_List of TAG_Double. Pseudocode:
        #
        #   import nbtlib, io
        #   tree = nbtlib.File.parse(io.BytesIO(nbt))
        #   pos = tree.root["Pos"]    # [x, y, z] doubles
        #   uuid = path.removeprefix("playerdata/").removesuffix(".dat")
        #   print(f"{label}  {uuid}  {pos[0]:.1f},{pos[1]:.1f},{pos[2]:.1f}")
```

---

## Cross-snapshot analytics

The killer move: walk many snapshots without ever extracting to disk.

```python
import gzip
from collections import defaultdict
from chunkvault.store import ChunkSnapshotRepo

repo = ChunkSnapshotRepo(r"F:\Vaults\EX-Server")

# Track each player's position across the timeline
trajectories: dict[str, list[tuple[str, bytes]]] = defaultdict(list)

for snap in sorted(repo.list(), key=lambda s: s.timestamp):
    for path, data in repo.iter_snapshot_files(snap, prefix="playerdata/"):
        uuid = path.removeprefix("playerdata/").removesuffix(".dat")
        trajectories[uuid].append((snap.timestamp.isoformat(), data))

# trajectories[uuid] is a chronological list of NBT snapshots for one
# player. Decompress + parse each to plot their journey, count online
# time, find when they got an achievement, etc.
```

Because of content-addressed dedup, a player who didn't log in
between two snapshots produces the **same sha256** in both — the
underlying blob is read once from the pool either way, but if you're
iterating two consecutive identical snapshots, you'll get the
identical `data: bytes` object on both yields. (You can short-circuit
on equal sha256s by using `list_snapshot_files` first, comparing
hashes, and only `read_snapshot_file`-ing when content actually
changed.)

```python
# Smarter: skip identical state across consecutive snapshots
seen_sha: dict[str, bytes] = {}    # uuid → last seen sha256

for snap in sorted(repo.list(), key=lambda s: s.timestamp):
    for path, sha in repo.list_snapshot_files(snap, prefix="playerdata/"):
        uuid = path.removeprefix("playerdata/").removesuffix(".dat")
        if seen_sha.get(uuid) == sha:
            continue                # nothing changed — skip the read
        seen_sha[uuid] = sha
        data = repo.read_snapshot_file(snap, path)
        # process...
```

---

## Reading chunk (block) data

Region chunks are NOT in `manifest.files` — they're indexed
chunk-by-chunk in `manifest.dimensions`. Each `ChunkRecord` has a
`content_hash` you can dereference via `repo.chunks.read_chunk(h)`.

```python
from chunkvault.store.manifest import read_manifest
from chunkvault.mca.region import EXTERNAL_FLAG
from chunkvault.mca.nbt_lite import decompress_chunk_payload

manifest = read_manifest(snap.manifest_path)

for region in manifest.dimensions["region"]:    # overworld
    for chunk in region.chunks:
        blob = repo.chunks.read_chunk(chunk.content_hash)
        # blob[0] is the masked-compression byte; blob[1:] is the
        # zlib/gzip-compressed NBT payload of the chunk.
        nbt = decompress_chunk_payload(blob[0] & ~EXTERNAL_FLAG, blob[1:])
        # `nbt` is the decompressed chunk NBT bytes.
        # Use nbtlib or chunkvault.mca.semantic helpers to inspect
        # blocks, biomes, structures, entities, etc.
```

For dimension keys, see what's in the manifest:

```python
print(list(manifest.dimensions.keys()))
# e.g. ['region', 'entities', 'poi', 'DIM-1/region', 'DIM1/region']
```

`region/` is the overworld blocks. `entities/` is mob & item entities
(1.17+). `poi/` is points of interest (1.14+). `DIM-1/region` is the
nether's blocks. `DIM1/region` is the end's blocks. Per-dimension
`entities/` and `poi/` follow the same pattern.

---

## NBT parsing

chunkvault ships a minimal NBT reader at
`chunkvault.mca.nbt_lite` for the specific fields it needs internally
(`LastPlayed`, MC version, DataVersion). For broader analytics work,
use [`nbtlib`](https://pypi.org/project/nbtlib/):

```bash
pip install nbtlib
```

```python
import io, gzip, nbtlib

raw = repo.read_snapshot_file(snap, "level.dat")
tree = nbtlib.File.parse(io.BytesIO(gzip.decompress(raw)))
print(tree.root["Data"]["LevelName"])
print(tree.root["Data"]["Time"])     # in-game ticks
```

For chunk NBT (already decompressed by
`decompress_chunk_payload`):

```python
import io, nbtlib
nbt = decompress_chunk_payload(blob[0] & ~EXTERNAL_FLAG, blob[1:])
tree = nbtlib.parse_nbt(io.BytesIO(nbt))
# tree["sections"], tree["Heightmaps"], tree["block_entities"], ...
```

---

## What about logs?

Log snapshots (`logs/*.log`, `crash-reports/*.txt`) live in a
**parallel pool**, not the file pool the methods above iterate over.
Use the dedicated log API:

```python
log_snap = repo.list_log_snapshots()[-1]      # most recent
# Bulk extract to a directory:
repo.extract_logs(log_snap, "/tmp/logs", server="EX-Server")

# Or read programmatically:
from chunkvault.store.log_manifest import read_log_manifest
manifest = read_log_manifest(log_snap.manifest_path)
for server_name, files in manifest.servers.items():
    for log_file_record in files:
        bytes_content = repo.logs.read_log(log_file_record.sha256)
        # bytes_content is the raw log file bytes
```

---

## Performance notes

- **Manifest reads are cheap** — the `.mcbk` file is a single
  zlib-compressed binary. Reading it is one disk seek + a fast
  decompress.
- **`list_snapshot_files` is O(M)** where M = number of files in
  manifest. No I/O beyond the manifest itself.
- **`iter_snapshot_files` is O(M) + O(total bytes)** — each yielded
  blob involves one filesystem read of `files/<sha>`.
- **Cross-snapshot read latency**: for a vault with 100 snapshots
  that share 95% of their playerdata content, walking all 100
  reads roughly 5% × 100 × file-size from the pool, not 100% × 100.
  Content-addressing wins here.
- **No locking** — the file pool is read-only and immutable once
  written. Multiple Python processes can iterate the same vault
  concurrently without coordination.
