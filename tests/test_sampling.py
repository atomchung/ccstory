"""Tests for the deterministic sketch/plan sampling module (#188, 0.8-E).

Two invariants are load-bearing and locked here:

* ``build_sampling_plan`` never looks at the order its input arrived in —
  the same population always produces the same plan, field for field, no
  matter how it was shuffled, and this file drives that with real
  permutations rather than trusting the implementation's sort calls;
* neither ``SessionSketch`` nor ``SamplingPlan`` ever retains raw transcript
  text — ``salience`` and ``evidence_chars`` are derived from bounded
  evidence, but the evidence itself never survives into either dataclass.
"""

from __future__ import annotations

import os
import random
import time as time_mod
from datetime import date, datetime, timedelta, timezone

import pytest

from ccstory import sampling
from ccstory.sampling import (
    COVERAGE_DIMENSIONS,
    REASON_BUDGET_FILL,
    REASON_DAY_TOP,
    REASON_PROJECT_TOP,
    REASON_PROVIDER_TOP,
    REASON_VOCABULARY,
    SALIENCE_CODES,
    SamplingPlan,
    SessionSketch,
    build_sampling_plan,
    build_session_sketch,
)
from ccstory.session_summarizer import SessionSummary
from ccstory.time_tracking import (
    SessionStat,
    active_intervals_for_timestamps,
    clip_active_intervals,
    make_window_evidence,
    session_slice_for_window,
)

UTC = timezone.utc
BASE = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
PHYSICAL_ID = "physical-session-1"


def _at(
    day_offset: int = 0, hour: int = 12, minute: int = 0, second: int = 0,
) -> datetime:
    return BASE + timedelta(
        days=day_offset, hours=hour, minutes=minute, seconds=second,
    )


# --------------------------------------------------------------------------
# Helpers to build SessionStat / SessionSlice objects directly, mirroring
# the pattern already proven out in tests/test_session_identity.py.
# --------------------------------------------------------------------------


def _stat(
    *offsets: int,
    session_id: str = PHYSICAL_ID,
    project: str = "-Users-t-myapp",
    category: str = "coding",
    agent: str = "claude",
    first_user_text: str = "fix the login bug",
) -> SessionStat:
    timestamps = [(BASE + timedelta(seconds=offset)).timestamp() for offset in offsets]
    return SessionStat(
        project=project,
        category=category,
        session_id=session_id,
        start=datetime.fromtimestamp(min(timestamps), tz=UTC),
        end=datetime.fromtimestamp(max(timestamps), tz=UTC),
        active_sec=int(max(timestamps) - min(timestamps)),
        msg_count=len(offsets),
        user_msg_count=2,
        first_user_text=first_user_text,
        agent=agent,
        timestamps=timestamps,
    )


def _bounded_evidence(
    stat: SessionStat,
    since: datetime,
    until: datetime,
    *,
    first_user_text: str = "in-window opener",
    latest_user_text: str = "in-window opener",
    final_assistant_text: str = "in-window answer",
    excerpt: str = "in-window opener\nin-window answer",
):
    bounded = [
        ts for ts in stat.timestamps if since.timestamp() <= ts < until.timestamp()
    ]
    return make_window_evidence(
        timestamps=bounded,
        active_intervals=clip_active_intervals(
            active_intervals_for_timestamps(stat.timestamps), since, until,
        ),
        msg_count=len(bounded),
        user_msg_count=1,
        first_user_text=first_user_text,
        latest_user_text=latest_user_text,
        final_assistant_text=final_assistant_text,
        excerpt=excerpt,
    )


def _clipped_slice(*, excerpt: str = "in-window opener\nin-window answer", **kwargs):
    """A slice of a session that starts before the window and ends after it."""

    stat = _stat(-3600, 0, 3600, **kwargs)
    evidence = _bounded_evidence(stat, _at(hour=0), _at(hour=1), excerpt=excerpt)
    slice_ = session_slice_for_window(stat, _at(hour=0), _at(hour=1), evidence=evidence)
    assert slice_ is not None
    assert slice_.boundary_clipped is True
    assert slice_.session_id != slice_.physical_session_id
    return slice_


