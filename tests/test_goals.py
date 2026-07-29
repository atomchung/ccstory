"""Focused contract tests for provider-neutral GoalContext v1 (#216)."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import ccstory.goals as goals_module
from ccstory.goals import (
    GOAL_CONTEXT_SCHEMA_VERSION,
    GoalAttributionInput,
    GoalContextError,
    attribute_goals,
    build_goal_breakdown,
    load_goal_context,
    parse_goal_context,
)
from ccstory.time_tracking import SessionStat


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _context(**overrides):
    data = {
        "schema_version": 1,
        "goals": [
            {
                "id": "goal-a",
                "title": "Goal A",
                "projects": ["alpha"],
            }
        ],
    }
    data.update(overrides)
    return parse_goal_context(data, aliases={})


class TestGoalContextReader:
    def test_loads_canonical_deterministic_context_with_source_provenance(
        self, tmp_path: Path
    ):
        body = """\
schema_version = 1

[[goals]]
id = "zeta"
title = "Future project"
projects = ["new_project"]

[[goals]]
id = "alpha"
title = "Canonical aliases"
projects = ["stockdash", "stock", "stockdash"]
valid_from = 2026-07-01
valid_until = "2026-07-31"
"""
        path = _write(tmp_path / "goals.toml", body)

        context = load_goal_context(path, aliases={"stockdash": "stock"})

        assert context is not None
        assert context.schema_version == GOAL_CONTEXT_SCHEMA_VERSION == 1
        assert [goal.id for goal in context.goals] == ["alpha", "zeta"]
        assert context.goals[0].projects == ("stock",)
        # Syntactically valid future projects do not require current sessions.
        assert context.goals[1].projects == ("new-project",)
        assert context.goals[0].valid_from == date(2026, 7, 1)
        assert context.goals[0].valid_until == date(2026, 7, 31)
        assert dict(context.source_metadata) == {
            "format": "toml",
            "path": str(path.resolve()),
        }
        assert context.source_fingerprint == (
            "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
        )

    def test_schema_serialization_round_trips(self):
        original = parse_goal_context(
            {
                "schema_version": 1,
                "goals": [
                    {
                        "id": "dated",
                        "title": "Dated",
                        "projects": ["project_b", "project-a"],
                        "valid_from": "2026-07-01",
                        "valid_until": "2026-07-31",
                    }
                ],
            },
            aliases={},
        )

        reloaded = parse_goal_context(original.to_schema_dict(), aliases={})

        assert reloaded.to_schema_dict() == original.to_schema_dict()
        assert reloaded.goals[0].projects == ("a", "b")

    def test_missing_default_file_is_opt_in_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            goals_module, "DEFAULT_GOAL_CONTEXT_PATH", tmp_path / "missing.toml"
        )

        assert load_goal_context() is None

    def test_explicit_missing_file_is_actionable(self, tmp_path: Path):
        with pytest.raises(GoalContextError, match="does not exist"):
            load_goal_context(tmp_path / "missing.toml")

    def test_malformed_toml_is_not_silently_ignored(self, tmp_path: Path):
        path = _write(tmp_path / "goals.toml", "not = [valid")

        with pytest.raises(GoalContextError, match="could not parse.*TOML"):
            load_goal_context(path)


class TestStrictValidation:
    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({"goals": []}, "missing schema_version"),
            (
                {"schema_version": 2, "goals": []},
                "unsupported.*schema_version",
            ),
            (
                {"schema_version": True, "goals": []},
                "unsupported.*schema_version",
            ),
            ({"schema_version": 1}, "missing goals"),
            (
                {"schema_version": 1, "goals": {}, "extra": 1},
                "unknown field.*extra",
            ),
            (
                {"schema_version": 1, "goals": [{}]},
                r"goals\[0\] is missing id",
            ),
            (
                {
                    "schema_version": 1,
                    "goals": [{"id": " ", "title": "Title", "projects": ["p"]}],
                },
                r"goals\[0\]\.id must be a non-blank",
            ),
            (
                {
                    "schema_version": 1,
                    "goals": [{"id": "a", "projects": ["p"]}],
                },
                "missing title",
            ),
            (
                {
                    "schema_version": 1,
                    "goals": [{"id": "a", "title": " ", "projects": ["p"]}],
                },
                "title must be a non-blank",
            ),
            (
                {
                    "schema_version": 1,
                    "goals": [{"id": "a", "title": "A"}],
                },
                "missing projects",
            ),
            (
                {
                    "schema_version": 1,
                    "goals": [{"id": "a", "title": "A", "projects": []}],
                },
                "projects must be a non-empty",
            ),
            (
                {
                    "schema_version": 1,
                    "goals": [{"id": "a", "title": "A", "projects": [3]}],
                },
                r"projects\[0\] must be a non-blank string",
            ),
            (
                {
                    "schema_version": 1,
                    "goals": [
                        {"id": "a", "title": "A", "projects": ["bad\u0001ref"]}
                    ],
                },
                "must not contain control",
            ),
            (
                {
                    "schema_version": 1,
                    "goals": [
                        {
                            "id": "a",
                            "title": "A",
                            "projects": ["p"],
                            "valid_from": "July 1",
                        }
                    ],
                },
                "valid_from must be an ISO date",
            ),
            (
                {
                    "schema_version": 1,
                    "goals": [
                        {
                            "id": "a",
                            "title": "A",
                            "projects": ["p"],
                            "valid_from": "2026-07-02",
                            "valid_until": "2026-07-01",
                        }
                    ],
                },
                "valid_until must be on or after",
            ),
            (
                {
                    "schema_version": 1,
                    "goals": [
                        {
                            "id": "a",
                            "title": "A",
                            "projects": ["p"],
                            "looks_supported": True,
                        }
                    ],
                },
                "unknown field.*looks_supported",
            ),
            (
                {
                    "schema_version": 1,
                    "goals": [
                        {"id": "same", "title": "A", "projects": ["a"]},
                        {"id": "same", "title": "B", "projects": ["b"]},
                    ],
                },
                "duplicate goal id",
            ),
        ],
    )
    def test_rejects_invalid_schema(self, data, message):
        with pytest.raises(GoalContextError, match=message):
            parse_goal_context(data, aliases={})


class TestDeterministicAttribution:
    def test_exclusive_shared_and_unattributed_reconcile(self):
        context = parse_goal_context(
            {
                "schema_version": 1,
                "goals": [
                    {
                        "id": "z-shared",
                        "title": "Shared",
                        "projects": ["alpha", "beta"],
                    },
                    {
                        "id": "a-primary",
                        "title": "Primary",
                        "projects": ["alpha"],
                    },
                ],
            },
            aliases={},
        )
        items = [
            GoalAttributionInput("gamma", date(2026, 7, 2), 30),
            GoalAttributionInput("alpha", date(2026, 7, 2), 10),
            GoalAttributionInput("beta", date(2026, 7, 2), 20),
        ]

        breakdown = attribute_goals(items, context, aliases={})

        assert breakdown is not None
        assert breakdown.covered_contribution == 60
        assert breakdown.exclusive_contribution == 20
        assert breakdown.shared_contribution == 10
        assert breakdown.unattributed_contribution == 30
        assert (
            breakdown.exclusive_contribution
            + breakdown.shared_contribution
            + breakdown.unattributed_contribution
            == breakdown.covered_contribution
        )
        assert [goal.goal_id for goal in breakdown.goals] == [
            "a-primary",
            "z-shared",
        ]
        primary, shared = breakdown.goals
        assert (
            primary.exclusive_contribution,
            primary.shared_contribution,
            primary.total_contribution,
        ) == (0, 10, 10)
        assert primary.projects_touched == ("alpha",)
        assert primary.latest_activity == date(2026, 7, 2)
        assert (
            shared.exclusive_contribution,
            shared.shared_contribution,
            shared.total_contribution,
        ) == (20, 10, 30)
        assert shared.projects_touched == ("alpha", "beta")
        assert shared.latest_activity == date(2026, 7, 2)

        payload = breakdown.to_dict()
        assert breakdown.contribution_unit == "seconds"
        assert payload["contribution_unit"] == "seconds"
        assert payload["per_goal_shared_semantics"] == (
            "overlapping_non_additive"
        )
        assert all(
            goal["shared_contribution_is_non_additive"] is True
            for goal in payload["goals"]
        )
        assert payload["goals"][1]["projects_touched"] == ["alpha", "beta"]
        assert payload["goals"][1]["latest_activity"] == "2026-07-02"
        # Per-goal totals overlap and therefore intentionally do not reconcile
        # to global covered contribution.
        assert sum(goal.total_contribution for goal in breakdown.goals) == 40

    def test_validity_dates_are_inclusive_at_both_boundaries(self):
        context = parse_goal_context(
            {
                "schema_version": 1,
                "goals": [
                    {
                        "id": "dated",
                        "title": "Dated goal",
                        "projects": ["alpha"],
                        "valid_from": "2026-07-01",
                        "valid_until": "2026-07-02",
                    }
                ],
            },
            aliases={},
        )
        items = [
            GoalAttributionInput("alpha", date(2026, 6, 30), 1),
            GoalAttributionInput("alpha", date(2026, 7, 1), 2),
            GoalAttributionInput("alpha", date(2026, 7, 2), 4),
            GoalAttributionInput("alpha", date(2026, 7, 3), 8),
        ]

        breakdown = attribute_goals(items, context, aliases={})

        assert breakdown is not None
        assert breakdown.covered_contribution == 15
        assert breakdown.exclusive_contribution == 6
        assert breakdown.shared_contribution == 0
        assert breakdown.unattributed_contribution == 9
        assert breakdown.goals[0].exclusive_contribution == 6

    def test_aliases_fold_goal_and_input_through_existing_identity_lane(self):
        context = parse_goal_context(
            {
                "schema_version": 1,
                "goals": [
                    {
                        "id": "canonical",
                        "title": "Canonical",
                        "projects": ["stockdash"],
                    }
                ],
            },
            aliases={"stockdash": "stock"},
        )

        breakdown = attribute_goals(
            [GoalAttributionInput("stock_dash", date(2026, 7, 2), 12)],
            context,
            aliases={"stock-dash": "stock", "stockdash": "stock"},
        )

        assert context.goals[0].projects == ("stock",)
        assert breakdown is not None
        assert breakdown.exclusive_contribution == 12
        assert breakdown.unattributed_contribution == 0

    def test_input_order_does_not_change_breakdown(self):
        context = _context()
        items = [
            GoalAttributionInput("alpha", date(2026, 7, 2), 0.1),
            GoalAttributionInput("alpha", date(2026, 7, 1), 0.2),
            GoalAttributionInput("other", date(2026, 7, 1), 0.3),
        ]

        forward = attribute_goals(items, context, aliases={})
        reverse = attribute_goals(reversed(items), context, aliases={})

        assert forward == reverse

    def test_none_context_is_no_op_without_consuming_inputs(self):
        def should_not_run():
            raise AssertionError("items were consumed")
            yield  # pragma: no cover

        assert attribute_goals(should_not_run(), None) is None

    @pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True])
    def test_rejects_invalid_contribution(self, value):
        with pytest.raises(GoalContextError, match="finite, non-negative"):
            GoalAttributionInput("project", date(2026, 7, 1), value)


class TestSessionBreakdown:
    @staticmethod
    def _session(
        project: str,
        session_id: str,
        start: datetime,
        end: datetime,
    ) -> SessionStat:
        return SessionStat(
            project=project,
            category="coding",
            session_id=session_id,
            start=start,
            end=end,
            active_sec=int((end - start).total_seconds()),
            msg_count=2,
            timestamps=[start.timestamp(), end.timestamp()],
        )

    def test_uses_existing_one_global_wall_clock_scale(self):
        start = datetime(2026, 7, 2, 10, tzinfo=timezone.utc)
        end = start + timedelta(minutes=1)
        sessions = [
            self._session("alpha", "alpha-session", start, end),
            self._session("beta", "beta-session", start, end),
        ]
        context = parse_goal_context(
            {
                "schema_version": 1,
                "goals": [
                    {
                        "id": "alpha-goal",
                        "title": "Alpha",
                        "projects": ["alpha"],
                    },
                    {
                        "id": "beta-goal",
                        "title": "Beta",
                        "projects": ["beta"],
                    },
                ],
            },
            aliases={},
        )

        breakdown = build_goal_breakdown(
            sessions, context, aliases={}, timezone=timezone.utc
        )

        assert breakdown is not None
        # Raw sum is 120 seconds, but both sessions cover the same wall-clock
        # minute. The existing rollup scale is 60 / 120, so each gets 30.
        assert breakdown.covered_contribution == 60
        assert breakdown.exclusive_contribution == 60
        assert [goal.exclusive_contribution for goal in breakdown.goals] == [
            30,
            30,
        ]

    def test_splits_validity_at_report_local_midnight(self):
        pacific = timezone(timedelta(hours=-7))
        # 06:59:30-07:00:30 UTC straddles local midnight in UTC-7.
        start = datetime(2026, 7, 2, 6, 59, 30, tzinfo=timezone.utc)
        end = start + timedelta(minutes=1)
        session = self._session("alpha", "midnight", start, end)
        context = parse_goal_context(
            {
                "schema_version": 1,
                "goals": [
                    {
                        "id": "after",
                        "title": "After midnight",
                        "projects": ["alpha"],
                        "valid_from": "2026-07-02",
                    },
                    {
                        "id": "before",
                        "title": "Through local July 1",
                        "projects": ["alpha"],
                        "valid_until": "2026-07-01",
                    },
                ],
            },
            aliases={},
        )

        breakdown = build_goal_breakdown(
            [session], context, aliases={}, timezone=pacific
        )

        assert breakdown is not None
        assert breakdown.covered_contribution == 60
        assert breakdown.exclusive_contribution == 60
        assert breakdown.shared_contribution == 0
        assert [goal.goal_id for goal in breakdown.goals] == [
            "after",
            "before",
        ]
        assert [goal.exclusive_contribution for goal in breakdown.goals] == [
            30,
            30,
        ]

    def test_none_context_is_no_op_without_inspecting_sessions(self):
        class ExplodingSequence:
            def __iter__(self):
                raise AssertionError("sessions were inspected")

        assert build_goal_breakdown(ExplodingSequence(), None) is None

    def test_rejects_positive_contribution_without_interval_evidence(self):
        start = datetime(2026, 7, 2, 10, tzinfo=timezone.utc)
        session = SessionStat(
            project="alpha",
            category="coding",
            session_id="missing-intervals",
            start=start,
            end=start + timedelta(minutes=1),
            active_sec=60,
            msg_count=2,
            timestamps=[],
        )

        with pytest.raises(GoalContextError, match="without active-interval"):
            build_goal_breakdown(
                [session], _context(), aliases={}, timezone=timezone.utc
            )
