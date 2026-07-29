"""Minimal GoalContext-to-recap plumbing contracts (#217)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ccstory import recap
from ccstory.goals import parse_goal_context
from tests.conftest import make_assistant_msg, make_user_msg


def _recent_ts(hours_ago: float) -> str:
    value = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def test_goal_result_fields_are_appended_for_positional_compatibility():
    fields = list(recap.RecapResult.__dataclass_fields__)
    assert fields[-2:] == ["goal_context", "goal_breakdown"]


def test_recap_computes_one_goal_breakdown_without_exposing_surfaces(
    jsonl_factory, monkeypatch
):
    session_id = "goal-recap-session"
    jsonl_factory(
        "-Users-me-alpha-app",
        session_id,
        [
            make_user_msg("Work toward alpha", _recent_ts(2.5)),
            make_assistant_msg(
                "Starting", _recent_ts(2.4), f"{session_id}-m1"
            ),
            make_user_msg("Finish the test", _recent_ts(2.3)),
            make_assistant_msg(
                "Done", _recent_ts(2.2), f"{session_id}-m2"
            ),
        ],
    )
    context = parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {
                    "id": "goal-alpha",
                    "title": "Alpha outcome",
                    "projects": ["alpha-app"],
                }
            ],
        },
        aliases={},
        source_metadata={"source_kind": "explicit"},
        source_fingerprint="sha256:test",
    )

    calls = 0
    real_builder = recap.build_goal_breakdown

    def counted_builder(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_builder(*args, **kwargs)

    monkeypatch.setattr(recap, "build_goal_breakdown", counted_builder)
    result = recap.build_recap(
        "week",
        minimal=True,
        compare=False,
        artifacts=False,
        write_report=False,
        goal_context=context,
    )

    assert calls == 1
    assert result.goal_context is context
    assert result.goal_breakdown is not None
    assert result.goal_breakdown.exclusive_contribution > 0
    assert result.goal_breakdown.shared_contribution == 0
    assert result.goal_breakdown.unattributed_contribution == 0
    assert result.goal_breakdown.goals[0].goal_id == "goal-alpha"
    assert (
        result.goal_breakdown.goals[0].total_contribution
        == result.goal_breakdown.covered_contribution
    )

    # #218 owns output surfaces; #217 only hands the objects downstream.
    assert "Alpha outcome" not in result.markdown
    assert "goal_context" not in result.to_json()
    assert "goal_breakdown" not in result.to_json()


def test_recap_without_context_keeps_optional_fields_empty(jsonl_factory):
    session_id = "no-goal-recap-session"
    jsonl_factory(
        "-Users-me-alpha-app",
        session_id,
        [
            make_user_msg("Work without goals", _recent_ts(1.5)),
            make_assistant_msg(
                "Starting", _recent_ts(1.4), f"{session_id}-m1"
            ),
            make_user_msg("Finish", _recent_ts(1.3)),
            make_assistant_msg(
                "Done", _recent_ts(1.2), f"{session_id}-m2"
            ),
        ],
    )

    result = recap.build_recap(
        "week",
        minimal=True,
        compare=False,
        artifacts=False,
        write_report=False,
    )

    assert result.goal_context is None
    assert result.goal_breakdown is None
