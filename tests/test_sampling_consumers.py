"""The narrative lanes consume an explicit SamplingPlan (#188, 0.8-F).

`tests/test_sampling.py` proves the plan itself is deterministic. These tests
prove the four places that used to select sessions *implicitly* now consume
that plan instead:

* per-session backfill ordered by provider and filesystem encounter order;
* overall and category synthesis truncating one joined string at 6000 chars;
* comparison truncating each side at 3000;
* aggregate caches keyed by the whole population rather than by whoever
  actually represented it.

The observable difference between a prefix slice and an explicit selection is
what most of these assert: a prefix cut is positional, so everything past the
cut disappears no matter how small it is, while an explicit selection skips
an item that does not fit and *keeps scanning*.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from ccstory import session_summarizer as ss
from ccstory.report import build_report_json
from ccstory.sampling import (
    COVERAGE_DIMENSIONS,
    REASON_VOCABULARY,
    build_sampling_plan,
    build_session_sketch,
    plan_compact_projection,
    plan_for_sessions,
    plan_public_projection,
)
from ccstory.session_summarizer import (
    AGGREGATE_CHAR_BUDGET,
    COMPARISON_CHAR_BUDGET,
    SessionSummary,
    fit_within_char_budget,
    synthesize_category_for_period,
    synthesize_comparison,
    synthesize_overall_for_period,
)
from ccstory.time_tracking import SessionStat

BASE = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)


class _CapturingRun:
    """Stand-in for `subprocess.run` that records the prompt it was given."""

    def __init__(self, stdout: str = "A header line.\n- One outcome.") -> None:
        self.stdout = stdout
        self.prompts: list[str] = []

    def __call__(self, argv, *args, **kwargs):
        # The narrator receives its prompt as the last positional argument of
        # the command line; capture whichever element is the long one.
        self.prompts.append(max((str(a) for a in argv), key=len))
        run = self

        class R:
            returncode = 0
            stdout = run.stdout
            stderr = ""

        return R()


def _stat(
    session_id: str,
    *,
    project: str = "alpha",
    agent: str = "claude",
    day_offset: int = 0,
    active_sec: int = 600,
    text: str = "did some work",
) -> SessionStat:
    start = BASE + timedelta(days=day_offset)
    return SessionStat(
        project=project,
        category="coding",
        session_id=session_id,
        start=start,
        end=start + timedelta(seconds=active_sec),
        active_sec=active_sec,
        msg_count=4,
        user_msg_count=2,
        first_user_text=text,
        timestamps=[start.timestamp(), (start + timedelta(seconds=active_sec)).timestamp()],
        agent=agent,
    )


@pytest.fixture(autouse=True)
def _narrator_available(tmp_home, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ss, "claude_bin_available", lambda: True)


# ----- 1. The prefix slice is gone -------------------------------------------


class TestNoImplicitPrefixSlicing:
    """A short item behind an oversized one must still reach the narrator.

    This is the exact behavior a `[:N]` prefix slice cannot produce, so it
    doubles as the regression test for reintroducing one.
    """

    def _three_items(self) -> list[tuple[str, str]]:
        # Sized against AGGREGATE_CHAR_BUDGET so that: `big` fits; `medium`
        # then does not; `small` still does. A prefix cut would keep `big`,
        # truncate `medium` mid-sentence, and lose `small` entirely.
        big = "b" * (AGGREGATE_CHAR_BUDGET - 100)
        medium = "m" * 300
        small = "SMALL_ITEM_SURVIVES"
        return [("big", big), ("medium", medium), ("small", small)]

    def test_fit_skips_the_oversized_item_and_keeps_scanning(self):
        kept = fit_within_char_budget(
            self._three_items(), AGGREGATE_CHAR_BUDGET, per_item_overhead=3,
        )
        kept_ids = [item_id for item_id, _ in kept]

        assert kept_ids == ["big", "small"]
        # Nothing was cut mid-item: every kept text is byte-identical.
        originals = dict(self._three_items())
        for item_id, text in kept:
            assert text == originals[item_id]

    def test_overall_prompt_keeps_the_small_item_behind_the_oversized_one(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        capture = _CapturingRun()
        monkeypatch.setattr(ss.subprocess, "run", capture)

        synthesize_overall_for_period(
            period_key="2026-W30",
            category_hours=[("coding", 2.0)],
            sessions_by_category={"coding": self._three_items()},
        )

        assert len(capture.prompts) == 1
        prompt = capture.prompts[0]
        assert "SMALL_ITEM_SURVIVES" in prompt
        # The item that did not fit is absent *whole* — not truncated into
        # the prompt, which is what the old slice would have done.
        assert "m" * 300 not in prompt
        assert "m" * 50 not in prompt

    def test_category_prompt_keeps_the_small_item_too(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        capture = _CapturingRun()
        monkeypatch.setattr(ss.subprocess, "run", capture)

        items = self._three_items()
        synthesize_category_for_period(
            "2026-W30",
            "coding",
            [item_id for item_id, _ in items],
            [text for _, text in items],
        )

        assert len(capture.prompts) == 1
        assert "SMALL_ITEM_SURVIVES" in capture.prompts[0]
        assert "m" * 50 not in capture.prompts[0]

    def test_comparison_budgets_each_side_independently(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """A busy current window must not crowd out the window it is compared
        against — the failure mode one shared budget would introduce."""
        capture = _CapturingRun()
        monkeypatch.setattr(ss.subprocess, "run", capture)

        current = [
            ("cur-big", "c" * (COMPARISON_CHAR_BUDGET - 50)),
            ("cur-small", "CURRENT_SMALL"),
        ]
        previous = [("prev", "PREVIOUS_SIDE_PRESENT")]

        synthesize_comparison(
            current_key="2026-W30",
            previous_key="2026-W29",
            current_summaries=current,
            previous_summaries=previous,
        )

        assert len(capture.prompts) == 1
        prompt = capture.prompts[0]
        # The previous window survives a current window that alone would
        # exhaust a shared budget.
        assert "PREVIOUS_SIDE_PRESENT" in prompt
        assert "CURRENT_SMALL" in prompt


# ----- 2. Cache identity follows the representatives -------------------------


class TestAggregateCacheIdentity:
    def test_population_growth_that_changes_no_representative_stays_a_hit(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """The point of making selection explicit.

        A session added to a window that cannot fit the budget does not
        change who represents that window, so the cached narrative is still
        the right answer and must not be regenerated.
        """
        capture = _CapturingRun()
        monkeypatch.setattr(ss.subprocess, "run", capture)

        base_items = [("kept", "k" * (AGGREGATE_CHAR_BUDGET - 20))]
        first = synthesize_overall_for_period(
            period_key="2026-W30",
            category_hours=[("coding", 2.0)],
            sessions_by_category={"coding": list(base_items)},
        )
        assert first is not None
        assert len(capture.prompts) == 1

        grown = base_items + [("added", "a" * 500)]
        second = synthesize_overall_for_period(
            period_key="2026-W30",
            category_hours=[("coding", 2.0)],
            sessions_by_category={"coding": grown},
        )

        assert second == first
        # The new session could not fit, so the representatives — and
        # therefore the cached row — are unchanged.
        assert len(capture.prompts) == 1

    def test_a_changed_representative_rotates_the_cache(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        capture = _CapturingRun()
        monkeypatch.setattr(ss.subprocess, "run", capture)

        synthesize_overall_for_period(
            period_key="2026-W30",
            category_hours=[("coding", 2.0)],
            sessions_by_category={"coding": [("s1", "first summary")]},
        )
        assert len(capture.prompts) == 1

        synthesize_overall_for_period(
            period_key="2026-W30",
            category_hours=[("coding", 2.0)],
            sessions_by_category={"coding": [("s1", "a rewritten summary")]},
        )
        assert len(capture.prompts) == 2

    def test_a_policy_version_change_rotates_the_cache(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Without the policy fingerprint, a row cached under an older
        sampling policy would look valid forever.

        Both plans select the same single session, so nothing about the
        prompt or the representative set differs — the version alone must
        rotate the row.
        """
        capture = _CapturingRun()
        monkeypatch.setattr(ss.subprocess, "run", capture)

        items = {"coding": [("s1", "one summary")]}
        sketches = [build_session_sketch(_stat("s1"))]
        plan_v1 = build_sampling_plan(sketches, policy_version=1)
        plan_v2 = build_sampling_plan(sketches, policy_version=2)
        assert plan_v1.selected_ids == plan_v2.selected_ids == ("s1",)

        synthesize_overall_for_period(
            period_key="2026-W30",
            category_hours=[("coding", 2.0)],
            sessions_by_category=items,
            plan=plan_v1,
        )
        assert len(capture.prompts) == 1

        synthesize_overall_for_period(
            period_key="2026-W30",
            category_hours=[("coding", 2.0)],
            sessions_by_category=items,
            plan=plan_v2,
        )
        assert len(capture.prompts) == 2

    def test_input_order_permutation_alone_does_not_rotate_the_cache(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """A plan exists precisely so the caller's assembly order stops
        mattering — including to the cache."""
        capture = _CapturingRun()
        monkeypatch.setattr(ss.subprocess, "run", capture)

        stats = [_stat("s1"), _stat("s2", project="beta"), _stat("s3", day_offset=1)]
        texts = {"s1": "one summary", "s2": "two summary", "s3": "three summary"}
        items = [(stat.session_id, texts[stat.session_id]) for stat in stats]

        synthesize_overall_for_period(
            period_key="2026-W30",
            category_hours=[("coding", 2.0)],
            sessions_by_category={"coding": list(items)},
            plan=plan_for_sessions(stats),
        )
        assert len(capture.prompts) == 1

        for seed in range(10):
            shuffled = list(stats)
            random.Random(seed).shuffle(shuffled)
            reordered = [
                (stat.session_id, texts[stat.session_id]) for stat in shuffled
            ]
            synthesize_overall_for_period(
                period_key="2026-W30",
                category_hours=[("coding", 2.0)],
                sessions_by_category={"coding": reordered},
                plan=plan_for_sessions(shuffled),
            )
            assert len(capture.prompts) == 1, (
                f"input permutation seed={seed} rotated the cache"
            )


# ----- 3. Backfill order no longer follows encounter order -------------------


class TestBackfillOrdering:
    def _population(self) -> list[SessionStat]:
        return [
            _stat("claude-big", agent="claude", project="alpha", active_sec=3600),
            _stat("claude-small", agent="claude", project="alpha", active_sec=60),
            _stat("codex-one", agent="codex", project="beta", active_sec=600,
                  day_offset=1),
            _stat("antigravity-one", agent="antigravity", project="gamma",
                  active_sec=300, day_offset=2),
            _stat("filler", agent="claude", project="alpha", active_sec=120,
                  day_offset=2),
        ]

    def test_order_is_invariant_under_input_permutation(self):
        """Which sessions get narrator budget must not depend on which
        provider was registered first or which file the OS listed first."""
        baseline = ss.prepare_backfill_plan(self._population()).todo

        for seed in range(25):
            shuffled = self._population()
            random.Random(seed).shuffle(shuffled)
            assert ss.prepare_backfill_plan(shuffled).todo == baseline, (
                f"backfill order changed under permutation seed={seed}"
            )

    def test_coverage_picks_are_attempted_before_plain_capacity_fill(self):
        """An exhausted budget should still leave every provider, project,
        and local day represented."""
        todo = ss.prepare_backfill_plan(self._population()).todo
        plan = plan_for_sessions(self._population())

        covered = {
            sid for sid, reasons in plan.reasons.items()
            if any(reason != "budget_fill" for reason in reasons)
        }
        assert covered, "fixture must contain at least one coverage pick"

        first_fill = next(
            (i for i, sid in enumerate(todo) if sid not in covered), len(todo),
        )
        # No coverage pick sits behind a plain fill.
        assert all(sid in covered for sid in todo[:first_fill])
        assert not any(sid in covered for sid in todo[first_fill:])


# ----- 4. Projections are id-free and consistent -----------------------------


class TestPlanProjections:
    def _plan(self):
        return plan_for_sessions(
            [
                _stat("s1", project="alpha", agent="claude"),
                _stat("s2", project="beta", agent="codex", day_offset=1),
            ],
            {
                "s1": SessionSummary(
                    session_id="s1",
                    summary="fixed the failing test",
                    source=ss.SOURCE_GENERATED,
                    created_at=0.0,
                ),
            },
        )

    def test_public_projection_names_no_session(self):
        plan = self._plan()
        projection = plan_public_projection(plan)
        blob = repr(projection)

        for internal_id in plan.ordered_ids:
            assert internal_id not in blob
        assert "slice-" not in blob
        assert set(projection["reasons"]) <= set(REASON_VOCABULARY)
        assert set(projection["coverage"]) == set(COVERAGE_DIMENSIONS)

    def test_compact_projection_is_a_strict_subset_of_the_public_one(self):
        plan = self._plan()
        public = plan_public_projection(plan)
        compact = plan_compact_projection(plan)

        for key in ("policy_version", "population", "selected"):
            assert compact[key] == public[key]
        # MCP stays compact: no per-dimension breakdown, no reason histogram.
        assert "coverage" not in compact
        assert "reasons" not in compact
        assert compact["coverage_complete"] == all(
            public["coverage"][dimension]["hit"]
            >= public["coverage"][dimension]["target"]
            for dimension in COVERAGE_DIMENSIONS
        )

    def test_report_json_carries_the_projection_additively(self):
        plan = self._plan()
        payload = build_report_json(
            "week",
            BASE,
            BASE + timedelta(days=7),
            [],
            [],
            _EmptyUsage(),
            {},
            sampling=plan,
        )

        assert payload["narrative"]["sampling"] == plan_public_projection(plan)

    def test_report_json_omits_the_block_when_no_plan_ran(self):
        payload = build_report_json(
            "week",
            BASE,
            BASE + timedelta(days=7),
            [],
            [],
            _EmptyUsage(),
            {},
        )

        assert payload["narrative"]["sampling"] is None


class _EmptyUsage:
    """Minimal stand-in for UsageReport in the JSON-shape assertions above."""

    assistant_turns = 0
    cache_hit_ratio = 0.0
    total_input = 0
    total_cache_creation = 0
    total_cache_read = 0
    total_output = 0
    total_tokens = 0
    total_cost_usd = 0.0
    total_cost_uncached_usd = 0.0
    cache_savings_usd = 0.0
    unpriced_models: list[str] = []
    usage_complete = True
    incomplete_agents: list[str] = []
    provider_coverage: dict[str, str] = {}
    by_model: dict = {}
