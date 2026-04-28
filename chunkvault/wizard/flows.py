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
from .._timefmt import format_local as _fmt_ts
from .detect import EnvironmentSummary, detect_environment, summarize_repo
from .i18n import t
from .ui import (
    _build_health_submenu,
    _build_repair_submenu,
    _build_settings_submenu,
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
    submenu,
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
            f"[yellow]{_t('msg.nothing_auto_detected')}[/yellow]\n"
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
            # Top-level direct actions
            if choice == "i":
                run_ingest_flow(console, env)
            elif choice == "s":
                run_snapshot_flow(console, env)
            elif choice == "l":
                run_list_flow(console, env)
            elif choice == "x":
                run_restore_flow(console, env)
            elif choice == "d":
                run_diff_flow(console, env)
            elif choice == "b":
                run_browse_flow(console, env)
            elif choice == "L":
                run_logs_flow(console, env)
            # Submenu groups
            elif choice == "H":
                _dispatch_health_submenu(console, env)
            elif choice == "R":
                _dispatch_repair_submenu(console, env)
            elif choice == "S":
                _dispatch_settings_submenu(console, env)
        except KeyboardInterrupt:
            console.print(f"\n[yellow]{_t('prompt.cancel')}[/yellow]")
        except Exception as e:
            console.print(f"[red]{_t('prompt.error')}[/red] {e}")
        # Refresh env between iterations so newly-created vaults / changed
        # source paths show up in the next menu cycle. Wrapped in its own
        # try because detect_environment touches index.sqlite + scans the
        # filesystem; any transient error here (lock, AV scan, perm) used
        # to kill the wizard. Now we keep the previous env on failure.
        try:
            env = detect_environment()
        except Exception as e:
            console.print(
                f"[dim red]{_t('msg.env_refresh_failed', err=e)}[/dim red]"
            )


# ---- repo helper ----------------------------------------------------------

def _pick_snapshot(
    console: Console, repo: ChunkSnapshotRepo, *,
    prompt: str | None = None,
):
    prompt = prompt if prompt is not None else t("prompt.pick_snapshot")
    """Show all snapshots in the vault and let the user pick one.

    Returns the ChunkSnapshot, or None if the vault is empty / user
    cancelled. The displayed labels are short (id-prefix + ts + label)
    so even a long list scrolls cleanly in the questionary picker.
    """
    snaps = repo.list()
    if not snaps:
        console.print(f"[yellow]{t('msg.empty_vault')}[/yellow]")
        return None
    options = [
        f"{s.short_id}  {_fmt_ts(s.timestamp)}  {(s.label or '-')[:40]}"
        for s in snaps
    ]
    pick = choose_one(console, prompt, options)
    if pick is None:
        return None
    idx = options.index(pick)
    return snaps[idx]


def _pick_log_snapshot(
    console: Console, repo: ChunkSnapshotRepo, *,
    prompt: str | None = None,
):
    """Same as :func:`_pick_snapshot` but for log snapshots."""
    prompt = prompt if prompt is not None else t("prompt.pick_log_snapshot")
    snaps = repo.list_log_snapshots()
    if not snaps:
        console.print(f"[yellow]{t('msg.empty_log_snaps')}[/yellow]")
        return None
    options = [
        f"{s.short_id}  {_fmt_ts(s.timestamp)}  {(s.label or '-'):26}  "
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
    repo_path = prompt_path(console, t("prompt.repo_path"), default=default)
    repo = ChunkSnapshotRepo(repo_path)
    if not repo.is_initialized():
        if not confirm(console, t("prompt.create_repo", path=repo_path), default=True):
            raise RuntimeError(t("msg.aborted_repo_not_initialized"))
        repo.init()
        console.print(f"[green]{t('msg.initialized_at', path=repo_path)}[/green]")
    return repo


# ---- ingest --------------------------------------------------------------

def run_ingest_flow(
    console: Console, env: EnvironmentSummary,
) -> list[IngestResult]:
    """Bulk-import backup archives — the user's main use case."""
    render_op_intro(
        console, t("flow.ingest.title"),
        t("flow.ingest.body"),
        expects=t("flow.ingest.expects"),
        example=t("flow.ingest.example"),
    )

    # New: ask whether each server gets its own vault. Default yes — for
    # multi-server archives that's the cleaner mental model (one vault per
    # logical world, no cross-server chunk pool entanglement). Picking no
    # falls back to the original single-vault behaviour.
    per_server = confirm(
        console, t("prompt.per_server_vaults"), default=True,
    )

    repo = None if per_server else _pick_or_create_repo(console, env)

    # Collect candidate source paths
    candidates: list[Path] = []
    for s in env.source_paths:
        if confirm(
            console,
            t("prompt.use_source", path=s.path, count=s.archive_count),
            default=True,
        ):
            candidates.append(s.path)
    while True:
        if confirm(console, t("prompt.add_source"), default=False):
            extra = prompt_path(console, t("prompt.extra_source"), must_exist=True)
            candidates.append(extra)
        else:
            break
    if not candidates:
        console.print(f"[yellow]{t('msg.no_source_paths')}[/yellow]")
        return []

    # Enumerate archives
    archives: list[Path] = []
    for c in candidates:
        archives.extend(iter_archives(c))
    if not archives:
        console.print(f"[yellow]{t('msg.no_archives')}[/yellow]")
        return []

    skip_logs = not confirm(console, t("prompt.capture_logs"), default=True)
    verify_after = confirm(console, t("prompt.verify_each"), default=True)
    if not verify_after:
        console.print(f"[yellow]{t('msg.verification_skipped')}[/yellow]")

    # Inspect each archive — show servers, regions, logs INSIDE, not just file size.
    console.print(f"\n[bold]{t('msg.inspecting_count', count=len(archives))}[/bold]")
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
        console.print(f"[dim]{t('msg.compact_view_hint')}[/dim]")

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
        f"\n[bold]{t('msg.total_across_archives', count=len(archives))}[/bold]\n"
        f"{t('msg.total.servers', n=total_servers)}\n"
        f"{t('msg.total.regions', n=total_regions)}\n"
        f"{t('msg.total.logs', n=total_logs)}\n"
        f"{t('msg.total.world_bytes', n=fmt_bytes(total_world_bytes))}"
    )
    if failed:
        console.print(f"  [red]{t('msg.total.unreadable', n=len(failed))}[/red]")

    # Per-archive selection: user can uncheck individual archives instead of
    # the all-or-nothing Proceed? prompt. Pre-checks "clean" archives (every
    # detected server has regions); leaves problematic ones unchecked so they
    # require an explicit decision.
    selected = select_archives(archives, previews)
    if not selected:
        console.print(f"[yellow]{t('msg.nothing_selected')}[/yellow]")
        return []
    selected_set = {a for a in selected}
    archives = [a for a in archives if a in selected_set]
    # Keep previews aligned with the (now-filtered) archives list.
    selected_previews = [p for p in previews if p.path in selected_set]
    console.print(f"[dim]{t('msg.ingesting_count', count=len(archives))}[/dim]")

    if per_server:
        return _dispatch_per_server_vaults(
            console, env, archives, selected_previews,
            skip_logs=skip_logs, verify_after=verify_after,
        )

    # ── single-vault path (existing behaviour) ─────────────────────────
    results: list[IngestResult] = []
    verify_failures: list[tuple[str, str]] = []     # (archive_name, reason)
    progress = make_progress(console)
    from ..store.repo import RoundTripVerificationError
    with progress:
        archive_task = progress.add_task(t("task.archives"), total=len(archives))
        # A second task that follows the active phase inside the current
        # archive (regions/files/verify). Without this the outer bar sits at
        # 0/N for the entire first archive — a single 2 GB zip can take 10+
        # minutes, and a frozen-looking progress bar makes users think
        # the wizard hung.
        phase_task = progress.add_task(t("task.phase_idle"), total=None)

        def make_cb():
            return _make_phase_cb(progress, phase_task, prefix="phase")

        for archive in archives:
            progress.update(archive_task,
                            description=t("task.archives_named", name=archive.name))
            progress.update(
                phase_task,
                description=t("task.phase_opening", name=archive.name),
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
                    f"[red]{t('msg.verify_failed_inline')}[/red] {archive.name}: "
                    f"{e.report.summary()}"
                )
            except ImportError as e:
                console.print(f"[red]{t('msg.tag.skip')}[/red] {archive.name}: {e}")
            except Exception as e:
                console.print(f"[red]{t('msg.tag.error')}[/red] {archive.name}: {e}")
            progress.advance(archive_task, 1)
        progress.update(phase_task, description=t("task.phase_done"), completed=1, total=1)

    # Summary
    snaps = sum(len(r.snapshots) for r in results)
    log_snaps = sum(1 for r in results if r.log_snapshot is not None)
    already = sum(len(r.already_ingested) for r in results)
    console.print(
        f"\n[green]{t('msg.done_summary', snaps=snaps, logs=log_snaps, clean=len(results), total=len(archives))}[/green]"
    )
    if already:
        console.print(f"[dim]{t('msg.already_in_vault', count=already)}[/dim]")
    if verify_failures:
        console.print(
            f"[red]{t('msg.verify_failed_count', count=len(verify_failures))}[/red]"
        )
        for name, summary in verify_failures[:10]:
            console.print(f"  {name}: {summary}")
    return results


def _find_vault_for_server(server_name: str) -> Path | None:
    """Look up a registered vault that should hold this server's data.

    Match priority:
      1. Vault whose label exactly equals server_name
         (set by the wizard when auto-creating a per-server vault)
      2. Vault whose path basename equals server_name
         (catches manually-created `F:\\Vaults\\EX-Server\\` style)
    Returns None if no registered vault fits.
    """
    from . import config as _cfg
    repos = _cfg.list_repos()
    for r in repos:
        if r.label == server_name:
            return r.path
    for r in repos:
        if r.path.name == server_name:
            return r.path
    return None


def _suggest_vault_path_for(server_name: str, env: EnvironmentSummary) -> Path:
    """Pick a sensible default location for a new per-server vault.

    Preference order:
      1. <existing_vault.parent>/<server_name>  — same parent as any
         already-registered vault, so per-server vaults sit together
      2. <cwd>/Vaults/<server_name>             — fresh-start fallback
    """
    from . import config as _cfg
    for r in _cfg.list_repos():
        if r.path.is_dir() and r.path.parent.is_dir():
            return r.path.parent / server_name
    return Path.cwd() / "Vaults" / server_name


def _dispatch_per_server_vaults(
    console: Console, env: EnvironmentSummary,
    archives: list[Path], previews: list,
    *, skip_logs: bool, verify_after: bool,
) -> list[IngestResult]:
    """Each server in the selected archives gets its own dedicated vault.

    UX: aggregate the unique server names from the previews, match them
    against the registered-vault list (label match → basename match),
    prompt the user to create a new vault for any unmatched server.
    Then ingest each archive ONCE (single extraction), routing each
    server's data to its assigned vault.
    """
    from ..store.ingest import ingest_archive_per_server_vaults
    from . import config as _cfg

    # Aggregate unique server names across previews
    unique_servers: set[str] = set()
    for p in previews:
        if p.error is None:
            for s in p.servers:
                unique_servers.update([s.name])

    if not unique_servers:
        console.print(f"[yellow]{t('per_server.no_servers')}[/yellow]")
        return []

    console.print(
        f"\n[bold]{t('per_server.found_servers', n=len(unique_servers), archives=len(archives))}[/bold]"
    )

    # Resolve each server to a vault: registered match, or prompt-create.
    server_to_repo: dict[str, ChunkSnapshotRepo] = {}
    for server_name in sorted(unique_servers):
        existing = _find_vault_for_server(server_name)
        if existing is not None:
            console.print(
                f"  [green]✓[/green] {server_name} → [dim]{existing}[/dim]"
            )
            try:
                repo = ChunkSnapshotRepo(existing)
                if not repo.is_initialized():
                    console.print(
                        f"    [yellow]{t('per_server.vault_not_init')}[/yellow]"
                    )
                    repo.init()
                server_to_repo[server_name] = repo
            except Exception as e:
                console.print(f"    [red]{t('per_server.vault_open_error', err=e)}[/red]")
            continue

        # Unmatched — offer to create
        suggested = _suggest_vault_path_for(server_name, env)
        console.print(
            f"  [yellow]?[/yellow] {server_name}: "
            f"{t('per_server.no_match_prompt', path=suggested)}"
        )
        if not confirm(
            console, t("per_server.create_vault", server=server_name),
            default=True,
        ):
            console.print(
                f"    [dim]{t('per_server.skip_server', server=server_name)}[/dim]"
            )
            continue
        # Allow the user to override the suggested path
        chosen = prompt_path(
            console, t("per_server.vault_path_prompt", server=server_name),
            default=suggested, must_exist=False,
        )
        repo = ChunkSnapshotRepo(chosen)
        if not repo.is_initialized():
            repo.init()
        _cfg.add_repo(chosen, label=server_name)
        console.print(
            f"    [green]{t('per_server.created', path=chosen)}[/green]"
        )
        server_to_repo[server_name] = repo

    if not server_to_repo:
        console.print(f"[yellow]{t('per_server.no_vaults')}[/yellow]")
        return []

    def vault_resolver(server_name: str) -> ChunkSnapshotRepo | None:
        return server_to_repo.get(server_name)

    # Now do the ingest
    all_results: list[IngestResult] = []
    progress = make_progress(console)
    with progress:
        archive_task = progress.add_task(t("task.archives"), total=len(archives))
        phase_task = progress.add_task(t("task.phase_idle"), total=None)
        cb = _make_phase_cb(progress, phase_task, prefix="phase")

        for archive in archives:
            progress.update(archive_task,
                            description=t("task.archives_named", name=archive.name))
            try:
                results_by_server = ingest_archive_per_server_vaults(
                    archive, vault_resolver,
                    skip_logs=skip_logs,
                    verify_roundtrip=verify_after,
                    progress_cb=cb,
                )
                all_results.extend(results_by_server.values())
            except Exception as e:
                console.print(f"[red]{t('msg.tag.error')}[/red] {archive.name}: {e}")
            progress.advance(archive_task, 1)

    total_snaps = sum(len(r.snapshots) for r in all_results)
    total_already = sum(len(r.already_ingested) for r in all_results)
    console.print(
        f"\n[green]{t('per_server.done_summary', snaps=total_snaps, vaults=len(server_to_repo), archives=len(archives))}[/green]"
    )
    if total_already:
        console.print(f"[dim]{t('msg.already_in_vault', count=total_already)}[/dim]")
    return all_results


# ---- single snapshot ----------------------------------------------------

def run_snapshot_flow(console: Console, env: EnvironmentSummary):
    render_op_intro(
        console, t("flow.snapshot.title"),
        t("flow.snapshot.body"),
        expects=t("flow.snapshot.expects"),
        example=t("flow.snapshot.example"),
    )
    repo = _pick_or_create_repo(console, env)
    world = prompt_path(console, t("prompt.world_dir"), must_exist=True)
    label = console.input(t("prompt.label_blank")).strip() or None
    allow_live = confirm(console, t("prompt.allow_live"), default=False)
    verify_after = confirm(console, t("prompt.verify_after"), default=True)
    progress = make_progress(console)
    from ..store.repo import RoundTripVerificationError
    with progress:
        task = progress.add_task(t("task.snapshot"), total=None)
        cb = _make_phase_cb(progress, task, prefix="snapshot")

        try:
            snap = repo.snapshot(world, label=label, allow_live=allow_live,
                                 progress_cb=cb, verify_roundtrip=verify_after)
        except RoundTripVerificationError as e:
            console.print(
                f"[red]{t('msg.snap_verify_failed', sid=e.snapshot.short_id, label=e.snapshot.label or '-')}[/red]"
            )
            console.print(f"  {e.report.summary()}")
            console.print(f"[yellow]{t('msg.snap_kept_inspect')}[/yellow]")
            return
    if verify_after:
        console.print(f"[green]{t('msg.snap_done_verified', sid=snap.short_id, label=snap.label or '-', ver=snap.mc_version or '?')}[/green]")
    else:
        console.print(f"[green]{t('msg.snap_done', sid=snap.short_id, label=snap.label or '-', ver=snap.mc_version or '?')}[/green]")


# ---- diff ---------------------------------------------------------------

def run_diff_flow(console: Console, env: EnvironmentSummary):
    render_op_intro(
        console, t("flow.diff.title"),
        t("flow.diff.body"),
        expects=t("flow.diff.expects"),
    )
    repo = _pick_or_create_repo(console, env)
    snaps = repo.list()
    if len(snaps) < 2:
        console.print(f"[yellow]{t('msg.need_two_snaps')}[/yellow]")
        return
    console.print(f"\n[bold]{t('msg.snapshots_header')}[/bold]")
    for s in snaps[:20]:
        console.print(f"  {s.short_id}  {_fmt_ts(s.timestamp)}  "
                      f"{s.label or '-':25}  ({s.world_name})")
    a = console.input(t("prompt.snap_a")).strip()
    b = console.input(t("prompt.snap_b")).strip()
    diff = repo.diff_snapshots(a, b)
    counts = diff.count_by_kind()
    console.print(
        f"\n[bold]{t('msg.diff.summary', a=a, b=b, added=counts['added'], modified=counts['modified'], removed=counts['removed'], total=len(diff.changes))}[/bold]"
    )
    if diff.version_changed():
        console.print(
            f"  [yellow]{t('msg.diff.version_change', old=diff.old_mc_version, new=diff.new_mc_version)}[/yellow]"
        )


# ---- list ---------------------------------------------------------------

def run_list_flow(console: Console, env: EnvironmentSummary):
    render_op_intro(
        console, t("flow.list.title"),
        t("flow.list.body"),
        expects=t("flow.list.expects"),
    )
    repo = _pick_or_create_repo(console, env)
    snaps = repo.list()
    log_snaps = repo.list_log_snapshots()
    console.print(f"\n[bold]{t('msg.world_snaps_header', count=len(snaps))}[/bold]")
    for s in snaps[:50]:
        console.print(f"  {s.short_id}  {_fmt_ts(s.timestamp)}  "
                      f"{s.label or '-':25}  world={s.world_name}  "
                      f"mc={s.mc_version or '?'}")
    if len(snaps) > 50:
        console.print(t("msg.more_snaps", n=len(snaps) - 50))
    console.print(f"\n[bold]{t('msg.log_snaps_header', count=len(log_snaps))}[/bold]")
    for s in log_snaps[:50]:
        console.print(f"  {s.short_id}  {_fmt_ts(s.timestamp)}  "
                      f"{s.label or '-':25}  servers={s.server_count}  "
                      f"files={s.file_count}")


# ---- verify -------------------------------------------------------------

def run_verify_flow(console: Console, env: EnvironmentSummary):
    render_op_intro(
        console, t("flow.verify.title"),
        t("flow.verify.body"),
        expects=t("flow.verify.expects"),
    )
    repo = _pick_or_create_repo(console, env)
    repair = confirm(console, t("prompt.repair_blobs"), default=False)
    progress = make_progress(console)
    with progress:
        task = progress.add_task(t("task.verify_idle"), total=None)
        cb = _make_phase_cb(progress, task, prefix="verify")
        report = repo.verify(repair=repair, progress_cb=cb)
    console.print(t(
        "msg.verify.report",
        ok_c=report.ok_chunks, cc=report.corrupt_chunks,
        ok_f=report.ok_files, cf=report.corrupt_files,
        miss=report.missing_referenced,
        orph=report.orphan_blobs,
        rep=report.repaired,
    ))


# ---- gc -----------------------------------------------------------------

def run_gc_flow(console: Console, env: EnvironmentSummary):
    render_op_intro(
        console, t("flow.gc.title"),
        t("flow.gc.body"),
        expects=t("flow.gc.expects"),
    )
    repo = _pick_or_create_repo(console, env)
    if not confirm(console, t("prompt.run_gc_now"), default=True):
        return
    result = repo.gc()
    console.print(
        f"[green]{t('msg.gc.done', c=result.chunks, f=result.files, l=result.logs)}[/green]"
    )


def run_fsck_flow(console: Console, env: EnvironmentSummary):
    render_op_intro(
        console, t("flow.fsck.title"),
        t("flow.fsck.body"),
        expects=t("flow.fsck.expects"),
    )
    repo = _pick_or_create_repo(console, env)
    dry_run = confirm(console, t("prompt.fsck_dryrun"), default=False)
    progress = make_progress(console)
    with progress:
        task = progress.add_task(t("task.fsck"), total=None)
        cb = _make_phase_cb(progress, task, prefix="fsck")
        report = repo.fsck(repair=not dry_run, progress_cb=cb)
    if report.clean:
        console.print(f"[green]{t('msg.fsck.clean')}[/green]")
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
        console, t("flow.repair_ts.title"),
        t("flow.repair_ts.body"),
        expects=t("flow.repair_ts.expects"),
    )
    repo = _pick_or_create_repo(console, env)

    # The scan reads every manifest off disk. With hundreds of snapshots
    # on a slow drive that's tens of seconds to minutes — wire a progress
    # bar so it doesn't look like the wizard hung.
    progress = make_progress(console)
    with progress:
        task = progress.add_task(t("task.repair_scanning"), total=None)
        cb = _make_phase_cb(progress, task, prefix="repair")
        dry = repo.repair_timestamps(dry_run=True, progress_cb=cb)
    console.print(f"[bold]{dry.summary()}[/bold]")

    if dry.no_last_played:
        console.print(
            f"[yellow]{t('flow.repair_ts.no_lp_warning', count=len(dry.no_last_played))}[/yellow]"
        )
        for sid, label in dry.no_last_played[:10]:
            console.print(f"  {sid[:12]}  {label or '-'}")
        if len(dry.no_last_played) > 10:
            console.print(f"  ... ({len(dry.no_last_played) - 10} more)")

    if dry.unreadable:
        console.print(
            f"[red]{t('flow.repair_ts.unreadable_warning', count=len(dry.unreadable))}[/red]"
        )
        for sid, err in dry.unreadable[:5]:
            console.print(f"  {sid[:12]}: {err}")

    if dry.duplicate_groups:
        console.print(
            f"\n[bold]{t('flow.repair_ts.dup_groups_header', count=len(dry.duplicate_groups))}[/bold]"
        )
        for g in dry.duplicate_groups[:10]:
            ts_iso = _fmt_ts(datetime.fromtimestamp(
                g.target_ts_ms / 1000, tz=timezone.utc,
            ))
            console.print(f"  [cyan]{g.world_name}[/cyan] @ {ts_iso}")
            console.print(t("flow.repair_ts.dup_keep", sid=g.winner_id[:12]))
            for lid in g.loser_ids:
                console.print(t("flow.repair_ts.dup_delete", sid=lid[:12]))
        if len(dry.duplicate_groups) > 10:
            console.print(t("flow.repair_ts.more_groups",
                            n=len(dry.duplicate_groups) - 10))

    if dry.to_retime:
        console.print(
            f"\n[bold]{t('flow.repair_ts.retime_header', count=len(dry.to_retime))}[/bold]"
        )
        for plan in dry.to_retime[:10]:
            old_iso = _fmt_ts(datetime.fromtimestamp(
                plan.old_ts_ms / 1000, tz=timezone.utc,
            ))
            new_iso = _fmt_ts(datetime.fromtimestamp(
                plan.new_ts_ms / 1000, tz=timezone.utc,
            ))
            label_part = (
                f"  [dim]label: {plan.old_label!r} → {plan.new_label!r}[/dim]"
                if plan.new_label != plan.old_label else ""
            )
            console.print(f"  {plan.snap_id[:12]}  {old_iso} → {new_iso}{label_part}")
        if len(dry.to_retime) > 10:
            console.print(t("flow.repair_ts.more_retimes",
                            n=len(dry.to_retime) - 10))

    if not (dry.to_retime or dry.to_delete):
        console.print(f"[green]{t('flow.repair_ts.clean')}[/green]")
        return

    console.print()
    console.print(
        f"[yellow bold]{t('flow.repair_ts.warning_will_modify')}[/yellow bold]"
    )
    if not confirm(console, t("flow.repair_ts.confirm_apply"), default=False):
        console.print(f"[dim]{t('flow.repair_ts.cancelled')}[/dim]")
        return

    progress = make_progress(console)
    with progress:
        task = progress.add_task(t("task.repair_applying"), total=None)
        cb = _make_phase_cb(progress, task, prefix="repair")
        result = repo.repair_timestamps(dry_run=False, progress_cb=cb)
    console.print(f"[green]{result.summary()}[/green]")
    if result.errors:
        console.print(
            f"[red]{t('flow.repair_ts.errors_header', count=len(result.errors))}[/red]"
        )
        for op, sid, msg in result.errors[:10]:
            console.print(f"  [{op}] {sid[:12]}: {msg}")