def _summary(text: str, source: str = "generated") -> SessionSummary:
    return SessionSummary(
        session_id="irrelevant-for-these-tests", summary=text, source=source,
    )


def _sketch(
    *,
    internal_id: str,
    physical_id: str | None = None,
    provider: str = "claude",
    project: str = "proj-a",
    category: str = "coding",
    day_offset: int = 0,
    active_sec: int = 100,
    msg_count: int = 2,
    evidence_chars: int = 10,
    summary_source: str | None = None,
    salience: tuple[str, ...] = (),
) -> SessionSketch:
    start = _at(day_offset)
    return SessionSketch(
        internal_id=internal_id,
        physical_id=physical_id or internal_id,
        provider=provider,
        project=project,
        category=category,
        local_day=start.astimezone().date(),
        start=start,
        active_sec=active_sec,
        msg_count=msg_count,
        evidence_chars=evidence_chars,
        summary_source=summary_source,
        salience=salience,
    )


def _assert_stable_across_permutations(
    sketches: list[SessionSketch], *, seeds: int = 100, **plan_kwargs,
) -> SamplingPlan:
    """Rebuild the plan under many deterministic shuffles and require equality.

    ``random.Random(seed).shuffle`` (not the global RNG) keeps this
    reproducible across runs and machines without any real randomness.
    Comparing the whole dataclass covers every field in one assertion; the
    explicit per-field re-checks below exist so a future change to
    ``SamplingPlan``'s generated ``__eq__`` (e.g. adding a field it forgets
    to compare) cannot silently narrow what this test actually caught.
    """

    baseline = build_sampling_plan(sketches, **plan_kwargs)
    for seed in range(seeds):
        shuffled = list(sketches)
        random.Random(seed).shuffle(shuffled)
        plan = build_sampling_plan(shuffled, **plan_kwargs)
        assert plan == baseline, f"plan differs from baseline at seed={seed}"
        assert plan.ordered_ids == baseline.ordered_ids
        assert plan.selected_ids == baseline.selected_ids
        assert plan.reasons == baseline.reasons
        assert plan.coverage_hits == baseline.coverage_hits
    return baseline


class TestBuildSessionSketch:
    """Behavior of the SessionStat/SessionSlice -> SessionSketch projection."""

    def test_plain_session_stat_has_one_identity(self):
        stat = _stat(0, 60)

        sketch = build_session_sketch(stat)

        assert sketch.internal_id == PHYSICAL_ID
        assert sketch.physical_id == PHYSICAL_ID
        assert sketch.provider == "claude"
        assert sketch.project == stat.project
        assert sketch.category == "coding"
        assert sketch.start == stat.start
        assert sketch.active_sec == stat.active_sec
        assert sketch.msg_count == stat.msg_count
        assert sketch.summary_source is None

    def test_clipped_slice_separates_physical_and_internal_id(self):
        slice_ = _clipped_slice()

        sketch = build_session_sketch(slice_)

        assert sketch.physical_id == PHYSICAL_ID
        assert sketch.internal_id == slice_.session_id
        assert sketch.internal_id.startswith("slice-")
        assert sketch.internal_id != sketch.physical_id

    def test_evidence_chars_come_from_the_slice_bounded_excerpt(self):
        slice_ = _clipped_slice(excerpt="short bounded excerpt")

        sketch = build_session_sketch(slice_)

        assert sketch.evidence_chars == len("short bounded excerpt")

    def test_evidence_chars_come_from_first_user_text_for_a_plain_session(self):
        stat = _stat(0, 60, first_user_text="a plain opening message")

        sketch = build_session_sketch(stat)

        assert sketch.evidence_chars == len("a plain opening message")

    def test_no_summary_and_no_text_yields_zero_chars_and_no_salience(self):
        stat = _stat(0, 60, first_user_text="")

        sketch = build_session_sketch(stat)

        assert sketch.evidence_chars == 0
        assert sketch.salience == ()

    def test_trusted_summary_overrides_bounded_evidence_for_size_and_salience(self):
        stat = _stat(0, 60, first_user_text="short opener, nothing notable here")
        summary_text = (
            "a much longer trusted summary describing how the release "
            "was shipped and merged today"
        )
        summary = _summary(summary_text, source="generated")

        sketch = build_session_sketch(stat, summary=summary)

        assert sketch.evidence_chars == len(summary_text)
        assert "outcome" in sketch.salience  # "shipped"/"merged" from the summary
        assert sketch.summary_source == "generated"

    def test_summary_source_is_none_without_a_summary(self):
        stat = _stat(0, 60)

        sketch = build_session_sketch(stat)

        assert sketch.summary_source is None

    def test_summary_source_reflects_the_given_summary(self):
        stat = _stat(0, 60)

        sketch = build_session_sketch(
            stat, summary=_summary("human wrote this", source="provided"),
        )

        assert sketch.summary_source == "provided"

    def test_no_raw_evidence_text_survives_into_the_sketch(self):
        marker = "MARKER_PRIVATE_TEXT_9182"
        stat = _stat(0, 60, first_user_text=f"opening message {marker} more text")

        sketch = build_session_sketch(stat)

        assert marker not in repr(sketch)


