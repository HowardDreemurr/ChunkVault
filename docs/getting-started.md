# Getting started

This page walks you from a fresh checkout to a working vault, your first
snapshot, and your first restore.

## Install

```bash
pip install -e .                  # from a checkout
# or, when published:
# pip install chunkvault
```

Optional extras:

=== "Docs site"

    ```bash
    pip install -e .[docs]
    mkdocs serve   # http://127.0.0.1:8000
    ```

=== "Minimal runtime only"

    chunkvault has only three hard runtime deps (Pillow, rich, questionary).
    Everything else (map tile rendering via unmined, the git-backed store,
    nbtlib for analytics) is opt-in.

Verify the install:

```bash
chunkvault --help
```

## 30-second quick start

```bash
# 1. Initialize a vault
chunkvault init D:/backup-vault

# 2. Snapshot a live world
chunkvault snapshot D:/backup-vault D:/servers/smp/world --label "before-raid"

# 3. Take another later, then chunk-level-diff them
chunkvault snapshot D:/backup-vault D:/servers/smp/world --label "after-raid"
chunkvault diff-snaps D:/backup-vault before-raid after-raid \
    --html diff.html --png region heat.png

# 4. Restore a single file from any snapshot
chunkvault restore D:/backup-vault before-raid D:/tmp \
    --path region/r.0.0.mca

# 5. Reclaim space after deletes
chunkvault delete D:/backup-vault before-raid
chunkvault gc D:/backup-vault
```

Or skip the typing:

```bash
chunkvault          # drops you into the interactive wizard
```

## The two ways in

### CLI (scriptable)

Every operation is a subcommand. See the [CLI reference](cli.md) for the
exhaustive list.

```bash
chunkvault snapshot <vault> <world> [--label NAME] [--no-verify] [--parallelism N]
chunkvault ingest <vault> <archive.zip>
chunkvault list <vault>
chunkvault verify <vault>
chunkvault fsck <vault>
```

### Wizard (interactive)

Running `chunkvault` with no subcommand launches a rich-powered TUI that
auto-detects nearby vaults and source archives, then guides you through the
common workflows with live progress bars. See [Wizard](wizard.md).

```
chunkvault                        # ← just this
```

## Library use

```python
from chunkvault.store import ChunkSnapshotRepo

repo = ChunkSnapshotRepo("D:/backup-vault")
repo.init()
snap = repo.snapshot("D:/servers/smp/world", label="alpha")

print(snap.short_id, snap.timestamp, snap.mc_version)
for s in repo.list():
    print(s.short_id, s.label, s.timestamp)
```

See the [API reference](api/index.md) for everything `ChunkSnapshotRepo`
exposes.

## What's "a vault"?

A vault is a directory chunkvault owns. It contains:

- `index.sqlite` — the snapshot index (timestamps, labels, ref counts)
- `manifests/<id>.mcbk` — one durable manifest per snapshot
- `chunks/aa/bbcc…` — content-addressed chunk pool
- `files/aa/bbcc…` — content-addressed pool for non-MCA files (level.dat, playerdata, datapacks…)
- `logs/aa/bbcc…` — log pool (parallel to the chunk pool, for `logs/*.log` and `crash-reports/`)
- `tiles/`, `chunk-renders/` — map tiles, lazy-rendered

Don't edit anything inside a vault by hand. Treat it as opaque. See
[Architecture](architecture.md) for the why.

!!! tip "One vault per server"
    chunkvault works either way, but per-server vaults (`F:/Vaults/EX-Server/`,
    `F:/Vaults/CR-Server/`) keep blast-radius small: deletes, restores, and
    repair runs only touch one server's data. The wizard makes per-server
    dispatch the default; the CLI lets you do either.