def run_restore_flow(console: Console, env: EnvironmentSummary):
    """Materialize a snapshot back to a directory on disk."""
    render_op_intro(
        console, t("flow.restore.title"),
        t("flow.restore.body"),
        expects=t("flow.restore.expects"),
    )
    repo = _pick_or_create_repo(console, env)
    snap = _pick_snapshot(console, repo, prompt=t("prompt.snap_to_restore"))
    if snap is None:
        return
    dest = prompt_path(console, t("prompt.dest_dir"), default=Path.cwd() / "restored")
    if dest.exists() and any(dest.iterdir()):
        if not confirm(console, t("prompt.dest_not_empty", dest=dest), default=False):
            console.print(f"[dim]{t('prompt.cancel')}[/dim]")
            return
    progress = make_progress(console)
    with progress:
        task = progress.add_task(t("task.restore"), total=None)
        cb = _make_phase_cb(progress, task, prefix="phase")
        repo.restore(snap, dest, progress_cb=cb)
    console.print(f"[green]{t('msg.restore.done', sid=snap.short_id, dest=dest)}[/green]")


def run_delete_flow(console: Console, env: EnvironmentSummary):
    """Remove a snapshot from the vault. Chunks become eligible for gc."""
    render_op_intro(
        console, t("flow.delete.title"),
        t("flow.delete.body"),
        expects=t("flow.delete.expects"),
    )
    repo = _pick_or_create_repo(console, env)
    snap = _pick_snapshot(console, repo, prompt=t("prompt.snap_to_delete"))
    if snap is None:
        return
    if not confirm(
        console,
        t("prompt.delete_snap", sid=snap.short_id, label=snap.label or '-'),
        default=False,
    ):
        console.print(f"[dim]{t('prompt.cancel')}[/dim]")
        return
    repo.delete(snap)
    console.print(f"[green]{t('msg.delete.done', sid=snap.short_id)}[/green]")


