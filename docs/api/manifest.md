# `chunkvault.store.manifest`

Manifest is the durable, on-disk source of truth for one snapshot. The SQLite
index is a fast lookup cache rebuildable from manifests; manifests are not.
Everything that mutates a snapshot rewrites the manifest atomically (temp +
`os.replace`).

```python
from chunkvault.store.manifest import read_manifest, write_manifest, Manifest
```

## File format

- File name: `manifests/<32-hex-id>.mcbk`.
- Layout: `MAGIC || version_byte || uncompressed_size:u32be || zlib_body`.
- Body is a versioned binary block. V2 (current) extends V1 with
  `last_played_ms` and `original_timestamp_ms` trailing fields. V1 readers
  reject V2 (V2's extra trailing bytes look like garbage to V1's "no
  trailing data" check); V2 readers transparently handle V1.
- Atomic writes: `write_manifest` writes a sibling `.tmp.<pid>` and
  `os.replace`s it into place. Crash mid-write leaves either the old
  manifest or nothing — never a half-written file.

## Reading a manifest

### `read_manifest(path) -> Manifest`

```python
from chunkvault.store.manifest import read_manifest

m = read_manifest(snap.manifest_path)
print(m.header.timestamp_ms, m.header.last_played_ms, m.header.mc_version)
print(list(m.dimensions))                          # ['region', 'DIM-1/region', ...]
for region in m.dimensions["region"]:
    print(region.rx, region.rz, len(region.chunks))
```

Raises `ManifestError` on bad magic / unsupported version / decode failure.

### `write_manifest(path, manifest) -> int`

Returns the number of bytes written. Most code shouldn't call this directly
— it's used by `repo.snapshot()`, `repo.retime_snapshot()`, and
`repo.repair_timestamps()` after they've assembled a new `Manifest` object.

## Data classes

### `Manifest`

```python
@dataclass
class Manifest:
    header: ManifestHeader
    dimensions: dict[str, list[RegionRecord]]      # dim key → regions
    files: list[FileRecord]                        # non-region files
```

### `ManifestHeader`

```python
@dataclass
class ManifestHeader:
    timestamp_ms: int                  # snapshot timeline position (UTC ms)
    label: str | None
    world_name: str
    mc_version: str = ""
    data_version: int = 0
    last_played_ms: int = 0            # level.dat Data.LastPlayed
    original_timestamp_ms: int = 0     # pre-retime ts; 0 if never retimed
```

### `RegionRecord`

```python
@dataclass
class RegionRecord:
    rx: int
    rz: int
    chunks: list[ChunkRecord]          # ≤1024, indexed by local cx/cz
```

### `ChunkRecord`

```python
@dataclass(frozen=True)
class ChunkRecord:
    cx: int                            # 0..31, local in-region X
    cz: int                            # 0..31, local in-region Z
    compression: int                   # raw on-disk compression byte; 0x80 = external (.mcc)
    timestamp: int                     # MC's per-chunk last-modified (seconds)
    content_hash: bytes                # 16 bytes — key in chunks/ pool

    @property
    def external(self) -> bool:        # true if 0x80 set, payload is in .mcc
```

### `FileRecord`

```python
@dataclass(frozen=True)
class FileRecord:
    relative_path: str                 # posix-style under world root
    sha256: bytes                      # 32 bytes — key in files/ pool
```

### `ManifestError`

Raised on malformed/version-mismatched manifests.

## Common patterns

### Walk every chunk in every snapshot

```python
from chunkvault.store import ChunkSnapshotRepo
from chunkvault.store.manifest import read_manifest

repo = ChunkSnapshotRepo("F:/Vaults/EX-Server")
for snap in repo.list():
    m = read_manifest(snap.manifest_path)
    for dim_key, regions in m.dimensions.items():
        for region in regions:
            for chunk in region.chunks:
                yield (snap.id, dim_key, region.rx, region.rz,
                       chunk.cx, chunk.cz, chunk.content_hash)
```

### Read raw chunk bytes

```python
from chunkvault.mca.region import EXTERNAL_FLAG
from chunkvault.mca.nbt_lite import decompress_chunk_payload

blob = repo.chunks.read_chunk(chunk.content_hash)
# blob[0] = compression byte (with EXTERNAL_FLAG masked off)
# blob[1:] = compressed NBT payload
nbt = decompress_chunk_payload(blob[0] & ~EXTERNAL_FLAG, blob[1:])
# Now `nbt` is the decompressed chunk NBT bytes.
```

### Check if a snapshot pre-dates the v2 manifest format

```python
m = read_manifest(snap.manifest_path)
if m.header.last_played_ms == 0:
    # V1 manifest, or V2 where LastPlayed wasn't readable. Ineligible
    # for retime --from-level-dat without a re-snapshot of the original.
    ...
```

See [Architecture](../architecture.md) for the broader picture of how the
manifest fits with the chunk pool and SQLite index.
