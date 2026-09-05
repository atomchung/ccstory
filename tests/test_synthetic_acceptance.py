"""End-to-end acceptance for the 0.8 line, over one synthetic workspace (#188 0.8-G).

Every earlier slice proved its own layer in isolation: `test_session_slice.py`
the slice contract, `test_window_evidence.py` each provider's bounded
extraction, `test_window_integration.py` the snapshot glue,
`test_trend_windows.py` period assignment, `test_sampling.py` the plan, and
`test_sampling_consumers.py` the narrative lanes.

None of them answers the release question: with all three providers, four
projects, thirteen local days, a boundary-crossing session, duplicated
themes, and more evidence than any prompt budget can hold — all at once —
does the whole pipeline still produce window-pure, order-stable,
representative output on every public surface?

The population comes from `tests/synthetic_workspace.py`, whose own
self-checks (`test_synthetic_workspace.py`) prove the fixture is what it
claims to be. These tests assume that and assert on the *pipeline*.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccstory import providers
from ccstory.providers import collect_provider_snapshot
from ccstory.report import build_report_json, render_report
from ccstory.sampling import (
    COVERAGE_DIMENSIONS,
    build_session_sketch,
    plan_for_sessions,
    plan_public_projection,
)
from ccstory.session_summarizer import (
    AGGREGATE_CHAR_BUDGET,
    fit_within_char_budget,
)
from ccstory.time_tracking import SessionSlice, rollup_by_category
from tests.synthetic_workspace import build_synthetic_workspace


@pytest.fixture
def workspace(tmp_home: Path):
    return build_synthetic_workspace(tmp_home)


def _all_sessions(snapshot) -> list:
    return [s for window in snapshot.sessions_by_window.values() for s in window]


# ----- 1. Window purity across the whole population --------------------------


def test_no_window_contains_a_single_out_of_window_timestamp(workspace):
    """The release's core promise, asserted over every session at once.

    Not just the crossing session: a regression that leaked out-of-window
    facts through some *other* path — a contained session mis-clipped, a
    provider whose adapter drifted — would show up here and nowhere else.
    """
    snapshot = collect_provider_snapshot(workspace.windows)

    for key, (since, until) in workspace.windows.items():
        sessions = snapshot.sessions_by_window[key]
        assert sessions, f"window {key} is empty — the assertion would be vacuous"
        for session in sessions:
            for timestamp in session.timestamps:
                assert since.timestamp() <= timestamp < until.timestamp(), (
                    f"{key}: session {session.session_id} carries a timestamp "
                    f"outside [{since}, {until})"
                )


def test_the_crossing_session_shares_no_prose_between_its_two_windows(workspace):
    """Each half must describe only its own half's conversation."""
    snapshot = collect_provider_snapshot(workspace.windows)
    crossing = workspace.session(workspace.crossing_session_id)

    halves = {}
    for key in workspace.windows:
        matching = [
            s for s in snapshot.sessions_by_window[key]
            if getattr(s, "physical_session_id", s.session_id)
            == crossing.session_id
        ]
        assert len(matching) == 1, f"{key} should hold exactly one half"
        assert isinstance(matching[0], SessionSlice)
        halves[key] = matching[0]

    previous, current = halves["previous"], halves["current"]
    # Distinct bounded evidence means distinct evidence identity.
    assert previous.session_id != current.session_id
    assert previous.evidence_excerpt and current.evidence_excerpt
    assert previous.evidence_excerpt != current.evidence_excerpt
    # Both still publish the one physical id a user knows.
    assert previous.physical_session_id == current.physical_session_id


