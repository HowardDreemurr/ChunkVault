"""UI primitives for the wizard.

Two libraries, divided by job:

* **questionary** — interactive prompts (arrow-key menus, y/n confirms,
  text input). Works cross-platform via prompt_toolkit. This is what gives
  the wizard the "highlight + Enter" experience instead of typing letters.
* **rich** — everything else: tables, panels, progress bars, colored text.
  rich's Prompt is text-only; questionary fills the keyboard-nav gap.

Isolated from flow logic so flows can be tested without instantiating
console widgets. Each function takes a Console (passed in for testability)
and returns a structured result.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

import questionary
from rich.console import Console
from rich.live import Live
from rich.panel import Panel

from .._timefmt import format_local as _fmt_ts
from .i18n import t
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from ..store.inspect import ArchivePreview
from .detect import EnvironmentSummary


# A questionary style that pairs visually with rich's default theme.
_QSTYLE = questionary.Style([
    ("qmark",       "fg:#28c850 bold"),
    ("question",    "bold"),
    ("answer",      "fg:#28c850 bold"),
    ("pointer",     "fg:#28c850 bold"),
    ("highlighted", "fg:#28c850 bold"),
    ("selected",    "fg:#28c850"),
    ("instruction", "fg:#888888"),
])


# ---- formatting helpers ----------------------------------------------------

def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---- environment summary panel --------------------------------------------

def render_environment(console: Console, env: EnvironmentSummary) -> None:
    """Print a digestible summary of what we found on the local system."""
    if env.repos:
        table = Table(title=t("env.repos.title"), title_style="bold")
        table.add_column(t("env.repos.col.path"), style="cyan")
        table.add_column(t("env.repos.col.kind"), style="yellow")
        table.add_column(t("env.repos.col.snapshots"), justify="right")
        table.add_column(t("env.repos.col.log_snaps"), justify="right")
        table.add_column(t("env.repos.col.metadata"), justify="right")
        for r in env.repos:
            table.add_row(
                str(r.path), r.kind,
                str(r.snapshot_count), str(r.log_snapshot_count),
                fmt_bytes(r.on_disk_bytes),
            )
        console.print(table)
        console.print(f"[dim]{t('env.repos.metadata_hint')}[/dim]")
    else:
        console.print(Panel(
            t("env.repos.empty.body"),
            title=t("env.repos.empty.title"), border_style="dim",
        ))

    if env.source_paths:
        table = Table(title=t("env.sources.title"), title_style="bold")
        table.add_column(t("env.sources.col.path"), style="cyan")
        table.add_column(t("env.sources.col.archives"), justify="right")
        table.add_column(t("env.sources.col.total_bytes"), justify="right")
        table.add_column(t("env.sources.col.samples"), style="dim")
        for s in env.source_paths:
            samples = ", ".join(s.samples[:3])
            if len(s.samples) < s.archive_count:
                samples += t("env.sources.more_samples",
                             n=s.archive_count - len(s.samples))
            table.add_row(
                str(s.path), str(s.archive_count),
                fmt_bytes(s.total_bytes), samples,
            )
        console.print(table)
    else:
        console.print(Panel(
            t("env.sources.empty.body"),
            title=t("env.sources.empty.title"), border_style="dim",
        ))


# ---- archive preview -------------------------------------------------------

def render_archive_preview(
    console: Console, preview: ArchivePreview, *, compact: bool = False,
) -> None:
    """Show what's INSIDE an archive — servers, regions, logs.

    The wizard renders this for each archive before the final confirmation
    so the user can verify content matches expectations.

    ``compact=True`` collapses to one line per archive — used when many
    archives are previewed at once. Diagnostic noise (top-level entries,
    ignored siblings) is shown ONLY when something looks wrong (no servers
    or zero region files), per user request to keep clean cases quiet.
    """
    if compact:
        _render_archive_preview_compact(console, preview)
        return

    header = f"[bold cyan]{preview.path.name}[/bold cyan]"
    if preview.timestamp:
        header += f"  [dim](timestamp: {_fmt_ts(preview.timestamp)})[/dim]"
    console.print(header)

    if preview.error:
        console.print(f"  [red]{t('preview.error_label')}[/red] {preview.error}")
        return

    if not preview.servers:
        console.print(f"  [yellow]{t('preview.no_servers')}[/yellow]")
        # Diagnostic — only when we DIDN'T find anything: show what was
        # there so the user can tell if they pointed at the wrong path.
        if preview.other_top_level:
            entries = (
                ", ".join(preview.other_top_level[:6])
                + ("…" if len(preview.other_top_level) > 6 else "")
            )
            console.print(
                f"  [dim]{t('preview.top_level', entries=entries)}[/dim]"
            )
        return

    table = Table(box=None, padding=(0, 2), show_edge=False, pad_edge=False)
    table.add_column(t("preview.col.server"), style="cyan")
    table.add_column(t("preview.col.regions"), justify="right")
    table.add_column(t("preview.col.dimensions"), style="dim")
    table.add_column(t("preview.col.logs"), justify="right")
    table.add_column(t("preview.col.crashes"), justify="right")
    table.add_column(t("preview.col.level_dat"))
    table.add_column(t("preview.col.world_size"), justify="right")

    any_zero_region = False
    for s in preview.servers:
        dims = ", ".join(s.dimensions) if s.dimensions else "-"
        if len(dims) > 40:
            dims = dims[:37] + "…"
        if s.region_files == 0:
            any_zero_region = True
        table.add_row(
            s.name,
            str(s.region_files) if s.region_files else "[red]0[/red]",
            dims,
            str(s.log_files),
            str(s.crash_report_files),
            (f"[green]{t('preview.yes')}[/green]" if s.has_level_dat
             else f"[red]{t('preview.no')}[/red]"),
            fmt_bytes(s.estimated_world_bytes),
        )
    console.print(table)

    # Diagnostic — only when something looks wrong. A zero-region server is
    # almost always a layout-detection failure, so the user wants to see what
    # was inside that server folder. A clean archive doesn't need any noise.
    if any_zero_region:
        for s in preview.servers:
            if s.region_files == 0 and s.top_level:
                entries = (
                    ", ".join(s.top_level[:8])
                    + ("…" if len(s.top_level) > 8 else "")
                )
                console.print(
                    f"  [dim]{t('preview.contains', server=s.name, entries=entries)}[/dim]"
                )
    console.print()


def _render_archive_preview_compact(
    console: Console, preview: ArchivePreview,
) -> None:
    """One-line summary per archive — for bulk previews."""
    if preview.error:
        console.print(
            f"  [red]✗[/red] {preview.path.name}: {preview.error}"
        )
        return
    if not preview.servers:
        console.print(
            f"  [yellow]?[/yellow] {preview.path.name}: "
            f"[dim]{t('preview.compact.no_servers')}[/dim]"
        )
        return
    bits: list[str] = []
    for s in preview.servers:
        marker = "" if s.region_files else "[red]![/red]"
        bits.append(
            f"[cyan]{s.name}[/cyan]"
            f"({s.region_files}r/{s.log_files}l){marker}"
        )
    console.print(f"  [green]✓[/green] {preview.path.name}: {' '.join(bits)}")


def select_archives(
    archives: list, previews: list,
) -> list:
    """Multi-select checkbox over archives. Returns the filtered list.

    NOTHING is pre-checked. With dozens of archives, accidentally accepting
    a default-all selection (and then waiting for terabytes of ingest) is
    too easy. Users press ``a`` to select all if that's what they want, or
    Space to toggle individual ones. Returns the user's selection (empty
    list if cancelled).
    """
    import questionary
    choices = []
    for archive, preview in zip(archives, previews):
        # Build a single-line label that fits in a terminal
        if preview.error:
            tag = t("select_archives.tag.error", err=preview.error[:30])
        elif not preview.servers:
            tag = t("select_archives.tag.no_servers")
        else:
            servers_summary = ", ".join(
                f"{s.name}({s.region_files}r)" for s in preview.servers
            )
            if len(servers_summary) > 60:
                servers_summary = servers_summary[:57] + "…"
            tag = servers_summary
        label = f"{archive.name}  │ {tag}"
        choices.append(questionary.Choice(label, value=archive, checked=False))
    answer = questionary.checkbox(
        t("select_archives.title"),
        choices=choices,
        style=_QSTYLE,
        instruction=t("select_archives.instruction"),
    ).ask()
    return list(answer) if answer is not None else []


# ---- prompts --------------------------------------------------------------

def _menu_choice(label_key: str, hint_key: str | None, value: str):
    """Build a questionary.Choice with the localized label+hint at call time.

    Built fresh per ``main_menu`` invocation so a runtime ``set_locale``
    is reflected on the very next menu display, not only after restart.
    """
    label = t(label_key)
    if hint_key is not None:
        # Pad to a fixed visual column so the hint pipe lines up across rows.
        # (Kept tight at 18 chars — Chinese glyphs are wider than ASCII so
        # widths won't perfectly line up, but it stays readable either way.)
        label = f"{label:<18}│ {t(hint_key)}"
    return questionary.Choice(label, value=value)


def _build_menu_choices() -> list:
    """Top-level menu: 6 most-common operations + 4 submenu groups + quit.

    Categorised because flat 17-item lists are hostile UX, especially when
    half the items are diagnostics or recovery tools used once a quarter.
    The grouping is workflow-driven:

      direct actions       — daily use
      ── health           — periodic maintenance
      ── repair           — recovery from interruptions / bugs
      ── visualize        — browse + render
      ── settings         — config: language, registered paths

    Logs gets its own submenu inline (already had one internally).
    """
    return [
        # ── direct actions (most common) ────────────────────
        _menu_choice("menu.ingest",       "menu.ingest.hint",     "i"),
        _menu_choice("menu.snapshot",     "menu.snapshot.hint",   "s"),
        _menu_choice("menu.list",         "menu.list.hint",       "l"),
        _menu_choice("menu.restore",      "menu.restore.hint",    "x"),
        _menu_choice("menu.diff",         "menu.diff.hint",       "d"),
        _menu_choice("menu.browse",       "menu.browse.hint",     "b"),
        # ── grouped submenus ────────────────────────────────
        _menu_choice("menu.health",       "menu.health.hint",     "H"),
        _menu_choice("menu.repair_tools", "menu.repair_tools.hint","R"),
        _menu_choice("menu.logs",         "menu.logs.hint",       "L"),
        _menu_choice("menu.settings",     "menu.settings.hint",   "S"),
        # ── escape hatch ────────────────────────────────────
        _menu_choice("menu.quit",         None,                   "q"),
    ]


def _build_health_submenu() -> list:
    """Vault-health diagnostics + housekeeping."""
    return [
        _menu_choice("menu.fsck",         "menu.fsck.hint",       "f"),
        _menu_choice("menu.verify",       "menu.verify.hint",     "v"),
        _menu_choice("menu.verify_rt",    "menu.verify_rt.hint",  "V"),
        _menu_choice("menu.verify_dirs",  "menu.verify_dirs.hint","F"),
        _menu_choice("menu.gc",           "menu.gc.hint",         "g"),
        _menu_choice("menu.thumbnail",    "menu.thumbnail.hint",  "t"),
        _menu_choice("menu.back",         None,                   "_back"),
    ]


def _build_repair_submenu() -> list:
    """Recovery / migration tools — used after a Ctrl+C, an upgrade, or
    when fixing data created by an older buggy version."""
    return [
        _menu_choice("menu.repair_ts",    "menu.repair_ts.hint",  "r"),
        _menu_choice("menu.migrate_mca",  "menu.migrate_mca.hint","M"),
        _menu_choice("menu.retime",       "menu.retime.hint",     "T"),
        _menu_choice("menu.delete",       "menu.delete.hint",     "D"),
        _menu_choice("menu.back",         None,                   "_back"),
    ]


def _build_settings_submenu() -> list:
    """Persistent user config: language, registered vaults + source paths."""
    return [
        _menu_choice("menu.lang",         "menu.lang.hint",       "@"),
        _menu_choice("menu.repos",        "menu.repos.hint",      "p"),
        _menu_choice("menu.sources",      "menu.sources.hint",    "u"),
        _menu_choice("menu.back",         None,                   "_back"),
    ]


def submenu(console, title: str, choices: list) -> str | None:
    """Render a submenu picker. Returns the chosen value, or None if the
    user picked "back" / cancelled."""
    answer = questionary.select(
        title,
        choices=choices,
        default=choices[0],
        style=_QSTYLE,
        instruction=t("prompt.menu_keys"),
    ).ask()
    if answer is None or answer == "_back":
        return None
    return answer


def render_menu_guide(console: Console) -> None:
    """Show a guide panel BEFORE the menu so the user knows what each
    operation expects as input. Section headers + per-op short labels
    come from the active locale's string table; the rest is built
    dynamically from menu hints (which are themselves localized)."""
    def _line(menu_key: str) -> str:
        return f"[bold cyan]{t(menu_key)}[/bold cyan] — {t(menu_key + '.hint')}"

    body = (
        f"[bold]{t('guide.section.snapshots')}[/bold]\n"
        f"{_line('menu.ingest')}\n"
        f"{_line('menu.snapshot')}\n"
        f"{_line('menu.list')}\n"
        f"{_line('menu.restore')}\n"
        f"{_line('menu.delete')}\n"
        "\n"
        f"[bold]{t('guide.section.compare')}[/bold]\n"
        f"{_line('menu.diff')}\n"
        f"{_line('menu.browse')}\n"
        f"{_line('menu.thumbnail')}\n"
        "\n"
        f"[bold]{t('guide.section.logs')}[/bold]\n"
        f"{_line('menu.logs')}\n"
        "\n"
        f"[bold]{t('guide.section.integrity')}[/bold]\n"
        f"{_line('menu.verify')}\n"
        f"{_line('menu.verify_rt')}\n"
        f"{_line('menu.verify_dirs')}\n"
        "\n"
        f"[bold]{t('guide.section.repair')}[/bold]\n"
        f"{_line('menu.fsck')}\n"
        f"{_line('menu.repair_ts')}\n"
        f"{_line('menu.retime')}\n"
        f"{_line('menu.gc')}"
    )
    console.print(Panel.fit(body, title=t("guide.title"), border_style="cyan"))


def main_menu(console: Console, *, with_guide: bool = True) -> str:
    """Arrow-key main menu. Returns the chosen action key.

    When ``with_guide`` is True (default), prints a guidance panel above
    the menu describing what each operation does + what input it expects.

    Choices are rebuilt every call so a runtime ``set_locale`` reflects on
    the very next display.
    """
    if with_guide:
        render_menu_guide(console)
    choices = _build_menu_choices()
    answer = questionary.select(
        t("prompt.what_to_do"),
        choices=choices,
        default=choices[0],
        style=_QSTYLE,
        instruction=t("prompt.menu_keys"),
    ).ask()
    if answer is None:
        return "q"
    return answer


# ---- per-operation pre-flight panels --------------------------------------

def render_op_intro(
    console: Console, title: str, body: str, *, expects: str | None = None,
    example: str | None = None,
) -> None:
    """A consistent "you picked X — here's what we're about to do" block."""
    text = body
    if expects:
        text += f"\n\n[bold]Expects:[/bold] {expects}"
    if example:
        text += f"\n[bold]Example:[/bold] [yellow]{example}[/yellow]"
    console.print(Panel(text, title=title, border_style="cyan", padding=(0, 1)))


def prompt_path(
    console: Console, label: str, default: Path | None = None,
    *, must_exist: bool = False,
) -> Path:
    """Ask for a filesystem path with tab completion + history."""
    while True:
        raw = questionary.path(
            label,
            default=str(default) if default else "",
            only_directories=False,
            style=_QSTYLE,
        ).ask()
        if raw is None:
            raise KeyboardInterrupt("user cancelled")
        p = Path(raw).expanduser()
        if must_exist and not p.exists():
            console.print(f"[red]path does not exist:[/red] {p}")
            continue
        return p


def confirm(console: Console, label: str, *, default: bool = True) -> bool:
    """y/n prompt with Enter-for-default support."""
    answer = questionary.confirm(
        label, default=default, style=_QSTYLE,
    ).ask()
    return default if answer is None else answer


def choose_one(
    console: Console, label: str, options: Sequence[str],
    *, default: str | None = None,
) -> str:
    """Arrow-key choice from a list of strings."""
    if not options:
        raise ValueError("choose_one needs at least one option")
    answer = questionary.select(
        label, choices=list(options),
        default=default if default in options else options[0],
        style=_QSTYLE,
    ).ask()
    return answer if answer is not None else (default or options[0])


# ---- progress display ----------------------------------------------------

def make_progress(console: Console) -> Progress:
    """Build a Progress widget configured for our use cases."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed:>5}/{task.total} ({task.percentage:>3.0f}%)"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )


def make_console() -> Console:
    """Default Console for the wizard. Centralized so tests can override."""
    return Console()
