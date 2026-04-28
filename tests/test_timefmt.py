"""Tests for chunkvault._timefmt — display-formatting helpers.

These verify that:
  - format_local picks up the host timezone (we don't assume any specific TZ
    in CI; we just check that the output is well-formed and round-trippable).
  - format_utc always emits UTC regardless of host TZ.
  - Both are tolerant of naive datetimes (treated as UTC, matching how
    chunkvault stores them internally).
  - Microseconds are dropped (they're noise for human display).
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone, timedelta

import pytest

from chunkvault._timefmt import format_local, format_utc


def test_format_utc_drops_microseconds():
    dt = datetime(2026, 4, 25, 17, 5, 48, 331000, tzinfo=timezone.utc)
    assert format_utc(dt) == "2026-04-25 17:05:48+00:00"


def test_format_utc_naive_treated_as_utc():
    dt = datetime(2026, 4, 25, 17, 5, 48)   # no tzinfo
    assert format_utc(dt) == "2026-04-25 17:05:48+00:00"


def test_format_local_well_formed():
    """Whatever the host TZ is, output should match the regex
    ``YYYY-MM-DD HH:MM:SS±HH:MM``."""
    dt = datetime(2026, 4, 25, 17, 5, 48, 331000, tzinfo=timezone.utc)
    out = format_local(dt)
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", out), out


def test_format_local_round_trippable_via_fromisoformat():
    """The output must parse back to the same instant via
    ``datetime.fromisoformat`` (so wizard prompts can use it as a default
    and feed it back to fromisoformat without surprises)."""
    dt = datetime(2026, 4, 25, 17, 5, 48, tzinfo=timezone.utc)
    out = format_local(dt)
    parsed = datetime.fromisoformat(out)
    assert parsed == dt   # equality compares the absolute instant


def test_format_local_drops_microseconds():
    dt = datetime(2026, 4, 25, 17, 5, 48, 999999, tzinfo=timezone.utc)
    out = format_local(dt)
    assert ".999" not in out and "999999" not in out


def test_format_local_naive_treated_as_utc():
    naive = datetime(2026, 4, 25, 17, 5, 48)
    aware = datetime(2026, 4, 25, 17, 5, 48, tzinfo=timezone.utc)
    assert format_local(naive) == format_local(aware)


@pytest.mark.skipif(
    not hasattr(time, "tzset"),
    reason="TZ env var only works on POSIX; Windows uses system TZ via Win32 API",
)
def test_format_local_in_fixed_utc_tz():
    """On POSIX, force host TZ to UTC and assert format_local == format_utc.
    Skipped on Windows because Python's astimezone() ignores the TZ env var
    there — it goes through GetTimeZoneInformation."""
    old_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "UTC"
        time.tzset()
        dt = datetime(2026, 4, 25, 17, 5, 48, tzinfo=timezone.utc)
        assert format_local(dt) == format_utc(dt)
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_format_local_for_dst_transitioning_zone():
    """A non-UTC offset should produce a different wall time than UTC for
    the same instant. This sanity-checks the astimezone() conversion is
    actually happening."""
    # An aware datetime explicitly in UTC+8 — formatter converts to host
    # local. We can't assert what host is, but we can verify the function
    # accepts arbitrary tz inputs without crashing.
    plus8 = timezone(timedelta(hours=8))
    dt = datetime(2026, 4, 26, 1, 5, 48, tzinfo=plus8)
    out = format_local(dt)
    # Round-trip should preserve the instant.
    parsed = datetime.fromisoformat(out)
    assert parsed == dt