def run_retime_flow(console: Console, env: EnvironmentSummary):
    """Reassign one snapshot's timeline timestamp."""
    render_op_intro(
        console, t("flow.retime.title"),
        t("flow.retime.body"),
        expects=t("flow.retime.expects"),
    )
    repo = _pick_or_create_repo(console, env)
    snap = _pick_snapshot(console, repo, prompt=t("prompt.snap_to_retime"))
    if snap is None:
        return
    use_level_dat = confirm(console, t("prompt.use_level_dat"), default=True)
    if use_level_dat:
        updated, source = repo.retime_snapshot_from_manifest(snap)
        if source == "no_last_played":
            console.print(f"[yellow]{t('msg.retime.no_lp')}[/yellow]")
            return
        console.print(
            f"[green]{t('msg.retime.done_from_lp', sid=updated.short_id, iso=_fmt_ts(updated.timestamp))}[/green]"
        )
        return
    raw = prompt_path(
        console, t("prompt.new_ts"),
        default=Path(_fmt_ts(snap.timestamp)),
    )
    try:
        new_ts = datetime.fromisoformat(str(raw))
    except ValueError as e:
        console.print(f"[red]{t('msg.retime.invalid_ts', err=e)}[/red]")
        return
    if new_ts.tzinfo is None:
        new_ts = new_ts.replace(tzinfo=timezone.utc)
    updated = repo.retime_snapshot(snap, new_ts)
    console.print(
        f"[green]{t('msg.retime.done_explicit', sid=updated.short_id, iso=_fmt_ts(updated.timestamp))}[/green]"
    )


