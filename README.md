# chunkvault

> **Chunk-level incremental backup for Minecraft Java worlds.**
> Built for servers with hundreds of historical snapshots that need to fit in a 2 TB SSD.

```
┌─────────────────────────────────────────────────────────────┐
│  100 GB world  ×  100+ snapshots  ─►  ~1 TB on disk         │
│  cross-version (1.7 → latest)     ─►  preserved per-chunk   │
│  multi-server zip ingest          ─►  one command            │
│  chunk-level diff + map viz       ─►  built-in              │
└─────────────────────────────────────────────────────────────┘
```

[![tests](https://img.shields.io/badge/tests-464%20passing-brightgreen)]() [![python](https://img.shields.io/badge/python-3.11%2B-blue)]() [![license](https://img.shields.io/badge/license-Apache%202.0-blue)]()

---

## The problem

Every existing Minecraft backup tool stores **whole region files**. The trouble is, MC rewrites those files constantly — every chunk-load updates `LastUpdate` fields, the zlib output changes, and tools like git, restic, and rsync see "different bytes → store a new copy". After a year of daily backups your repo is bigger than your hard drive.

The actual content — the chunks — barely changes. Players cluster around bases; 95% of the world hasn't been touched in months. **chunkvault stores at chunk granularity**, so unchanged chunks never get a second copy. A 100 GB world with 100 historical snapshots collapses to roughly the size of one snapshot plus the actual deltas.

---

## What's in the box

| | |
|---|---|
| **Chunk-level dedup** | Hash-addressed pool of unique chunks. Same chunk across N snapshots → one copy. |
| **Cross-version aware** | Stores raw payload bytes; works equally on 1.7 and 1.21. Reads `level.dat` to record `mc_version` per snapshot. |
| **Carpet-friendly** | Vanilla MCA format = vanilla parser. Carpet's extra config files go through whole-file dedup. |
| **Multi-server zip ingest** | One `chunkvault ingest backup.zip` discovers `EX-Server/`, `CR-Server/`, etc., snapshots each world, captures logs separately. |
| **Logs handled, not mixed** | Logs/crash-reports go to a parallel deduplicated pool. Browse, extract, or delete independently of world history. |
| **Interactive wizard** | `chunkvault` with no args drops into a rich-powered TUI: detects your repos and source archives, prompts for config, runs with live progress. |
| **Chunk-level diff + maps** | Compare any two snapshots in milliseconds (manifest-only). Render PNG heatmaps or self-contained Leaflet HTML overlaid on optional unmined base tiles. |
| **Verify + gc** | `chunkvault verify` rehashes every blob; `gc` reclaims unreferenced chunks via ref-count fast path (O(deleted), not O(N×M)). |
| **Two backends** | Default chunk store (recommended) plus a git-backed store inspired by FastBack — for users who want git ergonomics over space efficiency. |

---

## Why not [other tool]

|  | git / FastBack | restic / borg | **chunkvault** |
|---|---|---|---|
| Dedup unit | whole region file | content-defined chunks (~1 MB) | **MC chunk** (logical) |
| Survives MC's `LastUpdate` churn | ✗ (false positives) | partial | **✓** |
| 100 GB × 100 snapshots, realistic | 3 – 6 TB | 1 – 2 TB | **0.7 – 1.3 TB** |
| Cross-version metadata | ✗ | ✗ | **✓** (`mc_version` per snapshot) |
| Live progress UI | ✗ | partial | **✓** (rich) |
| Single-file restore | ✓ | ✓ | **✓** |
| Multi-server zip ingest | ✗ | ✗ | **✓** |

---

## Install

```bash
pip install -e .                  # or: pip install chunkvault (when published)

# verifies install + gives you the CLI
chunkvault --help
```

Requires **Python 3.11+**. Pillow is the only hard runtime dependency. `git` is needed only if you opt into the git-backend (`--store=git`). `unmined` is optional, only used if you want real map tiles under your diff overlays.

---

## 30-second quick start

```bash
# 1. Initialize a vault
chunkvault init D:/backup-vault

# 2. Snapshot a live world (or `chunkvault ingest <zip>` for archives)
chunkvault snapshot D:/backup-vault D:/servers/smp/world --label "before-raid"

# 3. Take another later, then diff
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

---

## The wizard

`chunkvault` with no subcommand launches an interactive TUI. It detects what's around, asks you to confirm, then runs with live progress.

```
chunkvault interactive wizard
detecting environment…

┌─ Detected chunkvault repos ──────────────────────────────────────────┐
│ path                  kind    snapshots  log snaps  on-disk          │
│ D:\backup-vault       chunk          47         12  812.4 GB         │
└──────────────────────────────────────────────────────────────────────┘

┌─ Source archive locations ───────────────────────────────────────────┐
│ path                archives  total bytes  samples                   │
│ D:\backups\               87       6.8 TB  2024-08-15-23-30-00.zip…  │
│ E:\old-saves\             12     800.2 GB  2022-12-25-20-00-00.zip…  │
└──────────────────────────────────────────────────────────────────────┘

╭─ Main menu ──────────────────────────────────────────────────╮
│  What would you like to do?                                  │
│    [i]ngest archives  — bulk-import backup zips              │
│    [s]napshot a live world                                   │
│    [d]iff two snapshots                                      │
│    [l]ist snapshots                                          │
│    [v]erify repo integrity                                   │
│    [g]c (reclaim space)                                      │
│    [q]uit                                                    │
╰──────────────────────────────────────────────────────────────╯
Choice [i/s/d/l/v/g/q] (i): i
```

After confirming, you get rich progress bars per archive + per region + per file, with elapsed/ETA and live counters.

---

## CLI reference

### Repo lifecycle
```
chunkvault init REPO                      Initialize a chunk-store vault
chunkvault verify REPO [--repair]         Re-hash every blob; detect bit-rot
chunkvault gc REPO                        Reclaim space from deleted snapshots
```

### World snapshots
```
chunkvault snapshot REPO WORLD [--label L] [--allow-live]
chunkvault list REPO
chunkvault restore REPO SNAP DEST [--path P …]
chunkvault delete REPO SNAP
```

### Multi-server archives
```
chunkvault ingest REPO ARCHIVE [--skip-logs]   Ingest a YYYY-MM-DD-HH-MM-SS.zip
chunkvault import REPO ARCHIVE                  Single-world archive (legacy alias)
```

### Logs (parallel subsystem)
```
chunkvault logs-list REPO
chunkvault logs-extract REPO SNAP DEST [--server NAME]
chunkvault logs-delete REPO SNAP
```

### Diff + visualization
```
chunkvault diff       OLD_DIR NEW_DIR  [--json] [--png DIM FILE] [--html FILE]
chunkvault diff-snaps REPO SNAP_A SNAP_B [--json] [--png DIM FILE] [--html FILE]
chunkvault render     OLD NEW DIM -o FILE       PNG heatmap only
chunkvault render-tiles WORLD OUTPUT             unmined base-tile renderer
```

### Backend switch
Every storage command takes `--store=chunk|git`. **Default is chunk.** Use `--store=git` only if you need git's ergonomics (e.g. `git log` on the snapshot history) and don't mind the larger footprint.

---

## Library API

```python
from chunkvault.store import ChunkSnapshotRepo

repo = ChunkSnapshotRepo("D:/backup-vault")
repo.init()

# Snapshot with live progress
def on_event(e):
    print(f"{e.phase}: {e.current}/{e.total}  {e.label}")

snap = repo.snapshot(
    "D:/servers/smp/world",
    label="before-raid",
    progress_cb=on_event,
)

# Compare two snapshots — manifest-only, milliseconds regardless of world size
diff = repo.diff_snapshots("before-raid", "after-raid")
print(diff.count_by_kind())              # {"added": …, "modified": …, "removed": …}
print(diff.version_changed())            # True iff DataVersion differs

# Render an interactive map
from chunkvault.viz import render_diff_html
render_diff_html(diff, out_path="diff.html",
                 base_tiles_url="./tiles/{z}/{x}/{y}.png")

# Restore a single file
repo.restore(snap, "D:/tmp", paths=["region/r.0.0.mca"])
```

### Multi-server archive in two lines

```python
from chunkvault.store import ChunkSnapshotRepo, ingest_archive

repo = ChunkSnapshotRepo("D:/backup-vault"); repo.init()
result = ingest_archive(repo, "D:/backups/2025-04-25-12-34-56.zip")
# → snapshots EX-Server/world, CR-Server/world; captures all logs;
# all labeled with the parsed timestamp.
```

---

## Architecture

```
chunkvault/
├── mca/                ← MCA region parsing, NBT-lite, content hashing
│   ├── region.py       Region(path) / Region.from_bytes() — read-only parser
│   ├── hasher.py       blake2b-128 content hash (MC-aware)
│   ├── nbt_lite.py     Minimal NBT reader (no amulet dep)
│   └── semantic.py     chunk_data_version, chunk_block_palette
│
├── world/              ← World-layout enumeration (vanilla + datapacks)
├── diff/               ← WorldDiff, ChunkDiff, error-tolerant comparison
│
├── storage/            ← Git-backed backend (FastBack-style, opt-in)
│   ├── repo.py         orphan-branch snapshots, --delta off for *.mca
│   ├── batch.py        cat-file --batch (avoids fork-per-blob overhead)
│   └── cache.py        SQLite hash cache for fast diff
│
├── store/              ← Chunk-store backend (DEFAULT)
│   ├── chunk_store.py  Content-addressed pool: chunks/XX/YY/<hash>
│   ├── log_store.py    Parallel pool for logs/crash-reports
│   ├── manifest.py     Custom binary manifest (zlib, no msgpack dep)
│   ├── log_manifest.py JSON manifests for log snapshots
│   ├── index.py        SQLite: snapshots, ref-counted chunk/file/log presence
│   ├── ingest.py       Multi-server zip → snapshots + log snapshot
│   ├── importer.py     Format detection (zip/tar/dir) + safe extraction
│   ├── progress.py     ProgressEvent protocol for UIs
│   └── repo.py         ChunkSnapshotRepo: snapshot, list, restore, gc, verify, diff
│
├── viz/                ← Visualization layer
│   ├── heatmap.py      PIL → PNG heatmap (added=green, mod=orange, rm=red)
│   ├── leaflet.py      Self-contained interactive HTML map
│   └── tiles.py        unmined CLI wrapper (optional)
│
├── wizard/             ← Interactive TUI (rich-powered)
│   ├── detect.py       Environment auto-detection
│   ├── ui.py           Prompts, tables, progress bars
│   └── flows.py        Per-operation interactive flows
│
└── cli.py              ← argparse subcommands + wizard fallback
```

### On-disk layout of a vault

```
D:\backup-vault\
├── chunks/                    ← unique chunk payloads (XX/YY/<hash>)
│   └── …                      (~1 TB after years of snapshots)
├── files/                     ← non-region whole files (level.dat, datapacks…)
├── logs/                      ← deduplicated log content
├── manifests/                 ← per-snapshot binary manifests (~50 MB each)
│   ├── <uuid>.mcbk
│   └── …
├── log-snapshots/             ← per-archive JSON log manifests
│   └── <uuid>.json
└── index.sqlite               ← snapshot registry + ref counts
```

---

## Why chunk-store works at scale

The thing git, restic, and friends miss: a Minecraft chunk's content is determined by its **decompressed NBT tree**, but every save also rewrites a `LastUpdate` integer. Run two backups a minute apart, the chunk's *bytes* differ, the *content* does not.

chunkvault hashes the chunk's compression byte + payload — i.e. the bytes that hash equivalently across saves of identical content. Identical content → identical hash → identical pool entry. The `LastUpdate` flicker still flips the hash for chunks that were genuinely loaded, but it doesn't re-store the other 99%.

| Scenario | Re-stored? |
|---|---|
| Player runs through a chunk, no blocks change | ✗ |
| Player breaks one block | ✓ (just that chunk) |
| Server restart, no edits | ✗ (entire pool unchanged) |
| Cross-version world conversion | ✓ (chunks rewritten by MC; we keep both) |

For external chunks (`c.X.Z.mcc`, MC's escape hatch for huge chunks), the pool keys by `(region_blob, mcc_blob)` so the .mcc file's bytes participate in the hash.

---

## Performance characteristics

| Operation | 100 GB world, warm cache |
|---|---|
| Cold first snapshot | 5 – 15 min (one-time, I/O-bound) |
| Incremental snapshot (typical session) | seconds – tens of seconds |
| Diff between any two snapshots | **milliseconds** (manifest-only) |
| Verify (rehash everything) | minutes (proportional to repo size) |
| GC after a delete | O(reclaimed blobs), seconds |
| Single-file restore | tens of milliseconds |

346 unit + integration tests covering MCA parsing edge cases, chunk-level dedup correctness, ref-counted gc bootstrap, cross-platform `session.lock` detection, and synthetic multi-server ingest end-to-end. Suite runs in **~20 seconds**.

---

## Test philosophy

> Tests are mandatory for this project. A backup library operating on live save data is a single-point-of-failure for years of play; silent corruption is unrecoverable.

- Every module has a matching test file.
- Adversarial inputs covered: truncated MCA, corrupt sector offsets, mixed-version chunks within one region, external `.mcc` references with missing files, concurrent `session.lock`, race conditions on Windows mtime resolution.
- Round-trip tests on every write path (snapshot → restore → re-snapshot must be a no-op for chunk count).
- Synthetic MCA fixtures keep CI portable; real-world fixtures recommended for major version-range coverage.

---

## A realistic deployment

You have ~8 TB of historical backups spread across two drives, accumulated as `YYYY-MM-DD-HH-MM-SS.zip` archives, each containing `EX-Server/` and `CR-Server/` folders with full server trees inside. Target: fit it all in a 2 TB SSD.

```bash
chunkvault init D:/backup-vault

# One command per archive — auto-detects servers, snapshots worlds,
# captures logs, and dedupes everything against the existing pool.
for archive in D:/backups/*.zip E:/old-saves/*.zip; do
    chunkvault ingest D:/backup-vault "$archive"
done

# Or skip the loop and let the wizard scan both drives for you:
chunkvault          # → menu → ingest → confirm

chunkvault list   D:/backup-vault                # see all snapshots
chunkvault verify D:/backup-vault                # re-hash everything
chunkvault diff-snaps D:/backup-vault \
    EX-Server-2024-08-15-23-30-00 \
    EX-Server-2025-04-25-12-34-56 \
    --html cross-version-diff.html
```

Expected outcome: `~700 GB – 1.3 TB` on disk depending on how active your servers were. Logs occupy a separate `logs/` pool, browsable via `chunkvault logs-list / logs-extract`.

---

## Roadmap

- [x] Chunk-level pool with content-addressed dedup
- [x] Cross-version metadata (`level.dat` parser, MC version per snapshot)
- [x] Multi-server zip ingest
- [x] Log subsystem (parallel pool, JSON manifests, extract)
- [x] Interactive wizard (rich)
- [x] Verify + repair
- [x] Ref-counted gc
- [x] Self-contained Leaflet diff maps
- [x] Optional unmined base tiles
- [ ] amulet-core integration for semantic block-level diffs
- [ ] Live tail-style progress over an HTTP endpoint (web dashboard)
- [ ] Compaction (rewrite manifests after gc to physically reclaim manifest bytes)
- [ ] Tested integration with FastBack mod's git layout (interop)

---

## Prior art

| Tool | What it does | Why we built our own |
|---|---|---|
| [FastBack](https://github.com/pcal43/fastback) | Fabric mod, git-backed | mod, not a library; file-level dedup |
| [BTFU](https://www.curseforge.com/minecraft/mc-mods/btfu-continuous-rsync-incremental-backup) | rsync + hardlinks | file-level only |
| [restic](https://restic.net) / [borgbackup](https://www.borgbackup.org/) | Generic chunked dedup | not MC-aware; LastUpdate churn defeats CDC |
| [MCA Selector](https://github.com/Querz/mcaselector) | Per-chunk visualization | viewer, not backup; no diff |
| [amulet-core](https://github.com/Amulet-Team/Amulet-Core) | MC save read/write | library, not backup |

chunkvault stitches together the parts these projects do well — MCA awareness from amulet-core's lineage, chunk-level dedup inspired by restic's CDC philosophy, FastBack's git-as-pool insight — and adds the missing piece: **a chunk hash that survives MC's metadata churn.**

---

## License

Apache License 2.0. See [`LICENSE`](LICENSE) for the full text and
[`NOTICE`](NOTICE) for the attribution requirement that Section 4(d)
imposes on any redistribution. Commercial use is permitted; the
attribution notice (the `NOTICE` file) must travel with any
redistribution or derivative work.

---

<sub>Made for a server with 100+ historical snapshots that needed to fit in 2 TB. If it fits yours, even better.</sub>
