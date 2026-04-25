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

def render_archive_preview(console: Console, preview: ArchivePreview) -> None:
    """Show what's INSIDE an archive — servers, regions, logs.

    The wizard renders this for each archive before the final confirmation
    so the user can verify content matches expectations.
    """
    header = f"[bold cyan]{preview.path.name}[/bold cyan]"
    if preview.timestamp:
        header += f"  [dim](timestamp: {preview.timestamp.isoformat()})[/dim]"
    console.print(header)

    if preview.error:
        console.print(f"  [red]error:[/red] {preview.error}")
        return

    if not preview.servers:
        console.print("  [yellow]no server folders detected[/yellow]")
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

    for s in preview.servers:
        dims = ", ".join(s.dimensions) if s.dimensions else "-"
        if len(dims) > 40:
            dims = dims[:37] + "…"
        table.add_row(
            s.name,
            str(s.region_files),
            dims,
            str(s.log_files),
            str(s.crash_report_files),
            "[green]yes[/green]" if s.has_level_dat else "[red]no[/red]",
            fmt_bytes(s.estimated_world_bytes),
        )
    console.print(table)
    if preview.other_top_level:
        console.print(
            f"  [dim]ignored top-level entries: "
            f"{', '.join(preview.other_top_level[:6])}"
            f"{'…' if len(preview.other_top_level) > 6 else ''}[/dim]"
        )
    console.print()


# ---- prompts --------------------------------------------------------------

_MENU_CHOICES = [
    questionary.Choice("Ingest archives  (bulk-import backup zips)", value="i"),
    questionary.Choice("Snapshot a live world",                       value="s"),
    questionary.Choice("Diff two snapshots",                          value="d"),
    questionary.Choice("List snapshots",                              value="l"),
    questionary.Choice("Verify repo integrity",                       value="v"),
    questionary.Choice("Garbage-collect (reclaim space)",             value="g"),
    questionary.Choice("Quit",                                        value="q"),
]


def main_menu(console: Console) -> str:
    """Arrow-key main menu. Returns the chosen action key (i/s/d/l/v/g/q).

    Falls back to a typed prompt if questionary can't take over the
    terminal (e.g. running in a non-interactive shell or a captured
    pytest stdin).
    """
    answer = questionary.select(
        "What would you like to do?",
        choices=_MENU_CHOICES,
        default=_MENU_CHOICES[0],
        style=_QSTYLE,
        instruction="(use ↑↓ arrows to move, Enter to select)",
    ).ask()
    if answer is None:
        # User pressed Ctrl-C / Esc — treat as quit
        return "q"
    return answer


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