def test_only_crossing_transcripts_are_reopened(workspace):
    """Bounded extraction must stay bounded.

    The #188 brief sanctioned reopening a transcript *only* for a session
    that actually crosses a boundary. With 25 sessions in play, a regression
    that reopened every transcript per window would be invisible in output
    and expensive in practice, so it is asserted directly.

    Measured **per provider**, against that provider's own contained
    transcripts, because the three do not agree on a baseline: Claude and Codex
    open a contained transcript once (#174), and Antigravity three times.
    Those differences are pre-existing parser behavior (#174), and an absolute
    "one open per transcript" assertion would be testing that instead of testing
    what window purity costs.

    Within one provider the claim is exact: every contained transcript costs
    the same, and the crossing one costs that plus one bounded read per
    window it crosses.
    """
    transcripts = {session.path: session for session in workspace.sessions}

    def open_counts(windows) -> Counter:
        """Count transcript opens for one snapshot run.

        Patches `Path.open` by hand rather than through `monkeypatch`: that
        fixture is function-scoped and *shared* with `conftest.tmp_home`, so
        an `undo()` here would also revert the fake-`$HOME` redirection and
        point a later run at the developer's real transcripts.
        """
        opened: list[Path] = []
        original_open = Path.open

        def counted_open(self, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if self in transcripts and "r" in mode:
                opened.append(self)
            return original_open(self, *args, **kwargs)

        Path.open = counted_open
        try:
            collect_provider_snapshot(windows)
        finally:
            Path.open = original_open
        return Counter(opened)

    counts = open_counts(workspace.windows)
    assert counts, "nothing was opened — the assertions would be vacuous"

    by_provider: dict[str, list] = {}
    for path, session in transcripts.items():
        by_provider.setdefault(session.provider, []).append((session, counts[path]))

    assert set(by_provider) == {"claude", "codex", "antigravity"}
    saw_crossing = False
    for provider, rows in by_provider.items():
        contained = {
            count for session, count in rows if not session.crosses_boundary
        }
        assert len(contained) == 1, (
            f"{provider}: contained transcripts disagree on open count "
            f"{sorted(contained)} — one was reopened for no reason"
        )
        per_transcript = contained.pop()
        if provider in ("claude", "codex"):
            assert per_transcript == 1, (
                f"{provider}: contained transcripts should be opened exactly once in snapshot"
            )

        for session, count in rows:
            if not session.crosses_boundary:
                continue
            saw_crossing = True
            assert count == per_transcript + len(workspace.windows), (
                f"{provider}: crossing transcript opened {count} times, "
                f"expected {per_transcript} + {len(workspace.windows)} bounded reads"
            )
    assert saw_crossing, "the fixture no longer contains a crossing session"


# ----- 2. Order stability ----------------------------------------------------


def test_plan_and_public_output_are_stable_under_provider_permutation(
    workspace, monkeypatch,
):
    """Provider registration order must not reach the output.

    This is the failure the whole sampling layer exists to remove, so it is
    asserted against the real registry rather than a hand-built list: the
    specs are re-registered in a different order and the entire public
    projection must come back identical.
    """
    original_specs = dict(providers._PROVIDER_SPECS)

    def snapshot_and_plan():
        snapshot = collect_provider_snapshot(workspace.windows)
        sessions = snapshot.sessions_by_window["current"]
        plan = plan_for_sessions(sessions)
        rollups = rollup_by_category(sessions)
        payload = build_report_json(
            "current",
            *workspace.windows["current"],
            sessions,
            rollups,
            snapshot.usage_by_window["current"],
            {},
            sampling=plan,
        )
        payload.pop("generated_at")
        return plan, payload

    baseline_plan, baseline_payload = snapshot_and_plan()

    names = list(original_specs)
    for seed in range(10):
        shuffled = list(names)
        random.Random(seed).shuffle(shuffled)
        monkeypatch.setattr(
            providers,
            "_PROVIDER_SPECS",
            {name: original_specs[name] for name in shuffled},
        )
        plan, payload = snapshot_and_plan()

        assert plan == baseline_plan, f"plan moved under provider order seed={seed}"
        assert payload == baseline_payload, (
            f"public JSON moved under provider order seed={seed}"
        )


def test_plan_is_stable_under_session_permutation(workspace):
    snapshot = collect_provider_snapshot(workspace.windows)
    sessions = snapshot.sessions_by_window["current"]
    baseline = plan_for_sessions(sessions)

    for seed in range(25):
        shuffled = list(sessions)
        random.Random(seed).shuffle(shuffled)
        assert plan_for_sessions(shuffled) == baseline, (
            f"plan moved under session order seed={seed}"
        )


# ----- 3. Coverage and recall ------------------------------------------------


def test_every_provider_project_and_day_is_represented(workspace):
    """With no budget pressure the plan must reach full coverage.

    A coverage *target* that the policy cannot hit even when it has room
    would mean the coverage passes are not doing their job at all.
    """
    snapshot = collect_provider_snapshot(workspace.windows)
    sessions = _all_sessions(snapshot)
    plan = plan_for_sessions(sessions)

    for dimension in COVERAGE_DIMENSIONS:
        assert plan.coverage_hits[dimension] == plan.coverage_targets[dimension], (
            f"{dimension} coverage fell short with no budget pressure"
        )
    assert plan.coverage_targets["provider"] == 3
    assert plan.coverage_targets["project"] >= 4


def test_outcome_test_and_error_sessions_survive_a_tight_budget(workspace):
    """Salience recall under pressure.

    A budget far below the population is exactly when it matters that the
    interesting sessions are the ones kept. Each of the three signals the
    #188 acceptance list names must still be represented.
    """
    snapshot = collect_provider_snapshot(workspace.windows)
    sessions = _all_sessions(snapshot)
    sketches = {
        sketch.internal_id: sketch
        for sketch in (build_session_sketch(s) for s in sessions)
    }

    plan = plan_for_sessions(sessions, char_budget=1500)
    assert plan.unselected_count > 0, "budget must actually bind for this to mean anything"

    selected_salience: set[str] = set()
    for internal_id in plan.selected_ids:
        selected_salience |= set(sketches[internal_id].salience)

    for code in ("outcome", "test", "error"):
        assert code in selected_salience, (
            f"no {code!r} session survived the tight budget"
        )


# ----- 4. Budget respected without prefix truncation -------------------------


def test_selected_evidence_fits_the_prompt_budget_without_truncating_an_item(
    workspace,
):
    """The population exceeds the aggregate budget, so selection must bind —
    and must bind by dropping whole items, never by cutting one."""
    snapshot = collect_provider_snapshot(workspace.windows)
    sessions = _all_sessions(snapshot)
    sketches = [build_session_sketch(s) for s in sessions]
    assert sum(s.evidence_chars for s in sketches) > AGGREGATE_CHAR_BUDGET, (
        "fixture no longer exceeds the aggregate budget"
    )

    items = [(s.internal_id, "x" * s.evidence_chars) for s in sketches]
    kept = fit_within_char_budget(items, AGGREGATE_CHAR_BUDGET, per_item_overhead=3)

    assert len(kept) < len(items), "budget did not bind"
    total = sum(len(text) + 3 for _, text in kept)
    assert total <= AGGREGATE_CHAR_BUDGET

    # Every kept item is whole — the property a prefix cut cannot have.
    originals = dict(items)
    for internal_id, text in kept:
        assert text == originals[internal_id]


# ----- 5. Public contracts stay additive -------------------------------------


def test_json_markdown_and_mcp_keep_their_documented_shape(workspace):
    """Existing consumers read these by key. 0.8 may add, never move."""
    snapshot = collect_provider_snapshot(workspace.windows)
    sessions = snapshot.sessions_by_window["current"]
    rollups = rollup_by_category(sessions)
    usage = snapshot.usage_by_window["current"]
    plan = plan_for_sessions(sessions)

    payload = build_report_json(
        "current",
        *workspace.windows["current"],
        sessions,
        rollups,
        usage,
        {},
        sampling=plan,
    )

    assert payload["schema_version"] == 1
    for key in (
        "kind", "agent", "generated_at", "window", "totals", "buckets",
        "sessions", "narrative", "usage_coverage", "unpriced_models",
    ):
        assert key in payload, f"public JSON lost {key!r}"
    assert payload["narrative"]["sampling"] == plan_public_projection(plan)

    # No internal evidence identity reaches any public surface.
    published = {item["id"] for item in payload["sessions"]}
    physical = {
        getattr(s, "physical_session_id", s.session_id) for s in sessions
    }
    assert published == physical
    serialized = json.dumps(payload, default=str)
    assert "slice-" not in serialized

    markdown = render_report(
        "current", *workspace.windows["current"], sessions, rollups, usage, {},
    )
    assert markdown and "slice-" not in markdown

    pytest.importorskip("mcp")
    from ccstory.mcp_server import _compact_recap

    compact = _compact_recap(
        SimpleNamespace(
            agent="all",
            label="current",
            since=workspace.windows["current"][0],
            until=workspace.windows["current"][1],
            sessions=sessions,
            rollups=rollups,
            summaries={},
            category_narratives={},
            overall_narrative=None,
            usage=usage,
            report_path=None,
            narrative_provenance={},
            sampling=plan,
        )
    )
    for key in ("ok", "agent", "label", "since", "until", "categories"):
        assert key in compact, f"MCP compact result lost {key!r}"
    assert compact["sampling"]["population"] == plan.population_size
    assert "slice-" not in json.dumps(compact, default=str)


def test_a_month_window_over_the_same_workspace_stays_window_pure(workspace):
    """The acceptance list names `month` as well as `week`.

    A month window spans the whole fixture, so nothing crosses its
    boundaries and every session must come back as a plain `SessionStat` —
    the contained path, over the same data that exercises the crossing path
    at week scale.
    """
    since = workspace.boundary - timedelta(days=30)
    until = workspace.boundary + timedelta(days=30)
    snapshot = collect_provider_snapshot({"month": (since, until)})
    sessions = snapshot.sessions_by_window["month"]

    assert len(sessions) == len(workspace.sessions)
    assert not any(isinstance(s, SessionSlice) for s in sessions), (
        "nothing crosses a month boundary here, so nothing should be clipped"
    )
    for session in sessions:
        for timestamp in session.timestamps:
            assert since.timestamp() <= timestamp < until.timestamp()
