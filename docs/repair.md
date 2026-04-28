# Repair workflows

chunkvault tries hard to leave the vault in a consistent state, but the real
world has Ctrl+C, kill -9, power loss, antivirus mid-write, and historical
bugs. This page covers the four repair operations and when to reach for
each.

## TL;DR decision tree

```
Vault won't open / weird errors          → chunkvault fsck
Snapshots have wrong timestamps          → chunkvault repair-timestamps --apply
entities/poi blobs ballooning the vault  → chunkvault migrate-mca-files --apply
A specific snapshot is wrong-dated       → chunkvault retime <snap> ...
```

All four take an `--apply` or auto-`repair=True`; the default for the
heavier ones is dry-run.

## `fsck` — reconcile on-disk vs index

```bash
chunkvault fsck <vault>             # repairs by default
chunkvault fsck <vault> --dry-run   # report only
```

What it catches and fixes:

| Issue                                           | Cause                                            | Fix                          |
| ---                                             | ---                                              | ---                          |
| Stray `*.tmp.<pid>.<tid>` in pool dirs          | Snapshot interrupted mid-atomic-write            | Delete temp files.           |
| Manifest exists, no index row                   | Crash between manifest write and index commit    | Delete orphan manifest.      |
| Index row, manifest missing                     | Manifest deleted out of band                     | Delete dangling row + decrement refs. |
| Manifest references chunks index says are 0-ref | Refcount desync from interrupted writes          | Reconcile (re-add refs).     |

`fsck` is **safe to interrupt**: it operates one stage at a time, and each
stage is internally atomic. Re-running picks up where it left off.

By default, `repair_timestamps` and `migrate_mca_files_to_chunks` run
`fsck` first to clean up any lingering inconsistencies before doing their
own scan. Disable with `--no-fsck`.

## `repair-timestamps` — fix the historical fallback-to-now bug

**Symptoms.** Snapshots whose timestamp is "right now you ran the ingest"
instead of when the world was actually saved. Often shows up as: bulk
ingest of historical archives produced N snapshots all timestamped within
seconds of each other.

**Root cause.** The legacy `ingest_archive` code parsed timestamps from
filenames (`YYYY-MM-DD-HH-MM-SS.zip`). Filenames with five components
(`YYYY-MM-DD-HH-MM.zip`) didn't match the regex and silently fell back to
`datetime.now()`. Since `level.dat`'s `LastPlayed` was correctly stored in
the manifest header at snapshot time, the **fix data is on disk** — we just
need to walk every manifest, read `last_played_ms`, and retime the snapshot
to that.

```bash
chunkvault repair-timestamps <vault>             # dry-run (DEFAULT)
chunkvault repair-timestamps <vault> --apply     # actually fix
```

**The plan it prints (dry-run):**

```
repair-timestamps F:/Vaults/EX-Server (DRY RUN)

scanning 231 snapshots…
  - 18 snapshots already correctly timestamped
  - 196 snapshots will be retimed to their level.dat LastPlayed
  - 17 snapshots have no usable LastPlayed (will be skipped)
  - 8 duplicate-pair groups detected (same world, multiple wrong-ts entries)
    → 8 winners kept, 12 duplicates will be deleted

3 snapshots' LastPlayed was recovered from level.dat in the file pool
   (manifest didn't carry it — pre-V2 format).

index lag: 4 manifests' header.timestamp_ms differs from their index row;
   will be reconciled during apply.
```

**During apply:**

1. `fsck --repair` (skip with `--no-fsck`).
2. `backup_index()` — `.bak` snapshot of the SQLite index.
3. For each duplicate group: pick the survivor (the one whose timestamp
   matches `last_played_ms`); delete the others.
4. For each survivor: rewrite its manifest with the correct timestamp;
   atomically update the index row. The label is also retitled (`old-label`
   → `old-label-correct-ts`) when timestamps differ.
5. Reconcile any remaining manifest-vs-index timestamp lag.

The whole thing is **safe to interrupt**: each per-snapshot retime is one
SQL transaction. Ctrl-C in the middle leaves some snapshots already
fixed and others not — re-running picks up where it left off.

**Recovery of `LastPlayed` from the file pool.** A few historical V1
manifests don't carry `last_played_ms` at all. For those, `repair-timestamps`
reads the snapshot's `level.dat` *out of the file pool* (it's there as a
content-addressed blob), parses NBT, and uses that. Reported as
`recovered_from_pool` in the report.

## `migrate-mca-files` — entities/poi from file-pool to chunk-pool

**Symptoms.** Vault has snapshots from before chunkvault treated
`entities/*.mca` and `poi/*.mca` as chunk-deduped. Those files sat in the
sha256 file pool, where every `LastUpdate` field flicker produced a fresh
copy. The fix migrates them to the chunk pool: re-parse the .mca bytes
(already in `files/`), hash chunks individually, rewrite the manifest's
dimension blocks.

```bash
chunkvault migrate-mca-files <vault>             # dry-run (DEFAULT)
chunkvault migrate-mca-files <vault> --apply     # actually migrate
```

The migration is per-snapshot atomic (one SQL transaction wraps manifest
rewrite + ref count adjustments). Ctrl-C is safe.

**What changes after migration:**

- Old `entities/r.X.Z.mca` / `poi/r.X.Z.mca` are now stored as chunks under
  dimension keys `entities/`, `poi/`, `DIM-1/entities/`, etc.
- Manifests' `dimensions` dict gains those keys.
- Manifests' `files` list loses the entries for those `.mca` files.
- `gc()` afterwards reclaims a lot of the file-pool blobs (every chunked
  region's old whole-file blob becomes 0-ref).

## `retime` — fix one snapshot at a time

When `repair-timestamps` doesn't cover the case (e.g., a snapshot you
explicitly want to put at a different timeline position):

```bash
chunkvault retime <vault> <snap> --timestamp 2024-03-15T10:30:00
chunkvault retime <vault> <snap> --from-level-dat
chunkvault retime <vault> --all  --from-level-dat --dry-run
```

`--from-level-dat` re-reads the manifest's stored `last_played_ms` and
retimes to that. Useful after fixing a snapshot's `level.dat` retroactively
(by re-snapshotting and importing) but the original snapshot's row still
points at the wrong time.

The first retime captures the previous timestamp into
`manifest.original_timestamp_ms` so audits always answer "where was this
snapshot born?", regardless of subsequent retimes.

## Operational order

When in doubt, run them in this order:

```bash
chunkvault fsck                 <vault>                # cleanup first
chunkvault verify               <vault>                # confirm pool integrity
chunkvault repair-timestamps    <vault>                # dry-run
chunkvault repair-timestamps    <vault> --apply        # actually fix
chunkvault migrate-mca-files    <vault>                # dry-run
chunkvault migrate-mca-files    <vault> --apply        # if mca files in file pool
chunkvault gc                   <vault>                # reclaim now-orphan blobs
chunkvault verify               <vault>                # final sanity check
```

A vault that survives `verify` after this sequence is in known-good shape.
