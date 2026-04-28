# chunkvault

> **Chunk-level incremental backup for Minecraft Java worlds.**
> A Python library and CLI for keeping years of historical snapshots without
> the vault growing linearly with snapshot count.

---

## What it does

chunkvault stores Minecraft saves at **chunk granularity**, not file
granularity. Every existing backup tool — git, restic, rsync, borg — sees the
constant churn in `LastUpdate` fields inside region files and treats every
re-saved `r.0.0.mca` as new bytes. Daily backups stack up linearly even when
nothing meaningfully changed.

chunkvault hashes each MC chunk's raw payload independently. Unchanged chunks
across N snapshots share **one** copy in the content-addressed pool. The vault
grows with the actual deltas, not with snapshot count.

## Highlights

- **Chunk-level dedup** &nbsp;— hash-addressed pool, identical chunks across snapshots → one copy.
- **Cross-version aware** &nbsp;— stores raw payload bytes; works on 1.7 through 1.21+; reads `level.dat` to record `mc_version` per snapshot.
- **Multi-server zip ingest** &nbsp;— `chunkvault ingest backup.zip` discovers `EX-Server/`, `CR-Server/`, snapshots each world, captures logs separately.
- **Chunk-level diff + maps** &nbsp;— compare any two snapshots from manifests alone; render PNG heatmaps or self-contained Leaflet HTML.
- **Logs handled, not mixed** &nbsp;— `logs/` and `crash-reports/` go to a parallel deduplicated pool. Browse, extract, or delete independently.
- **Interactive wizard** &nbsp;— `chunkvault` with no args drops into a rich-powered TUI with auto-detection, prompts, and live progress.
- **Verify + gc + fsck** &nbsp;— rehash every blob; reclaim unreferenced chunks via ref-count fast path; recover from interrupted writes.
- **Analytics-friendly** &nbsp;— read `level.dat`, `playerdata/*.dat`, datapacks directly from the pool without restoring.
- **9 locales** &nbsp;— en, zh-CN, zh-TW, ja, ko, de, fr, es, ru.

## Where to look next

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **[Getting started](getting-started.md)**

    ---

    Install, initialize a vault, take and restore your first snapshot in under
    a minute.

-   :material-console:{ .lg .middle } **[CLI reference](cli.md)**

    ---

    Every subcommand, every flag, with examples. The flat reference: `init`,
    `snapshot`, `ingest`, `restore`, `diff-snaps`, `verify`, `fsck`,
    `repair-timestamps`, …

-   :material-cog:{ .lg .middle } **[API reference](api/index.md)**

    ---

    `ChunkSnapshotRepo` and friends — for embedding chunkvault inside your
    Python code (server-side automation, dashboards, custom analytics).

-   :material-chart-line:{ .lg .middle } **[Analytics](analytics.md)**

    ---

    Read player positions, scoreboards, datapacks, chunk NBT directly from any
    snapshot without restoring.

-   :material-floor-plan:{ .lg .middle } **[Architecture](architecture.md)**

    ---

    On-disk layout, manifest format, content-addressing, region/file/log/tile
    pools, the SQLite index.

-   :material-wrench:{ .lg .middle } **[Repair workflows](repair.md)**

    ---

    `fsck`, `repair-timestamps`, `migrate-mca-files` — what they do and when
    to reach for them.

</div>

## Requirements

- Python **3.11+**
- Pillow (only hard runtime dep)
- `git` only if you opt into the `--store=git` backend
- `unmined` only if you want real map tiles under your diff overlays

## License

Apache-2.0. See `LICENSE` and `NOTICE` for attribution and commercial-use
notes.
