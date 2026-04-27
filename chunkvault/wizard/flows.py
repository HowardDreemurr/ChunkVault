"""Top-level wizard flows — orchestrate detect → prompt → confirm → run.

Each ``run_*`` returns the operation result (or 0 on success, 1 on failure)
so the wizard's main loop knows how to proceed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from ..store import (
    ChunkSnapshotRepo,
    ImportError,
    ProgressEvent,
    ingest_archive,
    iter_archives,
)
from ..store.ingest import IngestResult
from .detect import EnvironmentSummary, detect_environment, summarize_repo
from .ui import (
    choose_one,
    confirm,
    fmt_bytes,
    main_menu,
    make_console,
    make_progress,
    prompt_path,
    render_environment,
    render_op_intro,
    select_archives,
)


def _set_phase_total(progress, task_id, new_total) -> None:
    """Force-set a Progress task's ``total`` (including to ``None``).

    ``rich.progress.Progress.update(total=None)`` and ``reset(total=None)``
    are both no-ops — they treat ``None`` as "keep current total". So once a
    task gets a numeric total, the only way to clear it back to indeterminate
    is to mutate the Task object directly. Without this, when one phase
    finishes (e.g. region-hashing with total=11534) and the next starts
    (e.g. roundtrip-restore with no known total), the bar shows a stale
    ``0/11534`` for the entire next phase, looking frozen.
    """
    with progress._lock:
        progress._tasks[task_id].total = new_total


def _make_phase_cb(progress, task_id, prefix: str):
    """Standard phase-tracking callback for a single rich Progress task.

    All long-running ops (ingest, snapshot, verify, ...) share the same
    pattern: one task that follows whichever phase is active. Translate
    ``ProgressEvent`` into ``progress.update`` calls, with the stale-total
    workaround (see :func:`_set_phase_total`) baked in.
    """
    def cb(e: ProgressEvent):
        if e.kind == "phase_start":
            _set_phase_total(progress, task_id, e.total if e.total else None)
            progress.update(
                task_id,
                description=f"{prefix}: {e.phase} {e.label or ''}".strip(),
                completed=0,
            )
        elif e.kind == "phase_progress":
            if e.total:
                _set_phase_total(progress, task_id, e.total)
            progress.update(
                task_id,
                completed=e.current,
                description=(
                    f"{prefix}: {e.phase} {e.label}".strip()
                    if e.label else None
                ),
            )
        elif e.kind == "phase_done":
            final = e.total or e.current or 0
            if final:
                _set_phase_total(progress, task_id, final)
            progress.update(task_id, completed=e.current or e.total or 0)
    return cb


def run_wizard(console: Console | None = None) -> int:
    """Top-level entry point. Returns the process exit code."""
    from .i18n import t as _t
    console = console or make_console()
    console.print(f"[bold magenta]{_t('wizard.title')}[/bold magenta]")
    cwd = Path.cwd()
    console.print(f"[dim]{_t('wizard.detecting')} ({cwd})[/dim]")

    env = detect_environment()
    render_environment(console, env)
    if not env.repos and not env.source_paths:
        console.print(
            "[yellow]Nothing auto-detected. You can still proceed — "
            "the wizard will prompt you for paths to use.[/yellow]\n"
        )

    first_loop = True
    while True:
        # Show full guide on first loop, abbreviated on subsequent
        choice = main_menu(console, with_guide=first_loop)
        first_loop = False
        if choice == "q":
            console.print(f"[dim]{_t('prompt.bye')}[/dim]")
            return 0
        try:
            if choice == "i":
                run_ingest_flow(console, env)
            elif choice == "s":
                run_snapshot_flow(console, env)
            elif choice == "l":
                run_list_flow(console, env)
            elif choice == "x":
                run_restore_flow(console, env)
            elif choice == "D":
                run_delete_flow(console, env)
            elif choice == "d":
                run_diff_flow(console, env)
            elif choice == "b":
                run_browse_flow(console, env)
            elif choice == "t":
                run_thumbnail_flow(console, env)
            elif choice == "L":
                run_logs_flow(console, env)
            elif choice == "v":
                run_verify_flow(console, env)
            elif choice == "V":
                run_verify_roundtrip_flow(console, env)
            elif choice == "F":
                run_verify_folders_flow(console, env)
            elif choice == "f":
                run_fsck_flow(console, env)
            elif choice == "r":
                run_repair_timestamps_flow(console, env)
            elif choice == "R":
                run_retime_flow(console, env)
            elif choice == "g":
                run_gc_flow(console, env)
            elif choice == "@":
                run_language_flow(console, env)
        except KeyboardInterrupt:
            console.print(f"\n[yellow]{_t('prompt.cancel')}[/yellow]")
        except Exception as e:
            console.print(f"[red]{_t('prompt.error')}[/red] {e}")
        env = detect_environment()


# ---- repo helper ----------------------------------------------------------

def _pick_snapshot(
    console: Console, repo: ChunkSnapshotRepo, *,
    prompt: str = "Pick a snapshot",
):
    """Show all snapshots in the vault and let the user pick one.

    Returns the ChunkSnapshot, or None if the vault is empty / user
    cancelled. The displayed labels are short (id-prefix + ts + label)
    so even a long list scrolls cleanly in the questionary picker.
    """
    snaps = repo.list()
    if not snaps:
        console.print("[yellow]vault has no snapshots yet.[/yellow]")
        return None
    options = [
        f"{s.short_id}  {s.timestamp.isoformat()}  {(s.label or '-')[:40]}"
        for s in snaps
    ]
    pick = choose_one(console, prompt, options)
    if pick is None:
        return None
    idx = options.index(pick)
    return snaps[idx]


def _pick_log_snapshot(
    console: Console, repo: ChunkSnapshotRepo, *,
    prompt: str = "Pick a log snapshot",
):
    """Same as :func:`_pick_snapshot` but for log snapshots."""
    snaps = repo.list_log_snapshots()
    if not snaps:
        console.print("[yellow]vault has no log snapshots.[/yellow]")
        return None
    options = [
        f"{s.short_id}  {s.timestamp.isoformat()}  {(s.label or '-'):26}  "
        f"servers={s.server_count} files={s.file_count}"
        for s in snaps
    ]
    pick = choose_one(console, prompt, options)
    if pick is None:
        return None
    idx = options.index(pick)
    return snaps[idx]


def _pick_or_create_repo(console: Console, env: EnvironmentSummary) -> ChunkSnapshotRepo:
    chunk_repos = [r for r in env.repos if r.kind == "chunk"]
    default = chunk_repos[0].path if chunk_repos else (Path.cwd() / "backup-repo")
    repo_path = prompt_path(console, "Repo path", default=default)
    repo = ChunkSnapshotRepo(repo_path)
    if not repo.is_initialized():
        if not confirm(console, f"Create new repo at {repo_path}?", default=True):
            raise RuntimeError("aborted: repo not initialized")
        repo.init()
        console.print(f"[green]initialized[/green] {repo_path}")
    return repo


# ---- ingest --------------------------------------------------------------

def run_ingest_flow(
    console: Console, env: EnvironmentSummary,
) -> list[IngestResult]:
    """Bulk-import backup archives — the user's main use case."""
    render_op_intro(
        console, "Ingest archives",
        "Bulk-import backup .zip / .tar.gz files. The wizard scans the "
        "source folder(s) you give it, peeks inside each archive to "
        "detect server folders + worlds + logs, shows you a preview, "
        "then asks confirm before doing anything.",
        expects="A folder containing backup archive files.",
        example="D:\\day_backups\\  (with files like 2024-08-15-...zip inside)",
    )
    repo = _pick_or_create_repo(console, env)

    # Collect candidate source paths
    candidates: list[Path] = []
    for s in env.source_paths:
        if confirm(console, f"Use source path {s.path} ({s.archive_count} archives)?",
                   default=True):
            candidates.append(s.path)
    while True:
        if confirm(console, "Add another source path?", default=False):
            extra = prompt_path(console, "Extra source", must_exist=True)
            candidates.append(extra)
        else:
            break
    if not candidates:
        console.print("[yellow]no source paths chosen — nothing to do.[/yellow]")
        return []

    # Enumerate archives
    archives: list[Path] = []
    for c in candidates:
        archives.extend(iter_archives(c))
    if not archives:
        console.print("[yellow]no archives found in chosen sources.[/yellow]")
        return []

    skip_logs = not confirm(
        console, "Capture logs into the repo (deduped)?", default=True,
    )
    verify_after = confirm(
        console,
        "Round-trip verify each snapshot? (slower but proves restorability)",
        default=True,
    )
    if not verify_after:
        console.print(
            "[yellow]Heads up:[/yellow] verification skipped — you can run "
            "[bold]chunkvault verify-roundtrip[/bold] later for spot checks."
        )

    # Inspect each archive — show servers, regions, logs INSIDE, not just file size.
    console.print(f"\n[bold]inspecting {len(archives)} archive(s)…[/bold]")
    from ..store import preview_archive
    from .ui import render_archive_preview
    previews = []
    # Compact one-line view if many archives; full table per archive when ≤ 5.
    # (Five fits comfortably on one screen; beyond that, the table scrolling
    # makes it impossible to compare archives side-by-side.)
    use_compact = len(archives) > 5
    for archive in archives:
        prev = preview_archive(archive)
        previews.append(prev)
        render_archive_preview(console, prev, compact=use_compact)
    if use_compact:
        console.print(
            "[dim](compact view — pick [c]ompact-off below to see full per-archive "
            "tables, or accept defaults to ingest each ✓ archive.)[/dim]"
        )

    # Aggregate totals across all previews
    total_servers = sum(len(p.servers) for p in previews if p.error is None)
    total_regions = sum(s.region_files for p in previews if p.error is None
                        for s in p.servers)
    total_logs = sum(s.log_files + s.crash_report_files
                     for p in previews if p.error is None for s in p.servers)
    total_world_bytes = sum(s.estimated_world_bytes
                            for p in previews if p.error is None
                            for s in p.servers)
    failed = [p for p in previews if p.error is not None]
    console.print(
        f"\n[bold]TOTAL across {len(archives)} archive(s):[/bold]\n"
        f"  servers: {total_servers}\n"
        f"  region files (world chunks): {total_regions}\n"
        f"  log/crash files: {total_logs}\n"
        f"  estimated world bytes: {fmt_bytes(total_world_bytes)}"
    )
    if failed:
        console.print(f"  [red]{len(failed)} unreadable archive(s) — will be skipped[/red]")

    # Per-archive selection: user can uncheck individual archives instead of
    # the all-or-nothing Proceed? prompt. Pre-checks "clean" archives (every
    # detected server has regions); leaves problematic ones unchecked so they
    # require an explicit decision.
    selected = select_archives(archives, previews)
    if not selected:
        console.print("[yellow]nothing selected — cancelled.[/yellow]")
        return []
    selected_set = {a for a in selected}
    archives = [a for a in archives if a in selected_set]
    console.print(
        f"[dim]ingesting {len(archives)} archive(s)…[/dim]"
    )

    results: list[IngestResult] = []
    verify_failures: list[tuple[str, str]] = []     # (archive_name, reason)
    progress = make_progress(console)
    from ..store.repo import RoundTripVerificationError
    with progress:
        archive_task = progress.add_task("archives", total=len(archives))
        # A second task that follows the active phase inside the current
        # archive (regions/files/verify). Without this the outer bar sits at
        # 0/N for the entire first archive — a single 2 GB zip can take 10+
        # minutes, and a frozen-looking progress bar makes users think
        # the wizard hung.
        phase_task = progress.add_task("phase: idle", total=None)

        def make_cb():
            return _make_phase_cb(progress, phase_task, prefix="phase")

        for archive in archives:
            progress.update(archive_task, description=f"archives: {archive.name}")
            progress.update(
                phase_task, description=f"phase: opening {archive.name}",
                completed=0, total=None,
            )
            try:
                result = ingest_archive(
                    repo, archive,
                    skip_logs=skip_logs,
                    verify_roundtrip=verify_after,
                    progress_cb=make_cb(),
                )
                results.append(result)
            except RoundTripVerificationError as e:
                verify_failures.append((archive.name, e.report.summary()))
                console.print(
                    f"[red]VERIFY FAILED[/red] {archive.name}: "
                    f"{e.report.summary()}"
                )
            except ImportError as e:
                console.print(f"[red]skip[/red] {archive.name}: {e}")
            except Exception as e:
                console.print(f"[red]error[/red] {archive.name}: {e}")
            progress.advance(archive_task, 1)
        progress.update(phase_task, description="phase: done", completed=1, total=1)

    # Summary
    snaps = sum(len(r.snapshots) for r in results)
    log_snaps = sum(1 for r in results if r.log_snapshot is not None)
    already = sum(len(r.already_ingested) for r in results)
    console.print(
        f"\n[green]done.[/green] {snaps} new world snapshots, "
        f"{log_snaps} log snapshots, "
        f"{len(results)}/{len(archives)} archives processed cleanly."
    )
    if already:
        console.print(
            f"[dim]{already} server(s) already in the vault — skipped "
            f"(re-ingest is idempotent).[/dim]"
        )
    if verify_failures:
        console.print(
            f"[red]{len(verify_failures)} archive(s) failed round-trip verify "
            f"— their snapshots are kept but should be inspected:[/red]"
        )
        for name, summary in verify_failures[:10]:
            console.print(f"  {name}: {summary}")
    return results