@pytest.mark.skipif(not hasattr(time_mod, "tzset"), reason="tzset is POSIX-only")
class TestLocalDayUsesHostTimezone:
    """Locks the #22 rule (see tests/test_timezone.py) for per-session sketches.

    ``_local_day`` must attribute a session to the day the user experienced,
    not the UTC day a transcript happens to store. ``time.tzset()`` lets the
    test force a specific, non-UTC, no-DST host timezone (Kiritimati, a
    stable UTC+14 with no daylight-saving transitions) so the assertion does
    not depend on whatever timezone happens to run the test suite.
    """

    def test_local_day_crosses_over_relative_to_the_utc_day(self):
        original = os.environ.get("TZ")
        os.environ["TZ"] = "Pacific/Kiritimati"  # UTC+14, no DST
        time_mod.tzset()
        try:
            # 2026-05-10 12:00 UTC == 2026-05-11 02:00 local (UTC+14): a
            # different calendar day locally than in UTC.
            start = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
            stat = SessionStat(
                project="proj", category="coding", session_id="s1",
                start=start, end=start + timedelta(minutes=1),
                active_sec=60, msg_count=1, timestamps=[start.timestamp()],
            )

            sketch = build_session_sketch(stat)

            assert sketch.local_day == date(2026, 5, 11)
            assert sketch.local_day != start.date()
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time_mod.tzset()


class TestSalienceDetection:
    """Each fixed salience code fires on a representative bounded phrase."""

    @pytest.mark.parametrize(
        ("phrase", "expected_code"),
        [
            ("hit an unexpected exception during startup", "error"),
            ("added new pytest coverage for the parser", "test"),
            (
                "rotate the leaked credential before it becomes a vulnerability",
                "security",
            ),
            ("attached a screenshot of the exported report", "artifact"),
            ("fixed the bug and shipped the release, PR merged", "outcome"),
        ],
    )
    def test_representative_phrase_triggers_its_code(self, phrase, expected_code):
        stat = _stat(0, 60, first_user_text=phrase)

        sketch = build_session_sketch(stat)

        assert expected_code in sketch.salience

    def test_neutral_text_triggers_no_salience(self):
        stat = _stat(
            0, 60,
            first_user_text="let's plan the roadmap for next quarter and review budget",
        )

        sketch = build_session_sketch(stat)

        assert sketch.salience == ()

    def test_salience_order_is_always_the_fixed_priority_order(self):
        # A single message hitting every category at once must still report
        # them in SALIENCE_CODES order, not text-appearance order.
        phrase = (
            "outcome: shipped and merged. also: bug, exception, pytest coverage, "
            "vulnerability, credential leak, screenshot artifact, exported report"
        )
        stat = _stat(0, 60, first_user_text=phrase)

        sketch = build_session_sketch(stat)

        assert sketch.salience == SALIENCE_CODES


