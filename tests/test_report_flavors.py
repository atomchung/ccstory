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


class TestObsidianYamlImplicitScalars:
    def test_bucket_named_yes_is_quoted(self):
        # `yes` parses as boolean True under YAML 1.1 (which Obsidian /
        # Dataview tend to use) — must be quoted so top_focus stays a string.
        s = _stat("yes", "-Users-alice-code-foo", "s1", mins=60)
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("yes", [s])],
            usage=_usage(),
            summaries={},
            flavor="obsidian",
        )
        assert 'top_focus: "yes"' in md
        assert 'buckets: ["yes"]' in md

    def test_bucket_digits_only_is_quoted(self):
        s = _stat("123", "-Users-alice-code-foo", "s1", mins=60)
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("123", [s])],
            usage=_usage(),
            summaries={},
            flavor="obsidian",
        )
        assert 'top_focus: "123"' in md
        assert 'buckets: ["123"]' in md

    def test_bucket_date_like_is_quoted(self):
        # 2026-05-16 would otherwise parse as a YAML date.
        s = _stat("2026-05-16", "-Users-alice-code-foo", "s1", mins=60)
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("2026-05-16", [s])],
            usage=_usage(),
            summaries={},
            flavor="obsidian",
        )
        assert 'top_focus: "2026-05-16"' in md


class TestObsidianYamlEscaping:
    def test_bucket_with_special_chars_is_quoted(self):
        s = _stat("client: acme, inc", "-Users-alice-code-foo", "s1", mins=60)
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("client: acme, inc", [s])],
            usage=_usage(),
            summaries={},
            flavor="obsidian",
        )
        # Bare emit would be: top_focus: client: acme, inc  → broken YAML
        # We expect JSON-style double quoting.
        assert 'top_focus: "client: acme, inc"' in md
        assert 'buckets: ["client: acme, inc"]' in md

    def test_simple_alnum_bucket_not_quoted(self):
        s = _stat("coding", "-Users-alice-code-foo", "s1", mins=60)
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("coding", [s])],
            usage=_usage(),
            summaries={},
            flavor="obsidian",
        )
        # Plain identifiers stay unquoted to keep diffs clean.
        assert "top_focus: coding" in md
        assert "buckets: [coding]" in md

    def test_unsorted_rollups_still_pick_largest(self):
        # If a caller ever hands in rollups not sorted by active_min,
        # top_focus must still be the bucket with the most time.
        small = _stat("small", "-Users-alice-code-foo", "s1", mins=10)
        big = _stat("big", "-Users-alice-code-bar", "s2", mins=120)
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[small, big],
            rollups=[_rollup("small", [small]), _rollup("big", [big])],
            usage=_usage(),
            summaries={},
            flavor="obsidian",
        )
        assert "top_focus: big" in md


class TestObsidianWikilinkEscaping:
    def test_wikilink_pipe_is_stripped(self):
        s = _stat("coding", "weird|name", "s1")
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("coding", [s])],
            usage=_usage(),
            summaries={},
            flavor="obsidian",
        )
        # `|` would otherwise be interpreted as the wikilink alias separator
        assert "[[weird|name]]" not in md
        assert "[[weird-name]]" in md

    def test_wikilink_bracket_is_stripped(self):
        s = _stat("coding", "name]withbracket", "s1")
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("coding", [s])],
            usage=_usage(),
            summaries={},
            flavor="obsidian",
        )
        # `]` would terminate the wikilink prematurely
        assert "[[name]withbracket]]" not in md
        assert "[[name-withbracket]]" in md

    def test_wikilink_newline_is_collapsed(self):
        s = _stat("coding", "name\nwithnewline", "s1")
        md = render_report(
            label="2026-05",
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 31, tzinfo=timezone.utc),
            sessions=[s],
            rollups=[_rollup("coding", [s])],
            usage=_usage(),
            summaries={},
            flavor="obsidian",
        )
        # Newline in a wikilink breaks Obsidian's parser
        assert "[[name\nwithnewline]]" not in md
        assert "[[name withnewline]]" in md