# ---- single snapshot ----------------------------------------------------

def run_snapshot_flow(console: Console, env: EnvironmentSummary):
    render_op_intro(
        console, "Snapshot a live world",
        "Take a snapshot of one specific MC world directory. Don't pick "
        "this for archive folders — use [bold]Ingest archives[/bold] for "
        ".zip files. The directory you give must contain "
        "[yellow]level.dat[/yellow] or a [yellow]region/[/yellow] "
        "subdirectory; the wizard will refuse if it doesn't look like a "
        "real world (so a typo doesn't burn 80 GB of garbage into the vault).",
        expects="A single MC world/ directory (containing level.dat or region/).",
        example="D:\\servers\\smp\\world  (NOT D:\\servers\\smp — point at the world subdir)",
    )
    repo = _pick_or_create_repo(console, env)
    world = prompt_path(console, "World directory (must contain level.dat or region/)",
                        must_exist=True)
    label = console.input("Label (blank for none): ").strip() or None
    allow_live = confirm(console, "Allow snapshotting a live world?", default=False)
    verify_after = confirm(
        console,
        "Round-trip verify after snapshot? (slower but proves restorability)",
        default=True,
    )
    progress = make_progress(console)
    from ..store.repo import RoundTripVerificationError
    with progress:
        task = progress.add_task("snapshot", total=None)
        cb = _make_phase_cb(progress, task, prefix="snapshot")

        try:
            snap = repo.snapshot(world, label=label, allow_live=allow_live,
                                 progress_cb=cb, verify_roundtrip=verify_after)
        except RoundTripVerificationError as e:
            console.print(
                f"[red]VERIFY FAILED[/red] for {e.snapshot.short_id} "
                f"(label={e.snapshot.label!r}):"
            )
            console.print(f"  {e.report.summary()}")
            console.print(
                "[yellow]Snapshot was kept in the index — inspect it or "
                "remove it explicitly.[/yellow]"
            )
            return
    console.print(f"[green]snapshot[/green] {snap.short_id}  {snap.label or ''}  "
                  f"mc={snap.mc_version or '?'}"
                  + ("  [dim](verified)[/dim]" if verify_after else ""))