def run_thumbnail_flow(console: Console, env: EnvironmentSummary):
    """Render thumbnail tiles for one snapshot or all snapshots."""
    render_op_intro(
        console, t("flow.thumbnail.title"),
        t("flow.thumbnail.body"),
        expects=t("flow.thumbnail.expects"),
    )
    repo = _pick_or_create_repo(console, env)
    do_all = confirm(console, t("prompt.thumbnails_all"), default=False)
    from ..viz.snapshot_render import (
        ensure_tiles_for_manifest, write_snapshot_sidecars,
    )
    from ..store.manifest import read_manifest

    targets = repo.list() if do_all else [
        s for s in [_pick_snapshot(console, repo, prompt=t("flow.thumbnail.pick_one"))] if s is not None
    ]
    if not targets:
        return
    progress = make_progress(console)
    with progress:
        task = progress.add_task(t("task.render"), total=None)
        cb = _make_phase_cb(progress, task, prefix="phase")
        for snap in targets:
            manifest = read_manifest(snap.manifest_path)
            ensure_tiles_for_manifest(repo, manifest, progress_cb=cb)
            write_snapshot_sidecars(repo, snap.id, manifest, progress_cb=cb)
    console.print(f"[green]{t('msg.thumbnail.done', count=len(targets))}[/green]")


