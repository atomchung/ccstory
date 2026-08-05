"""Pure deterministic weekly GoalActivitySeries core contracts (#225 PR A)."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import ccstory.goals as goals_module
from ccstory.goals import (
    GOAL_ACTIVITY_DISCLAIMER,
    GLOBAL_GOAL_BUCKET_SEMANTICS,
    GoalActivityWindow,
    GoalAttributionInput,
    GoalBreakdown,
    GoalContextError,
    attribute_goals,
    build_goal_activity_series,
    parse_goal_context,
)
from ccstory.time_tracking import SessionStat, session_slice_for_window


UTC = timezone.utc
OLDER_SINCE = datetime(2026, 7, 6, tzinfo=UTC)
BOUNDARY = datetime(2026, 7, 13, tzinfo=UTC)
NEWER_UNTIL = datetime(2026, 7, 20, tzinfo=UTC)


def _session(
    project: str,
    session_id: str,
    start: datetime,
    seconds: int,
) -> SessionStat:
    end = start + timedelta(seconds=seconds)
    timestamps = [
        (start + timedelta(seconds=offset)).timestamp()
        for offset in range(0, seconds, 5 * 60)
    ]
    timestamps.append(end.timestamp())
    return SessionStat(
        project=project,
        category="coding",
        session_id=session_id,
        start=start,
        end=end,
        active_sec=seconds,
        msg_count=2,
        timestamps=timestamps,
    )


def _context(*, source_kind: str = "configured", fingerprint: str = "sha256:test"):
    return parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {
                    "id": "always",
                    "title": "Always",
                    "projects": ["alpha"],
                },
                {
                    "id": "old-only",
                    "title": "Old only",
                    "projects": ["legacy"],
                    "valid_until": "2026-07-12",
                },
                {
                    "id": "new-only",
                    "title": "New only",
                    "projects": ["new"],
                    "valid_from": "2026-07-13",
                },
                {
                    "id": "zero-new",
                    "title": "Zero activity is still evidence",
                    "projects": ["zero"],
                    "valid_from": "2026-07-13",
                },
                {
                    "id": "shared-a",
                    "title": "Shared A",
                    "projects": ["shared"],
                },
                {
                    "id": "shared-b",
                    "title": "Shared B",
                    "projects": ["shared"],
                },
                {
                    "id": "expired",
                    "title": "Expired",
                    "projects": ["alpha"],
                    "valid_until": "2026-07-05",
                },
                {
                    "id": "future",
                    "title": "Future",
                    "projects": ["alpha"],
                    "valid_from": "2026-07-20",
                },
            ],
        },
        aliases={},
        source_metadata={
            "source_kind": source_kind,
            "path": "/private/goals.toml",
        },
        source_fingerprint=fingerprint,
    )


def _windows() -> tuple[GoalActivityWindow, GoalActivityWindow]:
    older = GoalActivityWindow(
        since=OLDER_SINCE,
        until=BOUNDARY,
        sessions=(
            _session("alpha", "older-alpha", OLDER_SINCE + timedelta(hours=1), 3600),
            _session("shared", "older-shared", OLDER_SINCE + timedelta(hours=3), 1800),
            _session("unknown", "older-unknown", OLDER_SINCE + timedelta(hours=5), 900),
            _session("legacy", "older-legacy", BOUNDARY - timedelta(hours=2), 1200),
        ),
        coverage_status="complete",
    )
    newer = GoalActivityWindow(
        since=BOUNDARY,
        until=NEWER_UNTIL,
        sessions=(
            _session("alpha", "newer-alpha", BOUNDARY + timedelta(hours=1), 2400),
            _session("new", "newer-new", BOUNDARY + timedelta(hours=3), 600),
            _session("unknown", "newer-unknown", BOUNDARY + timedelta(hours=5), 300),
        ),
        coverage_status="complete",
    )
    return older, newer


def test_weekly_buckets_are_independent_complete_and_self_describing():
    series = build_goal_activity_series(
        _windows(), _context(), aliases={}, timezone=UTC
    )

    assert series is not None
    assert series.bucket_unit == "week"
    assert series.source_kind == "configured"
    assert series.context_fingerprint == "sha256:test"
    assert series.coverage_status == "complete"
    assert series.global_bucket_semantics == GLOBAL_GOAL_BUCKET_SEMANTICS
    assert series.disclaimer == GOAL_ACTIVITY_DISCLAIMER
    older, newer = series.buckets
    assert (older.since, older.until) == (OLDER_SINCE, BOUNDARY)
    assert (newer.since, newer.until) == (BOUNDARY, NEWER_UNTIL)

    assert [goal.goal_id for goal in older.goals] == [
        "always",
        "old-only",
        "shared-a",
        "shared-b",
    ]
    assert [goal.goal_id for goal in newer.goals] == [
        "always",
        "new-only",
        "shared-a",
        "shared-b",
        "zero-new",
    ]
    zero = next(goal for goal in newer.goals if goal.goal_id == "zero-new")
    assert zero.total_contribution == 0
    assert zero.projects_touched == ()
    assert zero.latest_activity is None

    assert (
        older.covered_contribution,
        older.exclusive_contribution,
        older.shared_contribution,
        older.unattributed_contribution,
    ) == (7500, 4800, 1800, 900)
    assert (
        newer.covered_contribution,
        newer.exclusive_contribution,
        newer.shared_contribution,
        newer.unattributed_contribution,
    ) == (3300, 3000, 0, 300)
    for bucket in series.buckets:
        assert (
            bucket.exclusive_contribution
            + bucket.shared_contribution
            + bucket.unattributed_contribution
            == bucket.covered_contribution
        )
        assert bucket.source_kind == series.source_kind
        assert bucket.context_fingerprint == series.context_fingerprint
        assert bucket.coverage_status == "complete"
        assert bucket.per_goal_shared_semantics == "overlapping_non_additive"
        assert bucket.global_bucket_semantics == GLOBAL_GOAL_BUCKET_SEMANTICS
        assert bucket.disclaimer == GOAL_ACTIVITY_DISCLAIMER
    assert older.unattributed_share == 900 / 7500
    assert newer.unattributed_share == 300 / 3300

    shared_a = next(goal for goal in older.goals if goal.goal_id == "shared-a")
    shared_b = next(goal for goal in older.goals if goal.goal_id == "shared-b")
    assert shared_a.shared_contribution == shared_b.shared_contribution == 1800
    assert older.shared_contribution == 1800

    payload = series.to_dict()
    assert payload["buckets"][0]["source_kind"] == "configured"
    assert payload["coverage_status"] == "complete"
    assert payload["buckets"][0]["coverage_status"] == "complete"
    assert payload["buckets"][0]["global_bucket_semantics"] == (
        "additive_each_contribution_counted_once"
    )
    assert payload["buckets"][0]["coverage"]["unattributed_share"] == 900 / 7500
    assert "/private/goals.toml" not in repr(payload)


def test_cross_window_session_contributes_only_its_bounded_slice():
    physical = _session(
        "alpha",
        "crossing",
        BOUNDARY - timedelta(minutes=2),
        4 * 60,
    )
    older_slice = session_slice_for_window(physical, OLDER_SINCE, BOUNDARY)
    newer_slice = session_slice_for_window(physical, BOUNDARY, NEWER_UNTIL)
    assert older_slice is not None and newer_slice is not None

    series = build_goal_activity_series(
        (
            GoalActivityWindow(
                OLDER_SINCE,
                BOUNDARY,
                (older_slice,),
                coverage_status="complete",
            ),
            GoalActivityWindow(
                BOUNDARY,
                NEWER_UNTIL,
                (newer_slice,),
                coverage_status="complete",
            ),
        ),
        _context(),
        aliases={},
        timezone=UTC,
    )

    assert series is not None
    assert [bucket.covered_contribution for bucket in series.buckets] == [120, 120]
    assert sum(bucket.covered_contribution for bucket in series.buckets) == 240
    assert [bucket.goals[0].latest_activity for bucket in series.buckets] == [
        date(2026, 7, 12),
        date(2026, 7, 13),
    ]

    forged = dataclasses.replace(older_slice, active_sec=older_slice.active_sec + 1)
    with pytest.raises(GoalContextError, match="active_sec must match"):
        build_goal_activity_series(
            (
                GoalActivityWindow(
                    OLDER_SINCE,
                    BOUNDARY,
                    (forged,),
                    coverage_status="complete",
                ),
            ),
            _context(),
            timezone=UTC,
        )


def test_input_window_and_session_permutations_do_not_change_output():
    older, newer = _windows()
    permuted = (
        GoalActivityWindow(
            newer.since,
            newer.until,
            tuple(reversed(newer.sessions)),
            coverage_status=newer.coverage_status,
        ),
        GoalActivityWindow(
            older.since,
            older.until,
            tuple(reversed(older.sessions)),
            coverage_status=older.coverage_status,
        ),
    )

    forward = build_goal_activity_series(
        (older, newer), _context(), aliases={}, timezone=UTC
    )
    reverse = build_goal_activity_series(permuted, _context(), aliases={}, timezone=UTC)

    assert forward == reverse
    assert forward is not None and reverse is not None
    assert forward.to_dict() == reverse.to_dict()


def test_dst_week_uses_seven_local_days_not_fixed_elapsed_seconds():
    pacific = ZoneInfo("America/Los_Angeles")
    since = datetime(2026, 3, 2, tzinfo=pacific)
    until = datetime(2026, 3, 9, tzinfo=pacific)
    assert until.timestamp() - since.timestamp() == 167 * 3600
    context = parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {
                    "id": "through-sunday",
                    "title": "Through Sunday",
                    "projects": ["alpha"],
                    "valid_until": "2026-03-08",
                },
                {
                    "id": "starts-next-week",
                    "title": "Starts next week",
                    "projects": ["alpha"],
                    "valid_from": "2026-03-09",
                },
            ],
        },
        aliases={},
    )

    series = build_goal_activity_series(
        (GoalActivityWindow(since, until, ()),),
        context,
        aliases={},
        timezone=pacific,
    )

    assert series is not None
    assert [goal.goal_id for goal in series.buckets[0].goals] == [
        "through-sunday"
    ]
    with pytest.raises(GoalContextError, match="seven local calendar days"):
        GoalActivityWindow(since, until + timedelta(hours=1), ())
    with pytest.raises(GoalContextError, match="valid local wall times"):
        GoalActivityWindow(
            datetime(2026, 3, 1, 2, 30, tzinfo=pacific),
            datetime(2026, 3, 8, 2, 30, tzinfo=pacific),
            (),
        )


def test_rejects_overlapping_windows_and_unbounded_or_duplicate_sessions():
    older, _newer = _windows()
    overlapping = GoalActivityWindow(
        OLDER_SINCE + timedelta(days=6),
        OLDER_SINCE + timedelta(days=13),
        (),
    )
    with pytest.raises(GoalContextError, match="overlap or duplicate"):
        build_goal_activity_series((older, overlapping), _context(), timezone=UTC)

    crossing = _session("alpha", "whole-crossing", BOUNDARY - timedelta(minutes=1), 120)
    with pytest.raises(GoalContextError, match="bounded SessionSlice"):
        build_goal_activity_series(
            (GoalActivityWindow(OLDER_SINCE, BOUNDARY, (crossing,)),),
            _context(),
            timezone=UTC,
        )

    contained = _session("alpha", "duplicate", OLDER_SINCE + timedelta(hours=1), 60)
    with pytest.raises(GoalContextError, match="duplicate physical session"):
        build_goal_activity_series(
            (GoalActivityWindow(OLDER_SINCE, BOUNDARY, (contained, contained)),),
            _context(),
            timezone=UTC,
        )


def test_unknown_source_is_public_safe_and_none_context_is_lazy():
    series = build_goal_activity_series(
        _windows(),
        _context(source_kind="private-file", fingerprint=""),
        aliases={},
        timezone=UTC,
    )
    assert series is not None
    assert series.source_kind == "provided"
    assert series.context_fingerprint is None
    assert {bucket.source_kind for bucket in series.buckets} == {"provided"}

    def exploding_windows():
        raise AssertionError("windows were consumed")
        yield  # pragma: no cover

    assert build_goal_activity_series(exploding_windows(), None) is None


def test_activity_coverage_status_is_caller_supplied_and_conservative():
    unavailable = GoalActivityWindow(OLDER_SINCE, BOUNDARY, ())
    unavailable_series = build_goal_activity_series(
        (unavailable,), _context(), timezone=UTC
    )
    assert unavailable_series is not None
    assert unavailable_series.coverage_status == "unavailable"
    assert unavailable_series.buckets[0].coverage_status == "unavailable"

    complete = GoalActivityWindow(
        OLDER_SINCE,
        BOUNDARY,
        (),
        coverage_status="complete",
    )
    mixed = GoalActivityWindow(BOUNDARY, NEWER_UNTIL, ())
    mixed_series = build_goal_activity_series(
        (mixed, complete), _context(), timezone=UTC
    )
    assert mixed_series is not None
    assert mixed_series.coverage_status == "partial"
    assert [bucket.coverage_status for bucket in mixed_series.buckets] == [
        "complete",
        "unavailable",
    ]

    partial = GoalActivityWindow(
        OLDER_SINCE,
        BOUNDARY,
        (),
        coverage_status="partial",
    )
    partial_series = build_goal_activity_series(
        (partial,), _context(), timezone=UTC
    )
    assert partial_series is not None
    assert partial_series.coverage_status == "partial"
    with pytest.raises(GoalContextError, match="coverage_status"):
        GoalActivityWindow(
            OLDER_SINCE,
            BOUNDARY,
            (),
            coverage_status="assumed-complete",
        )


def test_empty_goal_context_keeps_each_bucket_and_counts_all_work_unattributed():
    empty = parse_goal_context(
        {"schema_version": 1, "goals": []},
        aliases={},
        source_metadata={"source_kind": "managed"},
    )
    older, _newer = _windows()

    series = build_goal_activity_series((older,), empty, aliases={}, timezone=UTC)

    assert series is not None
    assert len(series.buckets) == 1
    bucket = series.buckets[0]
    assert bucket.goals == ()
    assert bucket.exclusive_contribution == 0
    assert bucket.shared_contribution == 0
    assert bucket.unattributed_contribution == bucket.covered_contribution == 7500


def test_series_reuses_existing_attribute_goals_once_per_bucket(monkeypatch):
    calls = 0
    real = goals_module.attribute_goals

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(goals_module, "attribute_goals", counted)
    series = build_goal_activity_series(
        _windows(), _context(), aliases={}, timezone=UTC
    )

    assert series is not None
    assert calls == len(series.buckets) == 2


def test_goal_breakdown_fields_and_behavior_remain_byte_shape_compatible():
    assert [field.name for field in dataclasses.fields(GoalBreakdown)] == [
        "goals",
        "covered_contribution",
        "exclusive_contribution",
        "shared_contribution",
        "unattributed_contribution",
        "contribution_unit",
        "per_goal_shared_semantics",
    ]
    context = parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {"id": "goal-a", "title": "Goal A", "projects": ["alpha"]}
            ],
        },
        aliases={},
    )
    breakdown = attribute_goals(
        (
            GoalAttributionInput("alpha", date(2026, 7, 6), 10),
            GoalAttributionInput("other", date(2026, 7, 6), 5),
        ),
        context,
        aliases={},
    )
    assert breakdown is not None
    assert breakdown.to_dict() == {
        "contribution_unit": "seconds",
        "goals": [
            {
                "goal_id": "goal-a",
                "title": "Goal A",
                "exclusive_contribution": 10.0,
                "shared_contribution": 0.0,
                "shared_contribution_is_non_additive": True,
                "total_contribution": 10.0,
                "projects_touched": ["alpha"],
                "latest_activity": "2026-07-06",
            }
        ],
        "coverage": {
            "covered_contribution": 15.0,
            "exclusive_contribution": 10.0,
            "shared_contribution": 0.0,
            "unattributed_contribution": 5.0,
        },
        "per_goal_shared_semantics": "overlapping_non_additive",
    }