# ---- diff ---------------------------------------------------------------

def run_diff_flow(console: Console, env: EnvironmentSummary):
    render_op_intro(
        console, "Diff snapshots",
        "Compare any two snapshots already in the vault. Pure manifest "
        "lookup, no disk I/O — completes in milliseconds regardless of "
        "world size. Reports added / modified / removed chunks per "
        "dimension and (for cross-version snapshots) the MC version delta.",
        expects="Two snapshot identifiers (label or short id) from the same vault.",
    )
    repo = _pick_or_create_repo(console, env)
    snaps = repo.list()
    if len(snaps) < 2:
        console.print("[yellow]need at least 2 snapshots to diff.[/yellow]")
        return
    console.print("\n[bold]snapshots:[/bold]")
    for s in snaps[:20]:
        console.print(f"  {s.short_id}  {s.timestamp.isoformat()}  "
                      f"{s.label or '-':25}  ({s.world_name})")
    a = console.input("Snap A (label or short id): ").strip()
    b = console.input("Snap B (label or short id): ").strip()
    diff = repo.diff_snapshots(a, b)
    counts = diff.count_by_kind()
    console.print(
        f"\n[bold]{a}[/bold] → [bold]{b}[/bold]: "
        f"+{counts['added']} ~{counts['modified']} -{counts['removed']}  "
        f"({len(diff.changes)} chunks)"
    )
    if diff.version_changed():
        console.print(
            f"  [yellow]version change:[/yellow] "
            f"{diff.old_mc_version} → {diff.new_mc_version}"
        )