def run_browse_flow(console: Console, env: EnvironmentSummary):
    """Start the local Leaflet browser for the vault."""
    render_op_intro(
        console, t("flow.browse.title"),
        t("flow.browse.body"),
        expects=t("flow.browse.expects"),
    )
    repo = _pick_or_create_repo(console, env)
    host = "127.0.0.1"
    port = 8765
    if confirm(console, t("prompt.bind_default", host=host, port=port), default=True):
        pass
    else:
        raw_host = prompt_path(console, t("prompt.bind_host"), default=Path(host))
        host = str(raw_host)
        raw_port = prompt_path(console, t("prompt.bind_port"), default=Path(str(port)))
        try:
            port = int(str(raw_port))
        except ValueError:
            console.print(f"[red]{t('msg.invalid_port')}[/red]")
            port = 8765
    from ..viz.browser import serve
    try:
        serve(repo.repo_path, host=host, port=port)
    except KeyboardInterrupt:
        console.print(f"\n[dim]{t('msg.browse.stopped')}[/dim]")


def run_verify_roundtrip_flow(console: Console, env: EnvironmentSummary):
    """Compare a snapshot against a known-good source tree byte-for-byte."""
    render_op_intro(
        console, t("flow.verify_rt.title"),
        t("flow.verify_rt.body"),
        expects=t("flow.verify_rt.expects"),
    )
    repo = _pick_or_create_repo(console, env)
    snap = _pick_snapshot(console, repo, prompt=t("prompt.snap_to_verify"))
    if snap is None:
        return
    source = prompt_path(console, t("prompt.original_world"), default=Path.cwd())
    from ..store.roundtrip import verify_roundtrip
    progress = make_progress(console)
    with progress:
        task = progress.add_task(t("task.verify"), total=None)
        cb = _make_phase_cb(progress, task, prefix="phase")
        report = verify_roundtrip(repo, snap, source, progress_cb=cb)
    color = "green" if report.passed else "red"
    console.print(f"[{color}]{report.summary()}[/{color}]")


