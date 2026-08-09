"""Shared report-local timezone resolution (#230)."""

from __future__ import annotations

import os
import time as system_time
from datetime import datetime, timezone

import pytest

from ccstory.local_time import system_local_timezone

UTC = timezone.utc


@pytest.mark.skipif(
    not hasattr(system_time, "tzset"),
    reason="host does not expose POSIX local-time rule switching",
)
def test_posix_tz_fallback_distinguishes_repeated_hour_folds():
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "EST5EDT,M3.2.0,M11.1.0"
        system_time.tzset()
        local_timezone = system_local_timezone()
        first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC).astimezone(
            local_timezone
        )
        second = datetime(2026, 11, 1, 6, 30, tzinfo=UTC).astimezone(
            local_timezone
        )
        first_result = first.isoformat(), first.fold
        second_result = second.isoformat(), second.fold
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        system_time.tzset()

    assert first_result == (
        "2026-11-01T01:30:00-04:00",
        0,
    )
    assert second_result == (
        "2026-11-01T01:30:00-05:00",
        1,
    )
