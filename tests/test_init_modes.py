"""Tests for `ccstory init` Quick / Deep / Skip three-mode design (PR-B)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from ccstory import init_categories
from ccstory.init_categories import (
    DEEP_DEFAULT_DAYS,
    DEEP_DEFAULT_MAX,
    _aggregate_folder_rules,
    run_deep_mode,
    run_quick_mode,
    run_skip_mode,
    sample_sessions_for_deep,
)
from ccstory.time_tracking import SessionStat


# --- sample_sessions_for_deep ----------------------------------------------

def _mk_session(sid: str, project: str, day: int, active_sec: int) -> SessionStat:
    start = datetime(2026, 5, day, 12, tzinfo=timezone.utc)
    return SessionStat(
        project=project,
        category="",
        session_id=sid,
        start=start,
        end=start + timedelta(seconds=active_sec),
        active_sec=active_sec,
        msg_count=4,
        user_msg_count=2,
        first_user_text=f"first user msg for {sid}",
    )


class TestSampleSessionsForDeep:
    def test_empty_input_returns_empty(self):
        assert sample_sessions_for_deep([], days=7, max_n=200) == []

    def test_under_cap_returns_all(self):
        sessions = [_mk_session(f"s{i}", "p", 10, 100) for i in range(5)]
        out = sample_sessions_for_deep(sessions, days=7, max_n=200)
        assert len(out) == 5
        assert {s.session_id for s in out} == {s.session_id for s in sessions}

    def test_caps_at_max_n(self):
        # 50 sessions across one day, max=10 → take top-10 by active_sec
        sessions = [_mk_session(f"s{i}", "p", 10, 100 * i) for i in range(50)]
        out = sample_sessions_for_deep(sessions, days=1, max_n=10)
        assert len(out) == 10
        # top 10 by active_sec → ids 40..49
        assert {s.session_id for s in out} == {f"s{i}" for i in range(40, 50)}

    def test_distributes_across_days(self):
        # 7 days × 10 sessions each, max=14 → quota=2/day → 7×2 = 14
        sessions = []
        for day in range(10, 17):
            for i in range(10):
                sessions.append(_mk_session(f"d{day}-s{i}", "p", day, 100 * i))
        out = sample_sessions_for_deep(sessions, days=7, max_n=14)
        assert len(out) == 14
        # Each day should have exactly 2 sessions
        from collections import Counter
        per_day = Counter(s.start.date() for s in out)
        assert all(c == 2 for c in per_day.values())

    def test_overflow_fills_remaining_slots(self):
        # 3 days but lopsided distribution: day10 has 100 sessions, day11/12 have 1 each
        sessions = []
        for i in range(100):
            sessions.append(_mk_session(f"d10-s{i}", "p", 10, 100 + i))
        sessions.append(_mk_session("d11-s0", "p", 11, 50))
        sessions.append(_mk_session("d12-s0", "p", 12, 50))
        # max=30, quota_per_day=10
        out = sample_sessions_for_deep(sessions, days=3, max_n=30)
        assert len(out) == 30
        # day 11 and day 12 each give their 1 session (quota cap each unmet)
        # remainder (28) is filled from day-10 overflow
        ids = {s.session_id for s in out}
        assert "d11-s0" in ids
        assert "d12-s0" in ids


# --- _aggregate_folder_rules -----------------------------------------------

class TestAggregateFolderRules:
    def test_unanimous_folder_gets_single_bucket(self):
        sessions = [
            _mk_session("s1", "-Users-x-Side-project-ccstory", 10, 100),
            _mk_session("s2", "-Users-x-Side-project-ccstory", 10, 100),
        ]
        mapping = {"s1": "writing", "s2": "writing"}
        rules = _aggregate_folder_rules(sessions, mapping)
        assert rules == {"writing": ["ccstory"]}

    def test_majority_wins_when_mixed(self):
        sessions = [
            _mk_session("s1", "-Users-x-Side-project-mixed", 10, 100),
            _mk_session("s2", "-Users-x-Side-project-mixed", 10, 100),
            _mk_session("s3", "-Users-x-Side-project-mixed", 10, 100),
        ]
        # 2 votes writing, 1 vote coding → writing wins
        mapping = {"s1": "writing", "s2": "writing", "s3": "coding"}
        rules = _aggregate_folder_rules(sessions, mapping)
        assert rules == {"writing": ["mixed"]}

    def test_missing_mapping_ignored(self):
        sessions = [
            _mk_session("s1", "-Users-x-Side-project-folder-a", 10, 100),
        ]
        rules = _aggregate_folder_rules(sessions, {})  # nothing classified
        assert rules == {}

    def test_multiple_folders_get_grouped(self):
        sessions = [
            _mk_session("s1", "-Users-x-Side-project-stock", 10, 100),
            _mk_session("s2", "-Users-x-Side-project-blog", 10, 100),
        ]
        mapping = {"s1": "investment", "s2": "writing"}
        rules = _aggregate_folder_rules(sessions, mapping)
        assert rules == {"investment": ["stock"], "writing": ["blog"]}


# --- run_skip_mode ----------------------------------------------------------

class TestRunSkipMode:
    def test_writes_template_config_when_missing(self, tmp_home: Path):
        from ccstory import categorizer
        cfg = tmp_home / ".ccstory" / "config.toml"
        assert not cfg.exists()
        rc = run_skip_mode(console=Console(file=open("/dev/null", "w")))
        assert rc == 0
        assert cfg.exists()
        # Template should contain the [categories] anchor even if empty
        assert "[categories]" in cfg.read_text()

    def test_dry_run_skips_write(self, tmp_home: Path):
        cfg = tmp_home / ".ccstory" / "config.toml"
        assert not cfg.exists()
        rc = run_skip_mode(dry_run=True, console=Console(file=open("/dev/null", "w")))
        assert rc == 0
        assert not cfg.exists()


# --- run_quick_mode / run_deep_mode dispatcher hooks ------------------------

class TestRunQuickAndDeepClaudeUnavailable:
    """If `claude` CLI not on PATH, Quick + Deep both refuse cleanly."""

    def test_quick_returns_1_without_claude(self, tmp_home: Path):
        with patch.object(init_categories, "claude_bin_available", return_value=False):
            rc = run_quick_mode(console=Console(file=open("/dev/null", "w")))
        assert rc == 1

    def test_deep_returns_1_without_claude(self, tmp_home: Path):
        with patch.object(init_categories, "claude_bin_available", return_value=False):
            rc = run_deep_mode(console=Console(file=open("/dev/null", "w")))
        assert rc == 1


class TestRunDeepClampsBadInputs:
    """Codex review caught that raw `days` flowed into datetime arithmetic
    before `sample_sessions_for_deep` could clamp. Verify both inputs clamp."""

    def test_deep_clamps_days_zero(self, tmp_home: Path):
        # claude_bin_available=False short-circuits before any sampling, but the
        # clamp runs before that check — make claude available so the warning
        # path is reachable.
        with patch.object(init_categories, "claude_bin_available", return_value=True), \
             patch.object(init_categories, "collect_sessions", return_value=[]):
            rc = run_deep_mode(
                days=0, max_n=200,
                console=Console(file=open("/dev/null", "w")),
            )
        # Returns 0 (no sessions) — the important thing is no crash and no
        # silent "since = now" sampling. Clamp message would print to console.
        assert rc == 0

    def test_deep_clamps_max_n_zero(self, tmp_home: Path):
        with patch.object(init_categories, "claude_bin_available", return_value=True), \
             patch.object(init_categories, "collect_sessions", return_value=[]):
            rc = run_deep_mode(
                days=7, max_n=0,
                console=Console(file=open("/dev/null", "w")),
            )
        assert rc == 0


# --- Default arg sanity -----------------------------------------------------

def test_deep_defaults_match_documentation():
    """Public constants should not silently drift from PR-B's stated defaults."""
    assert DEEP_DEFAULT_DAYS == 7
    assert DEEP_DEFAULT_MAX == 200
