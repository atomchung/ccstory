"""Tests for ccstory.provenance (#136, slice 1 of 4).

Covers the pure InteractionMode/InteractionProvenance resolver, the
SessionStat/SessionSlice adapter, and the labeled-fixture evaluation
harness. All fixtures here are synthetic; nothing is read from real
transcripts.
"""

from __future__ import annotations

import dataclasses
import random
import re
from datetime import datetime, timedelta, timezone

import pytest

from ccstory.provenance import (
    EvaluationReport,
    InteractionMode,
    InteractionSignals,
    LabeledFixture,
    evaluate_fixtures,
    interaction_signals_from_session,
    resolve_interaction_provenance,
    resolve_session_provenance,
)
from ccstory.time_tracking import (
    SessionStat,
    active_intervals_for_timestamps,
    clip_active_intervals,
    make_window_evidence,
    session_slice_for_window,
)

UTC = timezone.utc
BASE = datetime(2026, 8, 1, tzinfo=UTC)

_SIGNAL_TOKEN_RE = re.compile(r"^[a-z_.]+=[A-Za-z0-9_.+-]+$")


def _stat(
    *,
    session_id: str = "physical-1",
    is_scheduled: bool = False,
    is_delegated: bool = False,
    delegation_source: str = "",
    user_msg_count: int = 0,
    agent: str = "claude",
) -> SessionStat:
    timestamps = [BASE.timestamp(), (BASE + timedelta(seconds=30)).timestamp()]
    return SessionStat(
        project="synthetic-project",
        category="coding",
        session_id=session_id,
        start=BASE,
        end=BASE + timedelta(seconds=30),
        active_sec=30,
        msg_count=2,
        user_msg_count=user_msg_count,
        agent=agent,
        timestamps=timestamps,
        is_scheduled=is_scheduled,
        is_delegated=is_delegated,
        delegation_source=delegation_source,
    )


def _slice_default_evidence(stat: SessionStat):
    since, until = BASE - timedelta(seconds=5), BASE + timedelta(seconds=60)
    produced = session_slice_for_window(stat, since, until)
    assert produced is not None
    return produced


def _slice_with_user_messages(stat: SessionStat, user_msg_count: int):
    """Build a real SessionSlice whose evidence carries an explicit count.

    Mirrors what `session_slice_for_window` does internally for the
    default-evidence path, but supplies `user_msg_count` explicitly — the
    auto-built default evidence always reports 0 (see its docstring) — so
    the resolver can be exercised through the full window-pure evidence
    pipeline, not just the SessionStat shortcut.
    """

    since, until = BASE - timedelta(seconds=5), BASE + timedelta(seconds=60)
    physical = tuple(stat.timestamps)
    bounded = tuple(t for t in physical if since.timestamp() <= t < until.timestamp())
    intervals = clip_active_intervals(
        active_intervals_for_timestamps(physical), since, until
    )
    evidence = make_window_evidence(
        timestamps=bounded,
        active_intervals=intervals,
        msg_count=max(user_msg_count, 2),
        user_msg_count=user_msg_count,
    )
    produced = session_slice_for_window(stat, since, until, evidence=evidence)
    assert produced is not None
    return produced


class TestSignalTokenContract:
    """Guardrails on the InteractionSignals/InteractionProvenance shape itself."""

    def test_signals_has_no_free_text_field(self):
        """The resolver cannot guess from wording if there is nowhere to put it.

        `delegation_source` is the only string field, and it is a closed,
        provider-authored tag (`"claude_code"` / `"codex_delegation"`
        today), never free text. Adding a prompt/title/excerpt-shaped field
        here would silently reopen the "scheduled must not be guessed from
        wording" hazard the issue calls out.
        """
        field_names = {f.name for f in dataclasses.fields(InteractionSignals)}
        assert field_names == {
            "is_scheduled",
            "is_delegated",
            "delegation_source",
            "is_system_review",
            "user_msg_count",
        }


