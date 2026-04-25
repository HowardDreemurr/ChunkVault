"""Progress event protocol for long-running snapshot/ingest operations.

A simple callback-based design: pass a function that takes a ``ProgressEvent``,
get fired on key milestones. UI wrappers (rich Live, plain stderr, none)
implement ``ProgressCallback`` and ignore events they don't care about.

Kept deliberately minimal — adding fields is cheap, refactoring a chatty
event protocol later is expensive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

EventKind = Literal[
    "phase_start",     # a named phase begins
    "phase_progress",  # progress within a phase (current / total)
    "phase_done",      # phase finished cleanly
    "info",            # informational message (no progress numbers)
    "warning",         # something noteworthy but not fatal
    "error",           # something failed but the operation continues
    "finish",          # the whole operation finished
]


@dataclass(frozen=True)
class ProgressEvent:
    kind: EventKind
    phase: str = ""
    label: str = ""        # short human-readable label for this event
    current: int = 0
    total: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


# A callback. None = no progress reporting (a tight no-op for the caller).
ProgressCallback = Callable[[ProgressEvent], None] | None


def _emit(cb: ProgressCallback, event: ProgressEvent) -> None:
    """Cheap no-op when the callback is None."""
    if cb is not None:
        try:
            cb(event)
        except Exception:
            # Swallow callback errors — never let UI break a backup.
            pass
