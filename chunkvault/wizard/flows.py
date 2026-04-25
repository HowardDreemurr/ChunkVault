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
    console = console or make_console()
    console.print("[bold magenta]chunkvault[/bold magenta] interactive wizard")
    cwd = Path.cwd()
    console.print(
        f"[dim]scanning {cwd} (depth ≤ 3, skipping system dirs) "
        f"for repos and archives…[/dim]"
    )

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
            console.print("[dim]bye.[/dim]")
            return 0
        try:
            if choice == "i":
                run_ingest_flow(console, env)
            elif choice == "s":
                run_snapshot_flow(console, env)
            elif choice == "d":
                run_diff_flow(console, env)
            elif choice == "l":
                run_list_flow(console, env)
            elif choice == "v":
                run_verify_flow(console, env)
            elif choice == "f":
                run_fsck_flow(console, env)
            elif choice == "g":
                run_gc_flow(console, env)
        except KeyboardInterrupt:
            console.print("\n[yellow]cancelled.[/yellow]")
        except Exception as e:
            console.print(f"[red]error:[/red] {e}")
        env = detect_environment()


# ---- repo helper ----------------------------------------------------------

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