# ---- list ---------------------------------------------------------------

def run_list_flow(console: Console, env: EnvironmentSummary):
    render_op_intro(
        console, "List snapshots",
        "Show every world snapshot and log snapshot currently in the "
        "vault, newest first.",
        expects="Just the vault path — no source data needed.",
    )
    repo = _pick_or_create_repo(console, env)
    snaps = repo.list()
    log_snaps = repo.list_log_snapshots()
    console.print(f"\n[bold]world snapshots ({len(snaps)}):[/bold]")
    for s in snaps[:50]:
        console.print(f"  {s.short_id}  {s.timestamp.isoformat()}  "
                      f"{s.label or '-':25}  world={s.world_name}  "
                      f"mc={s.mc_version or '?'}")
    if len(snaps) > 50:
        console.print(f"  … (+{len(snaps) - 50} more)")
    console.print(f"\n[bold]log snapshots ({len(log_snaps)}):[/bold]")
    for s in log_snaps[:50]:
        console.print(f"  {s.short_id}  {s.timestamp.isoformat()}  "
                      f"{s.label or '-':25}  servers={s.server_count}  "
                      f"files={s.file_count}")


# ---- verify -------------------------------------------------------------

def run_verify_flow(console: Console, env: EnvironmentSummary):
    render_op_intro(
        console, "Verify integrity",
        "Walk every blob in the vault and re-hash it; flag any blob "
        "whose contents don't match its file name (= silent disk bit-rot). "
        "Also cross-checks that every chunk referenced by a manifest "
        "actually exists on disk. Slow (proportional to vault size) but "
        "the gold standard for 'is my backup still healthy?'.",
        expects="Just the vault path.",
    )
    repo = _pick_or_create_repo(console, env)
    repair = confirm(console, "Repair (delete) corrupt blobs?", default=False)
    progress = make_progress(console)
    with progress:
        task = progress.add_task("verify: idle", total=None)
        cb = _make_phase_cb(progress, task, prefix="verify")
        report = repo.verify(repair=repair, progress_cb=cb)
    console.print(
        f"chunks: ok={report.ok_chunks} corrupt={report.corrupt_chunks}\n"
        f"files:  ok={report.ok_files} corrupt={report.corrupt_files}\n"
        f"missing-referenced: {report.missing_referenced}\n"
        f"orphan blobs (gc to reclaim): {report.orphan_blobs}\n"
        f"repaired: {report.repaired}"
    )


