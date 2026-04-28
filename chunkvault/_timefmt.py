"""Display-formatting helpers for timestamps.

chunkvault stores everything as UTC ms since epoch — manifest headers,
SQLite rows, JSON API responses. That's the right choice for storage:
no ambiguity, no DST surprises, vaults travel between machines without
silent rebasing.

But raw UTC ISO strings (``2026-04-25T17:05:48+00:00``) are confusing for
humans on non-UTC machines. A user in Beijing seeing "17:05 UTC" has to
mentally add 8 hours to know "that was just past 1 AM here". This module
formats datetimes in the host's local timezone for display.

Internal logic, manifest writes, SQL queries, and JSON should KEEP using
UTC ISO. Only call these helpers when emitting text humans will read
in a terminal.
"""
from __future__ import annotations

from datetime import datetime, timezone


def format_local(dt: datetime) -> str:
    """Format a datetime in the host's local timezone, dropping microseconds.

    Naive datetimes are assumed to be UTC (which they always are in
    chunkvault internals). The returned string is unambiguous and
    machine-parseable — it carries the local UTC offset.

    Examples (host in UTC+8):
        >>> from datetime import datetime, timezone
        >>> dt = datetime(2026, 4, 25, 17, 5, 48, 331000, tzinfo=timezone.utc)
        >>> format_local(dt)
        '2026-04-26 01:05:48+08:00'

    Examples (host in UTC):
        >>> format_local(datetime(2026, 4, 25, 17, 5, 48, tzinfo=timezone.utc))
        '2026-04-25 17:05:48+00:00'
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    # strftime('%z') yields '+0800'; reformat to '+08:00' for ISO-8601
    # extended form. Manual splice avoids platform-specific '%:z' that
    # isn't supported on Windows.
    raw = local.strftime("%z")
    if raw:
        offset = f"{raw[:3]}:{raw[3:]}"
    else:
        offset = ""
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')}{offset}"


def format_utc(dt: datetime) -> str:
    """Format in UTC, dropping microseconds. For ``--utc`` flag and tests."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")