def _make_diverse_population() -> list[SessionSketch]:
    """14 sketches spanning 3 providers, 4 projects, 5 local days, all 5
    salience codes, and a couple of zero-activity sessions — enough overlap
    that every coverage stage in the policy has real work to do.
    """

    # (internal_id, provider, project, day_offset, active_sec, salience)
    rows: list[tuple[str, str, str, int, int, tuple[str, ...]]] = [
        ("s01", "claude", "alpha", 0, 900, ("error",)),
        ("s02", "claude", "alpha", 1, 300, ()),
        ("s03", "claude", "beta", 0, 200, ()),
        ("s04", "claude", "gamma", 2, 150, ("test",)),
        ("s05", "codex", "alpha", 0, 700, ()),
        ("s06", "codex", "beta", 3, 500, ("security",)),
        ("s07", "codex", "delta", 1, 100, ()),
        ("s08", "antigravity", "gamma", 4, 600, ("artifact",)),
        ("s09", "antigravity", "delta", 2, 250, ()),
        ("s10", "antigravity", "alpha", 3, 50, ("outcome",)),
        ("s11", "claude", "delta", 4, 0, ()),
        ("s12", "codex", "gamma", 0, 0, ()),
        ("s13", "antigravity", "beta", 1, 400, ()),
        ("s14", "claude", "beta", 2, 800, ()),
    ]
    return [
        _sketch(
            internal_id=sid,
            provider=provider,
            project=project,
            day_offset=day_offset,
            active_sec=active_sec,
            evidence_chars=20 + (active_sec % 7) * 3,
            salience=salience,
        )
        for sid, provider, project, day_offset, active_sec, salience in rows
    ]


class TestSamplingPlanDeterminism:
    def test_plan_is_stable_across_100_permutations(self):
        population = _make_diverse_population()

        baseline = _assert_stable_across_permutations(
            population, seeds=100, char_budget=120, max_selected=8,
        )

        assert baseline.population_size == 14
        assert len(baseline.selected_ids) <= 8
        assert baseline.unselected_count == (
            baseline.population_size - len(baseline.selected_ids)
        )

    def test_plan_is_stable_across_100_permutations_without_a_budget(self):
        population = _make_diverse_population()

        baseline = _assert_stable_across_permutations(population, seeds=100)

        # With no char_budget and no max_selected, the fill step (#188 step
        # 6) eventually sweeps in every candidate.
        assert baseline.population_size == 14
        assert len(baseline.selected_ids) == 14
        assert baseline.unselected_count == 0


class TestBudgetSmallerThanCoverageTargets:
    def test_coverage_hits_fall_short_of_targets_when_capacity_is_tight(self):
        population = [
            _sketch(
                internal_id="p1", provider="claude", project="alpha",
                day_offset=0, active_sec=300,
            ),
            _sketch(
                internal_id="p2", provider="codex", project="beta",
                day_offset=1, active_sec=200,
            ),
            _sketch(
                internal_id="p3", provider="antigravity", project="gamma",
                day_offset=2, active_sec=100,
            ),
        ]

        baseline = _assert_stable_across_permutations(
            population, seeds=100, max_selected=1,
        )

        assert baseline.coverage_targets == {"provider": 3, "project": 3, "day": 3}
        assert baseline.coverage_hits == {"provider": 1, "project": 1, "day": 1}
        for dimension in COVERAGE_DIMENSIONS:
            assert (
                baseline.coverage_hits[dimension] < baseline.coverage_targets[dimension]
            )
        # The highest-active candidate is the one kept.
        assert baseline.selected_ids == ("p1",)
        assert set(baseline.reasons["p1"]) == {REASON_PROVIDER_TOP, REASON_PROJECT_TOP}
        assert "p2" not in baseline.reasons
        assert "p3" not in baseline.reasons


class TestDuplicateCoverage:
    """Two sessions sharing a provider, project, and local day."""

    def test_only_the_top_candidate_gets_coverage_reasons(self):
        population = [
            _sketch(
                internal_id="dup-a", provider="claude", project="proj-x",
                day_offset=0, active_sec=500,
            ),
            _sketch(
                internal_id="dup-b", provider="claude", project="proj-x",
                day_offset=0, active_sec=200,
            ),
        ]

        baseline = _assert_stable_across_permutations(population, seeds=100)

        # No budget/cap constraint, so both eventually get selected — but
        # only the genuinely highest-active one earns the coverage reasons.
        assert set(baseline.selected_ids) == {"dup-a", "dup-b"}
        assert set(baseline.reasons["dup-a"]) == {
            REASON_PROVIDER_TOP, REASON_PROJECT_TOP,
        }
        assert baseline.reasons["dup-b"] == (REASON_BUDGET_FILL,)
        # The day step never fires at all: the day is already covered by
        # dup-a by the time step 4 runs.
        assert REASON_DAY_TOP not in {
            code for codes in baseline.reasons.values() for code in codes
        }
        assert baseline.coverage_targets == {"provider": 1, "project": 1, "day": 1}
        assert baseline.coverage_hits == {"provider": 1, "project": 1, "day": 1}