class TestResolverCoreModes:
    def test_missing_metadata_is_unknown_low_confidence(self):
        result = resolve_interaction_provenance(InteractionSignals())
        assert result.mode is InteractionMode.UNKNOWN
        assert result.confidence == "low"
        assert result.signals == ()

    def test_known_but_empty_metadata_is_unknown_medium_confidence(self):
        """All fields concretely known, none point anywhere: still UNKNOWN,
        but distinguishable from the fully-missing case by confidence."""
        result = resolve_interaction_provenance(
            InteractionSignals(
                is_scheduled=False, is_delegated=False, user_msg_count=0
            )
        )
        assert result.mode is InteractionMode.UNKNOWN
        assert result.confidence == "medium"
        assert set(result.signals) == {
            "is_scheduled=false",
            "is_delegated=false",
            "user_msg_count=0",
        }

    def test_direct_multi_turn_is_interactive_high_confidence(self):
        result = resolve_interaction_provenance(InteractionSignals(user_msg_count=3))
        assert result.mode is InteractionMode.INTERACTIVE
        assert result.confidence == "high"
        assert result.signals == ("user_msg_count=3",)

    def test_direct_single_turn_is_interactive_medium_confidence(self):
        result = resolve_interaction_provenance(InteractionSignals(user_msg_count=1))
        assert result.mode is InteractionMode.INTERACTIVE
        assert result.confidence == "medium"
        assert result.signals == ("user_msg_count=1",)

    def test_scheduled_envelope_is_high_confidence_regardless_of_turn_count(self):
        result = resolve_interaction_provenance(
            InteractionSignals(is_scheduled=True, user_msg_count=1)
        )
        assert result.mode is InteractionMode.SCHEDULED
        assert result.confidence == "high"
        # The lone triggering message is the normal shape of a scheduled
        # session and must not itself read as a second, competing signal.
        assert result.signals == ("is_scheduled=true",)

    def test_delegated_with_known_source_is_high_confidence(self):
        result = resolve_interaction_provenance(
            InteractionSignals(
                is_delegated=True, delegation_source="claude_code", user_msg_count=1
            )
        )
        assert result.mode is InteractionMode.DELEGATED
        assert result.confidence == "high"
        assert result.signals == (
            "is_delegated=true",
            "delegation_source=claude_code",
        )

    def test_delegated_without_source_is_medium_confidence(self):
        """Defensive: today's Codex provider never sets is_delegated without
        a source, but the resolver must still degrade instead of crashing
        if some future caller passes an inconsistent signal bundle."""
        result = resolve_interaction_provenance(InteractionSignals(is_delegated=True))
        assert result.mode is InteractionMode.DELEGATED
        assert result.confidence == "medium"
        assert result.signals == ("is_delegated=true",)

    def test_system_review_is_high_confidence(self):
        """No bundled provider sets is_system_review yet (#136 field gap);
        the resolver must still be correct for the day one does."""
        result = resolve_interaction_provenance(
            InteractionSignals(is_system_review=True, user_msg_count=1)
        )
        assert result.mode is InteractionMode.SYSTEM_REVIEW
        assert result.confidence == "high"
        assert result.signals == ("is_system_review=true",)


class TestResolverConflicts:
    def test_two_structural_markers_conflict_at_medium_confidence(self):
        """Not reachable in today's real data (Claude is the only source of
        is_scheduled, Codex the only source of is_delegated, so the two
        never co-occur yet) — this locks in forward-compatible behavior."""
        result = resolve_interaction_provenance(
            InteractionSignals(
                is_scheduled=True,
                is_delegated=True,
                delegation_source="codex_delegation",
            )
        )
        assert result.mode is InteractionMode.SCHEDULED
        assert result.confidence == "medium"
        assert "conflict=scheduled+delegated" in result.signals

    def test_scheduled_session_resumed_interactively_is_mixed_conflict(self):
        """A scheduled task where a human then carried on a real
        back-and-forth — genuinely ambiguous, so confidence drops."""
        result = resolve_interaction_provenance(
            InteractionSignals(is_scheduled=True, user_msg_count=5)
        )
        assert result.mode is InteractionMode.SCHEDULED
        assert result.confidence == "medium"
        assert result.signals == (
            "is_scheduled=true",
            "user_msg_count=5",
            "conflict=scheduled+interactive",
        )

    def test_delegated_session_resumed_interactively_is_mixed_conflict(self):
        result = resolve_interaction_provenance(
            InteractionSignals(
                is_delegated=True, delegation_source="claude_code", user_msg_count=4
            )
        )
        assert result.mode is InteractionMode.DELEGATED
        assert result.confidence == "medium"
        assert "conflict=delegated+interactive" in result.signals

    def test_triple_conflict_is_low_confidence(self):
        result = resolve_interaction_provenance(
            InteractionSignals(
                is_system_review=True,
                is_scheduled=True,
                is_delegated=True,
                delegation_source="claude_code",
                user_msg_count=6,
            )
        )
        assert result.mode is InteractionMode.SYSTEM_REVIEW
        assert result.confidence == "low"
        assert (
            "conflict=system_review+scheduled+delegated+interactive"
            in result.signals
        )