# ---- gc -----------------------------------------------------------------

def run_gc_flow(console: Console, env: EnvironmentSummary):
    render_op_intro(
        console, "Garbage-collect",
        "Reclaim disk space from blobs that no remaining snapshot "
        "references. Run this after [bold]Delete[/bold] (CLI only — "
        "wizard doesn't let you delete) or after fsck removes orphan "
        "manifests.",
        expects="Just the vault path.",
    )
    repo = _pick_or_create_repo(console, env)
    if not confirm(console, "Run gc now?", default=True):
        return
    result = repo.gc()
    console.print(
        f"[green]gc done[/green]  "
        f"chunks={result.chunks} files={result.files} logs={result.logs}"
    )


def run_fsck_flow(console: Console, env: EnvironmentSummary):
    render_op_intro(
        console, "Fsck (repair)",
        "Reconcile the vault's on-disk state with the index. Fixes "
        "half-written snapshots left behind by Ctrl-C / kill / power-loss: "
        "deletes orphan manifests, reverses dangling refs, removes "
        "stray .tmp files. Idempotent and safe to run any time.",
        expects="Just the vault path.",
    )
    repo = _pick_or_create_repo(console, env)
    dry_run = confirm(console, "Dry-run only (just report, don't fix)?",
                      default=False)
    report = repo.fsck(repair=not dry_run)
    if report.clean:
        console.print("[green]fsck: clean — no issues found[/green]")
        return
    console.print(f"[yellow]{report.summary()}[/yellow]")
    for path in report.orphan_manifests[:10]:
        console.print(f"  orphan manifest: {path}")
    for snap_id in report.dangling_rows[:10]:
        console.print(f"  dangling row: {snap_id}")
    for path in report.stray_temp_files[:10]:
        console.print(f"  stray .tmp file: {path}")


def run_repair_timestamps_flow(console: Console, env: EnvironmentSummary):
    """Vault-wide timestamp/label realignment + dedup using level.dat as the
    authoritative source. Two-stage: dry-run report → confirm → apply."""
    from datetime import datetime, timezone

    render_op_intro(
        console, "Repair timestamps",
        "Align every snapshot's timestamp + label with its manifest's "
        "level.dat LastPlayed (the authoritative save time). Detects and "
        "dedupes snapshots created by the historical fallback-to-now() "
        "ingest bug. Always shows a dry-run plan first; you confirm "
        "before anything is modified.",
        expects="Just the vault path.",
    )
    repo = _pick_or_create_repo(console, env)

    console.print("[dim]scanning vault…[/dim]")
    dry = repo.repair_timestamps(dry_run=True)
    console.print(f"[bold]{dry.summary()}[/bold]")

    if dry.no_last_played:
        console.print(
            f"[yellow]{len(dry.no_last_played)} snapshot(s) lack LastPlayed "
            f"in their manifest — these can NOT be auto-repaired and will "
            f"be left alone:[/yellow]"
        )
        for sid, label in dry.no_last_played[:10]:
            console.print(f"  {sid[:12]}  {label or '-'}")
        if len(dry.no_last_played) > 10:
            console.print(f"  ... ({len(dry.no_last_played) - 10} more)")

    if dry.unreadable:
        console.print(
            f"[red]{len(dry.unreadable)} manifest(s) unreadable:[/red]"
        )
        for sid, err in dry.unreadable[:5]:
            console.print(f"  {sid[:12]}: {err}")

    if dry.duplicate_groups:
        console.print(
            f"\n[bold]{len(dry.duplicate_groups)} duplicate group(s)[/bold] "
            f"(snapshots that should collapse into one):"
        )
        for g in dry.duplicate_groups[:10]:
            ts_iso = datetime.fromtimestamp(
                g.target_ts_ms / 1000, tz=timezone.utc,
            ).isoformat()
            console.print(f"  [cyan]{g.world_name}[/cyan] @ {ts_iso}")
            console.print(f"    keep:   {g.winner_id[:12]}")
            for lid in g.loser_ids:
                console.print(f"    delete: {lid[:12]}")
        if len(dry.duplicate_groups) > 10:
            console.print(
                f"  ... ({len(dry.duplicate_groups) - 10} more groups)"
            )

    if dry.to_retime:
        console.print(
            f"\n[bold]{len(dry.to_retime)} snapshot(s) will be retimed[/bold] "
            f"(showing first 10):"
        )
        for plan in dry.to_retime[:10]:
            old_iso = datetime.fromtimestamp(
                plan.old_ts_ms / 1000, tz=timezone.utc,
            ).isoformat()
            new_iso = datetime.fromtimestamp(
                plan.new_ts_ms / 1000, tz=timezone.utc,
            ).isoformat()
            label_part = (
                f"  [dim]label: {plan.old_label!r} → {plan.new_label!r}[/dim]"
                if plan.new_label != plan.old_label else ""
            )
            console.print(f"  {plan.snap_id[:12]}  {old_iso} → {new_iso}{label_part}")
        if len(dry.to_retime) > 10:
            console.print(f"  ... ({len(dry.to_retime) - 10} more)")

    if not (dry.to_retime or dry.to_delete):
        console.print("[green]vault is clean — nothing to do.[/green]")
        return

    console.print()
    console.print(
        "[yellow bold]This will modify the vault.[/yellow bold] "
        "Duplicates will be deleted; survivors retimed. The chunk pool "
        "is content-addressed so no chunk data is lost — only stale "
        "manifest+index rows."
    )
    if not confirm(console, "Apply the plan above?", default=False):
        console.print("[dim]cancelled — vault unchanged.[/dim]")
        return

    result = repo.repair_timestamps(dry_run=False)
    console.print(f"[green]{result.summary()}[/green]")
    if result.errors:
        console.print(
            f"[red]{len(result.errors)} error(s) during apply:[/red]"
        )
        for op, sid, msg in result.errors[:10]:
            console.print(f"  [{op}] {sid[:12]}: {msg}")


