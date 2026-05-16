"""Regression tests for timezone correctness (#22).

Locks the rule: every cross-module bound is tz-aware. A session whose
UTC timestamp puts it on a different calendar day than the local-tz day
must be attributed to the local day (not the UTC day).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ccstory.time_tracking import collect_sessions
from ccstory.token_usage import collect_usage
from ccstory.trends import collect_trend, previous_window

from tests.conftest import _ts, make_assistant_msg, make_user_msg


class TestCollectSessionsTzAware:
    def test_tz_aware_bounds_dont_crash(self, tmp_home: Path, jsonl_factory):
        # Session at 2026-05-10 10:00 UTC
        jsonl_factory(
            "-Users-alice-code-myapp",
            "sess-1",
            [
                make_user_msg("hello", _ts(2026, 5, 10, 10, 0, 0)),
                make_assistant_msg("ok", _ts(2026, 5, 10, 10, 1, 0), "msg_1"),
            ],
        )
        # tz-aware Pacific (UTC-7) bounds covering the same day
        pacific = timezone(timedelta(hours=-7))
        since = datetime(2026, 5, 10, 0, 0, 0, tzinfo=pacific)
        until = datetime(2026, 5, 11, 0, 0, 0, tzinfo=pacific)
        sessions = collect_sessions(since, until)
        assert len(sessions) == 1

    def test_naive_bounds_treated_as_utc(self, tmp_home: Path, jsonl_factory):
        # Same setup as above, but pass naive bounds.
        jsonl_factory(
            "-Users-alice-code-myapp",
            "sess-naive",
            [
                make_user_msg("hello", _ts(2026, 5, 10, 10, 0, 0)),
                make_assistant_msg("ok", _ts(2026, 5, 10, 10, 1, 0), "msg_1"),
            ],
        )
        since = datetime(2026, 5, 10, 0, 0, 0)  # naive → UTC
        until = datetime(2026, 5, 11, 0, 0, 0)
        sessions = collect_sessions(since, until)
        assert len(sessions) == 1

    def test_local_midnight_boundary_not_off_by_one(
        self, tmp_home: Path, jsonl_factory,
    ):
        """A session that ends at 2026-05-10 23:30 *local* (UTC-7) — i.e.
        06:30 UTC on 2026-05-11 — must be attributed to the local day
        2026-05-10 when bounds are local-aware.
        """
        # Session at 06:30 UTC on 2026-05-11 = 23:30 PDT on 2026-05-10
        jsonl_factory(
            "-Users-alice-code-x",
            "sess-mid",
            [
                make_user_msg("late night", _ts(2026, 5, 11, 6, 30, 0)),
                make_assistant_msg("ok", _ts(2026, 5, 11, 6, 31, 0), "msg_late"),
            ],
        )
        pacific = timezone(timedelta(hours=-7))
        # The user's "May 10" window: local midnight on the 10th to midnight on the 11th
        since = datetime(2026, 5, 10, 0, 0, 0, tzinfo=pacific)
        until = datetime(2026, 5, 11, 0, 0, 0, tzinfo=pacific)
        sessions = collect_sessions(since, until)
        # The session at 23:30 local on 2026-05-10 must fall inside this window.
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-mid"

    def test_session_after_until_excluded(self, tmp_home: Path, jsonl_factory):
        jsonl_factory(
            "-Users-alice-code-x",
            "sess-after",
            [
                make_user_msg("future", _ts(2026, 5, 12, 10, 0, 0)),
                make_assistant_msg("ok", _ts(2026, 5, 12, 10, 1, 0), "msg_after"),
            ],
        )
        utc = timezone.utc
        since = datetime(2026, 5, 10, 0, 0, 0, tzinfo=utc)
        until = datetime(2026, 5, 11, 0, 0, 0, tzinfo=utc)
        sessions = collect_sessions(since, until)
        assert sessions == []


class TestCollectUsageNaiveAsUtc:
    def test_naive_since_treated_as_utc(self, tmp_home: Path, jsonl_factory):
        jsonl_factory(
            "-Users-alice-code-x",
            "sess-1",
            [
                make_user_msg("hi", _ts(2026, 5, 10, 10, 0, 0)),
                make_assistant_msg(
                    "ok", _ts(2026, 5, 10, 10, 0, 5), "msg_1",
                    input_tokens=42,
                ),
            ],
        )
        # Naive bounds — under the fixed behavior these are interpreted as UTC,
        # so the session at 10:00 UTC on 2026-05-10 is in range regardless
        # of the host TZ.
        rep = collect_usage(
            datetime(2026, 5, 10, 0, 0, 0),
            datetime(2026, 5, 11, 0, 0, 0),
        )
        assert rep.assistant_turns == 1
        assert rep.total_input == 42

    def test_tz_aware_pacific_bounds(self, tmp_home: Path, jsonl_factory):
        jsonl_factory(
            "-Users-alice-code-x",
            "sess-pacific",
            [
                make_user_msg("hi", _ts(2026, 5, 10, 10, 0, 0)),
                make_assistant_msg(
                    "ok", _ts(2026, 5, 10, 10, 0, 5), "msg_1",
                    input_tokens=99,
                ),
            ],
        )
        pacific = timezone(timedelta(hours=-7))
        rep = collect_usage(
            datetime(2026, 5, 10, 0, 0, 0, tzinfo=pacific),
            datetime(2026, 5, 11, 0, 0, 0, tzinfo=pacific),
        )
        assert rep.total_input == 99


class TestPreviousWindowPreservesTz:
    def test_preserves_tzinfo(self):
        pacific = timezone(timedelta(hours=-7))
        since = datetime(2026, 5, 10, 0, 0, 0, tzinfo=pacific)
        until = datetime(2026, 5, 17, 0, 0, 0, tzinfo=pacific)
        prev_since, prev_until = previous_window(since, until)
        assert prev_since.tzinfo == pacific
        assert prev_until.tzinfo == pacific
        # Same length window immediately preceding
        assert (prev_until - prev_since) == (until - since)
        assert prev_until == since


class TestCollectTrendTzAware:
    def test_default_now_is_tz_aware(self, tmp_home: Path):
        # collect_trend() with no `now` falls back to datetime.now().astimezone()
        # which is tz-aware. The scan should not crash on tz comparison.
        points = collect_trend(period="week", count=2)
        # No sessions in the empty fake projects dir → all empty points
        assert len(points) == 2
        for p in points:
            assert p.total_h == 0.0