def run_verify_folders_flow(console: Console, env: EnvironmentSummary):
    """Compare two arbitrary directory trees byte-for-byte."""
    render_op_intro(
        console, t("flow.verify_dirs.title"),
        t("flow.verify_dirs.body"),
        expects=t("flow.verify_dirs.expects"),
    )
    a = prompt_path(console, t("prompt.left_dir"), default=Path.cwd())
    b = prompt_path(console, t("prompt.right_dir"), default=Path.cwd())
    write_report = confirm(console, t("prompt.write_report"), default=False)
    report_path = None
    if write_report:
        report_path = prompt_path(
            console, t("prompt.report_path"),
            default=Path.cwd() / "verify-folders-report.txt",
        )
    from ..store.roundtrip import compare_directories
    progress = make_progress(console)
    with progress:
        task = progress.add_task(t("task.compare"), total=None)
        cb = _make_phase_cb(progress, task, prefix="phase")
        report = compare_directories(a, b, progress_cb=cb)
    color = "green" if report.passed else "red"
    console.print(f"[{color}]{report.summary()}[/{color}]")
    if report_path is not None:
        from ..cli import _write_verify_folders_report
        _write_verify_folders_report(report_path, a, b, report)
        console.print(f"[dim]{t('flow.verify_dirs.report_written', path=report_path)}[/dim]")


