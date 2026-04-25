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
        table = Table(title="Detected chunkvault repos", title_style="bold")
        table.add_column("path", style="cyan")
        table.add_column("kind", style="yellow")
        table.add_column("snapshots", justify="right")
        table.add_column("log snaps", justify="right")
        table.add_column("on-disk", justify="right")
        for r in env.repos:
            table.add_row(
                str(r.path), r.kind,
                str(r.snapshot_count), str(r.log_snapshot_count),
                fmt_bytes(r.on_disk_bytes),
            )
        console.print(table)
    else:
        console.print(Panel(
            "No existing repo found near cwd.\n"
            "[dim](First-time setup will create one.)[/dim]",
            title="Repos", border_style="dim",
        ))

    if env.source_paths:
        table = Table(title="Source archive locations", title_style="bold")
        table.add_column("path", style="cyan")
        table.add_column("archives", justify="right")
        table.add_column("total bytes", justify="right")
        table.add_column("samples", style="dim")
        for s in env.source_paths:
            samples = ", ".join(s.samples[:3])
            if len(s.samples) < s.archive_count:
                samples += f", … (+{s.archive_count - len(s.samples)} more)"
            table.add_row(
                str(s.path), str(s.archive_count),
                fmt_bytes(s.total_bytes), samples,
            )
        console.print(table)
    else:
        console.print(Panel(
            "No backup archives detected on the default scan paths.\n"
            "[dim](You can point one in manually.)[/dim]",
            title="Sources", border_style="dim",
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
        header += f"  [dim](timestamp: {preview.timestamp.isoformat()})[/dim]"
    console.print(header)

    if preview.error:
        console.print(f"  [red]error:[/red] {preview.error}")
        return

    if not preview.servers:
        console.print("  [yellow]no server folders detected[/yellow]")
        # Diagnostic — only when we DIDN'T find anything: show what was
        # there so the user can tell if they pointed at the wrong path.
        if preview.other_top_level:
            console.print(
                f"  [dim]top-level entries: "
                f"{', '.join(preview.other_top_level[:6])}"
                f"{'…' if len(preview.other_top_level) > 6 else ''}[/dim]"
            )
        return

    table = Table(box=None, padding=(0, 2), show_edge=False, pad_edge=False)
    table.add_column("server", style="cyan")
    table.add_column("regions", justify="right")
    table.add_column("dimensions", style="dim")
    table.add_column("logs", justify="right")
    table.add_column("crashes", justify="right")
    table.add_column("level.dat")
    table.add_column("world size", justify="right")

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
            "[green]yes[/green]" if s.has_level_dat else "[red]no[/red]",
            fmt_bytes(s.estimated_world_bytes),
        )
    console.print(table)

    # Diagnostic — only when something looks wrong. A zero-region server is
    # almost always a layout-detection failure, so the user wants to see what
    # was inside that server folder. A clean archive doesn't need any noise.
    if any_zero_region:
        for s in preview.servers:
            if s.region_files == 0 and s.top_level:
                console.print(
                    f"  [dim]{s.name} contains: "
                    f"{', '.join(s.top_level[:8])}"
                    f"{'…' if len(s.top_level) > 8 else ''}[/dim]"
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
            f"[dim]no servers detected[/dim]"
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
            tag = f"[error: {preview.error[:30]}]"
        elif not preview.servers:
            tag = "[no servers]"
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
        "Select archives to ingest:",
        choices=choices,
        style=_QSTYLE,
        instruction=(
            "(↑↓ move • Space toggle • a select-all • i invert • Enter accept)"
        ),
    ).ask()
    return list(answer) if answer is not None else []


# ---- prompts --------------------------------------------------------------

_MENU_CHOICES = [
    questionary.Choice(
        "Ingest archives    │ for bulk-importing .zip/.tar.gz backup files",
        value="i"),
    questionary.Choice(
        "Snapshot a world   │ for a live MC server's world/ directory",
        value="s"),
    questionary.Choice(
        "Diff snapshots     │ compare any two snapshots already in the vault",
        value="d"),
    questionary.Choice(
        "List snapshots     │ show what's in the vault",
        value="l"),
    questionary.Choice(
        "Verify integrity   │ rehash every blob, detect bit-rot",
        value="v"),
    questionary.Choice(
        "Fsck (repair)      │ clean up after Ctrl-C / kill / power-loss",
        value="f"),
    questionary.Choice(
        "Garbage-collect    │ reclaim space from deleted snapshots",
        value="g"),
    questionary.Choice(
        "Quit",
        value="q"),
]


def render_menu_guide(console: Console) -> None:
    """Show a guide panel BEFORE the menu so the user knows what each
    operation expects as input."""
    console.print(Panel.fit(
        "[bold cyan]Ingest archives[/bold cyan] — pick this for "
        "[bold]a folder of backup .zip files[/bold] (e.g. `D:\\day_backups\\`).\n"
        "    The wizard finds every archive, peeks inside each one, shows you\n"
        "    what servers + worlds + logs it found, then asks confirm.\n"
        "\n"
        "[bold cyan]Snapshot a world[/bold cyan] — pick this for "
        "[bold]a single live MC world directory[/bold]\n"
        "    (the one containing [yellow]level.dat[/yellow] and "
        "[yellow]region/[/yellow], e.g. `D:\\server\\world\\`).\n"
        "    Don't point this at the server root — point at the "
        "[bold]world/[/bold] subdir.\n"
        "\n"
        "[bold cyan]Diff snapshots[/bold cyan] — compare any two snapshots "
        "you already took.\n"
        "[bold cyan]List snapshots[/bold cyan] — see everything currently in the vault.\n"
        "[bold cyan]Verify integrity[/bold cyan] — rehash every blob; "
        "catches disk bit-rot.\n"
        "[bold cyan]Fsck (repair)[/bold cyan] — fixes half-written state from "
        "Ctrl-C / kill.\n"
        "[bold cyan]Garbage-collect[/bold cyan] — reclaims disk space "
        "after `delete`.",
        title="What each operation does",
        border_style="cyan",
    ))


def main_menu(console: Console, *, with_guide: bool = True) -> str:
    """Arrow-key main menu. Returns the chosen action key.

    When ``with_guide`` is True (default), prints a guidance panel above
    the menu describing what each operation does + what input it expects.
    """
    if with_guide:
        render_menu_guide(console)
    answer = questionary.select(
        "What would you like to do?",
        choices=_MENU_CHOICES,
        default=_MENU_CHOICES[0],
        style=_QSTYLE,
        instruction="(↑↓ to move, Enter to select)",
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
