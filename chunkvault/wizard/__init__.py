"""Interactive wizard mode for chunkvault.

Entry point: ``chunkvault`` (no subcommand) or ``chunkvault wizard``. Walks the user
through repo detection, operation choice, configuration, confirmation, and
runs the chosen operation with live progress feedback.

Built on `rich` for the UI. Falls back to a simpler text mode if rich is
absent (which shouldn't happen since we declare it as a dependency, but
keeps the wizard import-safe in any case).
"""
from .detect import (
    EnvironmentSummary,
    RepoSummary,
    SourcePathSummary,
    default_paths_to_scan,
    detect_environment,
    scan_for_archives,
    summarize_repo,
)
from .flows import run_wizard

__all__ = [
    "EnvironmentSummary",
    "RepoSummary",
    "SourcePathSummary",
    "default_paths_to_scan",
    "detect_environment",
    "scan_for_archives",
    "summarize_repo",
    "run_wizard",
]
