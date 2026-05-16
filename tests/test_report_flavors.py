"""Tests for #28 — markdown report flavors (plain vs obsidian)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ccstory.report import VALID_FLAVORS, render_report
from ccstory.session_summarizer import SessionSummary
from ccstory.time_tracking import CategoryRollup, SessionStat
from ccstory.token_usage import ModelUsage, UsageReport


def _stat(category: str, project: str, sid: str, mins: int = 30) -> SessionStat:
    base = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    return SessionStat(
        project=project,
        category=category,
        session_id=sid,
        start=base,
        end=base,
        active_sec=mins * 60,
        msg_count=10,
        user_msg_count=3,
        first_user_text="initial request",
    )


def _rollup(category: str, sessions: list[SessionStat]) -> CategoryRollup:
    return CategoryRollup(
        category=category,
        active_min=sum(s.active_sec for s in sessions) // 60,
        sessions=len(sessions),
        messages=sum(s.msg_count for s in sessions),
        top_sessions=sessions,
    )


def _usage(cost: float = 100.0) -> UsageReport:
    rep = UsageReport(
        since=datetime(2026, 5, 1, tzinfo=timezone.utc),
        until=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )
    rep.by_model["claude-opus-4-7"] = ModelUsage(
        model="claude-opus-4-7", turns=5, input_tokens=10000, output_tokens=5000,
    )
    rep.assistant_turns = 5
    return rep


class TestFlavorValidation:
    def test_valid_flavors_constant(self):
        assert "plain" in VALID_FLAVORS
        assert "obsidian" in VALID_FLAVORS

    def test_unknown_flavor_raises(self):
        s = _stat("coding", "-Users-alice-code-myapp", "s1")
        with pytest.raises(ValueError, match="unsupported flavor"):
            render_report(
                label="2026-05",
                since=datetime(2026, 5, 1, tzinfo=timezone.utc),
                until=datetime(2026, 5, 31, tzinfo=timezone.utc),
                sessions=[s],
                rollups=[_rollup("coding", [s])],
                usage=_usage(),
                summaries={},
                flavor="json",
            )


class TestPlainFlavor:
    def test_no_frontmatter(self):
        s = _stat("coding", "-Users-alice-code-myapp", "s1")
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("coding", [s])],
            usage=_usage(),
            summaries={},
        )
        # Plain output starts with the H1, not "---"
        assert md.startswith("# Claude Code Recap")
        assert "[[" not in md  # no wikilinks

    def test_session_line_no_wikilink(self):
        s = _stat("coding", "-Users-alice-code-myapp", "s1")
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("coding", [s])],
            usage=_usage(),
            summaries={
                "s1": SessionSummary(
                    session_id="s1", summary="refactored auth",
                    source="auto", project="myapp", created_at=1.0,
                ),
            },
        )
        assert "[[myapp]]" not in md
        assert "refactored auth" in md


class TestObsidianFlavor:
    def test_starts_with_frontmatter(self):
        s = _stat("coding", "-Users-alice-code-myapp", "s1")
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 10, tzinfo=timezone.utc),
            until=datetime(2026, 5, 17, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("coding", [s])],
            usage=_usage(),
            summaries={},
            flavor="obsidian",
        )
        assert md.startswith("---\n")
        # YAML closes before the H1
        h1_idx = md.index("# Claude Code Recap")
        end_yaml_idx = md.index("\n---\n")
        assert end_yaml_idx < h1_idx

    def test_frontmatter_has_expected_keys(self):
        s = _stat("coding", "-Users-alice-code-myapp", "s1", mins=120)
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 10, tzinfo=timezone.utc),
            until=datetime(2026, 5, 17, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("coding", [s])],
            usage=_usage(),
            summaries={},
            flavor="obsidian",
        )
        assert "date_start: 2026-05-10" in md
        assert "date_end: 2026-05-17" in md
        assert "active_hours: 2.0" in md
        assert "top_focus: coding" in md
        assert "buckets: [coding]" in md
        assert "cost_usd:" in md
        assert "output_tokens:" in md

    def test_session_line_has_wikilink(self):
        s = _stat("coding", "-Users-alice-code-awesome-app", "s1")
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("coding", [s])],
            usage=_usage(),
            summaries={
                "s1": SessionSummary(
                    session_id="s1", summary="refactored auth",
                    source="auto", project="awesome-app", created_at=1.0,
                ),
            },
            flavor="obsidian",
        )
        # normalize_project_name strips prefixes → "awesome-app"
        assert "[[awesome-app]]" in md
        assert "refactored auth" in md

    def test_multiple_buckets_in_frontmatter(self):
        s1 = _stat("coding", "-Users-alice-code-foo", "s1", mins=120)
        s2 = _stat("writing", "-Users-alice-blog-bar", "s2", mins=60)
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[s1, s2],
            rollups=[
                _rollup("coding", [s1]),
                _rollup("writing", [s2]),
            ],
            usage=_usage(),
            summaries={},
            flavor="obsidian",
        )
        assert "buckets: [coding, writing]" in md
        # Top focus is the bucket with the most active time
        assert "top_focus: coding" in md