def run_restore_flow(console: Console, env: EnvironmentSummary):
    """Materialize a snapshot back to a directory on disk."""
    render_op_intro(
        console, "Restore a snapshot",
        "Pick a snapshot and reassemble its world tree to a destination "
        "directory. Optionally restore only specific files (by manifest "
        "relative path). The chunk pool is read-only — restore never "
        "alters vault state.",
        expects="The vault path, a snapshot to restore, and a destination dir.",
    )
    repo = _pick_or_create_repo(console, env)
    snap = _pick_snapshot(console, repo, prompt="Snapshot to restore")
    if snap is None:
        return
    dest = prompt_path(console, "Destination directory", default=Path.cwd() / "restored")
    if dest.exists() and any(dest.iterdir()):
        if not confirm(
            console, f"{dest} is not empty — write into it anyway?",
            default=False,
        ):
            console.print("[dim]cancelled.[/dim]")
            return
    progress = make_progress(console)
    with progress:
        task = progress.add_task("restore", total=None)
        cb = _make_phase_cb(progress, task, prefix="phase")
        repo.restore(snap, dest, progress_cb=cb)
    console.print(f"[green]restored[/green] {snap.short_id} → {dest}")


def run_delete_flow(console: Console, env: EnvironmentSummary):
    """Remove a snapshot from the vault. Chunks become eligible for gc."""
    render_op_intro(
        console, "Delete a snapshot",
        "Removes the snapshot's index row + manifest. Referenced chunks "
        "have their ref counts decremented; chunks no longer referenced "
        "by anything become eligible for garbage-collect (run gc to "
        "actually reclaim their disk space).",
        expects="The vault path and the snapshot to remove.",
    )
    repo = _pick_or_create_repo(console, env)
    snap = _pick_snapshot(console, repo, prompt="Snapshot to delete")
    if snap is None:
        return
    if not confirm(
        console,
        f"DELETE snapshot {snap.short_id} ({snap.label or '-'})? This "
        f"cannot be undone (but the chunk pool is content-addressed, so "
        f"the same content can be re-ingested without data loss).",
        default=False,
    ):
        console.print("[dim]cancelled.[/dim]")
        return
    repo.delete(snap)
    console.print(f"[green]deleted[/green] {snap.short_id}")


