"""Narrative + time semantics across agents.

Two regressions this file exists to prevent:

1. Every non-Claude session summarized as "(no meaningful conversation)".
   `_extract_excerpt` only understood Claude's record shape, so Codex
   excerpts came back empty — and `summarize_session` writes that string with
   `source="skipped"` on an empty excerpt. Because the report prefers a cached
   summary over `first_user_text`, that placeholder then *overwrote* narrative
   the provider had parsed correctly, and survived until `--refresh`.

2. Per-agent hours presented as if they were durations. Agents run in
   parallel, so their raw times overlap; only the deduplicated wall clock is
   a duration.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ccstory import session_summarizer
from ccstory.report import agent_breakdown, build_report_json, render_report
from ccstory.session_summarizer import _extract_excerpt, summarize_session
from ccstory.time_tracking import SessionStat, wall_clock_active_sec
from ccstory.token_usage import UsageReport


def _ts(minute: int) -> str:
    return (
        datetime(2026, 7, 22, 12, minute, tzinfo=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _codex_transcript(tmp_home: Path, session_id: str) -> Path:
    records = [
        {
            "timestamp": _ts(0),
            "type": "session_meta",
            "payload": {"session_id": session_id, "cwd": "/Users/x/Side_project/demo"},
        },
        # Harness-injected twin of the real turn: must not become the excerpt.
        {
            "timestamp": _ts(1),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<recommended_plugins>\nBox\nJira\n"}
                ],
            },
        },
        {
            "timestamp": _ts(1),
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "<task>\nmigrate the price fetcher off the legacy API\n</task>",
            },
        },
        {
            "timestamp": _ts(4),
            "type": "response_item",
            "payload": {"type": "reasoning", "encrypted_content": "gAAAA…"},
        },
        {
            "timestamp": _ts(6),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Swapped the fetcher and added a cache."}
                ],
            },
        },
    ]
    path = tmp_home / ".codex" / "sessions" / "2026" / "07" / "22" / (
        f"rollout-2026-07-22T12-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


SID = "019f8a2c-2df3-7f01-b55d-b8dcae9f2516"
SINCE = datetime(2026, 7, 22, tzinfo=timezone.utc)
UNTIL = datetime(2026, 7, 23, tzinfo=timezone.utc)


class TestCodexExcerpt:
    def test_codex_transcript_yields_a_non_empty_excerpt(self, tmp_home):
        path = _codex_transcript(tmp_home, SID)
        _project, excerpt = _extract_excerpt(path)

        assert excerpt, "Codex excerpt came back empty — narrative is dead again"
        assert "migrate the price fetcher off the legacy API" in excerpt
        assert "Swapped the fetcher and added a cache." in excerpt

    def test_excerpt_prefers_the_real_turn_over_injected_context(self, tmp_home):
        _project, excerpt = _extract_excerpt(_codex_transcript(tmp_home, SID))
        assert "recommended_plugins" not in excerpt

    def test_excerpt_project_comes_from_the_recorded_cwd(self, tmp_home):
        project, _excerpt = _extract_excerpt(_codex_transcript(tmp_home, SID))
        from ccstory.categorizer import normalize_project_name

        assert normalize_project_name(project) == "demo"

    def test_no_placeholder_summary_is_cached_for_a_codex_session(self, tmp_home):
        """The bug that poisoned the cache: a skipped row outranks the
        provider's own first_user_text and needs --refresh to clear."""
        path = _codex_transcript(tmp_home, SID)
        result = summarize_session(SID, path, use_llm=False)

        assert result is not None
        assert result.source == "fallback"
        assert "no meaningful conversation" not in result.summary
        assert session_summarizer.get(SID).source != "skipped"

    def test_claude_transcript_still_parses(self, jsonl_factory):
        """The multi-format dispatch must not regress the default path."""
        from tests.conftest import make_assistant_msg, make_user_msg

        path = jsonl_factory(
            "-Users-x-demo",
            "claude-sid",
            [
                make_user_msg("refactor the auth middleware", _ts(0)),
                make_assistant_msg("Extracted token validation.", _ts(3), "m1"),
            ],
        )
        project, excerpt = _extract_excerpt(path)
        assert project == "-Users-x-demo"
        assert "refactor the auth middleware" in excerpt
        assert "Extracted token validation." in excerpt


