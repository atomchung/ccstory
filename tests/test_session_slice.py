"""Pure contract tests for the 0.8 window-bounded SessionSlice model."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ccstory.time_tracking import (
    ActiveInterval,
    SessionStat,
    clip_active_intervals,
    make_window_evidence,
    session_slice_for_window,
    wall_clock_active_sec,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 28, 12, tzinfo=UTC)


def _stat(*offsets: int, session_id: str = "physical-1") -> SessionStat:
    timestamps = [(BASE + timedelta(seconds=offset)).timestamp() for offset in offsets]
    return SessionStat(
        project="synthetic-project",
        category="coding",
        session_id=session_id,
        start=datetime.fromtimestamp(min(timestamps), tz=UTC),
        end=datetime.fromtimestamp(max(timestamps), tz=UTC),
        active_sec=0,
        msg_count=len(timestamps),
        user_msg_count=2,
        agent="codex",
        timestamps=timestamps,
    )


def _window(start: int, end: int) -> tuple[datetime, datetime]:
    return BASE + timedelta(seconds=start), BASE + timedelta(seconds=end)


class TestSessionSliceIntervals:
    def test_clips_crossing_intervals_with_half_open_boundaries(self):
        stat = _stat(-60, 60, 180)
        since, until = _window(0, 120)

        slice_ = session_slice_for_window(stat, since, until)

        assert slice_ is not None
        assert slice_.timestamps == [(BASE + timedelta(seconds=60)).timestamp()]
        assert [(item.start, item.end) for item in slice_.active_intervals] == [
            (since.timestamp(), (BASE + timedelta(seconds=60)).timestamp()),
            ((BASE + timedelta(seconds=60)).timestamp(), until.timestamp()),
        ]
        assert slice_.active_sec == 120
        assert slice_.start == since
        assert slice_.end == until
        assert slice_.boundary_clipped is True

    def test_interval_can_span_a_window_without_an_event_inside(self):
        stat = _stat(-60, 600)
        since, until = _window(0, 240)

        slice_ = session_slice_for_window(stat, since, until)

        assert slice_ is not None
        assert slice_.timestamps == []
        assert slice_.active_sec == 240

    def test_exact_until_event_is_excluded_but_single_inside_event_survives(self):
        stat = _stat(0, 60)
        since, until = _window(0, 60)

        slice_ = session_slice_for_window(stat, since, until)

        assert slice_ is not None
        assert slice_.timestamps == [since.timestamp()]
        assert slice_.active_sec == 60

    def test_empty_slice_returns_none(self):
        stat = _stat(0, 30)
        since, until = _window(60, 120)

        assert session_slice_for_window(stat, since, until) is None

    def test_naive_and_aware_windows_normalize_to_the_same_slice(self):
        stat = _stat(0, 60)
        aware = session_slice_for_window(stat, *_window(0, 120))
        naive = session_slice_for_window(
            stat,
            datetime(2026, 7, 28, 12),
            datetime(2026, 7, 28, 12, 2),
        )

        assert aware is not None
        assert naive is not None
        assert naive.session_id == aware.session_id
        assert naive.evidence_fingerprint == aware.evidence_fingerprint

    def test_rejects_evidence_that_crosses_the_window(self):
        stat = _stat(0, 60)
        since, until = _window(0, 120)
        evidence = make_window_evidence(
            timestamps=[(BASE + timedelta(seconds=120)).timestamp()],
            active_intervals=(),
            msg_count=1,
            user_msg_count=0,
        )

        with pytest.raises(ValueError, match="out-of-window timestamp"):
            session_slice_for_window(stat, since, until, evidence=evidence)

    def test_rejects_evidence_with_nonphysical_intervals(self):
        stat = _stat(-60, 60)
        since, until = _window(0, 120)
        evidence = make_window_evidence(
            timestamps=[(BASE + timedelta(seconds=60)).timestamp()],
            active_intervals=(
                ActiveInterval(
                    (BASE + timedelta(seconds=30)).timestamp(),
                    (BASE + timedelta(seconds=60)).timestamp(),
                ),
            ),
            msg_count=1,
            user_msg_count=0,
        )

        with pytest.raises(ValueError, match="do not match clipped physical intervals"):
            session_slice_for_window(stat, since, until, evidence=evidence)

    def test_default_evidence_does_not_invent_message_counts(self):
        stat = _stat(0, 60)

        slice_ = session_slice_for_window(stat, *_window(0, 120))

        assert slice_ is not None
        assert slice_.msg_count == 0
        assert slice_.user_msg_count == 0


class TestSessionSliceIdentity:
    def test_contained_session_keeps_physical_identity(self):
        stat = _stat(60, 120)

        slice_ = session_slice_for_window(stat, *_window(0, 180))

        assert slice_ is not None
        assert slice_.physical_session_id == "physical-1"
        assert slice_.session_id == "physical-1"
        assert slice_.boundary_clipped is False

    def test_clipped_identity_is_stable_for_same_evidence_and_rotates_for_change(self):
        stat = _stat(-60, 60)
        since, until = _window(0, 120)
        timestamps = [(BASE + timedelta(seconds=60)).timestamp()]
        intervals = (ActiveInterval(since.timestamp(), timestamps[0]),)
        same = make_window_evidence(
            timestamps=timestamps,
            active_intervals=intervals,
            msg_count=1,
            user_msg_count=0,
            excerpt="bounded result",
        )
        changed = make_window_evidence(
            timestamps=timestamps,
            active_intervals=intervals,
            msg_count=1,
            user_msg_count=0,
            excerpt="bounded revised result",
        )

        first = session_slice_for_window(stat, since, until, evidence=same)
        again = session_slice_for_window(stat, since, until, evidence=same)
        rotated = session_slice_for_window(stat, since, until, evidence=changed)

        assert first is not None
        assert again is not None
        assert rotated is not None
        assert first.session_id == again.session_id
        assert first.session_id != rotated.session_id
        assert first.physical_session_id == rotated.physical_session_id

    def test_advancing_until_does_not_rotate_unchanged_clipped_evidence(self):
        stat = _stat(-60, 60)

        earlier = session_slice_for_window(stat, *_window(0, 120))
        later = session_slice_for_window(stat, *_window(0, 180))

        assert earlier is not None
        assert later is not None
        assert earlier.boundary_clipped is True
        assert earlier.evidence_fingerprint == later.evidence_fingerprint
        assert earlier.session_id == later.session_id

    def test_preserves_the_physical_engagement_decision(self):
        stat = _stat(-60, 60)
        # The original physical session is engaged because it has two user turns.
        assert stat.engaged is True

        slice_ = session_slice_for_window(stat, *_window(0, 120))

        assert slice_ is not None
        assert slice_.user_msg_count == 0
        assert slice_.engaged is True


class TestSliceWallClock:
    def test_uses_a_slice_clipped_interval_not_its_remaining_timestamp(self):
        stat = _stat(-60, 60)

        slice_ = session_slice_for_window(stat, *_window(0, 120))

        assert slice_ is not None
        # The slice retains one in-window timestamp, which could not itself
        # infer a duration.  Its explicit [0, 60) interval is the fact.
        assert wall_clock_active_sec([slice_]) == 60

    def test_unions_explicit_clipped_intervals_for_concurrent_slices(self):
        first = _stat(-60, 180, session_id="first")
        second = _stat(-30, 210, session_id="second")
        since, until = _window(0, 120)

        first_slice = session_slice_for_window(first, since, until)
        second_slice = session_slice_for_window(second, since, until)

        assert first_slice is not None
        assert second_slice is not None
        assert wall_clock_active_sec([first_slice, second_slice]) == 120

    def test_clip_helper_rejects_empty_or_reversed_window(self):
        interval = (ActiveInterval(0, 10),)
        with pytest.raises(ValueError, match="until must be after since"):
            clip_active_intervals(
                interval,
                datetime.fromtimestamp(1, tz=UTC),
                datetime.fromtimestamp(1, tz=UTC),
            )
