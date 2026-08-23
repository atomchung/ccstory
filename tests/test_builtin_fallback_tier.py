"""Tests for issue #214 — unify the deterministic built-in folder fallback
tier across preview, live recap, and cache-only (trend/comparison) resolution.

Before this fix, ``preview_classification()`` (the first-run preview table)
classified projects through ``load_rules()`` + ``classify()``, which merges
the user's ``[categories]`` config with the built-in ``DEFAULT_RULES`` in one
pass. The real per-session resolver (``resolve_session_bucket`` and its
callers in ``recap._resolve_all_sessions`` / ``trends._resolve_sessions_from_cache``)
never consulted ``DEFAULT_RULES`` at all: a session with no explicit user
rule and no cached/fresh content classification collapsed straight to the
scalar ``default_bucket``. A brand-new user with no config therefore saw a
multi-category preview immediately followed by a one-bucket report.

These tests cover the shared ``builtin_or_fallback`` tier this fix adds:

  * ``TestResolveAllSessionsBuiltinFallback`` — ``recap._resolve_all_sessions``
    applies the built-in tier in hybrid mode once fresh content
    classification is unavailable/ineligible; content mode does not.
  * ``TestResolveSessionsFromCacheBuiltinFallback`` — the cache-only lane
    behind ``compare_to_previous`` / ``collect_trend``
    (``trends._resolve_sessions_from_cache``) applies the same tier on a
    hybrid cache miss, firing no model call.
  * ``TestPreviewMatchesFolderContract`` — ``preview_classification`` now
    resolves through the exact same deterministic folder contract
    (``resolve_session_bucket(..., mode="folder")``) as everything else.
  * ``TestFirstRunPreviewMatchesReport`` — the fresh-HOME end-to-end
    regression: a first-run preview's multi-category split must be
    reproduced by the very next report, with no fresh model call available.
  * ``TestBoundaryCrossingSymmetry`` — a session split across a period
    boundary resolves to the identical built-in bucket in both periods.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Console

from ccstory import categorizer, recap
from ccstory import session_summarizer as ss
from ccstory.categorizer import preview_classification, resolve_session_bucket
from ccstory.cli import _print_first_run_preview
from ccstory.providers import collect_provider_snapshot
from ccstory.recap import build_recap
from ccstory.time_tracking import SessionSlice, SessionStat
from ccstory.trends import _resolve_sessions_from_cache, collect_trend
from tests.conftest import make_assistant_msg, make_user_msg

# A leaf that hits the built-in "investment" rule via the "stock" keyword,
# and one brand-name leaf that hits nothing in DEFAULT_RULES — same fixtures
# tests/test_resolver.py uses, kept local so this file stays self-contained.
PROJ_INVESTMENT = "-Users-alice-Side-project-stock"
PROJ_BRANDED = "-Users-alice-Side-project-mybranded"


def _stat(project: str, category: str = "") -> SessionStat:
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return SessionStat(
        project=project,
        category=category,
        session_id=f"sess-{project}",
        start=now,
        end=now + timedelta(minutes=5),
        active_sec=300,
        msg_count=4,
        user_msg_count=2,
        first_user_text="hello",
    )


class TestResolveAllSessionsBuiltinFallback:
    """`recap._resolve_all_sessions` — the live-report resolution path."""

    def test_hybrid_uses_builtin_tier_when_no_eligible_summary(
        self, tmp_home,
    ):
        # No cached content bucket, no summary at all (so nothing is
        # synthesis-eligible and classify_sessions_by_content never even
        # gets an item to send). Hybrid mode still owes the built-in tier
        # before collapsing to the scalar fallback (#214).
        s = _stat(PROJ_INVESTMENT)
        recap._resolve_all_sessions(
            [s], summaries={}, mode="hybrid", fallback="other",
            console=Console(quiet=True),
        )
        assert s.category == "investment"
        assert s.category_source == "builtin_rule"

    def test_content_mode_stays_scalar_only(self, tmp_home):
        # Same project, same empty summaries — but content mode must never
        # apply the built-in tier. It stays content-only end to end.
        s = _stat(PROJ_INVESTMENT)
        recap._resolve_all_sessions(
            [s], summaries={}, mode="content", fallback="other",
            console=Console(quiet=True),
        )
        assert s.category == "other"
        assert s.category_source == "fallback"

    def test_unmatched_project_still_uses_scalar_fallback(self, tmp_home):
        s = _stat(PROJ_BRANDED)
        recap._resolve_all_sessions(
            [s], summaries={}, mode="hybrid", fallback="other",
            console=Console(quiet=True),
        )
        assert s.category == "other"
        assert s.category_source == "fallback"

    def test_llm_fresh_still_wins_over_builtin_tier(self, tmp_home, monkeypatch):
        # When fresh content classification *does* land a bucket, it must
        # still win — the built-in tier is a fallback, never a competitor.
        s = _stat(PROJ_INVESTMENT)
        summary = ss.SessionSummary(
            f"sess-{PROJ_INVESTMENT}", "Wrote some prose", "provided",
        )
        monkeypatch.setattr(recap, "_classify_cache_get_many", lambda _ids: {})
        monkeypatch.setattr(
            recap, "classify_sessions_by_content",
            lambda items, **_kwargs: {sid: "writing" for sid, *_ in items},
        )
        recap._resolve_all_sessions(
            [s], summaries={f"sess-{PROJ_INVESTMENT}": summary},
            mode="hybrid", fallback="other", console=Console(quiet=True),
        )
        assert s.category == "writing"
        assert s.category_source == "llm_fresh"


class TestResolveSessionsFromCacheBuiltinFallback:
    """`trends._resolve_sessions_from_cache` — the cache-only lane behind
    `compare_to_previous` / `collect_trend`. Must never fire a model call."""

    def test_hybrid_cache_miss_uses_builtin_tier(self, tmp_home, monkeypatch):
        monkeypatch.setattr(
            ss, "classify_sessions_by_content",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("cache-only lane must not classify fresh"),
            ),
        )
        s = _stat(PROJ_INVESTMENT)
        _resolve_sessions_from_cache([s], mode="hybrid", fallback="other")
        assert s.category == "investment"
        assert s.category_source == "builtin_rule"

    def test_content_mode_cache_miss_stays_scalar(self, tmp_home):
        s = _stat(PROJ_INVESTMENT)
        _resolve_sessions_from_cache([s], mode="content", fallback="other")
        assert s.category == "other"
        assert s.category_source == "fallback"

    def test_cache_hit_still_wins_over_builtin_tier(self, tmp_home):
        from ccstory.session_summarizer import _classify_cache_upsert_many
        from ccstory.session_identity import evidence_session_id

        s = _stat(PROJ_INVESTMENT)
        _classify_cache_upsert_many({evidence_session_id(s): "research"})
        _resolve_sessions_from_cache([s], mode="hybrid", fallback="other")
        assert s.category == "research"
        assert s.category_source == "llm_cache"


class TestPreviewMatchesFolderContract:
    """`preview_classification` must resolve through the exact same
    deterministic folder contract as everything else (#214)."""

    @pytest.mark.parametrize("project", [PROJ_INVESTMENT, PROJ_BRANDED])
    def test_matches_resolve_session_bucket_folder_mode(
        self, tmp_home, project,
    ):
        expected_bucket, _source = resolve_session_bucket(
            project, None, mode="folder",
        )
        preview = preview_classification([project])
        (bucket,) = preview.keys()
        assert bucket == expected_bucket

    def test_user_rule_beats_builtin_in_preview(self, tmp_home):
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('[categories]\n"writing" = ["stock"]\n', encoding="utf-8")
        preview = preview_classification([PROJ_INVESTMENT])
        assert set(preview.keys()) == {"writing"}


class TestFirstRunPreviewMatchesReport:
    """Fresh-HOME end-to-end regression: the first-run preview's multi-
    category split must be reproduced by the very next report — the
    original #214 bug report."""

    _PROJECTS: dict[str, str] = {
        "-Users-newuser-code-acme-stock-tracker": "investment",  # "stock"
        "-Users-newuser-code-acme-newsletter": "writing",        # "newsletter"
        "-Users-newuser-code-acme-mcp-server": "coding",         # "mcp"/"server"
    }

    @pytest.fixture(autouse=True)
    def _no_fresh_model_call(self, monkeypatch):
        # The exact "brand-new install" shape: no configured local narrator
        # at all, so no summary is ever synthesis-eligible and no fresh
        # content classification can happen. tmp_home's autouse fixture
        # already forces codex/antigravity off; this adds claude.
        monkeypatch.setattr(ss, "claude_bin_available", lambda: False)

    @staticmethod
    def _recent_ts(hours_ago: float) -> str:
        dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")

    def _seed(self, jsonl_factory, project: str, sid: str) -> None:
        jsonl_factory(project, sid, [
            make_user_msg("Working on this repo", self._recent_ts(2.5)),
            make_assistant_msg(
                "Got it, looking now.", self._recent_ts(2.4), f"{sid}-m1",
            ),
            make_user_msg("Thanks, one more thing", self._recent_ts(2.3)),
            make_assistant_msg("Done.", self._recent_ts(2.2), f"{sid}-m2"),
        ])

    def test_preview_and_report_resolve_the_same_multi_category_split(
        self, tmp_home, jsonl_factory,
    ):
        for idx, project in enumerate(self._PROJECTS):
            self._seed(jsonl_factory, project, f"sess-{idx}")

        # Genuinely first run: no ~/.ccstory/config.toml yet.
        assert not categorizer.CONFIG_PATH.exists()

        # --- Preview: exactly what `ccstory` prints before its first report
        # (cli._dispatch calls _print_first_run_preview(console) then
        # build_recap(...) in the same invocation — see ccstory/cli.py).
        console = Console(record=True, width=100)
        _print_first_run_preview(console)
        assert categorizer.CONFIG_PATH.exists()  # scaffolded by the preview

        preview = preview_classification(list(self._PROJECTS))
        preview_buckets = {bucket for bucket, items in preview.items() if items}

        # The preview must show more than one category — the entire point
        # of a "default bucket preview" is to demonstrate the built-in
        # split across a user's real projects.
        assert len(preview_buckets) >= 2
        assert preview_buckets == set(self._PROJECTS.values())

        # --- Report: the very next command a first-run user runs, same
        # process, same (now-scaffolded) config, still no narrator.
        result = build_recap("week")
        report_by_project = {s.project: s.category for s in result.sessions}
        report_buckets = set(report_by_project.values())

        # #214: before the fix, every one of these sessions collapsed to
        # the scalar default_bucket ("coding") — report_buckets == {"coding"}
        # — regardless of what the preview promised. After the fix, the
        # report must reproduce the exact same deterministic multi-category
        # split, entirely via the built-in folder tier (no narrator
        # available in this test at all).
        assert report_buckets == preview_buckets
        assert len(report_buckets) >= 2
        assert report_by_project == self._PROJECTS
        for session in result.sessions:
            assert session.category_source == "builtin_rule"


class TestBoundaryCrossingSymmetry:
    """A session split across a trend-period boundary must resolve to the
    identical built-in bucket in both periods — `builtin_rule_match` depends
    only on `.project`, which is identical for both slices of one physical
    session, so this is symmetric by construction. Locked down directly
    since #214 changes what a boundary-crossing session's ``needs_llm``
    collapse resolves to."""

    NOW = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    BOUNDARY = NOW - timedelta(days=7)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    def test_both_sides_of_a_crossing_session_agree_on_the_builtin_bucket(
        self, tmp_home, jsonl_factory,
    ):
        jsonl_factory(
            PROJ_INVESTMENT,
            "crossing-session",
            [
                make_user_msg(
                    "request before boundary",
                    self._iso(self.BOUNDARY - timedelta(minutes=1)),
                ),
                make_assistant_msg(
                    "outcome after boundary",
                    self._iso(self.BOUNDARY + timedelta(minutes=1)),
                    "crossing-a1",
                ),
                make_user_msg(
                    "confirm outcome",
                    self._iso(self.BOUNDARY + timedelta(minutes=2)),
                ),
            ],
        )

        points = collect_trend(
            period="week", count=2, now=self.NOW,
            mode="hybrid", fallback="other", agent="claude",
        )
        assert len(points) == 2
        older, newer = points

        # Both periods must resolve every session to the same deterministic
        # built-in bucket — no cache seeded, no narrator available, so this
        # can only happen via the shared built-in-or-fallback tier.
        assert {r.category for r in older.rollups} == {"investment"}
        assert {r.category for r in newer.rollups} == {"investment"}

        window_map = {older.label: (older.since, older.until),
                      newer.label: (newer.since, newer.until)}
        snapshot = collect_provider_snapshot(window_map, agent="claude")
        older_slice = snapshot.sessions_by_window[older.label][0]
        newer_slice = snapshot.sessions_by_window[newer.label][0]
        assert isinstance(older_slice, SessionSlice)
        assert isinstance(newer_slice, SessionSlice)
        assert older_slice.session_id != newer_slice.session_id  # distinct evidence ids

        sessions = [older_slice, newer_slice]
        _resolve_sessions_from_cache(sessions, mode="hybrid", fallback="other")
        assert {s.category_source for s in sessions} == {"builtin_rule"}
        assert {s.category for s in sessions} == {"investment"}