def run_logs_flow(console: Console, env: EnvironmentSummary):
    """Sub-menu for the parallel log-snapshot subsystem."""
    render_op_intro(
        console, t("flow.logs.title"),
        t("flow.logs.body"),
        expects=t("flow.logs.expects"),
    )
    repo = _pick_or_create_repo(console, env)
    actions = [
        t("prompt.logs.action.list"),
        t("prompt.logs.action.extract"),
        t("prompt.logs.action.delete"),
        t("prompt.logs.action.back"),
    ]
    action = choose_one(console, t("prompt.logs.action"), actions)
    # Match by index, not prefix — translated strings won't share an English prefix.
    if action == actions[3]:   # back
        return
    if action == actions[0]:   # list
        snaps = repo.list_log_snapshots()
        if not snaps:
            console.print(f"[yellow]{t('msg.no_log_snapshots')}[/yellow]")
            return
        for s in snaps:
            console.print(
                f"{s.short_id}  {_fmt_ts(s.timestamp)}  "
                f"{(s.label or '-'):26}  "
                f"servers={s.server_count} files={s.file_count}"
            )
        return
    if action == actions[1]:   # extract
        snap = _pick_log_snapshot(console, repo, prompt=t("prompt.log_snap_to_extract"))
        if snap is None:
            return
        dest = prompt_path(
            console, t("prompt.dest_dir"),
            default=Path.cwd() / "logs-extracted",
        )
        from ..store.log_manifest import read_log_manifest
        servers = list(read_log_manifest(snap.manifest_path).servers)
        wanted: str | None = None
        if servers and len(servers) > 1:
            if confirm(console, t("prompt.filter_to_one_server"), default=False):
                wanted = choose_one(console, t("prompt.server"), servers)
        repo.extract_logs(snap, dest, server=wanted)
        console.print(f"[green]{t('msg.logs.extract_done', dest=dest)}[/green]")
        return
    if action == actions[2]:   # delete
        snap = _pick_log_snapshot(console, repo, prompt=t("prompt.log_snap_to_delete"))
        if snap is None:
            return
        if not confirm(
            console, t("prompt.delete_log_snap", sid=snap.short_id),
            default=False,
        ):
            console.print(f"[dim]{t('prompt.cancel')}[/dim]")
            return
        repo.delete_log_snapshot(snap)
        console.print(f"[green]{t('msg.logs.delete_done', sid=snap.short_id)}[/green]")


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
            f"[yellow]{i18n.t('msg.lang.fallback_no_save', name=chosen.native_name)}[/yellow]"
        )
    else:
        console.print(i18n.t(
            "lang.changed", locale=chosen.native_name, path=saved_path,
        ))


# ---- submenu dispatchers ---------------------------------------------------

def _dispatch_health_submenu(console: Console, env: EnvironmentSummary):
    """Vault health diagnostics + housekeeping."""
    choice = submenu(console, t("menu.health"), _build_health_submenu())
    if choice is None:
        return
    if choice == "f":
        run_fsck_flow(console, env)
    elif choice == "v":
        run_verify_flow(console, env)
    elif choice == "V":
        run_verify_roundtrip_flow(console, env)
    elif choice == "F":
        run_verify_folders_flow(console, env)
    elif choice == "g":
        run_gc_flow(console, env)
    elif choice == "t":
        run_thumbnail_flow(console, env)


def _dispatch_repair_submenu(console: Console, env: EnvironmentSummary):
    """Recovery / migration tools."""
    choice = submenu(console, t("menu.repair_tools"), _build_repair_submenu())
    if choice is None:
        return
    if choice == "r":
        run_repair_timestamps_flow(console, env)
    elif choice == "M":
        run_migrate_mca_flow(console, env)
    elif choice == "T":
        run_retime_flow(console, env)
    elif choice == "D":
        run_delete_flow(console, env)


def _dispatch_settings_submenu(console: Console, env: EnvironmentSummary):
    """Persistent user config."""
    choice = submenu(console, t("menu.settings"), _build_settings_submenu())
    if choice is None:
        return
    if choice == "@":
        run_language_flow(console, env)
    elif choice == "p":
        run_repos_flow(console, env)
    elif choice == "u":
        run_sources_flow(console, env)


