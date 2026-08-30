"""Shared report-local timezone resolution with historical offset rules.

Every surface that maps timestamps onto local calendar dates must agree on the
same timezone, including for dates outside the currently active daylight-saving
season.  `datetime.now().astimezone().tzinfo` does not qualify: on CPython it
captures only the offset in effect right now, so reusing it for a historical
date silently shifts local midnight (#230).
"""

from __future__ import annotations

import calendar
import os
import time as system_time
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class SystemLocalTimezone(tzinfo):
    """Dynamic stdlib fallback over the host's local timezone rules.

    ``datetime.now().astimezone().tzinfo`` is only the offset in effect now on
    common platforms. Reusing it for historical calendar boundaries silently
    loses daylight-saving transitions. Prefer ``ZoneInfo`` below; this class
    keeps Windows and unusual local configurations correct without adding a
    runtime dependency when an IANA key cannot be discovered.
    """

    @staticmethod
    def _offsets() -> tuple[timedelta, timedelta]:
        standard = timedelta(seconds=-system_time.timezone)
        daylight = (
            timedelta(seconds=-system_time.altzone)
            if system_time.daylight
            else standard
        )
        return standard, daylight

    def _is_dst(self, value: datetime | None) -> bool:
        if value is None:
            return False
        candidates = self._wall_timestamp_candidates(value)
        if candidates:
            index = min(value.fold, len(candidates) - 1)
            return candidates[index][1]
        stamp = self._wall_timestamp(value, is_dst=-1)
        return system_time.localtime(stamp).tm_isdst > 0

    @staticmethod
    def _wall_timestamp(value: datetime, *, is_dst: int) -> float:
        naive = value.replace(tzinfo=None)
        return system_time.mktime(
            (
                naive.year,
                naive.month,
                naive.day,
                naive.hour,
                naive.minute,
                naive.second,
                naive.weekday(),
                0,
                is_dst,
            )
        )

    @classmethod
    def _wall_timestamp_candidates(
        cls, value: datetime
    ) -> tuple[tuple[float, bool], ...]:
        """Return real instants for a wall time, earliest/fold=0 first."""

        wall_fields = value.replace(tzinfo=None).timetuple()[:6]
        candidates: dict[float, bool] = {}
        for requested_dst in (0, 1):
            try:
                stamp = cls._wall_timestamp(value, is_dst=requested_dst)
                local = system_time.localtime(stamp)
            except (OverflowError, OSError, ValueError):
                continue
            if local[:6] == wall_fields:
                candidates[stamp] = local.tm_isdst > 0
        return tuple(sorted(candidates.items()))

    def utcoffset(self, value: datetime | None) -> timedelta:
        standard, daylight = self._offsets()
        return daylight if self._is_dst(value) else standard

    def dst(self, value: datetime | None) -> timedelta:
        standard, daylight = self._offsets()
        return daylight - standard if self._is_dst(value) else timedelta(0)

    def tzname(self, value: datetime | None) -> str:
        index = 1 if self._is_dst(value) and len(system_time.tzname) > 1 else 0
        return system_time.tzname[index]

    def fromutc(self, value: datetime) -> datetime:
        if value.tzinfo is not self:
            raise ValueError("fromutc requires this system-local timezone")
        whole_stamp = calendar.timegm(
            (
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
                0,
                0,
                0,
            )
        )
        stamp = whole_stamp + value.microsecond / 1_000_000
        local = system_time.localtime(stamp)
        result = datetime(
            *local[:6],
            microsecond=value.microsecond,
            tzinfo=self,
        )
        candidates = self._wall_timestamp_candidates(result)
        if len(candidates) > 1 and abs(whole_stamp - candidates[-1][0]) < 0.5:
            result = result.replace(fold=1)
        return result


def _zoneinfo_from_file(path: Path) -> ZoneInfo | None:
    try:
        with path.open("rb") as handle:
            return ZoneInfo.from_file(handle, key="system-local")
    except (OSError, ValueError):
        return None


def system_local_timezone() -> tzinfo:
    """Resolve the host timezone with historical offset rules intact."""

    configured = os.environ.get("TZ", "").removeprefix(":").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_absolute():
            from_file = _zoneinfo_from_file(candidate)
            if from_file is not None:
                return from_file
            # A dangling/corrupt tzfile is still an explicit choice. Do not
            # silently replace it with /etc/timezone or /etc/localtime
            # autodetection below — mirror the non-absolute branch instead.
            return SystemLocalTimezone()
        else:
            try:
                return ZoneInfo(configured)
            except (ZoneInfoNotFoundError, ValueError):
                # POSIX TZ rules (for example ``UTC0`` or
                # ``EST5EDT,M3.2.0,M11.1.0``) are valid host configuration,
                # but are not IANA keys. ``tzset()`` has already installed
                # them in the C runtime used by this dynamic fallback. Do not
                # silently replace that explicit choice with /etc/localtime.
                return SystemLocalTimezone()

    timezone_name = Path("/etc/timezone")
    try:
        configured_name = timezone_name.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        configured_name = ""
    if configured_name:
        try:
            return ZoneInfo(configured_name)
        except (ZoneInfoNotFoundError, ValueError):
            pass

    for localtime_path in (
        Path("/etc/localtime"),
        Path("/var/db/timezone/localtime"),
    ):
        from_file = _zoneinfo_from_file(localtime_path)
        if from_file is not None:
            return from_file
    return SystemLocalTimezone()