class TestCharBudgetSkipAndContinue:
    """#188 step 8: an over-budget candidate is skipped, not truncated, and
    never stops the scan from reaching later, smaller candidates."""

    def test_an_oversized_candidate_is_skipped_while_smaller_ones_are_kept(self):
        population = [
            _sketch(
                internal_id="big", provider="p-big", project="proj-1",
                day_offset=0, active_sec=1000, evidence_chars=500,
            ),
            _sketch(
                internal_id="small1", provider="p-small1", project="proj-2",
                day_offset=1, active_sec=100, evidence_chars=5,
            ),
            _sketch(
                internal_id="small2", provider="p-small2", project="proj-3",
                day_offset=2, active_sec=50, evidence_chars=5,
            ),
        ]

        baseline = _assert_stable_across_permutations(
            population, seeds=100, char_budget=10,
        )

        assert "big" not in baseline.selected_ids
        assert "big" not in baseline.reasons  # never selected -> no dangling reason
        assert set(baseline.selected_ids) == {"small1", "small2"}
        assert baseline.unselected_count == 1
        # Order is preserved: "big" ranked first by activity but was skipped;
        # the scan continued rather than stopping.
        assert baseline.ordered_ids[0] == "big"

    def test_evidence_text_is_never_truncated_only_whole_items_are_skipped(self):
        """A candidate either fits whole or is left out — there is no partial
        inclusion, which would imply silently truncating its evidence."""

        population = [
            _sketch(
                internal_id="fits", provider="p1", project="proj-1",
                active_sec=10, evidence_chars=10,
            ),
            _sketch(
                internal_id="too-big", provider="p2", project="proj-2",
                active_sec=20, evidence_chars=11,
            ),
        ]

        plan = build_sampling_plan(population, char_budget=10)

        assert "fits" in plan.selected_ids
        assert "too-big" not in plan.selected_ids


class TestUnicodeProjectNames:
    """Unicode-form differences in project names must not break ordering."""

    # Built with explicit \uXXXX escapes rather than typed glyphs: two
    # source-file characters that look identical could easily encode to
    # different codepoint sequences (or the same one by accident), which
    # would silently defeat this test's "these are genuinely different
    # strings" premise. Spelling them out removes that ambiguity.
    PRECOMPOSED = "caf\u00e9"  # e-acute as one precomposed codepoint
    DECOMPOSED = "cafe\u0301"  # plain e + a combining acute accent
    CYRILLIC = "\u043f\u0440\u043e\u0435\u043a\u0442"  # Russian for "project"

    def test_distinct_unicode_forms_are_distinct_but_ordering_is_stable(self):
        assert self.PRECOMPOSED != self.DECOMPOSED  # sanity: truly different strings

        population = [
            _sketch(
                internal_id="u1", provider="claude", project=self.PRECOMPOSED,
                active_sec=300, day_offset=0,
            ),
            _sketch(
                internal_id="u2", provider="claude", project=self.DECOMPOSED,
                active_sec=200, day_offset=1,
            ),
            _sketch(
                internal_id="u3", provider="claude", project=self.CYRILLIC,
                active_sec=100, day_offset=2,
            ),
        ]

        baseline = _assert_stable_across_permutations(population, seeds=100)

        # Every distinct raw project string is its own coverage target —
        # sampling does not fold Unicode variants into one group; see
        # `_normalized_project_key`'s docstring for why that grouping
        # decision is left to categorizer.alias_fold instead.
        assert baseline.coverage_targets["project"] == 3
        assert baseline.coverage_hits["project"] == 3
        assert set(baseline.selected_ids) == {"u1", "u2", "u3"}


