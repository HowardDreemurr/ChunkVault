"""Rich-based UI primitives for the wizard.

Isolated from flow logic so flows can be tested without instantiating
console widgets. Each function takes a Console (passed in for testability)
and returns a structured result.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

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
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from ..store.inspect import ArchivePreview
from .detect import EnvironmentSummary


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

def main_menu(console: Console) -> str:
    """Ask the user what they want to do. Returns the chosen action key."""
    console.print(Panel.fit(
        "[bold]What would you like to do?[/bold]\n"
        "  [cyan]i[/cyan]ngest archives  [dim]— bulk-import backup zips[/dim]\n"
        "  [cyan]s[/cyan]napshot a live world\n"
        "  [cyan]d[/cyan]iff two snapshots\n"
        "  [cyan]l[/cyan]ist snapshots\n"
        "  [cyan]v[/cyan]erify repo integrity\n"
        "  [cyan]g[/cyan]c (reclaim space)\n"
        "  [cyan]q[/cyan]uit",
        title="Main menu", border_style="green",
    ))
    return Prompt.ask(
        "Choice", choices=["i", "s", "d", "l", "v", "g", "q"],
        default="i", console=console,
    )


def prompt_path(
    console: Console, label: str, default: Path | None = None,
    *, must_exist: bool = False,
) -> Path:
    """Ask for a filesystem path, validating it if requested."""
    while True:
        raw = Prompt.ask(
            label,
            default=str(default) if default else None,
            console=console,
        )
        p = Path(raw).expanduser()
        if must_exist and not p.exists():
            console.print(f"[red]path does not exist:[/red] {p}")
            continue
        return p


def confirm(console: Console, label: str, *, default: bool = True) -> bool:
    return Confirm.ask(label, default=default, console=console)


def choose_one(
    console: Console, label: str, options: Sequence[str],
    *, default: str | None = None,
) -> str:
    if not options:
        raise ValueError("choose_one needs at least one option")
    return Prompt.ask(
        label, choices=list(options),
        default=default if default in options else options[0],
        console=console,
    )


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