def run_retime_flow(console: Console, env: EnvironmentSummary):
    """Reassign one snapshot's timeline timestamp."""
    render_op_intro(
        console, "Retime a snapshot",
        "Reassign one snapshot's timeline timestamp. Either re-derive it "
        "from the manifest's level.dat LastPlayed (recommended), or pass "
        "an explicit ISO8601 datetime. The original timestamp is "
        "preserved as an audit field. For vault-wide repair, use "
        "[bold]Repair timestamps[/bold] instead.",
        expects="The vault, a snapshot, and either nothing (auto) or a new ts.",
    )
    repo = _pick_or_create_repo(console, env)
    snap = _pick_snapshot(console, repo, prompt="Snapshot to retime")
    if snap is None:
        return
    use_level_dat = confirm(
        console, "Re-derive ts from manifest's level.dat LastPlayed?",
        default=True,
    )
    if use_level_dat:
        updated, source = repo.retime_snapshot_from_manifest(snap)
        if source == "no_last_played":
            console.print(
                f"[yellow]manifest has no LastPlayed — snapshot unchanged.[/yellow]"
            )
            return
        console.print(
            f"[green]retimed[/green] {updated.short_id} → "
            f"{updated.timestamp.isoformat()} [from level.dat]"
        )
        return
    raw = prompt_path(
        console, "New timestamp (ISO8601, e.g. 2024-06-15T10:30:00)",
        default=Path(snap.timestamp.isoformat()),
    )
    try:
        new_ts = datetime.fromisoformat(str(raw))
    except ValueError as e:
        console.print(f"[red]invalid timestamp: {e}[/red]")
        return
    if new_ts.tzinfo is None:
        new_ts = new_ts.replace(tzinfo=timezone.utc)
    updated = repo.retime_snapshot(snap, new_ts)
    console.print(
        f"[green]retimed[/green] {updated.short_id} → {updated.timestamp.isoformat()}"
    )


def run_thumbnail_flow(console: Console, env: EnvironmentSummary):
    """Render thumbnail tiles for one snapshot or all snapshots."""
    render_op_intro(
        console, "Render thumbnails",
        "Render or backfill the small per-chunk PNG tiles used by the "
        "browser and diff visualization. Idempotent — already-rendered "
        "tiles are skipped via cache hit.",
        expects="The vault and either one snapshot or 'all snapshots'.",
    )
    repo = _pick_or_create_repo(console, env)
    do_all = confirm(console, "Render thumbnails for ALL snapshots?", default=False)
    from ..viz.snapshot_render import (
        ensure_tiles_for_manifest, write_snapshot_sidecars,
    )
    from ..store.manifest import read_manifest

    targets = repo.list() if do_all else [
        s for s in [_pick_snapshot(console, repo, prompt="Snapshot")] if s is not None
    ]
    if not targets:
        return
    progress = make_progress(console)
    with progress:
        task = progress.add_task("render", total=None)
        cb = _make_phase_cb(progress, task, prefix="phase")
        for snap in targets:
            manifest = read_manifest(snap.manifest_path)
            ensure_tiles_for_manifest(repo, manifest, progress_cb=cb)
            write_snapshot_sidecars(repo, snap.id, manifest, progress_cb=cb)
    console.print(f"[green]done[/green] — processed {len(targets)} snapshot(s)")


def run_browse_flow(console: Console, env: EnvironmentSummary):
    """Start the local Leaflet browser for the vault."""
    render_op_intro(
        console, "Browse vault",
        "Spin up a local HTTP server with a Leaflet-based map browser "
        "for the vault. Open the printed URL in your browser. Press "
        "Ctrl-C in this terminal to stop the server.",
        expects="The vault path. Optional: bind host + port.",
    )
    repo = _pick_or_create_repo(console, env)
    host = "127.0.0.1"
    port = 8765
    if confirm(console, f"Bind to {host}:{port}?", default=True):
        pass
    else:
        raw_host = prompt_path(console, "Host (use 0.0.0.0 to expose on LAN)",
                               default=Path(host))
        host = str(raw_host)
        raw_port = prompt_path(console, "Port", default=Path(str(port)))
        try:
            port = int(str(raw_port))
        except ValueError:
            console.print("[red]invalid port — falling back to 8765[/red]")
            port = 8765
    from ..viz.browser import serve
    try:
        serve(repo.repo_path, host=host, port=port)
    except KeyboardInterrupt:
        console.print("\n[dim]server stopped.[/dim]")


def run_verify_roundtrip_flow(console: Console, env: EnvironmentSummary):
    """Compare a snapshot against a known-good source tree byte-for-byte."""
    render_op_intro(
        console, "Verify round-trip",
        "Restore a snapshot to a temp dir and compare against a known-"
        "good source tree (the original world or extracted archive). "
        "The snapshot passes only if every chunk + every file matches "
        "byte-for-byte.",
        expects="The vault, a snapshot, and the path to a source world dir.",
    )
    repo = _pick_or_create_repo(console, env)
    snap = _pick_snapshot(console, repo, prompt="Snapshot to verify")
    if snap is None:
        return
    source = prompt_path(console, "Original world directory", default=Path.cwd())
    from ..store.roundtrip import verify_roundtrip
    progress = make_progress(console)
    with progress:
        task = progress.add_task("verify", total=None)
        cb = _make_phase_cb(progress, task, prefix="phase")
        report = verify_roundtrip(repo, snap, source, progress_cb=cb)
    color = "green" if report.passed else "red"
    console.print(f"[{color}]{report.summary()}[/{color}]")


