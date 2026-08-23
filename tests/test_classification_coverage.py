"""Tests for #256 — per-window classification-source coverage disclosure.

Covers three layers:

  * ``categorizer.classification_source_breakdown()`` — pure aggregation of
    ``sessions[*].category_source`` into the public ``by_source`` shape.
    Deterministic regardless of input order; defensive against an empty or
    unrecognized ``category_source``.
  * ``recap.build_recap()``'s ``content_lane`` derivation — "on" | "off",
    computed from the invocation's own ``classify`` / ``minimal`` arguments
    plus narrator availability, never from a session's resolved
    ``category_source`` counts (a real invocation can be "on" and still
    resolve zero sessions via fresh LLM, e.g. when every session is a cache
    hit — that must not read as "off").
  * The three render surfaces (JSON via ``build_report_json``, Markdown via
    ``render_report``, terminal via ``render_terminal_card``) that combine
    both into the user-facing disclosure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Console

from ccstory import recap
from ccstory.categorizer import (
    CLASSIFICATION_SOURCES,
    UNRESOLVED_CLASSIFICATION_SOURCE,
    classification_source_breakdown,
)
from ccstory.recap import build_recap
from ccstory.report import (
    CLASSIFICATION_FALLBACK_DISCLOSURE_SHARE,
    _classification_coverage_markdown_line,
    _classification_coverage_terminal_text,
    build_report_json,
    render_report,
    render_terminal_card,
)
from ccstory.time_tracking import CategoryRollup, SessionStat
from ccstory.token_usage import UsageReport
from tests.conftest import make_assistant_msg, make_user_msg

SINCE = datetime(2026, 7, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 7, 8, tzinfo=timezone.utc)

# Same fixtures tests/test_builtin_fallback_tier.py uses: a leaf that hits
# the built-in "investment" rule via the "stock" keyword, kept local so this
# file stays self-contained.
PROJ_INVESTMENT = "-Users-alice-Side-project-stock"


def _stat(
    sid: str = "s1", category_source: str = "", mins: float = 60.0,
    category: str = "coding",
) -> SessionStat:
    base = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    s = SessionStat(
        project="-Users-t-myapp", category=category, session_id=sid,
        start=base, end=base, active_sec=int(mins * 60), msg_count=10,
        first_user_text="fix the login bug",
    )
    s.category_source = category_source
    return s


def _usage() -> UsageReport:
    return UsageReport(since=SINCE, until=UNTIL)


def _recent_ts(hours_ago: float) -> str:
    value = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _seed_session(jsonl_factory, project: str, sid: str, hours_ago: float) -> None:
    """One engaged session (2 real user messages) `hours_ago` hours back."""
    records = [
        make_user_msg("Fix the login bug", _recent_ts(hours_ago)),
        make_assistant_msg(
            "Looking at auth.py", _recent_ts(hours_ago - 0.05), f"{sid}-m1",
        ),
        make_user_msg("Also add a regression test", _recent_ts(hours_ago - 0.1)),
        make_assistant_msg(
            "Done — patched and tested.", _recent_ts(hours_ago - 0.15), f"{sid}-m2",
        ),
    ]
    jsonl_factory(project, sid, records)


# ----- classification_source_breakdown() — pure aggregation -----------------


class TestClassificationSourceBreakdown:
    def test_empty_sessions(self):
        out = classification_source_breakdown([])
        assert out["sessions_total"] == 0
        assert out["by_source"] == {
            src: {"sessions": 0, "active_hours": 0.0}
            for src in CLASSIFICATION_SOURCES
        }
        # No "unresolved" key when nothing needed it.
        assert UNRESOLVED_CLASSIFICATION_SOURCE not in out["by_source"]

    def test_every_canonical_source_counted_and_summed(self):
        sessions = [
            _stat("a", "user_rule", mins=30),
            _stat("b", "llm_cache", mins=60),
            _stat("c", "llm_fresh", mins=15),
            _stat("d", "builtin_rule", mins=90),
            _stat("e", "fallback", mins=45),
        ]
        out = classification_source_breakdown(sessions)
        assert out["sessions_total"] == 5
        by = out["by_source"]
        assert by["user_rule"] == {"sessions": 1, "active_hours": 0.5}
        assert by["llm_cache"] == {"sessions": 1, "active_hours": 1.0}
        assert by["llm_fresh"] == {"sessions": 1, "active_hours": 0.25}
        assert by["builtin_rule"] == {"sessions": 1, "active_hours": 1.5}
        assert by["fallback"] == {"sessions": 1, "active_hours": 0.75}
        assert UNRESOLVED_CLASSIFICATION_SOURCE not in by

    def test_multiple_sessions_same_source_sum_hours(self):
        sessions = [
            _stat("a", "fallback", mins=30),
            _stat("b", "fallback", mins=90),
        ]
        out = classification_source_breakdown(sessions)
        assert out["by_source"]["fallback"] == {"sessions": 2, "active_hours": 2.0}

    def test_empty_category_source_is_defensively_unresolved(self):
        # SessionStat.category_source defaults to "" for anything that never
        # ran through resolve_session_bucket() — must still be counted.
        sessions = [_stat("a", "")]
        out = classification_source_breakdown(sessions)
        assert out["sessions_total"] == 1
        assert out["by_source"][UNRESOLVED_CLASSIFICATION_SOURCE] == {
            "sessions": 1, "active_hours": 1.0,
        }
        # Every canonical source stays zero-filled, not skipped.
        for src in CLASSIFICATION_SOURCES:
            assert out["by_source"][src]["sessions"] == 0

    def test_unrecognized_category_source_is_defensively_unresolved(self):
        # A hypothetical future resolver layer, or a typo/corrupt value —
        # must fold into the catch-all instead of a KeyError or silent drop.
        sessions = [_stat("a", "some_future_layer")]
        out = classification_source_breakdown(sessions)
        assert out["sessions_total"] == 1
        assert out["by_source"][UNRESOLVED_CLASSIFICATION_SOURCE]["sessions"] == 1

    def test_sessions_total_always_equals_input_length(self):
        sessions = [_stat(str(i), "fallback") for i in range(7)]
        out = classification_source_breakdown(sessions)
        assert out["sessions_total"] == len(sessions) == 7
        assert sum(v["sessions"] for v in out["by_source"].values()) == 7

    @pytest.mark.parametrize("seed", [0, 1, 2, 3])
    def test_deterministic_regardless_of_input_order(self, seed):
        import random

        sessions = [
            _stat("a", "user_rule", mins=10),
            _stat("b", "llm_cache", mins=20),
            _stat("c", "llm_fresh", mins=30),
            _stat("d", "builtin_rule", mins=40),
            _stat("e", "fallback", mins=50),
            _stat("f", "", mins=60),
        ]
        rng = random.Random(seed)
        shuffled = sessions[:]
        rng.shuffle(shuffled)
        out_a = classification_source_breakdown(sessions)
        out_b = classification_source_breakdown(shuffled)
        assert out_a == out_b
        # Key order is fixed resolver-priority order regardless of input
        # order — safe to render directly without an extra sort.
        assert list(out_a["by_source"].keys()) == [
            *CLASSIFICATION_SOURCES, UNRESOLVED_CLASSIFICATION_SOURCE,
        ]
        assert list(out_b["by_source"].keys()) == list(out_a["by_source"].keys())


# ----- content_lane derivation (recap.build_recap) ---------------------------


class TestContentLaneDerivation:
    """content_lane must come from invocation flags + narrator availability,
    checked *before* resolution runs — never inferred from how many
    sessions actually ended up `llm_fresh` (#256)."""

    def test_minimal_forces_lane_off_even_with_hybrid_and_narrator(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        monkeypatch.setattr(recap, "llm_available", lambda: True)
        _seed_session(jsonl_factory, PROJ_INVESTMENT, "s1", hours_ago=2)
        result = build_recap(
            "week", minimal=True, classify="hybrid",
            compare=False, artifacts=False, write_report=False,
        )
        assert result.content_lane == "off"

    def test_folder_mode_forces_lane_off_even_without_minimal(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        monkeypatch.setattr(recap, "llm_available", lambda: True)
        _seed_session(jsonl_factory, PROJ_INVESTMENT, "s1", hours_ago=2)
        result = build_recap(
            "week", minimal=False, classify="folder",
            compare=False, artifacts=False, write_report=False,
        )
        assert result.content_lane == "off"

    def test_narrator_unavailable_forces_lane_off(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        monkeypatch.setattr(recap, "llm_available", lambda: False)
        _seed_session(jsonl_factory, PROJ_INVESTMENT, "s1", hours_ago=2)
        result = build_recap(
            "week", minimal=False, classify="hybrid",
            compare=False, artifacts=False, write_report=False,
        )
        assert result.content_lane == "off"

    def test_hybrid_not_minimal_narrator_available_is_lane_on(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        monkeypatch.setattr(recap, "llm_available", lambda: True)
        _seed_session(jsonl_factory, PROJ_INVESTMENT, "s1", hours_ago=2)
        result = build_recap(
            "week", minimal=False, classify="hybrid",
            compare=False, artifacts=False, write_report=False,
        )
        assert result.content_lane == "on"

    def test_content_mode_not_minimal_narrator_available_is_lane_on(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        monkeypatch.setattr(recap, "llm_available", lambda: True)
        _seed_session(jsonl_factory, PROJ_INVESTMENT, "s1", hours_ago=2)
        result = build_recap(
            "week", minimal=False, classify="content",
            compare=False, artifacts=False, write_report=False,
        )
        assert result.content_lane == "on"

    def test_lane_on_survives_a_window_resolved_entirely_via_builtin_rule(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        """The critical non-inference case: every session this window
        resolves via the built-in folder tier (zero `llm_fresh`, because
        nothing ever reached `classify_sessions_by_content`), yet the
        invocation itself allowed fresh classification to run. content_lane
        must still read "on" — 0 fresh sessions is equally consistent with
        "lane off" and "lane on, resolved some other way", so it must never
        be inferred from that count."""
        monkeypatch.setattr(recap, "llm_available", lambda: True)
        # No cached content bucket and no eligible summary source beyond the
        # instant fallback narrative (no --llm-narrative), so this session
        # never becomes a `classify_sessions_by_content` candidate — it
        # resolves via the built-in folder tier in Pass 1/Pass 2's hybrid
        # fallback, not via fresh content classification.
        _seed_session(jsonl_factory, PROJ_INVESTMENT, "s1", hours_ago=2)
        result = build_recap(
            "week", minimal=False, classify="hybrid", llm_narrative=False,
            compare=False, artifacts=False, write_report=False,
        )
        assert result.content_lane == "on"
        breakdown = classification_source_breakdown(result.sessions)
        assert breakdown["by_source"]["llm_fresh"]["sessions"] == 0
        assert breakdown["by_source"]["builtin_rule"]["sessions"] >= 1


# ----- Terminal card conditional disclosure ----------------------------------


class TestTerminalDisclosureThreshold:
    def test_lane_off_shows_even_with_zero_fallback(self):
        sessions = [_stat("a", "builtin_rule", mins=60)]
        text = _classification_coverage_terminal_text(sessions, "off")
        assert text is not None
        assert "lane off" in text.plain

    def test_lane_on_low_fallback_share_stays_silent(self):
        # 1 of 10 sessions fallback = 10% < 25% threshold.
        sessions = [_stat("a", "builtin_rule")] * 9 + [_stat("b", "fallback")]
        text = _classification_coverage_terminal_text(sessions, "on")
        assert text is None

    def test_lane_on_high_fallback_share_shows(self):
        # 3 of 10 = 30% >= 25% threshold.
        sessions = (
            [_stat(f"r{i}", "builtin_rule") for i in range(7)]
            + [_stat(f"f{i}", "fallback") for i in range(3)]
        )
        text = _classification_coverage_terminal_text(sessions, "on")
        assert text is not None
        assert "fallback 3" in text.plain
        assert "lane off" not in text.plain

    def test_threshold_boundary_exact_25_percent_shows(self):
        sessions = (
            [_stat(f"r{i}", "builtin_rule") for i in range(3)]
            + [_stat(f"f{i}", "fallback") for i in range(1)]
        )
        assert len(sessions) == 4
        share = 1 / 4
        assert share == CLASSIFICATION_FALLBACK_DISCLOSURE_SHARE
        text = _classification_coverage_terminal_text(sessions, "on")
        assert text is not None

    def test_threshold_boundary_just_below_stays_silent(self):
        # 1 of 5 = 20% < 25%.
        sessions = (
            [_stat(f"r{i}", "builtin_rule") for i in range(4)]
            + [_stat("f0", "fallback")]
        )
        share = 1 / 5
        assert share < CLASSIFICATION_FALLBACK_DISCLOSURE_SHARE
        text = _classification_coverage_terminal_text(sessions, "on")
        assert text is None

    def test_empty_sessions_never_divides_by_zero(self):
        assert _classification_coverage_terminal_text([], "off") is None
        assert _classification_coverage_terminal_text([], "on") is None

    def test_unresolved_sessions_count_toward_fallback_share(self):
        # A defensively-caught "" / unrecognized source is a coverage gap
        # just like an explicit fallback — must contribute to the trigger.
        sessions = (
            [_stat(f"r{i}", "builtin_rule") for i in range(7)]
            + [_stat(f"u{i}", "") for i in range(3)]
        )
        text = _classification_coverage_terminal_text(sessions, "on")
        assert text is not None
        assert "fallback 3" in text.plain

    def test_line_format_matches_rules_content_fallback_shape(self):
        sessions = [
            _stat("a", "user_rule"), _stat("b", "builtin_rule"),
            _stat("c", "llm_cache"), _stat("d", "fallback"),
        ]
        text = _classification_coverage_terminal_text(sessions, "off")
        assert text is not None
        # rules = user_rule + builtin_rule = 2; content = llm_cache +
        # llm_fresh = 1 (lane off); fallback = 1.
        assert text.plain == "classification: rules 2 · content 1 (lane off) · fallback 1"


# ----- Markdown line: always present ------------------------------------------


class TestMarkdownAlwaysIncluded:
    def test_present_even_when_terminal_would_stay_silent(self):
        # Low fallback share, lane on — terminal line would be suppressed,
        # but Markdown always carries the full breakdown (#256 contract).
        sessions = [_stat("a", "builtin_rule")] * 9 + [_stat("b", "fallback")]
        line = _classification_coverage_markdown_line(sessions, "on")
        assert "Classification coverage" in line
        assert "content lane: **on**" in line

    def test_carries_every_canonical_source(self):
        sessions = [_stat("a", "user_rule")]
        line = _classification_coverage_markdown_line(sessions, "off")
        for src in CLASSIFICATION_SOURCES:
            assert f"`{src}`" in line

    def test_empty_sessions_still_renders_a_line(self):
        line = _classification_coverage_markdown_line([], "off")
        assert "Classification coverage (0 sessions)" in line

    def test_full_report_always_contains_the_line(self):
        sessions = [_stat("a", "fallback")]
        rollup = CategoryRollup(
            category="coding", active_min=60.0, sessions=1, messages=10,
            top_sessions=[sessions[0]],
        )
        md = render_report(
            label="2026-W27", since=SINCE, until=UNTIL,
            sessions=sessions, rollups=[rollup], usage=_usage(), summaries={},
            content_lane="off",
        )
        assert "Classification coverage" in md


# ----- Terminal card: full render_terminal_card() integration ---------------


class TestTerminalCardIntegration:
    """Exercise the actual `render_terminal_card()` wiring, not just the
    standalone helper — confirms the new parameter reaches the Panel."""

    def _render_text(self, sessions, rollup, content_lane) -> str:
        console = Console(width=72, record=True)
        console.print(render_terminal_card(
            since=SINCE, until=UNTIL, sessions=sessions, rollups=[rollup],
            usage=_usage(), content_lane=content_lane,
        ))
        return console.export_text()

    def test_lane_off_line_appears_in_rendered_card(self):
        sessions = [_stat("a", "builtin_rule", mins=60)]
        rollup = CategoryRollup(
            category="coding", active_min=60.0, sessions=1, messages=1,
            top_sessions=[],
        )
        out = self._render_text(sessions, rollup, "off")
        assert "classification: rules 1 · content 0 (lane off) · fallback 0" in out

    def test_lane_on_low_fallback_omits_line_from_rendered_card(self):
        sessions = [_stat("a", "builtin_rule", mins=60)]
        rollup = CategoryRollup(
            category="coding", active_min=60.0, sessions=1, messages=1,
            top_sessions=[],
        )
        out = self._render_text(sessions, rollup, "on")
        assert "classification:" not in out

    def test_default_content_lane_is_off_when_caller_omits_it(self):
        # print_terminal_card() / render_terminal_card() must keep working
        # for any pre-#256 caller that never learned about content_lane.
        sessions = [_stat("a", "builtin_rule", mins=60)]
        rollup = CategoryRollup(
            category="coding", active_min=60.0, sessions=1, messages=1,
            top_sessions=[],
        )
        console = Console(width=72, record=True)
        console.print(render_terminal_card(
            since=SINCE, until=UNTIL, sessions=sessions, rollups=[rollup],
            usage=_usage(),
        ))
        out = console.export_text()
        assert "lane off" in out


# ----- JSON payload: shape, backward compatibility, no leakage --------------


class TestClassificationCoverageJson:
    def test_payload_shape(self):
        sessions = [
            _stat("a", "user_rule", mins=30),
            _stat("b", "fallback", mins=30),
        ]
        rollup = CategoryRollup(
            category="coding", active_min=60.0, sessions=2, messages=10,
            top_sessions=sessions,
        )
        payload = build_report_json(
            label="2026-W27", since=SINCE, until=UNTIL,
            sessions=sessions, rollups=[rollup], usage=_usage(), summaries={},
            content_lane="on",
        )
        cc = payload["classification_coverage"]
        assert cc["sessions_total"] == 2
        assert cc["content_lane"] == "on"
        assert set(cc["by_source"]) == {*CLASSIFICATION_SOURCES}
        assert cc["by_source"]["user_rule"] == {"sessions": 1, "active_hours": 0.5}
        assert cc["by_source"]["fallback"] == {"sessions": 1, "active_hours": 0.5}

    def test_defaults_to_lane_off_when_caller_omits_it(self):
        # Backward-compat default for any pre-#256 direct caller of
        # build_report_json() that does not know about content_lane yet.
        s = _stat("a", "fallback")
        rollup = CategoryRollup(
            category="coding", active_min=60.0, sessions=1, messages=10,
            top_sessions=[s],
        )
        payload = build_report_json(
            label="2026-W27", since=SINCE, until=UNTIL,
            sessions=[s], rollups=[rollup], usage=_usage(), summaries={},
        )
        assert payload["classification_coverage"]["content_lane"] == "off"

    def test_additive_existing_keys_unchanged(self):
        # #256 must not rename, remove, or change any pre-existing field —
        # only add the new classification_coverage key. Cross-check against
        # the exact fields test_json_output.py already asserts elsewhere.
        s = _stat("a", "fallback")
        rollup = CategoryRollup(
            category="coding", active_min=60.0, sessions=1, messages=10,
            top_sessions=[s],
        )
        payload = build_report_json(
            label="2026-W27", since=SINCE, until=UNTIL,
            sessions=[s], rollups=[rollup], usage=_usage(), summaries={},
            overall_narrative="You mostly fixed login.",
        )
        assert payload["schema_version"] == 1
        assert payload["totals"]["active_hours"] == 1.0
        assert payload["totals"]["sessions"] == 1
        assert payload["buckets"][0]["name"] == "coding"
        assert payload["narrative"]["overall"] == "You mostly fixed login."
        # Pre-existing sibling coverage lane stays independent and intact.
        assert payload["usage_coverage"] == {
            "complete": True, "incomplete_agents": [], "providers": {},
        }
        assert "classification_coverage" in payload

    def test_no_path_or_content_leakage(self):
        # A session carrying an obviously sensitive project path / prompt
        # text — none of it may surface anywhere in the coverage block.
        s = _stat("secret-session-id", "fallback")
        s.project = "-Users-alice-Side-project-top-secret-plan"
        s.first_user_text = "my SSN is 123-45-6789, do not share this"
        s.cwd = "/Users/alice/top-secret-plan"
        rollup = CategoryRollup(
            category="coding", active_min=60.0, sessions=1, messages=10,
            top_sessions=[s],
        )
        payload = build_report_json(
            label="2026-W27", since=SINCE, until=UNTIL,
            sessions=[s], rollups=[rollup], usage=_usage(), summaries={},
        )
        cc = payload["classification_coverage"]
        import json as _json
        blob = _json.dumps(cc)
        assert "secret-session-id" not in blob
        assert "top-secret-plan" not in blob
        assert "123-45-6789" not in blob
        assert "/Users/alice" not in blob
        # Only the fixed vocabulary + numbers should appear as string values.
        allowed_string_values = {*CLASSIFICATION_SOURCES, "off", "on"}
        for key, value in cc["by_source"].items():
            assert key in allowed_string_values


# ----- Trend surface: cache-only, content_lane always "off" -----------------


class TestTrendClassificationCoverage:
    """`collect_trend()` / `build_trend_json()` reuse the same aggregation
    for free (#256 non-goal note: no dedicated trend rendering surface —
    only the existing JSON-shaped data structures carry this)."""

    def test_period_point_carries_breakdown_and_lane_always_off(
        self, tmp_home, jsonl_factory,
    ):
        from ccstory.trends import collect_trend

        # No explicit `now=` — collect_trend() defaults to real now(), same
        # basis _seed_session()'s `hours_ago` is relative to.
        _seed_session(jsonl_factory, PROJ_INVESTMENT, "s1", hours_ago=2)
        points = collect_trend(
            period="week", count=1,
            mode="hybrid", fallback="coding", agent="claude",
        )
        assert len(points) == 1
        cc = points[0].classification_coverage
        assert cc["content_lane"] == "off"
        assert cc["sessions_total"] == 1
        # hybrid mode + no LLM cache/summary → resolves via the built-in
        # folder tier for this project leaf, same as the recap path.
        assert cc["by_source"]["builtin_rule"]["sessions"] == 1

    def test_build_trend_json_carries_the_same_breakdown(
        self, tmp_home, jsonl_factory,
    ):
        from ccstory.report import build_trend_json
        from ccstory.trends import collect_trend

        _seed_session(jsonl_factory, PROJ_INVESTMENT, "s1", hours_ago=2)
        points = collect_trend(
            period="week", count=1,
            mode="hybrid", fallback="coding", agent="claude",
        )
        payload = build_trend_json(points, "week")
        point = payload["points"][0]
        assert point["classification_coverage"] == points[0].classification_coverage
        assert point["classification_coverage"]["content_lane"] == "off"
        assert point["classification_coverage"]["sessions_total"] == 1

    def test_period_point_without_classification_coverage_defaults_empty(self):
        # A hand-built PeriodPoint from before #256 (or a test fixture that
        # never learned about the field) still constructs fine.
        from ccstory.trends import PeriodPoint

        point = PeriodPoint(
            label="2026-W01", since=SINCE, until=UNTIL, rollups=[],
            total_h=0.0, output_tokens=0, cost_usd=0.0,
        )
        assert point.classification_coverage == {}


# ----- MCP surface: get_recap / get_trend natural passthrough ---------------


class TestMcpClassificationCoverage:
    def test_compact_recap_includes_breakdown_via_get_recap(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        from ccstory.mcp_server import get_recap

        _seed_session(jsonl_factory, PROJ_INVESTMENT, "s1", hours_ago=2)
        out = get_recap(window="week")  # default classify="folder"
        cc = out["classification_coverage"]
        assert cc["content_lane"] == "off"
        assert cc["sessions_total"] == 1
        assert set(cc["by_source"]) == set(CLASSIFICATION_SOURCES)

    def test_compact_recap_defensive_getattr_for_pre_256_result_double(self):
        # _compact_recap() must not crash on a hand-built stand-in (as
        # test_mcp_server.py's own compact-shape test already builds) that
        # predates the content_lane field.
        from types import SimpleNamespace

        from ccstory.mcp_server import _compact_recap

        now = datetime.now(timezone.utc)
        s = _stat("a", "fallback")
        out = _compact_recap(
            SimpleNamespace(
                agent="claude", label="week", since=now, until=now,
                sessions=[s], rollups=[], summaries={}, category_narratives={},
                overall_narrative=None, usage=_usage(), report_path=None,
            )
        )
        assert out["classification_coverage"]["content_lane"] == "off"
        assert out["classification_coverage"]["sessions_total"] == 1

    def test_get_trend_includes_breakdown_per_point(
        self, tmp_home, jsonl_factory,
    ):
        from ccstory.mcp_server import get_trend

        _seed_session(jsonl_factory, PROJ_INVESTMENT, "s1", hours_ago=2)
        out = get_trend(period="week", count=1)
        cc = out["points"][0]["classification_coverage"]
        assert cc["content_lane"] == "off"
        assert cc["sessions_total"] >= 1
