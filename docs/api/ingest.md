# `chunkvault.store.ingest`

Multi-server archive ingestion. The CLI's `chunkvault ingest` is a thin
wrapper around `ingest_archive`; the per-server-vault wizard flow uses
`ingest_archive_per_server_vaults`.

```python
from chunkvault.store import ChunkSnapshotRepo
from chunkvault.store.ingest import ingest_archive
```

## What "ingest" means

Given a multi-server archive (a folder, a `.zip`, or a `.tar.gz`):

1. Discover every top-level `<server>/world/` subtree (`EX-Server/world/`,
   `CR-Server/world/`, …).
2. For each server: take a chunk-store snapshot of its world.
3. For all servers combined: capture one log snapshot covering every
   `logs/` and `crash-reports/` directory.
4. Be **idempotent**: re-ingesting the same archive should be a no-op.
   Idempotency hinges on each server's `level.dat` carrying a stable
   `LastPlayed` field (see below).

## `ingest_archive`

```python
def ingest_archive(
    repo: ChunkSnapshotRepo,
    archive: Path | str,
    *,
    timestamp: datetime | None = None,
    progress_cb: ProgressCallback = None,
    skip_logs: bool = False,
    verify_roundtrip: bool = True,
    server_filter: str | None = None,
) -> IngestResult: ...
```

Ingest one archive into one vault. Survives per-server errors: a corrupt
EX-Server doesn't block CR-Server's snapshot.

**Timestamp policy.** Each server's snapshot timestamp is read from its own
`level.dat`'s `LastPlayed` field. This is the only source that's stable
across re-ingests of the same archive. A server whose `level.dat` lacks
`LastPlayed` is **refused** and added to `result.skipped_servers`; the
archive continues with the others.

Pass `timestamp=...` to force a single timestamp across all servers — used
for tests, or to recover archives where `LastPlayed` truly isn't readable
and you have a known-good ts from another source.

**Server filter.** `server_filter="EX-Server"` drops every other server
silently. The per-server-vault wizard flow uses this to ingest one archive
into multiple vaults, one per server.

## `ingest_archive_per_server_vaults`

```python
def ingest_archive_per_server_vaults(
    archive: Path | str,
    vault_resolver: Callable[[str], ChunkSnapshotRepo | None],
    *,
    timestamp: datetime | None = None,
    progress_cb: ProgressCallback = None,
    skip_logs: bool = False,
    verify_roundtrip: bool = True,
) -> dict[str, IngestResult]: ...
```

Ingest each server in `archive` into a different vault. The archive is
extracted **once**, then for each discovered server, `vault_resolver(name)`
returns the target `ChunkSnapshotRepo` (or `None` to skip).

This is N× faster than calling `ingest_archive(..., server_filter=...)` N
times, because it doesn't re-extract for every server.

```python
vaults = {
    "EX-Server": ChunkSnapshotRepo("F:/Vaults/EX-Server"),
    "CR-Server": ChunkSnapshotRepo("F:/Vaults/CR-Server"),
}
def resolver(server_name):
    return vaults.get(server_name)

results = ingest_archive_per_server_vaults(
    "/backups/2024-08-15-23-30.zip", resolver,
)
for server, result in results.items():
    print(server, [s.short_id for s in result.snapshots])
```

## `preview_archive_servers`

```python
def preview_archive_servers(archive: Path | str) -> list[str]: ...
```

Cheaply enumerate the server names in an archive — extracts just to discover
folder structure, doesn't hash anything. Used by the wizard to ask the user
which vaults to assign each server to. Returns `[]` on unreadable archives;
doesn't raise.

## Helpers

### `discover_servers(extracted_root)` &nbsp;→&nbsp; `list[(server_name, world_path)]`

Walk an extracted archive root, find every `world/`-shaped directory, and
return its `(server_name, world_path)` pairs.

### `parse_timestamp_from_name(name)` &nbsp;→&nbsp; `datetime | None`

Try to parse `2024-08-15-23-30-00` or `2024-08-15-23-30` style timestamps
from an archive filename. Returns `None` on no match. **Used only as a
fallback for the log snapshot's display label** — never as an authority for
world snapshot timestamps (level.dat is the only authority).

### `collect_log_files(server_root)` &nbsp;→&nbsp; `list[(rel_path, content)]`

Walk `<server>/logs/` and `<server>/crash-reports/`, return their files as
`(relative_path, bytes)` tuples. `ingest_archive` uses this to assemble
`LogManifest` entries.

## `IngestResult`

```python
@dataclass
class IngestResult:
    archive: Path
    timestamp: datetime
    label: str
    snapshots: list[ChunkSnapshot]
    log_snapshot: LogSnapshot | None
    server_names: list[str]
    skipped_servers: list[tuple[str, str]]            # (name, reason)
    already_ingested: list[tuple[str, str]]           # (name, existing_short_id)
```

`already_ingested` is the idempotency signal: if you re-ingest an archive
that's already been ingested at the same timestamp, you'll get an
`IngestResult` whose `snapshots` is empty and `already_ingested` lists the
existing matches.
