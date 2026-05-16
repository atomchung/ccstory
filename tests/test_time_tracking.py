"""Tests for ccstory.time_tracking.

Locks the jsonl parsing contract + active-time math. When Claude Code's log
schema drifts, these tests fail loudly instead of silently dropping data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ccstory.time_tracking import (
    GAP_CAP_SEC,
    SessionStat,
    parse_session,
    rollup_by_category,
    wall_clock_active_sec,
)

from tests.conftest import _ts, make_assistant_msg, make_user_msg, write_jsonl


class TestParseSession:
    def test_basic_multi_turn(self, fixtures_dir: Path):
        s = parse_session(fixtures_dir / "jsonl" / "multi_turn.jsonl")
        assert s is not None
        # 4 records all count toward msg_count
        assert s.msg_count == 4
        # 2 user messages, both real (no <scheduled, no tool_use_id)
        assert s.user_msg_count == 2
        assert s.first_user_text.startswith("Refactor the auth middleware")
        # active time: gaps within 5-min cap = 15 + 105 + 30 = 150s
        assert s.active_sec == 150
        assert s.active_min == 2.5

    def test_malformed_lines_are_skipped(self, fixtures_dir: Path):
        # The fixture has 2 invalid lines between 3 valid records.
        s = parse_session(fixtures_dir / "jsonl" / "malformed_line.jsonl")
        assert s is not None
        # 3 valid records: 2 user + 1 assistant
        assert s.msg_count == 3
        assert s.user_msg_count == 2
        assert s.first_user_text.startswith("First valid user message")

    def test_empty_file_returns_none(self, tmp_path: Path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        assert parse_session(empty) is None

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert parse_session(tmp_path / "nope.jsonl") is None

    def test_gap_cap_caps_long_idle(self, tmp_path: Path):
        records = [
            make_user_msg("hi", _ts(2026, 5, 10, 10, 0, 0)),
            # 20-min gap — should cap at GAP_CAP_SEC (300s)
            make_assistant_msg("hi back", _ts(2026, 5, 10, 10, 20, 0), "msg_1"),
        ]
        p = write_jsonl(tmp_path / "test.jsonl", records)
        s = parse_session(p)
        assert s is not None
        assert s.active_sec == GAP_CAP_SEC

    def test_scheduled_task_flag(self, tmp_path: Path):
        records = [
            make_user_msg(
                "<scheduled-task id=x>do stuff</scheduled-task>",
                _ts(2026, 5, 10, 10, 0, 0),
            ),
            make_assistant_msg("ok", _ts(2026, 5, 10, 10, 0, 30), "msg_1"),
        ]
        p = write_jsonl(tmp_path / "sched.jsonl", records)
        s = parse_session(p)
        assert s is not None
        assert s.is_scheduled is True
        # The <scheduled-task ...> text doesn't count as a "real" user msg
        assert s.user_msg_count == 0

    def test_tool_use_id_text_not_counted_as_user(self, tmp_path: Path):
        # Tool-result wrapper messages contain `tool_use_id` — filter out.
        records = [
            make_user_msg("real user message", _ts(2026, 5, 10, 10, 0, 0)),
            make_user_msg(
                '[{"tool_use_id": "abc", "type": "tool_result"}]',
                _ts(2026, 5, 10, 10, 0, 10),
            ),
            make_assistant_msg("ok", _ts(2026, 5, 10, 10, 0, 30), "msg_1"),
        ]
        p = write_jsonl(tmp_path / "tool.jsonl", records)
        s = parse_session(p)
        assert s is not None
        # both user records counted in msg_count but only the real one in user_msg_count
        assert s.msg_count == 3
        assert s.user_msg_count == 1


class TestEngaged:
    def _stat(self, **kw) -> SessionStat:
        from datetime import datetime
        defaults = dict(
            project="x",
            category="coding",
            session_id="s",
            start=datetime(2026, 5, 10),
            end=datetime(2026, 5, 10),
            active_sec=0,
            msg_count=0,
            user_msg_count=0,
        )
        defaults.update(kw)
        return SessionStat(**defaults)

    def test_two_user_msgs_is_engaged(self):
        assert self._stat(user_msg_count=2).engaged is True

    def test_one_user_msg_long_session_is_engaged(self):
        assert self._stat(user_msg_count=1, active_sec=120).engaged is True

    def test_one_user_msg_short_session_not_engaged(self):
        assert self._stat(user_msg_count=1, active_sec=10).engaged is False

    def test_zero_user_not_engaged(self):
        assert self._stat(user_msg_count=0, active_sec=600).engaged is False

    def test_scheduled_with_one_user_is_engaged(self):
        assert self._stat(is_scheduled=True, user_msg_count=1).engaged is True

    def test_scheduled_with_zero_user_not_engaged(self):
        assert self._stat(is_scheduled=True, user_msg_count=0).engaged is False


class TestWallClockDedup:
    def _stat_with_ts(self, timestamps: list[float]) -> SessionStat:
        from datetime import datetime
        return SessionStat(
            project="x",
            category="coding",
            session_id="s",
            start=datetime(2026, 5, 10),
            end=datetime(2026, 5, 10),
            active_sec=0,
            msg_count=len(timestamps),
            user_msg_count=1,
            timestamps=timestamps,
        )

    def test_no_overlap_sums_normally(self):
        a = self._stat_with_ts([0.0, 60.0, 120.0])      # 2×60s = 120s
        b = self._stat_with_ts([300.0, 360.0])          # 60s
        # Combined: gaps are 60, 60, 180-capped-to-300, 60 → 60+60+180+60 = 360
        # But 180 < cap, so all gaps under cap → sum = 60+60+180+60 = 360
        assert wall_clock_active_sec([a, b]) == 360

    def test_overlapping_timestamps_deduped(self):
        # Two sessions with identical timestamps — should dedup, not double count
        a = self._stat_with_ts([0.0, 60.0])
        b = self._stat_with_ts([0.0, 60.0])
        # All timestamps merged + sorted: [0, 0, 60, 60]
        # Gaps: 0, 60, 0 → contributes 60s (0-gaps skipped by `if gap <= 0`)
        assert wall_clock_active_sec([a, b]) == 60

    def test_empty_returns_zero(self):
        assert wall_clock_active_sec([]) == 0


class TestRollupByCategory:
    def _stat(self, category: str, active_sec: int, msg_count: int = 1) -> SessionStat:
        from datetime import datetime
        return SessionStat(
            project=category,
            category=category,
            session_id=f"s-{category}-{active_sec}",
            start=datetime(2026, 5, 10),
            end=datetime(2026, 5, 10),
            active_sec=active_sec,
            msg_count=msg_count,
            user_msg_count=1,
            timestamps=[float(i) for i in range(0, active_sec + 1, 60)] or [0.0],
        )

    def test_rollup_sorts_by_active_time_desc(self):
        stats = [
            self._stat("writing", 120),
            self._stat("coding", 600),
            self._stat("investment", 300),
        ]
        rollups = rollup_by_category(stats, dedup_to_wall_clock=False)
        cats = [r.category for r in rollups]
        assert cats == ["coding", "investment", "writing"]

    def test_session_counts_per_bucket(self):
        stats = [
            self._stat("coding", 60),
            self._stat("coding", 120),
            self._stat("writing", 60),
        ]
        rollups = rollup_by_category(stats, dedup_to_wall_clock=False)
        by_cat = {r.category: r for r in rollups}
        assert by_cat["coding"].sessions == 2
        assert by_cat["writing"].sessions == 1