class TestAllZeroActivity:
    def test_no_crash_and_full_determinism_when_every_session_is_idle(self):
        population = [
            _sketch(
                internal_id="z1", provider="claude", project="alpha",
                active_sec=0, day_offset=0,
            ),
            _sketch(
                internal_id="z2", provider="claude", project="beta",
                active_sec=0, day_offset=1,
            ),
            _sketch(
                internal_id="z3", provider="codex", project="alpha",
                active_sec=0, day_offset=2,
            ),
            _sketch(
                internal_id="z4", provider="antigravity", project="gamma",
                active_sec=0, day_offset=3,
            ),
            _sketch(
                internal_id="z5", provider="antigravity", project="gamma",
                active_sec=0, day_offset=3,
            ),
        ]

        baseline = _assert_stable_across_permutations(
            population, seeds=100, char_budget=15, max_selected=3,
        )

        assert baseline.population_size == 5
        assert len(baseline.selected_ids) <= 3


class TestNoLeakedContent:
    def test_reasons_use_only_the_fixed_vocabulary(self):
        population = _make_diverse_population()

        plan = build_sampling_plan(population, char_budget=100, max_selected=6)

        seen_codes = {code for codes in plan.reasons.values() for code in codes}
        assert seen_codes  # the scenario exercises at least one reason
        assert seen_codes <= REASON_VOCABULARY

    def test_coverage_dimensions_are_exactly_the_fixed_set(self):
        population = _make_diverse_population()

        plan = build_sampling_plan(population, char_budget=100, max_selected=6)

        assert set(plan.coverage_targets) == set(COVERAGE_DIMENSIONS)
        assert set(plan.coverage_hits) == set(COVERAGE_DIMENSIONS)

    def test_no_raw_summary_or_project_substring_appears_in_the_plan(self):
        marker = "MARKER_SECRET_EVIDENCE_TEXT_ZZ"
        secret_project = "ultra-private-project-name-should-not-leak"
        stat = _stat(0, 60, project=secret_project, first_user_text="benign opener")
        sketch = build_session_sketch(
            stat, summary=_summary(f"summary containing {marker} inline"),
        )

        plan = build_sampling_plan([sketch])

        rendered = repr(plan)
        assert marker not in rendered
        assert secret_project not in rendered
        # The only "project" content the plan is allowed to carry is the
        # fixed dimension label itself, not any actual project name.
        assert "project" in plan.coverage_targets
        assert "project" in plan.coverage_hits


class TestPlanInvariants:
    def test_selected_ids_is_an_order_preserving_subsequence_of_ordered_ids(self):
        population = _make_diverse_population()

        plan = build_sampling_plan(population, char_budget=90, max_selected=7)

        filtered = tuple(i for i in plan.ordered_ids if i in plan.selected_ids)
        assert filtered == plan.selected_ids

    def test_population_and_unselected_bookkeeping_is_consistent(self):
        population = _make_diverse_population()

        plan = build_sampling_plan(population, char_budget=90, max_selected=7)

        assert plan.population_size == len(population)
        assert plan.population_size == len(plan.ordered_ids)
        assert plan.unselected_count == plan.population_size - len(plan.selected_ids)

    def test_policy_version_defaults_to_the_module_constant(self):
        plan = build_sampling_plan(_make_diverse_population())

        assert plan.policy_version == sampling.POLICY_VERSION

    def test_empty_population_produces_an_empty_but_well_formed_plan(self):
        plan = build_sampling_plan([])

        assert plan.population_size == 0
        assert plan.ordered_ids == ()
        assert plan.selected_ids == ()
        assert plan.reasons == {}
        assert plan.coverage_targets == {"provider": 0, "project": 0, "day": 0}
        assert plan.coverage_hits == {"provider": 0, "project": 0, "day": 0}
        assert plan.unselected_count == 0

    def test_duplicate_internal_id_in_input_is_canonicalized_not_double_counted(self):
        a = _sketch(
            internal_id="same-id", provider="claude", project="alpha", active_sec=100,
        )
        # A second, differently-scored sketch claiming the same internal_id —
        # e.g. two callers disagreeing about one session. Canonicalization
        # must pick one deterministically rather than count it twice.
        b = _sketch(
            internal_id="same-id", provider="claude", project="alpha", active_sec=999,
        )

        plan = build_sampling_plan([a, b])

        assert plan.population_size == 1
        assert plan.ordered_ids == ("same-id",)
