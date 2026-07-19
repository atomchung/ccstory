"""Tests for the recap terminal card: bucket color collisions (bug report)
and the "What you did" goal-thread narrative rendering (#98 follow-up).

Bug report: with several custom `[categories]` buckets (none matching the
built-in BUCKET_COLORS keys), color_for()'s per-bucket hash regularly put
two different buckets on the same bar color, and the "What you did" section
printed the raw `**bold**`/`- bullet` markup from the #98 goal-thread prompt
verbatim instead of rendering it — and printed all of it, several times
longer than the old 3-sentence narrative it replaced.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from rich.console import Console

from ccstory.report import _narrative_headers, render_terminal_card
from ccstory.time_tracking import CategoryRollup, ProjectRollup
from ccstory.token_usage import ModelUsage, UsageReport

SINCE = datetime(2026, 7, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 7, 8, tzinfo=timezone.utc)


def _usage() -> UsageReport:
    rep = UsageReport(since=SINCE, until=UNTIL)
    rep.by_model["claude-opus-4-7"] = ModelUsage(
        model="claude-opus-4-7", turns=5, input_tokens=1000, output_tokens=500,
    )
    rep.assistant_turns = 5
    return rep


def _rollups(pairs: list[tuple[str, float]]) -> list[CategoryRollup]:
    return [
        CategoryRollup(category=cat, active_min=mins, sessions=1, messages=1,
                        top_sessions=[])
        for cat, mins in pairs
    ]


class TestBarColorsDoNotCollide:
    def test_real_bucket_names_from_bug_report_get_distinct_colors(self):
        # Exact buckets from the reported recap card (custom [projects]
        # aliases — none are BUCKET_COLORS keys, so all 5 previously hashed
        # independently into the same 6-slot palette and collided).
        rollups = _rollups([
            ("輸出", 60 * 62.2), ("投資", 60 * 33.0), ("學習", 60 * 8.6),
            ("其他", 60 * 2.9), ("職涯", 60 * 0.5),
        ])
        console = Console(width=72, record=True)
        console.print(render_terminal_card(
            since=SINCE, until=UNTIL, sessions=[], rollups=rollups, usage=_usage(),
        ))
        ansi = console.export_text(styles=True)
        codes = {}
        for r in rollups:
            m = re.search(rf"\x1b\[([\d;]+)m{re.escape(r.category)}", ansi)
            assert m, f"{r.category!r} not found styled in bar chart output"
            codes[r.category] = m.group(1)
        assert len(set(codes.values())) == len(rollups), codes

    def test_same_bucket_keeps_one_color_across_sections(self):
        # A category with a project split also shows in "By project" below
        # the bars — same bucket must render the same color in both spots.
        rollups = [
            CategoryRollup(
                category="投資", active_min=120.0, sessions=2, messages=10,
                top_sessions=[],
                projects=[
                    ProjectRollup("stock", 80.0, 1, 6),
                    ProjectRollup("investment-note", 40.0, 1, 4),
                ],
            ),
            CategoryRollup(category="輸出", active_min=60.0, sessions=1,
                            messages=5, top_sessions=[]),
        ]
        console = Console(width=72, record=True)
        console.print(render_terminal_card(
            since=SINCE, until=UNTIL, sessions=[], rollups=rollups, usage=_usage(),
        ))
        ansi = console.export_text(styles=True)
        # "投資" is styled 3x (Top focus headline, bar row, By-project row).
        # Bold/non-bold styling differs by section, but the base SGR color
        # digit must be the same everywhere — that's the shared `colors`
        # map from render_terminal_card doing its job.
        color_digits = {
            m.group(1).split(";")[-1]
            for m in re.finditer(r"\x1b\[([\d;]+)m投資", ansi)
        }
        assert len(color_digits) == 1, color_digits


class TestNarrativeHeaders:
    def test_extracts_bold_header_lines(self):
        narrative = (
            "**fomo-kernel 現金 ingestion 地基與 TWR 三柱績效雙雙上卡**\n"
            "- 現金流與帳戶級現金部位首次接進核心引擎\n"
            "- TWR 正式上卡且拍板三柱指標\n"
            "\n"
            "**ccstory 敘事引擎重寫上線，週報從技術摘要改成有前後對比的故事**\n"
            "- 重寫 ccstory 敘事產生器\n"
        )
        assert _narrative_headers(narrative) == [
            "fomo-kernel 現金 ingestion 地基與 TWR 三柱績效雙雙上卡",
            "ccstory 敘事引擎重寫上線，週報從技術摘要改成有前後對比的故事",
        ]

    def test_returns_empty_for_plain_prose(self):
        # Pre-#98 cached narrative, or the LLM drifting off spec — no line
        # is fully wrapped in `**...**`. Falsy `[]`, same as the caller's
        # `if headers:` needs — there's no reachable path back to a
        # non-empty list here, so the type stays plain `list[str]`.
        narrative = "Focused on ccstory this week, shipping the v0.6 release."
        assert _narrative_headers(narrative) == []

    def test_unwraps_nested_bold_inside_a_header(self):
        # A header emphasizing e.g. a version number with its own **bold**
        # must not leak the inner ** markers into the extracted text.
        narrative = "**Shipped **v0.6.0** with two rendering bug fixes**\n"
        assert _narrative_headers(narrative) == [
            "Shipped v0.6.0 with two rendering bug fixes",
        ]


class TestWhatYouDidCard:
    def _card_text(self, narrative: str) -> str:
        rollups = _rollups([("輸出", 600.0)])
        console = Console(width=72, record=True)
        console.print(render_terminal_card(
            since=SINCE, until=UNTIL, sessions=[], rollups=rollups,
            usage=_usage(), overall_narrative=narrative,
        ))
        return console.export_text()

    def test_goal_thread_narrative_shows_headers_without_raw_markup(self):
        narrative = (
            "**fomo-kernel 現金 ingestion 地基與 TWR 三柱績效雙雙上卡**\n"
            "- 現金流與帳戶級現金部位首次接進核心引擎（build_state）\n"
            "- TWR 正式上卡且拍板三柱指標\n"
            "\n"
            "**ccstory 敘事引擎重寫上線**\n"
            "- 重寫 ccstory 敘事產生器\n"
        )
        out = self._card_text(narrative)
        assert "fomo-kernel 現金 ingestion 地基與 TWR 三柱績效雙雙上卡" in out
        assert "ccstory 敘事引擎重寫上線" in out
        assert "**" not in out
        # Bullets are supporting detail for the full markdown report, not
        # the screenshot-friendly card — omitted here to keep the card short.
        assert "build_state" not in out
        assert "重寫 ccstory 敘事產生器" not in out

    def test_plain_prose_narrative_still_renders_in_full(self):
        narrative = "Focused on ccstory this week, shipping the v0.6 release."
        out = self._card_text(narrative)
        assert narrative in out
