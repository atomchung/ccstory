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
from pathlib import Path

from rich.console import Console

from ccstory.artifacts import ArtifactsReport, RepoArtifacts
from ccstory.report import (
    _narrative_headers,
    _top_focus_terminal_detail_lines,
    _top_focus_terminal_text,
    render_terminal_card,
    TopFocusNarrative,
)
from ccstory.session_summarizer import SessionSummary
from ccstory.time_tracking import CategoryRollup, ProjectRollup, SessionStat
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


class TestAgentScopedTitle:
    def test_codex_only_card_is_not_labeled_claude(self):
        session = SessionStat(
            project="demo",
            category="coding",
            session_id="codex-1",
            start=SINCE,
            end=UNTIL,
            active_sec=60,
            msg_count=2,
            agent="codex",
        )
        console = Console(width=72, record=True)
        console.print(
            render_terminal_card(
                since=SINCE,
                until=UNTIL,
                sessions=[session],
                rollups=_rollups([("coding", 1.0)]),
                usage=_usage(),
                agent="codex",
            )
        )
        text = console.export_text()
        assert "Codex Recap" in text
        assert "OpenAI Codex" not in text
        assert "Claude Code Recap" not in text


class TestRepoActivityCard:
    def _render(self, artifacts: ArtifactsReport) -> str:
        console = Console(width=88, record=True)
        console.print(render_terminal_card(
            since=SINCE,
            until=UNTIL,
            sessions=[],
            rollups=_rollups([("coding", 1.0)]),
            usage=_usage(),
            artifacts=artifacts,
        ))
        return console.export_text()

    def test_missing_github_access_is_explicitly_local_only(self):
        out = self._render(ArtifactsReport(
            repos=[RepoArtifacts(root=Path("/x/p"), name="p", commits=4)],
            repos_discovered=1,
            github_status="not_connected",
            github_repos_total=1,
        ))
        assert "Repo activity" in out
        assert "4 commits" in out
        assert "showing local" in out
        assert "commit activity only" in out
        assert "PRs merged" not in out

    def test_title_is_a_separate_module_from_activity_metrics(self):
        out = self._render(ArtifactsReport(
            repos=[RepoArtifacts(root=Path("/x/p"), name="p", commits=4)],
            repos_discovered=1,
            github_status="not_connected",
            github_repos_total=1,
        ))
        lines = out.splitlines()
        title = next(i for i, line in enumerate(lines) if "Repo activity" in line)
        metrics = next(i for i, line in enumerate(lines) if "4 commits" in line)
        assert metrics == title + 1

    def test_partial_github_totals_are_not_rendered_as_complete(self):
        out = self._render(ArtifactsReport(
            repos=[RepoArtifacts(
                root=Path("/x/p"), name="p", commits=4,
                prs_merged=3, releases=["v1"],
            )],
            repos_discovered=12,
            github_status="connected",
            github_repos_total=12,
            github_repos_queried=10,
            github_repos_enriched=10,
        ))
        assert "Repo activity" in out
        assert "4 commits" in out
        assert "PRs merged" not in out
        assert "GitHub totals are partial" in out


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

    def test_top_focus_remains_category_first_while_story_stays_integrated(self):
        narrative = (
            "**Make the weekly recap decision-useful**\n"
            "- Goal: Help the user see the work that serves their real objective.\n"
            "- Target state: The primary recap explains intent, done state, and progress.\n"
            "- Completed: Replaced the time-bucket-only highlight with a structured focus.\n"
            "\n"
            "**Keep the remaining goals visible without another full card**\n"
            "- Goal: Preserve the rest of the weekly story.\n"
            "- Target state: Supporting goals remain scannable.\n"
            "- Completed: Moved them below the primary focus.\n"
        )
        out = self._card_text(narrative)
        assert "★ Top focus  輸出  10.0h" in out
        assert "Make the weekly recap decision-useful" in out
        assert "What you did" in out
        assert "User goal" not in out
        assert "Target state" not in out
        assert "Completed" not in out
        assert "Keep the remaining goals visible" in out


