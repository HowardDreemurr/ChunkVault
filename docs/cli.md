# CLI reference

`chunkvault` is the single entry point. With no subcommand it drops into the
interactive [wizard](wizard.md). With a subcommand it does that one thing and
exits.

```text
chunkvault [--help] <subcommand> [<args> ...]
```

Subcommands group into **vault lifecycle**, **snapshots**, **multi-server
ingest**, **inspection & diff**, **verify & repair**, **registry**, **logs**,
**visualization**, and **misc**. Below is the full reference.

---

## Vault lifecycle

### `init`

Initialize a new chunk-store vault.

```bash
chunkvault init <vault>
```

Creates `index.sqlite`, the empty pool directories, and `manifests/`. Idempotent.

**Options**

- `--store {chunk,git}` &nbsp;— default `chunk`. The `git` store is the legacy
  FastBack-style backend (whole-region dedup); avoid it for serious use.

---

## Snapshots

### `snapshot`

Take one snapshot of a single world directory.

```bash
chunkvault snapshot <vault> <world> [--label NAME] [--no-verify]
                                    [--allow-live] [--timestamp ISO]
                                    [--parallelism N]
```

The world must contain `level.dat` or a `region/` subdirectory. Refuses by
default if MC's `session.lock` is held (the world is live) — pass
`--allow-live` to force.

**Options**

- `--label, -l` &nbsp;— optional friendly name for the snapshot.
- `--no-verify` &nbsp;— skip the post-snapshot round-trip verification
  (default ON; doubles snapshot time).
- `--allow-live` &nbsp;— skip the `session.lock` check (may snapshot a torn
  world).
- `--timestamp ISO` &nbsp;— override snapshot timestamp. Default is to read
  `level.dat`'s `LastPlayed`, fall back to newest region mtime, then to the
  current wall clock.
- `--parallelism N` &nbsp;— region-hash + tile-render thread count. Default:
  `min(cpu_count, 8)`. Pass `1` for serial.

### `list`

List snapshots in a vault, newest first.

```bash
chunkvault list <vault>
```

### `restore`

Restore a snapshot (or just a subset) to a destination directory.

```bash
chunkvault restore <vault> <snap> <dest> [--path P ...]
```

`<snap>` is a SHA, short SHA, or label. `--path` is repeatable; without it,
the whole snapshot is restored.

### `delete`

Delete a snapshot. Decrements ref counts; reclaim disk with `gc`.

```bash
chunkvault delete <vault> <snap>
```

---

## Ingest from archive

### `ingest`

Discover, snapshot, and log-capture every server inside a multi-server
archive.

```bash
chunkvault ingest <vault> <archive> [--skip-logs] [--no-verify]
                                     [--force-timestamp ISO]
                                     [--server-filter NAME]
```

Layouts supported: a folder, a `.zip`, a `.tar.gz`. The archive may contain
multiple `<server>/world/` subtrees (for example `EX-Server/world/`,
`CR-Server/world/`); each becomes one snapshot, plus one log snapshot
covering all servers' `logs/` and `crash-reports/`.

**Options**

- `--skip-logs` &nbsp;— only snapshot worlds, skip log capture.
- `--no-verify` &nbsp;— skip post-snapshot round-trip verification (speeds up
  bulk ingest).
- `--force-timestamp ISO` &nbsp;— override the snapshot timestamp for **all**
  servers in this archive. Use only when the archive's `level.dat` lacks a
  `LastPlayed` field and you have a known-good timestamp from another source.
- `--server-filter NAME` &nbsp;— only ingest the named server folder. The
  per-server-vault wizard uses this to ingest one archive into multiple
  vaults.

!!! warning "level.dat LastPlayed is mandatory"
    chunkvault refuses to ingest a server whose `level.dat` has no readable
    `LastPlayed`, **unless** you pass `--force-timestamp`. The historical
    fallback to "now" produced silently wrong-timestamped snapshots; the
    refusal is deliberate. See [Repair workflows](repair.md) if you suspect
    a vault was poisoned by the old behavior.

### `import`

Import a single world from a directory or single-world archive (no
multi-server discovery, no log capture).

```bash
chunkvault import <vault> <archive> [--label NAME] [--world-subpath P]
```

---

## Inspection & diff

### `diff`

Compare two **directories on disk** chunk-by-chunk (no vault involved).

```bash
chunkvault diff <old> <new> [--json FILE] [--png DIM FILE] [--html FILE]
                            [--scale 8] [--base-tiles URL]
                            [--base-attribution TEXT]
```

### `render`

Render a per-dimension PNG heatmap from a directory pair.

```bash
chunkvault render <old> <new> <dimension> --output FILE [--scale N]
```

### `diff-snaps`

Chunk-level diff between two **snapshots in a vault** — pure manifest reads,
no chunk store I/O.

```bash
chunkvault diff-snaps <vault> <snap-a> <snap-b>
                      [--json FILE] [--png DIM FILE] [--html FILE]
                      [--scale 8] [--fast]
                      [--base-tiles URL] [--base-attribution TEXT]
```

`--fast` is a `git`-backend-only optimization. The chunk backend is already
fast.

### `verify-roundtrip`

Restore a snapshot to a temp directory and byte-compare against an original
world tree.

```bash
chunkvault verify-roundtrip <vault> <snap> <original>
```

### `verify-folders`

Compare two directory trees and report mismatched / missing / extra files.
Chunk-level diff on `.mca` regions, byte-level on the rest.

```bash
chunkvault verify-folders <left> <right> [--exclude GLOB ...] [--report FILE]
```

`--exclude` is repeatable; matching paths on the LEFT are treated as
"expected absence" if missing on the right (e.g., transient lock files).

---

## Verify & repair

### `verify`

