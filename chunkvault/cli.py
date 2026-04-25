"""Command-line interface for chunkvault.

Subcommands wrap the library API one-to-one. Each command is intentionally
simple — this is a thin adapter for interactive use, not a polished UX.

Usage:
    chunkvault diff OLD NEW [--json FILE] [--png DIM FILE]
    chunkvault init REPO
    chunkvault snapshot REPO WORLD [--label L]
    chunkvault list REPO
    chunkvault restore REPO SNAP DEST [--path P ...]
    chunkvault delete REPO SNAP
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .diff import diff_worlds


def _fmt_counts(counts: dict[str, int]) -> str:
    return f"+{counts['added']} ~{counts['modified']} -{counts['removed']}"


def cmd_diff(args: argparse.Namespace) -> int:
    result = diff_worlds(args.old, args.new)
    total = len(result.changes)
    counts = result.count_by_kind()
    print(f"{total} chunk changes ({_fmt_counts(counts)})")
    if result.errors:
        print(f"{len(result.errors)} region files failed to parse:",
              file=sys.stderr)
        for e in result.errors:
            print(f"  [{e.side}] {e.dimension_key}/r.{e.rx}.{e.rz}.mca: "
                  f"{e.message}", file=sys.stderr)
    for dim, changes in sorted(result.by_dimension().items()):
        dim_counts = {"added": 0, "modified": 0, "removed": 0}
        for c in changes:
            dim_counts[c.kind] += 1
        print(f"  {dim}: {_fmt_counts(dim_counts)}")

    if args.json:
        payload = {
            "summary": counts,
            "total": total,
            "errors": [
                {"dimension": e.dimension_key, "rx": e.rx, "rz": e.rz,
                 "side": e.side, "path": str(e.path), "message": e.message}
                for e in result.errors
            ],
            "changes": [
                {"dimension": c.dimension_key, "rx": c.rx, "rz": c.rz,
                 "cx": c.cx, "cz": c.cz, "kind": c.kind,
                 "old_hash": c.old_hash.hex() if c.old_hash else None,
                 "new_hash": c.new_hash.hex() if c.new_hash else None}
                for c in result.changes
            ],
        }
        Path(args.json).write_text(json.dumps(payload, indent=2))
        print(f"wrote JSON diff → {args.json}")

    if args.png:
        from .viz.heatmap import render_diff_png
        dim, out = args.png
        render_diff_png(result, dim, scale=args.scale, out_path=out)
        print(f"wrote heatmap ({dim}) → {out}")

    if args.html:
        from .viz.leaflet import render_diff_html
        render_diff_html(
            result, out_path=args.html,
            base_tiles_url=args.base_tiles,
            base_tiles_attribution=args.base_attribution,
        )
        print(f"wrote interactive map → {args.html}")

    return 0


def cmd_render(args: argparse.Namespace) -> int:
    from .viz.heatmap import render_diff_png
    result = diff_worlds(args.old, args.new)
    render_diff_png(result, args.dimension, scale=args.scale, out_path=args.output)
    print(f"wrote heatmap → {args.output}")
    return 0


def cmd_diff_snaps(args: argparse.Namespace) -> int:
    repo = _open_repo(args)
    snap_a = repo.get(args.snap_a)
    snap_b = repo.get(args.snap_b)
    if snap_a is None:
        raise RuntimeError(f"no such snapshot: {args.snap_a!r}")
    if snap_b is None:
        raise RuntimeError(f"no such snapshot: {args.snap_b!r}")
    # Chunk store: diff_snapshots is already manifest-only (i.e. fast).
    # Git store: --fast switches to the diff-tree path.
    if args.store == "git" and args.fast:
        result = repo.diff_snapshots_fast(snap_a, snap_b)
    else:
        result = repo.diff_snapshots(snap_a, snap_b)
    counts = result.count_by_kind()
    total = len(result.changes)
    print(f"{snap_a.short_id}..{snap_b.short_id}: {total} chunk changes "
          f"(+{counts['added']} ~{counts['modified']} -{counts['removed']})")
    for dim, changes in sorted(result.by_dimension().items()):
        dc = {"added": 0, "modified": 0, "removed": 0}
        for c in changes:
            dc[c.kind] += 1
        print(f"  {dim}: +{dc['added']} ~{dc['modified']} -{dc['removed']}")

    if args.json:
        payload = {
            "snap_a": snap_a.id, "snap_b": snap_b.id,
            "summary": counts, "total": total,
            "changes": [
                {"dimension": c.dimension_key, "rx": c.rx, "rz": c.rz,
                 "cx": c.cx, "cz": c.cz, "kind": c.kind,
                 "old_hash": c.old_hash.hex() if c.old_hash else None,
                 "new_hash": c.new_hash.hex() if c.new_hash else None}
                for c in result.changes
            ],
        }
        Path(args.json).write_text(json.dumps(payload, indent=2))
        print(f"wrote JSON → {args.json}")
    if args.png:
        from .viz.heatmap import render_diff_png
        dim, out = args.png
        render_diff_png(result, dim, scale=args.scale, out_path=out)
        print(f"wrote heatmap ({dim}) → {out}")
    if args.html:
        from .viz.leaflet import render_diff_html
        render_diff_html(
            result, out_path=args.html,
            title=f"chunkvault {snap_a.short_id}..{snap_b.short_id}",
            base_tiles_url=args.base_tiles,
            base_tiles_attribution=args.base_attribution,
        )
        print(f"wrote interactive map → {args.html}")
    return 0


def cmd_fsck(args: argparse.Namespace) -> int:
    """Reconcile on-disk state with the index — fixes half-written snapshots
    left behind by Ctrl-C / kill / power-loss."""
    from .store import ChunkSnapshotRepo
    repo = ChunkSnapshotRepo(args.repo)
    report = repo.fsck(repair=not args.dry_run)
    print(report.summary())
    if not report.clean and args.verbose:
        for path in report.orphan_manifests[:20]:
            print(f"  orphan manifest: {path}", file=sys.stderr)
        for snap_id in report.dangling_rows[:20]:
            print(f"  dangling row: {snap_id}", file=sys.stderr)
        for snap_id in report.dangling_log_rows[:20]:
            print(f"  dangling log row: {snap_id}", file=sys.stderr)
        for path in report.orphan_log_manifests[:20]:
            print(f"  orphan log manifest: {path}", file=sys.stderr)
        for path in report.stray_temp_files[:20]:
            print(f"  stray .tmp file: {path}", file=sys.stderr)
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    repo = _open_repo(args)
    if args.store == "git":
        repo.gc(aggressive=args.aggressive)
        print(f"gc complete for {args.repo}")
    else:
        rc, rf = repo.gc()
        print(f"gc complete for {args.repo}: removed {rc} chunks, {rf} files")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from .store import ChunkSnapshotRepo, ImportSession
    repo = ChunkSnapshotRepo(args.repo)
    if not repo.is_initialized():
        raise RuntimeError(
            f"Repo not initialized: {args.repo}. Run `chunkvault init {args.repo}` first."
        )
    session = ImportSession(args.archive, world_subpath=args.world_subpath)
    label = args.label or session.default_label
    with session as world_dir:
        snap = repo.snapshot(world_dir, label=label, allow_live=True)
    print(f"{snap.short_id}  {snap.timestamp.isoformat()}  {label}  "
          f"(imported from {args.archive.name if hasattr(args.archive, 'name') else args.archive})")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from .store import ChunkSnapshotRepo
    repo = ChunkSnapshotRepo(args.repo)
    report = repo.verify(repair=args.repair)
    print(
        f"verify: {report.ok_chunks} chunks ok, "
        f"{report.corrupt_chunks} corrupt, "
        f"{report.missing_referenced} missing-referenced, "
        f"{report.orphan_blobs} orphan blobs"
    )
    if report.repaired:
        print(f"repaired: removed {report.repaired} corrupt blob(s)")
    if report.corrupt_chunks or report.missing_referenced:
        return 1 if not args.repair else 0
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from .store import ChunkSnapshotRepo, ingest_archive
    repo = ChunkSnapshotRepo(args.repo)
    if not repo.is_initialized():
        raise RuntimeError(
            f"Repo not initialized: {args.repo}. "
            f"Run `chunkvault init {args.repo}` first."
        )
    result = ingest_archive(
        repo, args.archive,
        skip_logs=args.skip_logs,
        verify_roundtrip=not args.no_verify,
    )
    print(f"ingested {args.archive.name}")
    print(f"  servers: {', '.join(result.server_names) or '(none)'}")
    print(f"  snapshots: {len(result.snapshots)}")
    if result.skipped_servers:
        for name, err in result.skipped_servers:
            print(f"  ! skipped {name}: {err}")
    if result.log_snapshot is not None:
        print(f"  log snapshot: {result.log_snapshot.short_id}  "
              f"({result.log_snapshot.file_count} files, "
              f"{result.log_snapshot.server_count} servers)")
    return 0


def cmd_logs_list(args: argparse.Namespace) -> int:
    from .store import ChunkSnapshotRepo
    repo = ChunkSnapshotRepo(args.repo)
    snaps = repo.list_log_snapshots()
    if not snaps:
        print("(no log snapshots)")
        return 0
    for s in snaps:
        print(f"{s.short_id}  {s.timestamp.isoformat()}  "
              f"{(s.label or '-'):26}  servers={s.server_count} files={s.file_count}")
    return 0


def cmd_logs_extract(args: argparse.Namespace) -> int:
    from .store import ChunkSnapshotRepo
    repo = ChunkSnapshotRepo(args.repo)
    written = repo.extract_logs(args.snapshot, args.dest, server=args.server)
    print(f"extracted {written} log files to {args.dest}")
    return 0


def cmd_logs_delete(args: argparse.Namespace) -> int:
    from .store import ChunkSnapshotRepo
    repo = ChunkSnapshotRepo(args.repo)
    repo.delete_log_snapshot(args.snapshot)
    print(f"deleted log snapshot {args.snapshot}")
    return 0


def cmd_render_tiles(args: argparse.Namespace) -> int:
    from .viz.tiles import render_world_tiles
    out = render_world_tiles(
        args.world, args.output,
        dimension=args.dimension,
        zoom_range=(args.zoom_min, args.zoom_max)
            if args.zoom_min is not None and args.zoom_max is not None
            else None,
    )
    print(f"wrote tiles → {out}")
    return 0


def _open_repo(args: argparse.Namespace):
    """Return the configured backend (chunk by default, git via --store=git)."""
    if getattr(args, "store", "chunk") == "git":
        from .storage import SnapshotRepo
        return SnapshotRepo(args.repo)
    from .store import ChunkSnapshotRepo
    return ChunkSnapshotRepo(args.repo)


def cmd_init(args: argparse.Namespace) -> int:
    repo = _open_repo(args)
    repo.init()
    print(f"initialized {args.store} snapshot repo at {args.repo}")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    repo = _open_repo(args)
    kwargs = dict(label=args.label, allow_live=args.allow_live)
    if args.store == "chunk":
        # Round-trip verify is opt-OUT: runs by default, --no-verify skips.
        kwargs["verify_roundtrip"] = not args.no_verify
    if args.timestamp:
        from datetime import datetime, timezone
        try:
            ts = datetime.fromisoformat(args.timestamp)
        except ValueError as e:
            print(f"!! invalid --timestamp {args.timestamp!r}: {e}",
                  file=sys.stderr)
            return 2
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        kwargs["timestamp"] = ts
    try:
        snap = repo.snapshot(args.world, **kwargs)
    except Exception as e:
        from .store.repo import RoundTripVerificationError
        if isinstance(e, RoundTripVerificationError):
            print(f"!! ROUND-TRIP VERIFY FAILED for {e.snapshot.short_id} "
                  f"(label={e.snapshot.label!r})", file=sys.stderr)
            print(f"   {e.report.summary()}", file=sys.stderr)
            print(f"   The snapshot was kept in the index — inspect with "
                  f"`verify-roundtrip` or remove with `delete`.",
                  file=sys.stderr)
            return 2
        raise
    label = snap.label or ""
    extra = ""
    if hasattr(snap, "mc_version") and snap.mc_version:
        extra = f"  mc={snap.mc_version}"
    print(f"{snap.short_id}  {snap.timestamp.isoformat()}  {label}{extra}")
    return 0


def cmd_verify_roundtrip(args: argparse.Namespace) -> int:
    """Standalone round-trip verifier: restore an existing snapshot and
    compare against a known-good source tree."""
    from .store import ChunkSnapshotRepo
    from .store.roundtrip import verify_roundtrip
    repo = ChunkSnapshotRepo(args.repo)
    snap = repo.get(args.snapshot)
    if snap is None:
        raise RuntimeError(f"no such snapshot: {args.snapshot!r}")
    report = verify_roundtrip(repo, snap, args.original)
    print(report.summary())
    if not report.passed:
        for cm in report.chunk_mismatches[:20]:
            print(f"  chunk {cm.kind} {cm.dimension_key} "
                  f"r.{cm.rx}.{cm.rz} ({cm.cx},{cm.cz}) {cm.detail}",
                  file=sys.stderr)
        for fm in report.file_mismatches[:20]:
            print(f"  file {fm.kind}: {fm.relative_path}", file=sys.stderr)
        return 1
    return 0


def cmd_retime(args: argparse.Namespace) -> int:
    """Reassign one or many snapshots' timestamps."""
    from datetime import datetime, timezone
    from .store import ChunkSnapshotRepo
    from .store.repo import ChunkRepoError
    from .store.manifest import read_manifest

    repo = ChunkSnapshotRepo(args.repo)
    if not repo.is_initialized():
        print(f"!! repo not initialized: {args.repo}", file=sys.stderr)
        return 2

    if args.all and not args.from_level_dat:
        print("!! --all requires --from-level-dat", file=sys.stderr)
        return 2
    if args.all and args.snapshot:
        print("!! pass --all OR a snapshot id, not both", file=sys.stderr)
        return 2
    if not args.all and not args.snapshot:
        print("!! supply a snapshot id (or use --all --from-level-dat)",
              file=sys.stderr)
        return 2
    if args.timestamp and args.from_level_dat:
        print("!! --timestamp and --from-level-dat are mutually exclusive",
              file=sys.stderr)
        return 2
    if not args.timestamp and not args.from_level_dat:
        print("!! supply --timestamp or --from-level-dat", file=sys.stderr)
        return 2

    explicit_ts: "datetime | None" = None
    if args.timestamp:
        try:
            explicit_ts = datetime.fromisoformat(args.timestamp)
        except ValueError as e:
            print(f"!! invalid --timestamp {args.timestamp!r}: {e}",
                  file=sys.stderr)
            return 2
        if explicit_ts.tzinfo is None:
            explicit_ts = explicit_ts.replace(tzinfo=timezone.utc)

    targets = repo.list() if args.all else [repo.get(args.snapshot)]
    if not args.all and targets[0] is None:
        print(f"!! no such snapshot: {args.snapshot!r}", file=sys.stderr)
        return 2

    if not args.dry_run:
        bak = repo.backup_index()
        print(f"# index backed up to {bak.name}")

    changed = 0
    skipped: list[tuple[str, str]] = []
    for snap in targets:
        try:
            if explicit_ts is not None:
                new_ts = explicit_ts
                source = "explicit"
            else:
                manifest = read_manifest(snap.manifest_path)
                lp_ms = manifest.header.last_played_ms
                if not lp_ms:
                    skipped.append((snap.short_id, "no LastPlayed in manifest"))
                    continue
                new_ts = datetime.fromtimestamp(lp_ms / 1000, tz=timezone.utc)
                source = "last_played"
            if int(new_ts.timestamp() * 1000) == int(snap.timestamp.timestamp() * 1000):
                skipped.append((snap.short_id, "already at target timestamp"))
                continue
            if args.dry_run:
                print(f"would retime {snap.short_id} ({snap.label or '-'})  "
                      f"{snap.timestamp.isoformat()} → {new_ts.isoformat()}  "
                      f"[{source}]")
                changed += 1
                continue
            repo.retime_snapshot(snap, new_ts)
            print(f"retimed {snap.short_id} ({snap.label or '-'})  "
                  f"→ {new_ts.isoformat()}  [{source}]")
            changed += 1
        except ChunkRepoError as e:
            skipped.append((snap.short_id, str(e)))

    print(f"# {'would change' if args.dry_run else 'changed'}: {changed} / "
          f"{len(targets)} snapshot(s)")
    for sid, reason in skipped[:10]:
        print(f"# skip {sid}: {reason}", file=sys.stderr)
    if len(skipped) > 10:
        print(f"# ... ({len(skipped) - 10} more skipped)", file=sys.stderr)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    repo = _open_repo(args)
    for snap in repo.list():
        label = (snap.label or "-")[:20]
        if hasattr(snap, "mc_version") and snap.mc_version:
            tail = f"mc={snap.mc_version} world={snap.world_name}"
        else:
            tail = getattr(snap, "subject", snap.world_name or "")
        print(f"{snap.short_id}  {snap.timestamp.isoformat()}  {label:20}  {tail}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    repo = _open_repo(args)
    snap = repo.get(args.snapshot)
    if snap is None:
        raise RuntimeError(f"no such snapshot: {args.snapshot!r}")
    repo.restore(snap, args.dest, paths=args.path or None)
    print(f"restored {snap.short_id} → {args.dest}"
          + (f" (paths: {args.path})" if args.path else ""))
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    repo = _open_repo(args)
    repo.delete(args.snapshot)
    print(f"deleted snapshot {args.snapshot}")
    return 0


def cmd_wizard(args: argparse.Namespace) -> int:
    from .wizard import run_wizard
    return run_wizard()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chunkvault",
                                description="Minecraft world incremental backup + chunk-diff.")
    # No subcommand → drop into the interactive wizard.
    sub = p.add_subparsers(dest="cmd", required=False)

    d = sub.add_parser("diff", help="Compare two world directories.")
    d.add_argument("old", type=Path, help="Older world directory")
    d.add_argument("new", type=Path, help="Newer world directory")
    d.add_argument("--json", type=Path, metavar="FILE",
                   help="Write full diff as JSON.")
    d.add_argument("--png", nargs=2, metavar=("DIMENSION", "FILE"),
                   help="Also render a PNG heatmap for a dimension.")
    d.add_argument("--html", type=Path, metavar="FILE",
                   help="Also render a self-contained Leaflet HTML map.")
    d.add_argument("--scale", type=int, default=8,
                   help="Pixels per chunk in the PNG heatmap (default 8).")
    d.add_argument("--base-tiles", type=str, metavar="URL",
                   help="Tile-layer URL pattern for the HTML map (e.g. "
                        "./tiles/{z}/{x}/{y}.png).")
    d.add_argument("--base-attribution", type=str,
                   help="Attribution text for the base tile layer.")
    d.set_defaults(func=cmd_diff)

    r = sub.add_parser("render", help="Render a diff PNG heatmap for one dimension.")
    r.add_argument("old", type=Path)
    r.add_argument("new", type=Path)
    r.add_argument("dimension", help="Dimension key, e.g. 'region' or 'DIM-1/region'")
    r.add_argument("--output", "-o", type=Path, required=True)
    r.add_argument("--scale", type=int, default=8)
    r.set_defaults(func=cmd_render)

    def _add_store_arg(p):
        p.add_argument("--store", choices=["chunk", "git"], default="chunk",
                       help="Storage backend (default: chunk-level dedup).")

    i = sub.add_parser("init", help="Initialize a snapshot repo.")
    _add_store_arg(i)
    i.add_argument("repo", type=Path)
    i.set_defaults(func=cmd_init)

    s = sub.add_parser("snapshot", help="Take a snapshot of a world.")
    _add_store_arg(s)
    s.add_argument("repo", type=Path)
    s.add_argument("world", type=Path)
    s.add_argument("--label", "-l", type=str, default=None)
    s.add_argument("--allow-live", action="store_true",
                   help="Skip session.lock check (may snapshot a torn world).")
    s.add_argument("--no-verify", action="store_true",
                   help="Skip the post-snapshot round-trip verification "
                        "(default: verify is ON; doubles snapshot time).")
    s.add_argument("--timestamp", type=str, default=None,
                   help="Override snapshot timestamp (ISO8601, e.g. "
                        "'2024-03-15T10:30:00'). Default: read level.dat's "
                        "LastPlayed, fall back to newest region mtime, then "
                        "to current wall time.")
    s.set_defaults(func=cmd_snapshot)

    ls = sub.add_parser("list", help="List snapshots, newest first.")
    _add_store_arg(ls)
    ls.add_argument("repo", type=Path)
    ls.set_defaults(func=cmd_list)

    re = sub.add_parser("restore", help="Restore a snapshot (optionally a subset).")
    _add_store_arg(re)
    re.add_argument("repo", type=Path)
    re.add_argument("snapshot", type=str, help="SHA, short SHA, or label")
    re.add_argument("dest", type=Path)
    re.add_argument("--path", action="append",
                    help="Restrict restore to a path (repeatable).")
    re.set_defaults(func=cmd_restore)

    dl = sub.add_parser("delete", help="Delete a snapshot.")
    _add_store_arg(dl)
    dl.add_argument("repo", type=Path)
    dl.add_argument("snapshot", type=str)
    dl.set_defaults(func=cmd_delete)

    ds = sub.add_parser("diff-snaps", help="Chunk-level diff between two snapshots.")
    _add_store_arg(ds)
    ds.add_argument("repo", type=Path)
    ds.add_argument("snap_a", type=str)
    ds.add_argument("snap_b", type=str)
    ds.add_argument("--json", type=Path, metavar="FILE")
    ds.add_argument("--png", nargs=2, metavar=("DIMENSION", "FILE"))
    ds.add_argument("--html", type=Path, metavar="FILE")
    ds.add_argument("--scale", type=int, default=8)
    ds.add_argument("--fast", action="store_true",
                    help="(git store only) use diff-tree fast path.")
    ds.add_argument("--base-tiles", type=str, metavar="URL")
    ds.add_argument("--base-attribution", type=str)
    ds.set_defaults(func=cmd_diff_snaps)

    g = sub.add_parser("gc", help="Garbage-collect the snapshot repo.")
    _add_store_arg(g)
    g.add_argument("repo", type=Path)
    g.add_argument("--aggressive", action="store_true",
                   help="(git store only) extra-aggressive repack.")
    g.set_defaults(func=cmd_gc)

    fck = sub.add_parser("fsck",
                         help="Reconcile on-disk state with the index. "
                              "Fixes half-written snapshots left by Ctrl-C "
                              "or power-loss. (Chunk store only.)")
    fck.add_argument("repo", type=Path)
    fck.add_argument("--dry-run", action="store_true",
                     help="Report issues without fixing them.")
    fck.add_argument("--verbose", "-v", action="store_true",
                     help="List each issue.")
    fck.set_defaults(func=cmd_fsck)

    ing = sub.add_parser("ingest",
                         help="Ingest a multi-server archive: snapshot each "
                              "<server>/world to chunk store, capture logs "
                              "into a log snapshot.")
    ing.add_argument("repo", type=Path)
    ing.add_argument("archive", type=Path)
    ing.add_argument("--skip-logs", action="store_true",
                     help="Skip log capture (only do world snapshots).")
    ing.add_argument("--no-verify", action="store_true",
                     help="Skip post-snapshot round-trip verification "
                          "(default ON; speeds up bulk ingest).")
    ing.set_defaults(func=cmd_ingest)

    vrt = sub.add_parser("verify-roundtrip",
                         help="Restore an existing snapshot to a temp dir and "
                              "compare against an original world tree.")
    vrt.add_argument("repo", type=Path)
    vrt.add_argument("snapshot", type=str, help="snap id or label")
    vrt.add_argument("original", type=Path,
                     help="path to the original world to compare against")
    vrt.set_defaults(func=cmd_verify_roundtrip)

    rt = sub.add_parser("retime",
                        help="Reassign a snapshot's timeline timestamp. "
                             "Useful when the original timestamp was wrong "
                             "(e.g. a live snapshot defaulted to 'now' but "
                             "the world was actually a 2023 backup).")
    rt.add_argument("repo", type=Path)
    rt.add_argument("snapshot", type=str, nargs="?",
                    help="snap id or label (required unless --all)")
    rt.add_argument("--timestamp", type=str, default=None,
                    help="New timestamp (ISO8601, e.g. '2024-03-15T10:30:00')")
    rt.add_argument("--from-level-dat", action="store_true",
                    help="Re-derive timestamp from the manifest's stored "
                         "level.dat LastPlayed field. Skips snapshots whose "
                         "manifest has no usable LastPlayed.")
    rt.add_argument("--all", action="store_true",
                    help="Apply --from-level-dat to every snapshot in the "
                         "repo (only valid with --from-level-dat).")
    rt.add_argument("--dry-run", action="store_true",
                    help="Report what would change without writing anything.")
    rt.set_defaults(func=cmd_retime)

    ll = sub.add_parser("logs-list", help="List log snapshots.")
    ll.add_argument("repo", type=Path)
    ll.set_defaults(func=cmd_logs_list)

    le = sub.add_parser("logs-extract",
                        help="Materialize a log snapshot into a flat directory.")
    le.add_argument("repo", type=Path)
    le.add_argument("snapshot", type=str)
    le.add_argument("dest", type=Path)
    le.add_argument("--server", type=str, default=None,
                    help="Restrict to one server name.")
    le.set_defaults(func=cmd_logs_extract)

    ld = sub.add_parser("logs-delete", help="Delete a log snapshot.")
    ld.add_argument("repo", type=Path)
    ld.add_argument("snapshot", type=str)
    ld.set_defaults(func=cmd_logs_delete)

    imp = sub.add_parser("import",
                         help="Import a world from a directory or archive (.zip/.tar.gz/...).")
    imp.add_argument("repo", type=Path)
    imp.add_argument("archive", type=Path)
    imp.add_argument("--label", "-l", type=str, default=None)
    imp.add_argument("--world-subpath", type=str, default=None,
                     help="Path inside the archive pointing to the world dir.")
    imp.set_defaults(func=cmd_import)

    v = sub.add_parser("verify",
                       help="Verify chunk-store integrity (rehash all blobs).")
    v.add_argument("repo", type=Path)
    v.add_argument("--repair", action="store_true",
                   help="Delete corrupt blobs (will be regenerated on next snapshot).")
    v.set_defaults(func=cmd_verify)

    rt = sub.add_parser("render-tiles",
                        help="Render world base tiles via unmined (must be on PATH).")
    rt.add_argument("world", type=Path)
    rt.add_argument("output", type=Path)
    rt.add_argument("--dimension", type=str, default=None,
                    help="overworld / nether / the_end / custom")
    rt.add_argument("--zoom-min", type=int, default=None)
    rt.add_argument("--zoom-max", type=int, default=None)
    rt.set_defaults(func=cmd_render_tiles)

    w = sub.add_parser("wizard",
                       help="Launch the interactive wizard (also default with no args).")
    w.set_defaults(func=cmd_wizard)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not getattr(args, "func", None):
            return cmd_wizard(args)
        return args.func(args)
    except KeyboardInterrupt:
        # Friendly exit on Ctrl-C / Ctrl-Break — never dump a traceback.
        # Exit code 130 is the POSIX convention for SIGINT.
        print("\n[interrupted by user]", file=sys.stderr)
        print("If you Ctrl-C'd in the middle of a snapshot, run "
              "`chunkvault fsck <repo>` to clean up any half-written state.",
              file=sys.stderr)
        return 130
    except BrokenPipeError:
        # Piping into `head` etc. — silent exit
        return 0


if __name__ == "__main__":
    sys.exit(main())
