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
)


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

    while True:
        choice = main_menu(console)
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
            elif choice == "g":
                run_gc_flow(console, env)
        except KeyboardInterrupt:
            console.print("\n[yellow]cancelled.[/yellow]")
        except Exception as e:
            console.print(f"[red]error:[/red] {e}")
        # Refresh the env summary after each operation so future menu trips
        # see updated counts.
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
    show_detail = min(10, len(archives))
    for i, archive in enumerate(archives):
        prev = preview_archive(archive)
        previews.append(prev)
        if i < show_detail:
            render_archive_preview(console, prev)
    if len(archives) > show_detail:
        console.print(
            f"[dim]… (+{len(archives) - show_detail} more archives, "
            f"detail omitted)[/dim]"
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

    if not confirm(console, "Proceed?", default=True):
        console.print("[yellow]cancelled.[/yellow]")
        return []

    results: list[IngestResult] = []
    verify_failures: list[tuple[str, str]] = []     # (archive_name, reason)
    progress = make_progress(console)
    from ..store.repo import RoundTripVerificationError
    with progress:
        archive_task = progress.add_task("archives", total=len(archives))
        for archive in archives:
            progress.update(archive_task, description=f"archives: {archive.name}")
            try:
                result = ingest_archive(
                    repo, archive,
                    skip_logs=skip_logs,
                    verify_roundtrip=verify_after,
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

    # Summary
    snaps = sum(len(r.snapshots) for r in results)
    log_snaps = sum(1 for r in results if r.log_snapshot is not None)
    console.print(
        f"\n[green]done.[/green] {snaps} world snapshots, {log_snaps} log snapshots, "
        f"{len(results)}/{len(archives)} archives ingested cleanly."
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
    repo = _pick_or_create_repo(console, env)
    world = prompt_path(console, "World directory", must_exist=True)
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

        def cb(e: ProgressEvent):
            if e.kind == "phase_start":
                progress.update(task, description=f"snapshot: {e.label}", total=e.total or None)
            elif e.kind == "phase_progress":
                progress.update(task, completed=e.current, total=e.total or None)

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
    repo = _pick_or_create_repo(console, env)
    repair = confirm(console, "Repair (delete) corrupt blobs?", default=False)
    console.print("[dim]verifying…[/dim]")
    report = repo.verify(repair=repair)
    console.print(
        f"chunks: ok={report.ok_chunks} corrupt={report.corrupt_chunks}\n"
        f"files:  ok={report.ok_files} corrupt={report.corrupt_files}\n"
        f"missing-referenced: {report.missing_referenced}\n"
        f"orphan blobs (gc to reclaim): {report.orphan_blobs}\n"
        f"repaired: {report.repaired}"
    )


# ---- gc -----------------------------------------------------------------

def run_gc_flow(console: Console, env: EnvironmentSummary):
    repo = _pick_or_create_repo(console, env)
    if not confirm(console, "Run gc now?", default=True):
        return
    result = repo.gc()
    console.print(
        f"[green]gc done[/green]  "
        f"chunks={result.chunks} files={result.files} logs={result.logs}"
    )