# ---- new flow: migrate-mca-files ------------------------------------------

def run_migrate_mca_flow(console: Console, env: EnvironmentSummary):
    """Move entities/*.mca + poi/*.mca from whole-file dedup to chunk-
    level dedup using bytes already in the file pool."""
    render_op_intro(
        console, t("menu.migrate_mca"),
        t("menu.migrate_mca.hint"),
        expects=t("flow.repair_ts.expects"),
    )
    repo = _pick_or_create_repo(console, env)

    progress = make_progress(console)
    with progress:
        task = progress.add_task(t("task.migrate"), total=None)
        cb = _make_phase_cb(progress, task, prefix="migrate")
        dry = repo.migrate_mca_files_to_chunks(dry_run=True, progress_cb=cb)
    console.print(f"[bold]{dry.summary()}[/bold]")

    if dry.mca_files_total == 0:
        console.print(f"[green]{t('flow.repair_ts.clean')}[/green]")
        return

    if not confirm(console, t("flow.repair_ts.confirm_apply"), default=False):
        console.print(f"[dim]{t('flow.repair_ts.cancelled')}[/dim]")
        return

    progress = make_progress(console)
    with progress:
        task = progress.add_task(t("task.migrate"), total=None)
        cb = _make_phase_cb(progress, task, prefix="migrate")
        result = repo.migrate_mca_files_to_chunks(dry_run=False, progress_cb=cb)
    console.print(f"[green]{result.summary()}[/green]")


# ---- new flows: repo + source path registry management -------------------

def run_repos_flow(console: Console, env: EnvironmentSummary):
    """List / add / remove registered vaults."""
    from . import config as _cfg
    render_op_intro(
        console, t("menu.repos"),
        t("menu.repos.hint"),
        expects=t("registry.expects_repo"),
    )
    repos = _cfg.list_repos()
    if repos:
        console.print(f"\n[bold]{t('menu.repos')}:[/bold]")
        for r in repos:
            label = f"  [{r.label}]" if r.label else ""
            marker = "" if r.path.is_dir() else f"  [dim]({t('registry.missing')})[/dim]"
            console.print(f"  {r.path}{label}{marker}")
    else:
        console.print(f"[dim]{t('registry.none_registered')}[/dim]")
    actions = [
        f"{t('registry.action.add')}  │ {t('menu.repos.hint')}",
        f"{t('registry.action.remove')} │ {t('registry.action.remove.hint')}",
        t("prompt.logs.action.back"),
    ]
    pick = choose_one(console, t("registry.action.prompt"), actions)
    if pick == actions[2]:
        return
    if pick == actions[0]:
        path = prompt_path(console, t("prompt.repo_path"),
                           default=Path.cwd(), must_exist=False)
        label = console.input(t("prompt.label_blank")).strip()
        added = _cfg.add_repo(path, label=label)
        if added:
            console.print(f"[green]{t('registry.registered')}[/green] {Path(path).resolve()}")
        else:
            console.print(f"[dim]{t('registry.already_registered')}[/dim]")
        return
    if pick == actions[1]:
        if not repos:
            console.print(f"[dim]{t('registry.nothing_to_remove')}[/dim]")
            return
        options = [str(r.path) for r in repos]
        target = choose_one(console, t("registry.which_to_remove"), options)
        if target is None:
            return
        if _cfg.remove_repo(target):
            console.print(f"[green]{t('registry.removed')}[/green] {target}")


def run_sources_flow(console: Console, env: EnvironmentSummary):
    """List / add / remove registered source-archive directories."""
    from . import config as _cfg
    render_op_intro(
        console, t("menu.sources"),
        t("menu.sources.hint"),
        expects=t("registry.expects_source"),
    )
    sources = _cfg.list_source_paths()
    if sources:
        console.print(f"\n[bold]{t('menu.sources')}:[/bold]")
        for s in sources:
            label = f"  [{s.label}]" if s.label else ""
            marker = "" if s.path.is_dir() else f"  [dim]({t('registry.missing')})[/dim]"
            console.print(f"  {s.path}{label}{marker}")
    else:
        console.print(f"[dim]{t('registry.none_registered')}[/dim]")
    actions = [
        f"{t('registry.action.add')}  │ {t('menu.sources.hint')}",
        f"{t('registry.action.remove')} │ {t('registry.action.remove.hint')}",
        t("prompt.logs.action.back"),
    ]
    pick = choose_one(console, t("registry.action.prompt"), actions)
    if pick == actions[2]:
        return
    if pick == actions[0]:
        path = prompt_path(console, t("prompt.extra_source"),
                           default=Path.cwd(), must_exist=False)
        label = console.input(t("prompt.label_blank")).strip()
        added = _cfg.add_source_path(path, label=label)
        if added:
            console.print(f"[green]{t('registry.registered')}[/green] {Path(path).resolve()}")
        else:
            console.print(f"[dim]{t('registry.already_registered')}[/dim]")
        return
    if pick == actions[1]:
        if not sources:
            console.print(f"[dim]{t('registry.nothing_to_remove')}[/dim]")
            return
        options = [str(s.path) for s in sources]
        target = choose_one(console, t("registry.which_to_remove"), options)
        if target is None:
            return
        if _cfg.remove_source_path(target):
            console.print(f"[green]{t('registry.removed')}[/green] {target}")