class TestResolverDeterminism:
    def test_same_signals_always_resolve_equal(self):
        signals = InteractionSignals(is_scheduled=True, user_msg_count=5)
        first = resolve_interaction_provenance(signals)
        second = resolve_interaction_provenance(signals)
        assert first == second

    def test_field_construction_order_cannot_matter(self):
        """InteractionSignals is a fixed-shape dataclass: kwargs in a
        different order describe the same value, by construction."""
        a = InteractionSignals(
            is_scheduled=True, is_delegated=False, user_msg_count=2
        )
        b = InteractionSignals(
            user_msg_count=2, is_delegated=False, is_scheduled=True
        )
        assert a == b
        assert resolve_interaction_provenance(a) == resolve_interaction_provenance(b)

    def test_signal_tokens_are_metadata_pairs_never_free_text(self):
        cases = [
            InteractionSignals(),
            InteractionSignals(user_msg_count=1),
            InteractionSignals(user_msg_count=7),
            InteractionSignals(is_scheduled=True),
            InteractionSignals(is_delegated=True, delegation_source="claude_code"),
            InteractionSignals(is_system_review=True),
            InteractionSignals(is_scheduled=True, is_delegated=True,
                                delegation_source="codex_delegation"),
        ]
        for signals in cases:
            result = resolve_interaction_provenance(signals)
            for token in result.signals:
                if token.startswith("conflict="):
                    continue
                assert _SIGNAL_TOKEN_RE.match(token), token


class TestSessionAdapter:
    def test_adapter_reads_only_the_documented_fields(self):
        stat = _stat(is_scheduled=True, user_msg_count=1)
        signals = interaction_signals_from_session(stat)
        assert signals == InteractionSignals(
            is_scheduled=True,
            is_delegated=False,
            delegation_source="",
            is_system_review=None,
            user_msg_count=1,
        )

    def test_resolve_session_provenance_matches_manual_resolution(self):
        stat = _stat(
            is_delegated=True, delegation_source="claude_code", user_msg_count=1
        )
        assert resolve_session_provenance(stat) == resolve_interaction_provenance(
            interaction_signals_from_session(stat)
        )
        assert resolve_session_provenance(stat).mode is InteractionMode.DELEGATED

    def test_session_stat_defaults_are_concrete_not_missing(self):
        """SessionStat.is_scheduled/is_delegated default to False, not None
        — a session a parser never inspected and one confirmed negative are
        indistinguishable at this layer. Documented gap, not a resolver bug."""
        stat = _stat()
        signals = interaction_signals_from_session(stat)
        assert signals.is_scheduled is False
        assert signals.is_delegated is False
        assert resolve_session_provenance(stat).mode is InteractionMode.UNKNOWN
        assert resolve_session_provenance(stat).confidence == "medium"

    def test_session_slice_carries_scheduled_and_delegation_through(self):
        stat = _stat(is_scheduled=True, session_id="scheduled-1")
        slice_ = _slice_default_evidence(stat)
        assert slice_.is_scheduled is True
        assert resolve_session_provenance(slice_).mode is InteractionMode.SCHEDULED
        assert resolve_session_provenance(slice_) == resolve_session_provenance(stat)

    def test_session_slice_with_explicit_evidence_carries_user_msg_count(self):
        stat = _stat(session_id="interactive-1")
        slice_ = _slice_with_user_messages(stat, user_msg_count=3)
        assert slice_.user_msg_count == 3
        result = resolve_session_provenance(slice_)
        assert result.mode is InteractionMode.INTERACTIVE
        assert result.confidence == "high"

    def test_system_review_has_no_session_source_yet(self):
        """Field-coverage gap (#136): no provider sets this, so the live
        adapter can never produce SYSTEM_REVIEW from a real session today."""
        stat = _stat(user_msg_count=1)
        assert interaction_signals_from_session(stat).is_system_review is None