class TestCardWrapping:
    def test_top_focus_terminal_text_uses_short_subject_and_one_outcome(self):
        focus = TopFocusNarrative(
            category="輸出",
            active_min=60.0,
            share=1.0,
            project="kol-collector-fomo-kernel",
            project_active_min=60.0,
            project_sessions=1,
            strongest_session_summaries=(
                "解決跨 session 測試環境不一致。",
                "這段不應該出現在終端卡片。",
            ),
        )

        assert _top_focus_terminal_text(focus) == (
            "fomo-kernel: 解決跨 session 測試環境不一致"
        )

    def test_top_focus_detail_wraps_at_readable_boundaries_and_caps_at_two_lines(self):
        detail = (
            "kol-collector-fomo-kernel · 理解跨 session 開發測試環境不一致，"
            "完成可重現的驗收流程與回歸測試。 · 後續還要掃描公開 issue、"
            "整理效能改善與合併紀錄 TOP_FOCUS_THIRD_LINE"
        )

        lines = _top_focus_terminal_detail_lines(detail)

        assert len(lines) == 2
        assert "kol-collector-fomo-kernel" in lines[0].plain
        assert "TOP_FOCUS_THIRD_LINE" not in "".join(line.plain for line in lines)
        assert lines[-1].plain.endswith("…")

    def test_top_focus_detail_renders_one_concise_subject_outcome_line(self):
        top_session = SessionStat(
            project="kol-collector-fomo-kernel",
            category="輸出",
            session_id="wrapped-focus",
            start=SINCE,
            end=UNTIL,
            active_sec=3600,
            msg_count=2,
        )
        rollup = CategoryRollup(
            category="輸出",
            active_min=60.0,
            sessions=1,
            messages=2,
            top_sessions=[top_session],
            projects=[ProjectRollup("kol-collector-fomo-kernel", 60.0, 1, 2)],
        )
        summary = SessionSummary(
            session_id="wrapped-focus",
            source="auto",
            summary="解決跨 session 測試環境不一致，建立可重現的 QA 驗證。",
            evidence_fingerprint="current",
            observed_evidence_fingerprint="current",
        )
        console = Console(width=72, record=True)
        console.print(render_terminal_card(
            since=SINCE,
            until=UNTIL,
            sessions=[top_session],
            rollups=[rollup],
            summaries={top_session.session_id: summary},
            usage=_usage(),
        ))

        lines = console.export_text().splitlines()
        detail_start = next(i for i, line in enumerate(lines) if "↳" in line)
        metrics_start = next(i for i, line in enumerate(lines) if "Active" in line)
        detail_lines = [
            line for line in lines[detail_start:metrics_start]
            if line.strip("│ ")
        ]
        assert len(detail_lines) == 1
        assert "fomo-kernel: 解決跨 session 測試環境不一致" in detail_lines[0]
        assert "…" not in detail_lines[0]

    def test_highlight_omits_raw_prompt_fallback_while_project_and_narrative_wrap(self):
        top_session = SessionStat(
            project="demo",
            category="輸出",
            session_id="long-copy",
            start=SINCE,
            end=UNTIL,
            active_sec=3600,
            msg_count=2,
            first_user_text=(
                "Review [$record](/Users/demo/skills/record/SKILL.md) and coordinate "
                "several sessions before selecting the **next step** WRAP_END"
            ),
        )
        rollup = CategoryRollup(
            category="輸出",
            active_min=60.0,
            sessions=1,
            messages=2,
            top_sessions=[top_session],
            projects=[
                ProjectRollup(
                    "kol-collector-fomo-kernel-with-a-long-project-name",
                    45.0,
                    1,
                    1,
                ),
                ProjectRollup("personal-os-project-tail", 15.0, 1, 1),
            ],
        )
        narrative = (
            "**A long outcome header should remain readable across wrapped lines "
            "instead of ending in repeated dots HEADER_END**"
        )
        console = Console(width=72, record=True)
        console.print(
            render_terminal_card(
                since=SINCE,
                until=UNTIL,
                sessions=[top_session],
                rollups=[rollup],
                usage=_usage(),
                overall_narrative=narrative,
            )
        )

        text = console.export_text()
        assert "$record" not in text
        assert "/Users/demo" not in text
        assert "**" not in text
        assert "WRAP_END" not in text
        # With no eligible generated summary, the compact line is just the
        # deterministic primary project — never the raw prompt fallback.
        assert "↳ project-name" in text
        assert "personal-os-project-tail" in text
        assert "HEADER_END" in text


class TestUnpricedModelCaveatTerminalCard:
    def test_no_caveat_when_all_models_priced(self):
        usage = UsageReport(since=SINCE, until=UNTIL)
        usage.by_model["claude-opus-4-7"] = ModelUsage(
            model="claude-opus-4-7", turns=5, input_tokens=1000, output_tokens=500
        )
        console = Console(width=72, record=True)
        console.print(
            render_terminal_card(
                since=SINCE,
                until=UNTIL,
                sessions=[],
                rollups=_rollups([("coding", 60.0)]),
                usage=usage,
            )
        )
        text = console.export_text()
        assert "Claude Code only" not in text
        assert "Cost excludes" not in text

    def test_caveat_present_when_unpriced_model_exists(self):
        usage = UsageReport(since=SINCE, until=UNTIL)
        usage.by_model["gpt-5.7-super"] = ModelUsage(
            model="gpt-5.7-super", turns=5, input_tokens=1000, output_tokens=500
        )
        console = Console(width=72, record=True)
        console.print(
            render_terminal_card(
                since=SINCE,
                until=UNTIL,
                sessions=[],
                rollups=_rollups([("coding", 60.0)]),
                usage=usage,
            )
        )
        text = console.export_text()
        assert "Cost excludes gpt-5.7-super (missing from price table)." in text

    def test_caveat_is_a_footer_after_agent_breakdown_and_report_link(self):
        usage = UsageReport(since=SINCE, until=UNTIL)
        usage.by_model["gpt-5.7-super"] = ModelUsage(
            model="gpt-5.7-super", turns=5, input_tokens=1000, output_tokens=500
        )
        sessions = [
            SessionStat(
                project="demo",
                category="coding",
                session_id=f"{agent}-1",
                start=SINCE,
                end=UNTIL,
                active_sec=60,
                msg_count=2,
                agent=agent,
            )
            for agent in ("claude", "codex", "antigravity")
        ]
        console = Console(width=72, record=True)
        console.print(
            render_terminal_card(
                since=SINCE,
                until=UNTIL,
                sessions=sessions,
                rollups=_rollups([("coding", 60.0)]),
                usage=usage,
                report_path="/tmp/recap.md",
            )
        )

        text = console.export_text()
        assert text.index("Claude Code") < text.index("Full report")
        assert text.index("Full report") < text.index("Cost excludes")
        assert "Claude Code" in text
        assert "Codex" in text
        assert "Antigravity" in text
        agent_lines = [
            line for line in text.splitlines() if "1.0× parallel" in line
        ]
        assert len(agent_lines) == 1
        assert all(
            label in agent_lines[0]
            for label in ("Claude Code 33%", "Codex 33%", "Antigravity 33%")
        )
        assert "sessions (" not in text
        assert "OpenAI Codex" not in text
        assert "Google Antigr" not in text