Walk every blob on disk, recompute its hash, compare to its filename.

```bash
chunkvault verify <vault> [--repair] [--parallelism N]
```

- `--repair` &nbsp;— delete corrupt blobs (they'll be regenerated on the next
  snapshot that needs them; if no snapshot covers them, they were orphans).
- `--parallelism N` &nbsp;— rehash thread count. Default auto.

### `fsck`

Reconcile on-disk state with the index. Fixes half-written snapshots left by
Ctrl-C, kill-9, or power loss.

```bash
chunkvault fsck <vault> [--dry-run] [--verbose]
```

What it catches:

- Orphan manifests (manifest exists, no index row → snapshot was interrupted
  before commit).
- Dangling rows (index row points at a missing manifest → manifest deleted
  out of band).
- Stray `*.tmp.<pid>.<tid>` files (interrupted atomic writes).
- Index ref-count vs manifest reference mismatches.

### `gc`

Garbage-collect zero-reference chunks, files, and log blobs.

```bash
chunkvault gc <vault> [--aggressive]
```

`--aggressive` is a `git`-backend-only repack flag. The chunk backend uses an
exact ref-count fast path; there's nothing more aggressive to do.

### `repair-timestamps`

Vault-wide: align every snapshot's `timestamp` and `label` with its
manifest's `last_played_ms` (level.dat's authoritative time). Detects and
dedupes duplicate snapshots created by the historical fallback-to-now ingest
bug.

```bash
chunkvault repair-timestamps <vault> [--apply] [--no-fsck]
```

Default is **dry-run**. Pass `--apply` to actually delete duplicates and
retime survivors. See [Repair workflows](repair.md) for the full procedure.

### `migrate-mca-files`

Move `entities/*.mca` and `poi/*.mca` from whole-file dedup to chunk-level
dedup, rewriting existing manifests using bytes already in the file pool.

```bash
chunkvault migrate-mca-files <vault> [--apply] [--no-fsck]
```

Default is dry-run. One-shot migration for vaults predating the
chunk-deduped entities/poi treatment.

### `retime`

Reassign one snapshot's timestamp.

```bash
chunkvault retime <vault> <snap> [--timestamp ISO] [--from-level-dat]
                                  [--all] [--dry-run]
```

- `--timestamp` &nbsp;— set explicitly.
- `--from-level-dat` &nbsp;— re-derive from manifest's stored `LastPlayed`.
- `--all` &nbsp;— apply `--from-level-dat` to every snapshot. Only valid with
  `--from-level-dat`.

---

## Registry

The wizard finds vaults and source archives by consulting a user-config
registry at `~/.chunkvault/config.json`. These commands manage that registry.

### `repo {add,list,remove}`

```bash
chunkvault repo list
chunkvault repo add <path> [--label NAME]
chunkvault repo remove <path>
```

### `source {add,list,remove}`

```bash
chunkvault source list
chunkvault source add <path> [--label NAME]
chunkvault source remove <path>
```

---

## Logs

Logs and crash reports live in a separate, deduplicated pool — log snapshots
are independent of world snapshots. They share the same vault.

### `logs-list`

```bash
chunkvault logs-list <vault>
```

### `logs-extract`

Materialize a log snapshot into a flat directory.

```bash
chunkvault logs-extract <vault> <snap> <dest> [--server NAME]
```

### `logs-delete`

```bash
chunkvault logs-delete <vault> <snap>
```

---

## Visualization

### `thumbnail`

Render thumbnail tiles + per-dim PNG sidecars for one or all snapshots. Use
`--all` to backfill snapshots ingested before the renderer existed.

```bash
chunkvault thumbnail <vault> [<snap>] [--all]
```

### `browse`

Start a local HTTP server with a Leaflet frontend for browsing snapshots
visually. Localhost-only by default.

```bash
chunkvault browse <vault> [--host 127.0.0.1] [--port 8765]
```

### `render-tiles`

Render world base tiles via [`unmined`](https://github.com/nbjornson/unmined)
(must be on PATH).

```bash
chunkvault render-tiles <world> <output> [--dimension D]
                                          [--zoom-min N] [--zoom-max N]
```

---

## Wizard

### `wizard`

Launch the interactive wizard explicitly. Same as `chunkvault` with no args.

```bash
chunkvault wizard
```

See [Wizard](wizard.md) for the menu structure and locale switching.

---

## Common patterns

### One-server-per-vault layout

```bash
chunkvault init      F:/Vaults/EX-Server
chunkvault repo add  F:/Vaults/EX-Server  --label EX-Server

chunkvault init      F:/Vaults/CR-Server
chunkvault repo add  F:/Vaults/CR-Server  --label CR-Server

# Ingest one archive into both vaults, filtered:
chunkvault ingest F:/Vaults/EX-Server backups/2024-08-15-23-30.zip --server-filter EX-Server
chunkvault ingest F:/Vaults/CR-Server backups/2024-08-15-23-30.zip --server-filter CR-Server
```

### Bulk ingest historical archives

```bash
for f in /backups/*.zip; do
    chunkvault ingest F:/Vaults/EX-Server "$f" --server-filter EX-Server --no-verify
done
chunkvault verify F:/Vaults/EX-Server   # rehash everything once at the end
```

### Disaster recovery sanity check

```bash
chunkvault fsck   F:/Vaults/EX-Server
chunkvault verify F:/Vaults/EX-Server
chunkvault verify-roundtrip F:/Vaults/EX-Server <latest-snap> /path/to/original/world
```

### Exit codes

- `0` &nbsp;— success.
- `1` &nbsp;— usage error or operation failure (verification mismatch, fsck
  found uncorrectable issues, etc.).
- `130` &nbsp;— Ctrl-C. The vault is left in a state `fsck` can recover.