def run_verify_folders_flow(console: Console, env: EnvironmentSummary):
    """Compare two arbitrary directory trees byte-for-byte."""
    render_op_intro(
        console, "Verify two folders",
        "Compare two directory trees (e.g. a restored snapshot vs a "
        "manually extracted archive) chunk-by-chunk for region files "
        "and byte-by-byte for everything else. No vault required.",
        expects="Two directory paths. Optional: a path for a written report.",
    )
    a = prompt_path(console, "Left directory (treat as source)", default=Path.cwd())
    b = prompt_path(console, "Right directory (treat as restore)", default=Path.cwd())
    write_report = confirm(
        console, "Write a detailed text report to file?", default=False,
    )
    report_path = None
    if write_report:
        report_path = prompt_path(
            console, "Report path",
            default=Path.cwd() / "verify-folders-report.txt",
        )
    from ..store.roundtrip import compare_directories
    progress = make_progress(console)
    with progress:
        task = progress.add_task("compare", total=None)
        cb = _make_phase_cb(progress, task, prefix="phase")
        report = compare_directories(a, b, progress_cb=cb)
    color = "green" if report.passed else "red"
    console.print(f"[{color}]{report.summary()}[/{color}]")
    if report_path is not None:
        from ..cli import _write_verify_folders_report
        _write_verify_folders_report(report_path, a, b, report)
        console.print(f"[dim]detailed report: {report_path}[/dim]")


def run_logs_flow(console: Console, env: EnvironmentSummary):
    """Sub-menu for the parallel log-snapshot subsystem."""
    render_op_intro(
        console, "Logs subsystem",
        "Logs and crash-reports captured during ingest live in a "
        "parallel deduplicated pool, separate from world snapshots. "
        "Browse / extract / delete them here without touching world data.",
        expects="The vault, then an action: list / extract / delete.",
    )
    repo = _pick_or_create_repo(console, env)
    action = choose_one(
        console, "What to do with logs?",
        [
            "list   │ show every log snapshot",
            "extract │ materialize a log snapshot to a directory",
            "delete │ remove a log snapshot",
            "back",
        ],
    )
    if action.startswith("back"):
        return
    if action.startswith("list"):
        snaps = repo.list_log_snapshots()
        if not snaps:
            console.print("[yellow](no log snapshots)[/yellow]")
            return
        for s in snaps:
            console.print(
                f"{s.short_id}  {s.timestamp.isoformat()}  "
                f"{(s.label or '-'):26}  "
                f"servers={s.server_count} files={s.file_count}"
            )
        return
    if action.startswith("extract"):
        snap = _pick_log_snapshot(console, repo, prompt="Log snapshot to extract")
        if snap is None:
            return
        dest = prompt_path(
            console, "Destination directory",
            default=Path.cwd() / "logs-extracted",
        )
        from ..store.log_manifest import read_log_manifest
        servers = list(read_log_manifest(snap.manifest_path).servers)
        wanted: str | None = None
        if servers and len(servers) > 1:
            if confirm(console, "Filter to one server only?", default=False):
                wanted = choose_one(console, "Server", servers)
        repo.extract_logs(snap, dest, server=wanted)
        console.print(f"[green]extracted[/green] → {dest}")
        return
    if action.startswith("delete"):
        snap = _pick_log_snapshot(console, repo, prompt="Log snapshot to delete")
        if snap is None:
            return
        if not confirm(
            console, f"DELETE log snapshot {snap.short_id}?", default=False,
        ):
            console.print("[dim]cancelled.[/dim]")
            return
        repo.delete_log_snapshot(snap)
        console.print(f"[green]deleted log snapshot[/green] {snap.short_id}")


def run_language_flow(console: Console, env: EnvironmentSummary):
    """Pick a UI language. Persists to ~/.chunkvault/config.json."""
    from . import i18n

    current = i18n.get_locale_info(i18n.get_locale())
    current_label = current.native_name if current else i18n.get_locale()
    console.print(i18n.t("lang.current", locale=current_label))

    locales = i18n.list_locales()
    options: list[str] = []
    for L in locales:
        marker = " *" if L.tag == i18n.get_locale() else ""
        review = " [yellow](review needed)[/yellow]" if L.review_needed else ""
        # rich markup is preserved when questionary prints back, but the
        # selection value matches the visible label string.
        # Strip markup for the picker; we only use it for status hints.
        pretty = f"{L.native_name}  [{L.tag}]{marker}"
        options.append(pretty)

    pick = choose_one(console, i18n.t("lang.choose"), options)
    if pick is None:
        return
    idx = options.index(pick)
    chosen = locales[idx]
    saved_path = i18n.set_locale(chosen.tag)
    if saved_path is None:
        console.print(
            f"[yellow]language switched to {chosen.native_name} for this "
            f"session, but could not write the config file.[/yellow]"
        )
    else:
        console.print(i18n.t(
            "lang.changed", locale=chosen.native_name, path=saved_path,
        ))
