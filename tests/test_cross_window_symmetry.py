"""Regression test for bug #61: cross-window category asymmetry.

Before PR-A: `compare_to_previous` ran prev-window sessions through
folder-only classify, while current-window sessions went through LLM content
classification. Result: current window showed 5-6 LLM buckets, prev window
collapsed to `coding` (fallback). Comparison block was uninterpretable.

After PR-A: both windows resolve via `categorizer.resolve_session_bucket`,
reading the same `session_content_buckets` cache. When the same session id
exists in both windows' cache, it gets the same bucket regardless of which
window pulled it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ccstory import session_summarizer
from ccstory.session_summarizer import _classify_cache_upsert_many
from ccstory.time_tracking import SessionStat, collect_sessions, rollup_by_category
from ccstory.trends import (
    _resolve_sessions_from_cache,
    collect_trend,
    compare_to_previous,
)
from ccstory.token_usage import UsageReport, collect_usage


def _stat(
    session_id: str,
    project: str,
    start: datetime,
    active_sec: int = 300,
) -> SessionStat:
    """Build a SessionStat fixture with no resolved category (post-PR-A shape)."""
    return SessionStat(
        project=project,
        category="",  # unresolved — caller must run resolver
        session_id=session_id,
        start=start,
        end=start + timedelta(seconds=active_sec),
        active_sec=active_sec,
        msg_count=4,
        user_msg_count=2,
        first_user_text="hello",
    )


class TestCrossWindowResolverSymmetry:
    """When prev-window sessions have LLM cache entries, they must resolve to
    the same buckets as if they were current-window sessions."""

    def test_resolve_from_cache_uses_same_priority_chain(self, tmp_home: Path):
        # Two sessions with cached LLM verdicts: one in writing, one in research.
        # (No user rules — these projects are brand-name folders that
        # default rules can't classify.)
        _classify_cache_upsert_many({
            "sess-writing": "writing",
            "sess-research": "research",
        })

        sessions = [
            _stat("sess-writing", "-Users-x-Side-project-ccstory",
                  datetime(2026, 5, 10, tzinfo=timezone.utc)),
            _stat("sess-research", "-Users-x-Side-project-personal-os",
                  datetime(2026, 5, 10, tzinfo=timezone.utc)),
        ]
        _resolve_sessions_from_cache(sessions, mode="hybrid", fallback="other")

        # Both must come back with the cached buckets, NOT the fallback
        by_id = {s.session_id: s for s in sessions}
        assert by_id["sess-writing"].category == "writing"
        assert by_id["sess-writing"].category_source == "llm_cache"
        assert by_id["sess-research"].category == "research"

    def test_cache_miss_falls_to_fallback_not_needs_llm(self, tmp_home: Path):
        # Critical for `compare_to_previous`: it MUST NOT fire fresh LLM
        # for prev-window cache misses (would surprise the user with cost).
        sessions = [
            _stat("sess-uncached", "-Users-x-Side-project-novel-project",
                  datetime(2026, 5, 10, tzinfo=timezone.utc)),
        ]
        _resolve_sessions_from_cache(sessions, mode="hybrid", fallback="other")
        assert sessions[0].category == "other"
        assert sessions[0].category_source == "fallback"

    def test_user_rule_still_wins_in_cache_only_resolver(self, tmp_home: Path):
        # User explicitly pinned this folder. Even with a stale cache entry,
        # the user rule must take precedence (matches main-flow behaviour).
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.write_text(
            '[categories]\n"investment" = ["personal-os"]\n', encoding="utf-8",
        )
        _classify_cache_upsert_many({"sess-x": "writing"})  # stale cache

        sessions = [
            _stat("sess-x", "-Users-x-Side-project-personal-os",
                  datetime(2026, 5, 10, tzinfo=timezone.utc)),
        ]
        _resolve_sessions_from_cache(sessions, mode="hybrid", fallback="other")
        # user_rule beats llm_cache
        assert sessions[0].category == "investment"
        assert sessions[0].category_source == "user_rule"


class TestRollupAfterResolver:
    """rollup_by_category should produce same bucket vocabulary for both
    windows once they've been through the same resolver."""

    def test_current_and_prev_produce_same_buckets(self, tmp_home: Path):
        # Set up cache so both windows' sessions resolve to "writing"
        _classify_cache_upsert_many({
            "sess-cur": "writing",
            "sess-prev": "writing",
        })

        current = [_stat("sess-cur", "-Users-x-Side-project-ccstory",
                         datetime(2026, 5, 18, tzinfo=timezone.utc))]
        prev = [_stat("sess-prev", "-Users-x-Side-project-ccstory",
                      datetime(2026, 5, 11, tzinfo=timezone.utc))]

        _resolve_sessions_from_cache(current, mode="hybrid", fallback="other")
        _resolve_sessions_from_cache(prev, mode="hybrid", fallback="other")

        cur_rollups = rollup_by_category(current)
        prev_rollups = rollup_by_category(prev)

        # Both rollups must surface the SAME bucket name.
        # Pre-PR-A: current = ["writing"], prev = ["coding"] (asymmetric, #61).
        # Post-PR-A: both = ["writing"].
        assert {r.category for r in cur_rollups} == {"writing"}
        assert {r.category for r in prev_rollups} == {"writing"}


class TestCodexUsageWindowSymmetry:
    def test_trend_and_comparison_keep_only_each_windows_cumulative_delta(
        self, codex_factory,
    ):
        def token_count(ts: str, inp: int, out: int) -> dict:
            return {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": inp,
                            "cached_input_tokens": 0,
                            "cache_write_input_tokens": 0,
                            "output_tokens": out,
                        },
                    },
                },
            }

        codex_factory(
            "cross-window-codex",
            [
                {
                    "timestamp": "2026-07-08T10:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": "cross-window-codex",
                        "id": "cross-window-codex",
                        "cwd": "/Users/me/proj",
                    },
                },
                {
                    "timestamp": "2026-07-08T10:01:00Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-sol"},
                },
                token_count("2026-07-08T11:00:00Z", 100, 10),
                {
                    "timestamp": "2026-07-10T10:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "First"},
                },
                token_count("2026-07-10T11:00:00Z", 300, 30),
                {
                    "timestamp": "2026-07-17T10:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Continue"},
                },
                token_count("2026-07-17T11:00:00Z", 700, 70),
                token_count("2026-07-23T11:00:00Z", 900, 90),
            ],
        )
        now = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
        points = collect_trend(
            period="week", count=2, now=now, agent="codex",
        )
        assert [point.output_tokens for point in points] == [20, 40]

        since = now - timedelta(days=7)
        sessions = collect_sessions(since, now, agent="codex")
        _resolve_sessions_from_cache(sessions, mode="folder", fallback="coding")
        current_usage = collect_usage(since, now, agent="codex")
        comparison = compare_to_previous(
            current_sessions=sessions,
            current_rollups=rollup_by_category(sessions),
            current_usage=current_usage,
            current_label="current",
            since=since,
            until=now,
            mode="folder",
            fallback="coding",
            agent="codex",
        )
        assert comparison is not None
        assert comparison.current_output_tokens == 40
        assert comparison.previous_output_tokens == 20
