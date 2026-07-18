"""Tests for #83 — --json machine-readable output."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ccstory.artifacts import ArtifactsReport, PyPIDownloads, RepoArtifacts
from ccstory.report import JSON_SCHEMA_VERSION, build_report_json, build_trend_json
from ccstory.session_summarizer import SessionSummary
from ccstory.time_tracking import CategoryRollup, SessionStat
from ccstory.token_usage import ModelUsage, UsageReport
from ccstory.trends import CategoryDelta, PeriodComparison, PeriodPoint

SINCE = datetime(2026, 7, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 7, 8, tzinfo=timezone.utc)


def _stat(sid: str = "s1", bucket: str = "coding", mins: int = 60) -> SessionStat:
    base = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    return SessionStat(
        project="-Users-t-myapp", category=bucket, session_id=sid,
        start=base, end=base, active_sec=mins * 60, msg_count=10,
        first_user_text="fix the login bug",
    )


def _usage() -> UsageReport:
    rep = UsageReport(since=SINCE, until=UNTIL)
    rep.by_model["claude-opus-4-7"] = ModelUsage(
        model="claude-opus-4-7", turns=5, input_tokens=1000, output_tokens=500,
    )
    rep.assistant_turns = 5
    return rep


def _build(
    summaries: dict | None = None,
    comparison: PeriodComparison | None = None,
    artifacts: ArtifactsReport | None = None,
) -> dict:
    s = _stat()
    rollup = CategoryRollup(
        category="coding", active_min=60.0, sessions=1, messages=10,
        top_sessions=[s],
    )
    return build_report_json(
        label="2026-W27", since=SINCE, until=UNTIL,
        sessions=[s], rollups=[rollup], usage=_usage(),
        summaries=summaries or {},
        overall_narrative="You mostly fixed login.",
        comparison=comparison, artifacts=artifacts,
    )


class TestReportJson:
    def test_envelope_and_window(self):
        p = _build()
        assert p["schema_version"] == JSON_SCHEMA_VERSION
        assert p["kind"] == "recap"
        assert p["window"] == {
            "label": "2026-W27",
            "since": SINCE.isoformat(),
            "until": UNTIL.isoformat(),
        }

    def test_totals_and_buckets(self):
        p = _build()
        assert p["totals"]["active_hours"] == 1.0
        assert p["totals"]["sessions"] == 1
        assert p["buckets"] == [{
            "name": "coding", "active_hours": 1.0, "share": 1.0,
            "sessions": 1, "messages": 10, "narrative": None,
            # Additive layer-2 (#69); empty when the rollup carries no projects.
            "projects": [],
        }]

    def test_session_summary_precedence(self):
        # Cached summary wins; first_user_text is the fallback.
        no_cache = _build()
        assert no_cache["sessions"][0]["summary"] == "fix the login bug"
        assert no_cache["sessions"][0]["summary_source"] == "first_message"
        cached = _build(summaries={
            "s1": SessionSummary(
                session_id="s1", summary="Fixed the login flow end to end.",
                source="auto",
            ),
        })
        assert cached["sessions"][0]["summary"] == "Fixed the login flow end to end."
        assert cached["sessions"][0]["summary_source"] == "auto"

    def test_comparison_block(self):
        cmp = PeriodComparison(
            current_label="2026-W27", previous_label="2026-W26",
            deltas=[CategoryDelta(category="coding", current_min=60.0, previous_min=30.0)],
            current_total_h=1.0, previous_total_h=0.5,
            current_output_tokens=500, previous_output_tokens=250,
            current_cost_usd=10.0, previous_cost_usd=5.0,
        )
        p = _build(comparison=cmp)
        c = p["comparison"]
        assert c["previous_label"] == "2026-W26"
        assert c["deltas"][0] == {
            "bucket": "coding", "current_min": 60.0, "previous_min": 30.0,
            "delta_min": 30.0, "pct_change": 100.0,
        }

    def test_comparison_none(self):
        assert _build()["comparison"] is None

    def test_artifacts_block(self):
        arts = ArtifactsReport(
            repos=[RepoArtifacts(
                root=Path("/x/myapp"), name="myapp", github="t/myapp",
                commits=3, prs_merged=1, releases=["v1.0"], stars=10, stars_delta=2,
            )],
            pypi=[PyPIDownloads(package="myapp", downloads=42, window="last_week")],
        )
        p = _build(artifacts=arts)
        a = p["artifacts"]
        assert a["repos"][0]["commits"] == 3
        assert a["pypi"] == [
            {"package": "myapp", "downloads": 42, "window": "last_week"},
        ]
        assert a["totals"] == {"commits": 3, "prs_merged": 1, "releases": 1}

    def test_artifacts_none(self):
        assert _build()["artifacts"] is None

    def test_json_serializable_roundtrip(self):
        # datetimes and dataclasses must all be plain types by now.
        p = _build()
        assert json.loads(json.dumps(p, ensure_ascii=False)) == p


class TestTrendJson:
    def test_points(self):
        s = _stat()
        pt = PeriodPoint(
            label="2026-W27", since=SINCE, until=UNTIL,
            rollups=[CategoryRollup(
                category="coding", active_min=90.0, sessions=2, messages=20,
                top_sessions=[s],
            )],
            total_h=1.5, output_tokens=1000, cost_usd=12.0,
        )
        p = build_trend_json([pt], "week")
        assert p["schema_version"] == JSON_SCHEMA_VERSION
        assert p["kind"] == "trend"
        assert p["period"] == "week"
        assert p["points"][0]["total_hours"] == 1.5
        assert p["points"][0]["buckets"] == [
            {"name": "coding", "active_hours": 1.5, "sessions": 2},
        ]
        assert json.loads(json.dumps(p)) == p


class TestJsonFlagParsing:
    """Mirror the parser shape from cli.main(), same convention as
    test_cli_flags._build_parser."""

    def _build_parser(self) -> argparse.ArgumentParser:
        p = argparse.ArgumentParser()
        p.add_argument("window", nargs="?", default="month")
        p.add_argument("--format", dest="output_format",
                       choices=("auto", "markdown", "card", "json"),
                       default="auto")
        p.add_argument("--json", dest="output_format", action="store_const",
                       const="json")
        return p

    def test_json_shorthand_sets_format(self):
        args = self._build_parser().parse_args(["week", "--json"])
        assert args.output_format == "json"

    def test_format_json_equivalent(self):
        args = self._build_parser().parse_args(["week", "--format", "json"])
        assert args.output_format == "json"

    def test_default_stays_auto(self):
        args = self._build_parser().parse_args(["week"])
        assert args.output_format == "auto"

    def test_resolve_passes_json_through(self):
        from ccstory.cli import resolve_output_format
        assert resolve_output_format("json") == "json"
