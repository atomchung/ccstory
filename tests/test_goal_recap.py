"""Minimal GoalContext-to-recap plumbing contracts (#217)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

from ccstory import recap
from ccstory import session_summarizer as ss
from ccstory.goals import _date_segments, parse_goal_context
from tests.conftest import make_assistant_msg, make_user_msg


class _SeasonalTimezone(tzinfo):
    """A minimal DST-observing zone: UTC-4 in Apr-Oct, UTC-5 otherwise.

    Windows CI has no `tzdata`, so `ZoneInfo("America/New_York")` is not
    resolvable there. This carries the only property these tests need — an
    offset that depends on the date rather than on when the test runs.
    """

    _WINTER = timedelta(hours=-5)
    _SUMMER = timedelta(hours=-4)

    @staticmethod
    def _is_summer(value: datetime | None) -> bool:
        return value is not None and 4 <= value.month <= 10

    def utcoffset(self, value: datetime | None) -> timedelta:
        return self._SUMMER if self._is_summer(value) else self._WINTER

    def dst(self, value: datetime | None) -> timedelta:
        return timedelta(hours=1) if self._is_summer(value) else timedelta(0)

    def tzname(self, value: datetime | None) -> str:
        return "SUM" if self._is_summer(value) else "WIN"


def _recent_ts(hours_ago: float) -> str:
    value = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def test_goal_result_fields_are_appended_for_positional_compatibility():
    fields = list(recap.RecapResult.__dataclass_fields__)
    assert fields[-2:] == ["goal_context", "goal_breakdown"]


def test_recap_computes_one_goal_breakdown_and_projects_surfaces(
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

    assert "## Goal activity" in result.markdown
    assert "Alpha outcome" in result.markdown
    assert result.to_json()["goals"]["goals"][0]["id"] == "goal-alpha"
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
    assert "## Goal activity" not in result.markdown
    assert "goals" not in result.to_json()


def test_goal_context_never_enters_narrator_prompt_or_trace(
    jsonl_factory, monkeypatch
):
    session_id = "goal-privacy-session"
    jsonl_factory(
        "-Users-me-alpha-app",
        session_id,
        [
            make_user_msg("Do ordinary project work", _recent_ts(2.5)),
            make_assistant_msg(
                "Starting", _recent_ts(2.4), f"{session_id}-m1"
            ),
            make_user_msg("Add ordinary tests", _recent_ts(2.3)),
            make_assistant_msg(
                "Done", _recent_ts(2.2), f"{session_id}-m2"
            ),
        ],
    )
    secrets = (
        "SECRET GOAL TITLE",
        "SECRET SOURCE CONTENT",
        "/Users/private/goals.toml",
        "sha256:secret-goal-fingerprint",
    )
    context = parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {
                    "id": "private-goal",
                    "title": secrets[0],
                    "projects": ["alpha-app"],
                }
            ],
        },
        aliases={},
        source_metadata={
            "source_kind": "configured",
            "content": secrets[1],
            "path": secrets[2],
        },
        source_fingerprint=secrets[3],
    )
    ss.upsert(
        session_id,
        "Completed ordinary project work and added tests.",
        source="provided",
        project="alpha-app",
    )
    prompts: list[str] = []

    def fake_narrator(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        return ss.NarrativeCall(
            "**Ordinary work shipped**\n- Added ordinary tests.",
            "codex",
            "test-model",
        )

    monkeypatch.setattr(ss, "llm_available", lambda: True)
    monkeypatch.setattr(ss, "run_llm_p", fake_narrator)

    result = recap.build_recap(
        "week",
        narrative="overall",
        classify="folder",
        compare=False,
        artifacts=False,
        write_report=False,
        goal_context=context,
    )

    assert prompts
    narrator_material = "\n".join(prompts)
    narrator_trace = str(result.narrative_provenance)
    for secret in secrets:
        assert secret not in narrator_material
        assert secret not in narrator_trace


def test_recap_attributes_goals_with_historical_timezone_rules(
    jsonl_factory, monkeypatch
):
    """Recap must not attribute goals with the window's fixed-offset tzinfo.

    `parse_window()` derives the window from `datetime.now().astimezone()`,
    whose tzinfo carries only the offset in effect right now. Splitting
    contributions at local midnight with it moves an hour of work to the wrong
    local date once the window reaches across a DST transition, so recap and
    `goal-history` would disagree about the same activity (#230).
    """

    session_id = "goal-tz-session"
    jsonl_factory(
        "-Users-me-alpha-app",
        session_id,
        [
            make_user_msg("Work toward alpha", _recent_ts(2.5)),
            make_assistant_msg("Done", _recent_ts(2.4), f"{session_id}-m1"),
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
    )

    sentinel = _SeasonalTimezone()
    monkeypatch.setattr(recap, "system_local_timezone", lambda: sentinel)
    seen: dict[str, object] = {}
    real_builder = recap.build_goal_breakdown

    def capturing_builder(*args, **kwargs):
        seen["timezone"] = kwargs.get("timezone")
        return real_builder(*args, **kwargs)

    monkeypatch.setattr(recap, "build_goal_breakdown", capturing_builder)
    recap.build_recap(
        "week",
        minimal=True,
        compare=False,
        artifacts=False,
        write_report=False,
        goal_context=context,
    )

    assert seen["timezone"] is sentinel


def test_date_segments_split_at_true_local_midnight_out_of_season():
    """A fixed-offset tzinfo mis-dates activity outside the current season."""

    seasonal = _SeasonalTimezone()
    # 04:45-05:45 UTC on 2026-01-15 straddles local midnight at the winter
    # offset (-5), but not at the summer offset (-4) that a `.astimezone()`
    # call made during summer would capture and reuse for a January date.
    start = datetime(2026, 1, 15, 4, 45, tzinfo=timezone.utc).timestamp()
    end = start + 3600

    correct = [
        (day.isoformat(), round(seconds))
        for day, seconds in _date_segments(start, end, seasonal)
    ]
    fixed_offset = [
        (day.isoformat(), round(seconds))
        for day, seconds in _date_segments(
            start, end, timezone(timedelta(hours=-4))
        )
    ]

    assert correct == [("2026-01-14", 900), ("2026-01-15", 2700)]
    assert fixed_offset == [("2026-01-15", 3600)]