# ---------------------------------------------------------------------------
# Labeled-fixture evaluation harness
#
# One fixture per case the #136 brief calls out for coverage: direct,
# delegated, guardian (system_review), scheduled, mixed/resumed, missing
# metadata, and conflicting signals. All synthetic; none derived from real
# transcripts.
# ---------------------------------------------------------------------------

CANONICAL_FIXTURES: tuple[LabeledFixture, ...] = (
    LabeledFixture(
        name="direct_multi_turn",
        signals=InteractionSignals(user_msg_count=4),
        expected_mode=InteractionMode.INTERACTIVE,
        category="direct",
    ),
    LabeledFixture(
        name="direct_single_turn",
        signals=InteractionSignals(user_msg_count=1),
        expected_mode=InteractionMode.INTERACTIVE,
        category="direct",
    ),
    LabeledFixture(
        name="delegated_claude_code",
        signals=InteractionSignals(
            is_delegated=True, delegation_source="claude_code", user_msg_count=1
        ),
        expected_mode=InteractionMode.DELEGATED,
        category="delegated",
    ),
    LabeledFixture(
        name="delegated_codex_task",
        signals=InteractionSignals(
            is_delegated=True, delegation_source="codex_delegation"
        ),
        expected_mode=InteractionMode.DELEGATED,
        category="delegated",
    ),
    LabeledFixture(
        name="guardian_auto_review",
        signals=InteractionSignals(is_system_review=True, user_msg_count=1),
        expected_mode=InteractionMode.SYSTEM_REVIEW,
        category="guardian",
    ),
    LabeledFixture(
        name="scheduled_single_prompt",
        signals=InteractionSignals(is_scheduled=True, user_msg_count=1),
        expected_mode=InteractionMode.SCHEDULED,
        category="scheduled",
    ),
    LabeledFixture(
        name="scheduled_zero_messages",
        signals=InteractionSignals(is_scheduled=True, user_msg_count=0),
        expected_mode=InteractionMode.SCHEDULED,
        category="scheduled",
    ),
    LabeledFixture(
        name="mixed_resumed_scheduled_then_interactive",
        signals=InteractionSignals(is_scheduled=True, user_msg_count=6),
        expected_mode=InteractionMode.SCHEDULED,
        category="mixed_resumed",
    ),
    LabeledFixture(
        name="mixed_resumed_delegated_then_interactive",
        signals=InteractionSignals(
            is_delegated=True, delegation_source="claude_code", user_msg_count=3
        ),
        expected_mode=InteractionMode.DELEGATED,
        category="mixed_resumed",
    ),
    LabeledFixture(
        name="missing_metadata_entirely",
        signals=InteractionSignals(),
        expected_mode=InteractionMode.UNKNOWN,
        category="missing_metadata",
    ),
    LabeledFixture(
        name="missing_metadata_known_empty",
        signals=InteractionSignals(
            is_scheduled=False, is_delegated=False, user_msg_count=0
        ),
        expected_mode=InteractionMode.UNKNOWN,
        category="missing_metadata",
    ),
    LabeledFixture(
        name="conflicting_scheduled_and_delegated",
        signals=InteractionSignals(
            is_scheduled=True,
            is_delegated=True,
            delegation_source="codex_delegation",
        ),
        expected_mode=InteractionMode.SCHEDULED,
        category="conflicting_signals",
    ),
    LabeledFixture(
        name="conflicting_triple_marker",
        signals=InteractionSignals(
            is_system_review=True,
            is_scheduled=True,
            is_delegated=True,
            delegation_source="claude_code",
            user_msg_count=2,
        ),
        expected_mode=InteractionMode.SYSTEM_REVIEW,
        category="conflicting_signals",
    ),
)


