"""Tests for the layer-2 (area → project) read-time rollup + presentation (#69).

Covers:
  - ``rollup_by_category`` attaching a per-project breakdown that reconciles
    to the layer-1 area total;
  - alias folding grouping variant leaves under one project;
  - the migration-continuity fence: layer-1 numbers are byte-identical whether
    or not the additive ``projects`` field / ``aliases`` map is present (no new
    cache, no fingerprint — read-time only);
  - the additive ``projects`` array in ``--json`` (schema_version stays 1);
  - two-layer markdown + terminal-card rendering.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from ccstory.categorizer import resolve_session_bucket
from ccstory.report import (
    JSON_SCHEMA_VERSION,
    build_report_json,
    render_report,
    render_terminal_card,
)
from ccstory.time_tracking import (
    CategoryRollup,
    ProjectRollup,
    SessionStat,
    rollup_by_category,
)
from ccstory.token_usage import ModelUsage, UsageReport

BASE = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 7, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 7, 8, tzinfo=timezone.utc)


def _stat(project: str, category: str, sid: str, mins: int, msgs: int = 10) -> SessionStat:
    return SessionStat(
        project=project, category=category, session_id=sid,
        start=BASE, end=BASE, active_sec=mins * 60, msg_count=msgs,
    )


def _cfg(tmp_home: Path, body: str) -> Path:
    p = tmp_home / ".ccstory" / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def _usage() -> UsageReport:
    rep = UsageReport(since=SINCE, until=UNTIL)
    rep.by_model["claude-opus-4-7"] = ModelUsage(
        model="claude-opus-4-7", turns=5, input_tokens=1000, output_tokens=500,
    )
    rep.assistant_turns = 5
    return rep


class TestRollupProjects:
    def test_projects_grouped_within_area(self):
        stats = [
            _stat("-Users-x-code-stock", "investment", "s1", 60, 10),
            _stat("-Users-x-code-stock-dashboard", "investment", "s2", 30, 6),
            _stat("-Users-x-code-myblog", "writing", "s3", 20, 4),
        ]
        rollups = rollup_by_category(stats, dedup_to_wall_clock=False)
        by_area = {r.category: r for r in rollups}
        inv = by_area["investment"]
        assert inv.active_min == 90.0
        assert inv.sessions == 2
        assert inv.messages == 16
        # Two distinct project leaves, biggest first.
        assert [p.project for p in inv.projects] == ["stock", "stock-dashboard"]
        assert [p.active_min for p in inv.projects] == [60.0, 30.0]
        assert by_area["writing"].projects[0].project == "myblog"

    def test_project_hours_reconcile_to_area_total(self):
        stats = [
            _stat("-Users-x-code-a", "coding", "s1", 45, 5),
            _stat("-Users-x-code-b", "coding", "s2", 15, 3),
        ]
        (coding,) = rollup_by_category(stats, dedup_to_wall_clock=False)
        assert round(sum(p.active_min for p in coding.projects), 1) == coding.active_min

    def test_alias_folds_variants_into_one_project(self, tmp_home: Path):
        from ccstory.categorizer import load_project_aliases

        p = _cfg(tmp_home, '[projects]\n"stockdash" = "stock"\n')
        aliases = load_project_aliases(p)
        stats = [
            _stat("-Users-x-code-stock", "investment", "s1", 60, 10),
            _stat("-Users-x-code-stockdash", "investment", "s2", 30, 6),
        ]
        (inv,) = rollup_by_category(stats, dedup_to_wall_clock=False, aliases=aliases)
        assert [p.project for p in inv.projects] == ["stock"]
        assert inv.projects[0].active_min == 90.0
        assert inv.projects[0].sessions == 2


class TestMigrationContinuityFence:
    """Layer-1 area numbers must not move when layer-2 is added.

    The whole point of computing layer-2 at read time (no new cache family,
    no fingerprint) is that historical cache + existing configs keep producing
    identical layer-1 numbers — the #118-class regression the RFC calls out.
    These pin that: the additive ``projects`` field and the ``aliases`` map
    never perturb ``active_min`` / ``sessions`` / ``messages``.
    """

    def _sessions(self) -> list[SessionStat]:
        return [
            _stat("-Users-x-code-stock", "investment", "s1", 60, 10),
            _stat("-Users-x-code-stock-dashboard", "investment", "s2", 30, 6),
            _stat("-Users-x-code-myblog", "writing", "s3", 20, 4),
        ]

    def _layer1(self, rollups: list[CategoryRollup]) -> dict:
        return {
            r.category: (r.active_min, r.sessions, r.messages) for r in rollups
        }

    def test_aliases_param_is_additive_to_layer1(self):
        stats = self._sessions()
        base = self._layer1(rollup_by_category(stats, dedup_to_wall_clock=False))
        with_empty = self._layer1(
            rollup_by_category(stats, dedup_to_wall_clock=False, aliases={})
        )
        with_map = self._layer1(
            rollup_by_category(
                stats, dedup_to_wall_clock=False, aliases={"unused": "x"},
            )
        )
        assert base == with_empty == with_map
        # And the exact expected layer-1 numbers (what today's resolver+rollup
        # produces for this token-needle-shaped data).
        assert base == {
            "investment": (90.0, 2, 16),
            "writing": (20.0, 1, 4),
        }

    def test_resolver_v2_preserves_layer1_for_token_needle_config(self, tmp_home: Path):
        # A representative *existing* config: token needles, no exact-shadowing.
        # Resolver v2 must land every session in the same area old matching did.
        p = _cfg(
            tmp_home,
            '[categories]\n'
            '"investment" = ["stock"]\n'
            '"writing" = ["myblog"]\n',
        )
        stats = [
            _stat("-Users-x-code-stock", "", "s1", 60, 10),
            _stat("-Users-x-code-stock-dashboard", "", "s2", 30, 6),
            _stat("-Users-x-code-myblog", "", "s3", 20, 4),
        ]
        for s in stats:
            bucket, _ = resolve_session_bucket(
                s.project, None, mode="folder", fallback="other", config_path=p,
            )
            s.category = bucket
        rollups = rollup_by_category(stats, dedup_to_wall_clock=False)
        assert self._layer1(rollups) == {
            "investment": (90.0, 2, 16),
            "writing": (20.0, 1, 4),
        }


class TestReportJsonProjects:
    def _build(self, projects: list[ProjectRollup]) -> dict:
        rollup = CategoryRollup(
            category="coding", active_min=60.0, sessions=2, messages=13,
            top_sessions=[], projects=projects,
        )
        return build_report_json(
            label="2026-W27", since=SINCE, until=UNTIL, sessions=[],
            rollups=[rollup], usage=_usage(), summaries={},
        )

    def test_projects_array_present_and_shaped(self):
        payload = self._build([
            ProjectRollup(project="ccstory", active_min=45.0, sessions=1, messages=9),
            ProjectRollup(project="personal-os", active_min=15.0, sessions=1, messages=4),
        ])
        assert payload["schema_version"] == JSON_SCHEMA_VERSION == 1
        bucket = payload["buckets"][0]
        assert bucket["projects"] == [
            {"name": "ccstory", "active_hours": 0.75, "sessions": 1, "messages": 9},
            {"name": "personal-os", "active_hours": 0.25, "sessions": 1, "messages": 4},
        ]

    def test_empty_projects_serialize_as_empty_list(self):
        payload = self._build([])
        assert payload["buckets"][0]["projects"] == []


class TestTwoLayerRendering:
    def _rollups(self) -> list[CategoryRollup]:
        return [
            CategoryRollup(
                category="coding", active_min=60.0, sessions=3, messages=20,
                top_sessions=[],
                projects=[
                    ProjectRollup("ccstory", 40.0, 2, 12),
                    ProjectRollup("personal-os", 15.0, 1, 5),
                    ProjectRollup("paperclip", 3.0, 1, 2),
                    ProjectRollup("misc-tool", 2.0, 1, 1),
                ],
            ),
        ]

    def test_markdown_shows_top3_projects_and_more(self):
        md = render_report(
            label="2026-W27", since=SINCE, until=UNTIL, sessions=[],
            rollups=self._rollups(), usage=_usage(), summaries={},
        )
        assert "_Projects:_" in md
        assert "**ccstory**" in md
        assert "**personal-os**" in md
        assert "**paperclip**" in md
        # 4 projects → top-3 shown, remainder summarized.
        assert "+1 more" in md
        assert "**misc-tool**" not in md

    def test_terminal_card_shows_by_project_when_split(self):
        console = Console(width=72, record=True)
        console.print(render_terminal_card(
            since=SINCE, until=UNTIL, sessions=[],
            rollups=self._rollups(), usage=_usage(),
        ))
        out = console.export_text()
        assert "By project" in out
        assert "ccstory" in out
        assert "personal-os" in out

    def test_terminal_card_hides_by_project_for_single_project_area(self):
        rollups = [
            CategoryRollup(
                category="coding", active_min=60.0, sessions=1, messages=10,
                top_sessions=[],
                projects=[ProjectRollup("ccstory", 60.0, 1, 10)],
            ),
        ]
        console = Console(width=72, record=True)
        console.print(render_terminal_card(
            since=SINCE, until=UNTIL, sessions=[], rollups=rollups, usage=_usage(),
        ))
        out = console.export_text()
        assert "By project" not in out
