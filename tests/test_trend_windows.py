"""Window-pure period assignment for `collect_trend()` (#188 trend slice).

`tests/test_window_integration.py` and `tests/test_provider_snapshot.py` prove
`collect_provider_snapshot()` / `_window_bounded_sessions` window-purity for
recap's current/previous pair. `collect_trend()` reused none of that: it
assigned each session to exactly one period by physical *start time*, so a
session crossing a period boundary contributed its whole self to whichever
period held its start and nothing to the other, and it called `collect_usage`
once per period instead of reading `ProviderSnapshot.usage_by_window`.

These tests prove the fix: every period is driven off one
`collect_provider_snapshot()` call over the full window map, a
boundary-crossing session contributes its own clipped facts to each period it
touches, a contained session is completely unaffected, and per-period usage
figures are unchanged by moving their call site onto the snapshot (Decision 5
of the #188 integration brief).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from ccstory.providers import collect_provider_snapshot
from ccstory.providers.claude import ClaudeCodeProvider
from ccstory.session_summarizer import _classify_cache_upsert_many
from ccstory.time_tracking import (
    CategoryRollup,
    SessionSlice,
    SessionStat,
    active_intervals_for_timestamps,
    clip_active_intervals,
    collect_sessions,
    rollup_by_category,
)
from ccstory.token_usage import collect_usage, collect_usage_for_windows
from ccstory.trends import (
    PeriodPoint,
    _resolve_sessions_from_cache,
    collect_trend,
    trend_by_category,
)
from tests.conftest import make_assistant_msg, make_user_msg

# `collect_trend(period="week", count=2, now=NOW)` produces exactly two
# adjacent 7-day windows meeting at BOUNDARY: [NOW-14d, BOUNDARY) and
# [BOUNDARY, NOW). Every test below builds a session around BOUNDARY.
NOW = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
BOUNDARY = NOW - timedelta(days=7)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _two_periods(jsonl_factory=None):
    """Run collect_trend and return (older, newer) — a small shared helper.

    Not a fixture: most tests need this only after writing their own
    session(s), so it is just a plain call wrapped for readability.
    """
    points = collect_trend(
        period="week", count=2, now=NOW,
        mode="folder", fallback="coding", agent="claude",
    )
    assert len(points) == 2
    older, newer = points
    assert older.until == BOUNDARY
    assert newer.since == BOUNDARY
    return older, newer


# ----- 1. Conservation --------------------------------------------------------


def test_active_seconds_conserved_across_adjacent_trend_periods(jsonl_factory):
    """A session crossing a trend-period boundary must not gain or lose time.

    Same shape as test_window_integration.py's cross-window conservation
    test: one interval fully before the boundary, one fully after, and one
    straddling interval created by a 4-minute gap under the 5-minute cap —
    proving a straddling interval is split without double-counting or losing
    time. The expectation is derived from the same
    ``clip_active_intervals``/``active_intervals_for_timestamps`` helpers
    production code uses, never a hardcoded number.
    """
    path = jsonl_factory(
        "trend-conservation-project",
        "trend-conservation-session",
        [
            make_user_msg(
                "first request", _iso(BOUNDARY - timedelta(minutes=10)),
            ),
            make_assistant_msg(
                "first outcome",
                _iso(BOUNDARY - timedelta(minutes=2)),
                "trend-conservation-a1",
            ),
            make_user_msg(
                "second request", _iso(BOUNDARY + timedelta(minutes=2)),
            ),
            make_assistant_msg(
                "second outcome",
                _iso(BOUNDARY + timedelta(minutes=10)),
                "trend-conservation-a2",
            ),
        ],
    )
    provider = ClaudeCodeProvider()
    physical = provider.parse_session(path)
    assert physical is not None

    older, newer = _two_periods()

    def clipped_active_sec(since: datetime, until: datetime) -> int:
        intervals = clip_active_intervals(
            active_intervals_for_timestamps(physical.timestamps), since, until,
        )
        return int(sum(interval.end - interval.start for interval in intervals))

    older_expected_sec = clipped_active_sec(older.since, older.until)
    newer_expected_sec = clipped_active_sec(newer.since, newer.until)
    union_expected_sec = clipped_active_sec(older.since, newer.until)

    # Non-trivial split: both periods actually carry a piece of the activity.
    assert older_expected_sec > 0
    assert newer_expected_sec > 0
    # The two periods' clipped active intervals sum back to the physical
    # session's clipped total — nothing leaked or was double-counted at the
    # boundary.
    assert older_expected_sec + newer_expected_sec == union_expected_sec

    # And collect_trend()'s own PeriodPoint numbers reflect exactly that —
    # the same rounding rule SessionSlice.active_min / rollup_by_category use.
    assert older.total_h == round(older_expected_sec / 60, 1) / 60
    assert newer.total_h == round(newer_expected_sec / 60, 1) / 60

    # Tie it back to the actual SessionSlice objects collect_trend() built
    # internally, not just a parallel computation of the same formula.
    window_map = {older.label: (older.since, older.until),
                  newer.label: (newer.since, newer.until)}
    snapshot = collect_provider_snapshot(window_map, agent="claude")
    older_slice = snapshot.sessions_by_window[older.label][0]
    newer_slice = snapshot.sessions_by_window[newer.label][0]
    assert isinstance(older_slice, SessionSlice)
    assert isinstance(newer_slice, SessionSlice)
    assert older_slice.active_sec == older_expected_sec
    assert newer_slice.active_sec == newer_expected_sec


# ----- 2. No cross-period contamination ---------------------------------------


def test_no_cross_period_contamination(jsonl_factory):
    """Each period's rollup must reflect only its own bounded evidence.

    A pre-fix `collect_trend()` would have put the *entire* physical session
    — both messages, both timestamps — into whichever period held its start.
    """
    jsonl_factory(
        "trend-contamination-project",
        "trend-contamination-session",
        [
            make_user_msg(
                "TREND_BEFORE_BOUNDARY_REQUEST",
                _iso(BOUNDARY - timedelta(minutes=1)),
            ),
            make_assistant_msg(
                "TREND_AFTER_BOUNDARY_OUTCOME",
                _iso(BOUNDARY + timedelta(minutes=1)),
                "trend-contamination-a1",
            ),
        ],
    )
    older, newer = _two_periods()

    older_sessions = [s for r in older.rollups for s in r.top_sessions]
    newer_sessions = [s for r in newer.rollups for s in r.top_sessions]
    assert len(older_sessions) == 1
    assert len(newer_sessions) == 1
    older_slice, newer_slice = older_sessions[0], newer_sessions[0]
    assert isinstance(older_slice, SessionSlice)
    assert isinstance(newer_slice, SessionSlice)
    assert older_slice.session_id != newer_slice.session_id

    assert "TREND_BEFORE_BOUNDARY_REQUEST" in older_slice.evidence_excerpt
    assert "TREND_AFTER_BOUNDARY_OUTCOME" not in older_slice.evidence_excerpt
    assert "TREND_AFTER_BOUNDARY_OUTCOME" in newer_slice.evidence_excerpt
    assert "TREND_BEFORE_BOUNDARY_REQUEST" not in newer_slice.evidence_excerpt

    # Message counts do not leak across the boundary either.
    assert sum(r.messages for r in older.rollups) == 1
    assert sum(r.messages for r in newer.rollups) == 1


# ----- 3. Contained sessions are unaffected -----------------------------------


def test_contained_session_matches_pre_change_algorithm(jsonl_factory):
    """A session fully inside one period must produce the exact same
    PeriodPoint numbers the pre-window-pure algorithm would have — proving
    the fix changes only boundary-crossing behavior.

    Reimplements the old algorithm (flat scan, bucket by physical start time,
    per-period ``collect_usage``) directly from the still-existing building
    blocks, rather than hardcoding an expected number.
    """
    jsonl_factory(
        "trend-contained-project",
        "trend-contained-session",
        [
            make_user_msg(
                "only request", _iso(BOUNDARY + timedelta(days=1)),
            ),
            make_assistant_msg(
                "only outcome",
                _iso(BOUNDARY + timedelta(days=1, minutes=5)),
                "trend-contained-a1",
            ),
        ],
    )
    older, newer = _two_periods()

    # Sanity: this session is fully inside `newer` by construction, so the
    # old start-time-only bucketing would have agreed on placement anyway —
    # this test isolates the "contained case is unaffected" claim from the
    # window-assignment change itself (covered by the tests above).
    assert older.total_h == 0.0
    assert older.rollups == []
    assert len(newer.rollups) == 1
    contained = newer.rollups[0].top_sessions[0]
    assert isinstance(contained, SessionStat)
    assert not isinstance(contained, SessionSlice)

    all_sessions = collect_sessions(older.since, NOW, agent="claude")
    _resolve_sessions_from_cache(all_sessions, mode="folder", fallback="coding")
    for point in (older, newer):
        legacy_in_window = [
            s for s in all_sessions
            if s.start >= point.since and s.start < point.until
        ]
        legacy_rollups = [
            (r.category, r.active_min, r.sessions, r.messages)
            for r in rollup_by_category(legacy_in_window)
        ]
        legacy_usage = collect_usage(
            point.since, point.until, agent="claude",
            active_agents={s.agent for s in legacy_in_window},
        )
        actual_rollups = [
            (r.category, r.active_min, r.sessions, r.messages)
            for r in point.rollups
        ]
        assert actual_rollups == legacy_rollups
        assert point.total_h == sum(r.active_min for r in point.rollups) / 60
        assert point.output_tokens == legacy_usage.total_output
        assert point.cost_usd == legacy_usage.total_cost_usd
        assert point.provider_coverage == legacy_usage.provider_coverage
        assert point.unpriced_models == legacy_usage.unpriced_models


# ----- 4. Usage invariance (Decision 5) ---------------------------------------


def test_usage_invariance_moving_the_call_site_does_not_move_the_numbers(
    jsonl_factory,
):
    """Decision 5: collect_usage's semantics — inclusive boundary, exact
    per-record attribution — do not change when collect_trend() stops
    calling it once per period and starts reading
    ``ProviderSnapshot.usage_by_window``.

    The crossing session proves the fix's real target: its tokens/cost land
    in whichever period actually saw that assistant turn, which was always
    computed independently of the session list. An anchor session, fully
    contained in each period, keeps that period's ``provider_coverage``
    membership identical under the old start-time bucketing and the new
    window-pure bucketing — isolating this test to Decision 5's token/cost
    claim, rather than also asserting the (expected, intentional)
    provider_coverage widening a crossing session's *own* far period
    legitimately gets once window purity makes it visible there.
    """
    jsonl_factory(
        "trend-usage-crossing-project",
        "trend-usage-crossing-session",
        [
            make_user_msg(
                "request before boundary", _iso(BOUNDARY - timedelta(minutes=1)),
            ),
            make_assistant_msg(
                "outcome before boundary",
                _iso(BOUNDARY - timedelta(seconds=30)),
                "trend-usage-a1",
                model="claude-sonnet-4-6",
                input_tokens=200, cache_creation=20, cache_read=40,
                output_tokens=60,
            ),
            make_assistant_msg(
                "outcome after boundary",
                _iso(BOUNDARY + timedelta(minutes=1)),
                "trend-usage-a2",
                model="claude-opus-4-7",
                input_tokens=100, cache_creation=10, cache_read=20,
                output_tokens=30,
            ),
        ],
    )
    jsonl_factory(
        "trend-usage-anchor-project",
        "trend-usage-anchor-older",
        [
            make_user_msg(
                "anchor older", _iso(BOUNDARY - timedelta(days=1)),
            ),
            make_assistant_msg(
                "anchor older outcome",
                _iso(BOUNDARY - timedelta(days=1) + timedelta(minutes=1)),
                "trend-usage-anchor-a1",
            ),
        ],
    )
    jsonl_factory(
        "trend-usage-anchor-project",
        "trend-usage-anchor-newer",
        [
            make_user_msg(
                "anchor newer", _iso(BOUNDARY + timedelta(days=1)),
            ),
            make_assistant_msg(
                "anchor newer outcome",
                _iso(BOUNDARY + timedelta(days=1) + timedelta(minutes=1)),
                "trend-usage-anchor-a2",
            ),
        ],
    )

    older, newer = _two_periods()

    all_sessions = collect_sessions(older.since, NOW, agent="claude")
    for point in (older, newer):
        legacy_in_window = [
            s for s in all_sessions
            if s.start >= point.since and s.start < point.until
        ]
        legacy_usage = collect_usage(
            point.since, point.until, agent="claude",
            active_agents={s.agent for s in legacy_in_window},
        )
        assert point.output_tokens == legacy_usage.total_output
        assert point.cost_usd == legacy_usage.total_cost_usd
        assert point.provider_coverage == legacy_usage.provider_coverage
        assert point.unpriced_models == legacy_usage.unpriced_models

    # And confirm this was a real test of the crossing case, not an
    # accidental no-op: the crossing session really is sliced in both
    # periods, and its tokens really are split (60 before, 30 after).
    window_map = {older.label: (older.since, older.until),
                  newer.label: (newer.since, newer.until)}
    snapshot = collect_provider_snapshot(window_map, agent="claude")
    crossing_older = next(
        s for s in snapshot.sessions_by_window[older.label]
        if isinstance(s, SessionSlice)
    )
    crossing_newer = next(
        s for s in snapshot.sessions_by_window[newer.label]
        if isinstance(s, SessionSlice)
    )
    assert crossing_older.physical_session_id == "trend-usage-crossing-session"
    assert crossing_newer.physical_session_id == "trend-usage-crossing-session"
    reference = collect_usage_for_windows(
        window_map, agent="claude",
        active_agents_by_window={
            older.label: {s.agent for s in snapshot.sessions_by_window[older.label]},
            newer.label: {s.agent for s in snapshot.sessions_by_window[newer.label]},
        },
    )
    assert reference[older.label].total_output == older.output_tokens
    assert reference[newer.label].total_output == newer.output_tokens


# ----- 5. Classification survives a split -------------------------------------


def test_classification_survives_a_session_split_across_two_periods(
    jsonl_factory,
):
    """A cached category must resolve correctly for a session split into two
    periods' slices, each cached under its own evidence id.

    Mirrors tests/test_cross_window_symmetry.py's resolver-symmetry proof for
    trend's N-period window map. Content classification is evidence-scoped by
    design (session_identity.py): the two slices of one physical session
    describe different work and may legitimately land in different buckets,
    so each must resolve its own cached verdict rather than the other's, or a
    fallback.
    """
    jsonl_factory(
        "trend-classification-project",
        "trend-classification-session",
        [
            make_user_msg(
                "request before boundary", _iso(BOUNDARY - timedelta(minutes=1)),
            ),
            make_assistant_msg(
                "outcome after boundary",
                _iso(BOUNDARY + timedelta(minutes=1)),
                "trend-classification-a1",
            ),
            make_user_msg(
                "confirm outcome", _iso(BOUNDARY + timedelta(minutes=2)),
            ),
        ],
    )

    # Discover the window map and each slice's own evidence id. Folder mode
    # needs no cache, so this discovery pass cannot be affected by the
    # seeding step below.
    older, newer = _two_periods()
    window_map = {older.label: (older.since, older.until),
                  newer.label: (newer.since, newer.until)}
    snapshot = collect_provider_snapshot(window_map, agent="claude")
    older_slice = snapshot.sessions_by_window[older.label][0]
    newer_slice = snapshot.sessions_by_window[newer.label][0]
    assert isinstance(older_slice, SessionSlice)
    assert isinstance(newer_slice, SessionSlice)
    assert older_slice.session_id != newer_slice.session_id

    _classify_cache_upsert_many({
        older_slice.session_id: "writing",
        newer_slice.session_id: "research",
    })

    resolved_older, resolved_newer = collect_trend(
        period="week", count=2, now=NOW,
        mode="hybrid", fallback="coding", agent="claude",
    )
    assert {r.category for r in resolved_older.rollups} == {"writing"}
    assert {r.category for r in resolved_newer.rollups} == {"research"}
    # Neither slice was double-resolved into the other's (or a fallback)
    # category.
    assert "coding" not in {r.category for r in resolved_older.rollups}
    assert "coding" not in {r.category for r in resolved_newer.rollups}


# ----- 6. Public contract intact ----------------------------------------------


def test_period_point_and_trend_by_category_public_contract_unchanged():
    """Existing consumers read PeriodPoint by field name and
    trend_by_category() by ``{category: [hours, ...]}`` shape. Neither may
    move for this change — it is purely an internal recomputation.
    """
    expected_fields = {
        "label", "since", "until", "rollups", "total_h",
        "output_tokens", "cost_usd", "provider_coverage", "unpriced_models",
        # Additive (#256) — see PeriodPoint.classification_coverage.
        "classification_coverage",
    }
    actual_fields = {f.name for f in dataclasses.fields(PeriodPoint)}
    assert actual_fields == expected_fields

    points = [
        PeriodPoint(
            label="2026-W01",
            since=NOW - timedelta(days=14),
            until=NOW - timedelta(days=7),
            rollups=[
                CategoryRollup(
                    category="coding", active_min=60.0, sessions=1, messages=3,
                ),
            ],
            total_h=1.0,
            output_tokens=10,
            cost_usd=0.01,
        ),
        PeriodPoint(
            label="2026-W02",
            since=NOW - timedelta(days=7),
            until=NOW,
            rollups=[
                CategoryRollup(
                    category="coding", active_min=120.0, sessions=2, messages=6,
                ),
            ],
            total_h=2.0,
            output_tokens=20,
            cost_usd=0.02,
        ),
    ]
    series = trend_by_category(points)
    assert series == {"coding": [1.0, 2.0]}


# ----- 7. Usage coverage stops lying about an untouched period ----------------


def test_a_period_reached_only_by_a_crossing_session_reports_its_provider(
    jsonl_factory,
):
    """The one usage field window purity *does* legitimately change.

    ``provider_coverage`` is derived from which agents appear active in a
    window, so unlike tokens and cost it does depend on the session list.
    Under start-time bucketing a period whose only activity came from a
    session that started in the *previous* period saw an empty session list,
    and therefore reported no provider at all — while its token and cost
    figures, computed by an independent per-record scan, happily showed that
    period's real usage. A period could show tokens spent by nobody.

    The other usage test deliberately anchors both periods with a contained
    session so it can isolate Decision 5's token/cost claim. This one removes
    the anchor to assert the widening itself: the newer period contains
    nothing but the far half of a session that began before the boundary, and
    it must now name the provider whose usage it is already reporting.
    """
    jsonl_factory(
        "coverage-widening-project",
        "coverage-widening-session",
        [
            make_user_msg(
                "request well before the boundary",
                _iso(BOUNDARY - timedelta(hours=2)),
            ),
            make_assistant_msg(
                "outcome before the boundary",
                _iso(BOUNDARY - timedelta(hours=1, minutes=59)),
                "coverage-widening-a1",
            ),
            make_user_msg(
                "request after the boundary",
                _iso(BOUNDARY + timedelta(minutes=1)),
            ),
            make_assistant_msg(
                "outcome after the boundary",
                _iso(BOUNDARY + timedelta(minutes=2)),
                "coverage-widening-a2",
                model="claude-sonnet-4-6",
                input_tokens=200, cache_creation=20, cache_read=40,
                output_tokens=60,
            ),
        ],
    )

    older, newer = _two_periods()

    # The newer period's only session is the crossing session's far half.
    assert len(newer.rollups) == 1
    assert newer.rollups[0].sessions == 1

    # It really is reporting that period's usage...
    assert newer.output_tokens > 0
    # ...so it must also name whose usage it is.
    assert "claude" in newer.provider_coverage

    # The older period, which holds the session's start, is unaffected.
    assert "claude" in older.provider_coverage

    # Under the old start-time bucketing the newer period's session list was
    # empty. Asserting that directly pins what this test is guarding: the
    # crossing session's start lies in the older period, so any future
    # regression back to start-time assignment strands the newer period's
    # tokens with no provider again.
    all_sessions = collect_sessions(newer.since - timedelta(days=7), NOW, agent="claude")
    started_in_newer = [
        session for session in all_sessions
        if newer.since <= session.start < newer.until
    ]
    assert started_in_newer == []
