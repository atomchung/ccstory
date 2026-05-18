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
from ccstory.time_tracking import SessionStat, rollup_by_category
from ccstory.trends import _resolve_sessions_from_cache, compare_to_previous
from ccstory.token_usage import UsageReport


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