def _stat(agent: str, sid: str, start_min: int, minutes: int) -> SessionStat:
    """A session whose timestamps tick once a minute, so gaps never hit the cap."""
    base = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    start = base + timedelta(minutes=start_min)
    stamps = [
        (start + timedelta(minutes=i)).timestamp() for i in range(minutes + 1)
    ]
    return SessionStat(
        project="-Users-x-demo",
        category="build",
        session_id=sid,
        start=start,
        end=start + timedelta(minutes=minutes),
        active_sec=minutes * 60,
        msg_count=minutes,
        user_msg_count=2,
        first_user_text=f"{agent} work",
        timestamps=stamps,
        agent=agent,
    )


@pytest.fixture
def overlapping_sessions() -> list[SessionStat]:
    """Two agents working the same hour — raw sum 120 min, wall clock 60."""
    return [
        _stat("claude", "c1", 0, 60),
        _stat("codex", "x1", 0, 30),
        _stat("codex", "x2", 30, 30),
    ]


class TestTimeSemantics:
    def test_report_total_is_the_deduplicated_wall_clock(self, overlapping_sessions):
        from ccstory.time_tracking import rollup_by_category

        rollups = rollup_by_category(overlapping_sessions)
        total_min = sum(r.active_min for r in rollups)
        wall_min = wall_clock_active_sec(overlapping_sessions) / 60

        assert total_min == pytest.approx(wall_min, abs=0.2)
        # And it is genuinely smaller than the naive sum of agent times.
        raw_min = sum(s.active_sec for s in overlapping_sessions) / 60
        assert raw_min == pytest.approx(2 * wall_min, abs=0.2)

    def test_shares_sum_to_one_hundred_percent(self, overlapping_sessions):
        shares = agent_breakdown(overlapping_sessions)
        assert {a.agent for a in shares} == {"claude", "codex"}
        assert sum(a.time_share for a in shares) == pytest.approx(1.0)
        assert sum(a.session_share for a in shares) == pytest.approx(1.0)

    def test_time_share_and_session_share_can_disagree(self, overlapping_sessions):
        """Codex: half the time, two thirds of the sessions. Reporting only
        session counts would hide that its sessions are short."""
        by_agent = {a.agent: a for a in agent_breakdown(overlapping_sessions)}
        assert by_agent["codex"].time_share == pytest.approx(0.5)
        assert by_agent["codex"].session_share == pytest.approx(2 / 3)

    def test_markdown_agent_section_states_no_per_agent_hours(
        self, overlapping_sessions
    ):
        md = _render(overlapping_sessions)
        section = md.split("## Coding agents", 1)[1].split("##", 1)[0]

        # No per-agent duration anywhere in the block — the whole point.
        import re

        assert not re.search(r"\|\s*\d+h\s*\d*m?\s*\|", section)
        assert "Time share" in section
        assert "not** a duration" in section

    def test_markdown_reports_the_parallelism_factor(self, overlapping_sessions):
        md = _render(overlapping_sessions)
        assert "2.0× parallel" in md

    def test_single_agent_window_has_no_agent_section(self):
        md = _render([_stat("claude", "c1", 0, 60)])
        assert "## Coding agents" not in md

    def test_json_exposes_shares_and_parallelism(self, overlapping_sessions):
        from ccstory.time_tracking import rollup_by_category

        payload = build_report_json(
            "week",
            SINCE,
            UNTIL,
            overlapping_sessions,
            rollup_by_category(overlapping_sessions),
            UsageReport(since=SINCE, until=UNTIL),
            {},
        )
        assert payload["parallelism"] == pytest.approx(2.0)
        assert sum(a["time_share"] for a in payload["agents"]) == pytest.approx(1.0)
        assert {s["agent"] for s in payload["sessions"]} == {"claude", "codex"}
        # No per-agent hours key ever gets added back by accident.
        assert all("active_hours" not in a for a in payload["agents"])


def _render(sessions: list[SessionStat]) -> str:
    from ccstory.time_tracking import rollup_by_category

    return render_report(
        "week",
        SINCE,
        UNTIL,
        sessions,
        rollup_by_category(sessions),
        UsageReport(since=SINCE, until=UNTIL),
        {},
    )