class TestEvaluationHarness:
    def test_canonical_fixtures_all_resolve_to_their_expected_mode(self):
        report = evaluate_fixtures(CANONICAL_FIXTURES)
        mismatches = [r for r in report.results if not r.correct]
        assert mismatches == []
        assert report.accuracy == 1.0
        assert report.fixture_count == len(CANONICAL_FIXTURES)
        assert report.correct_count == len(CANONICAL_FIXTURES)

    def test_confusion_matrix_has_no_off_diagonal_entries(self):
        report = evaluate_fixtures(CANONICAL_FIXTURES)
        off_diagonal = [
            cell
            for cell in report.confusion
            if cell.expected_mode != cell.predicted_mode
        ]
        assert off_diagonal == []

    def test_per_mode_precision_and_recall_are_perfect_on_the_canonical_set(self):
        report = evaluate_fixtures(CANONICAL_FIXTURES)
        exercised = {m.mode: m for m in report.per_mode if m.support}
        # Every mode this fixture set actually labels should score perfectly;
        # that is what makes the canonical set usable as a regression lock.
        assert exercised.keys() == {
            InteractionMode.INTERACTIVE,
            InteractionMode.DELEGATED,
            InteractionMode.SYSTEM_REVIEW,
            InteractionMode.SCHEDULED,
            InteractionMode.UNKNOWN,
        }
        for metrics in exercised.values():
            assert metrics.precision == 1.0
            assert metrics.recall == 1.0

    def test_no_double_counting_across_confusion_and_per_mode(self):
        report = evaluate_fixtures(CANONICAL_FIXTURES)
        total = len(CANONICAL_FIXTURES)
        assert sum(cell.count for cell in report.confusion) == total
        assert sum(m.support for m in report.per_mode) == total
        assert sum(m.predicted for m in report.per_mode) == total
        # Every fixture name appears in results exactly once.
        names = [r.fixture for r in report.results]
        assert len(names) == len(set(names)) == total

    def test_permutation_of_fixtures_does_not_change_the_report(self):
        shuffled = list(CANONICAL_FIXTURES)
        random.Random(1234).shuffle(shuffled)
        assert shuffled != list(CANONICAL_FIXTURES)  # sanity: order really changed

        baseline = evaluate_fixtures(CANONICAL_FIXTURES)
        permuted = evaluate_fixtures(shuffled)
        assert permuted == baseline

    def test_every_required_brief_category_is_covered(self):
        categories = {fixture.category for fixture in CANONICAL_FIXTURES}
        assert categories == {
            "direct",
            "delegated",
            "guardian",
            "scheduled",
            "mixed_resumed",
            "missing_metadata",
            "conflicting_signals",
        }

    def test_empty_fixture_list_is_rejected(self):
        with pytest.raises(ValueError, match="at least one fixture"):
            evaluate_fixtures(())

    def test_duplicate_fixture_names_are_rejected(self):
        duplicate = (
            CANONICAL_FIXTURES[0],
            dataclasses.replace(CANONICAL_FIXTURES[1], name=CANONICAL_FIXTURES[0].name),
        )
        with pytest.raises(ValueError, match="duplicate fixture names"):
            evaluate_fixtures(duplicate)

    def test_report_is_a_plain_dataclass_assertable_structure(self):
        report = evaluate_fixtures(CANONICAL_FIXTURES)
        assert isinstance(report, EvaluationReport)
        # Every field a caller would assert on is a plain, hashable-friendly
        # value (tuples of frozen dataclasses / enums / primitives) — no
        # mutable containers or objects requiring custom comparison.
        assert isinstance(report.confusion, tuple)
        assert isinstance(report.per_mode, tuple)
        assert isinstance(report.results, tuple)
