"""Public GoalContext projections and human-facing recap surfaces (#218)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from rich.console import Console

from ccstory.goals import (
    GoalAttributionInput,
    attribute_goals,
    parse_goal_context,
)
from ccstory.report import (
    build_goal_projection,
    build_report_json,
    render_report,
    render_terminal_card,
)
from ccstory.time_tracking import CategoryRollup
from ccstory.token_usage import UsageReport


SINCE = datetime(2026, 7, 28, tzinfo=timezone.utc)
UNTIL = datetime(2026, 7, 29, tzinfo=timezone.utc)


def _goal_evidence():
    context = parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {
                    "id": "goal-alpha",
                    "title": "Alpha outcome",
                    "projects": ["alpha", "shared"],
                },
                {
                    "id": "goal-beta",
                    "title": "Beta outcome",
                    "projects": ["shared"],
                },
            ],
        },
        aliases={},
        source_metadata={
            "source_kind": "configured",
            "path": "/Users/private/goal-source.toml",
            "content": "must not escape",
        },
        source_fingerprint="sha256:public-fingerprint",
    )
    breakdown = attribute_goals(
        [
            GoalAttributionInput("alpha", date(2026, 7, 28), 7200),
            GoalAttributionInput("shared", date(2026, 7, 29), 3600),
            GoalAttributionInput("unmatched", date(2026, 7, 29), 1800),
        ],
        context,
    )
    assert breakdown is not None
    return context, breakdown


def _render_kwargs():
    rollups = [
        CategoryRollup(
            category="coding",
            active_min=210,
            sessions=0,
            messages=0,
            top_sessions=[],
        )
    ]
    usage = UsageReport(
        since=SINCE,
        until=UNTIL,
        provider_coverage={},
    )
    return {
        "label": "2026-07-28--2026-07-29",
        "since": SINCE,
        "until": UNTIL,
        "sessions": [],
        "rollups": rollups,
        "usage": usage,
        "summaries": {},
    }


def test_public_projection_converts_once_sanitizes_and_preserves_semantics():
    context, breakdown = _goal_evidence()

    projection = build_goal_projection(context, breakdown)

    assert projection == {
        "schema_version": 1,
        "source_kind": "configured",
        "context_fingerprint": "sha256:public-fingerprint",
        "per_goal_shared_semantics": "overlapping_non_additive",
        "global_bucket_semantics": "additive_each_contribution_counted_once",
        "disclaimer": (
            "Activity/contribution evidence only; not goal progress, "
            "completion, outcome, or acceptance."
        ),
        "coverage": {
            "covered_hours": 3.5,
            "exclusive_hours": 2.0,
            "shared_hours": 1.0,
            "unattributed_hours": 0.5,
            "unattributed_share": 0.1429,
            "top_unmapped_projects": [
                {"project_id": "unmatched", "active_hours": 0.5},
            ],
        },
        "goals": [
            {
                "id": "goal-alpha",
                "title": "Alpha outcome",
                "total_hours": 3.0,
                "exclusive_hours": 2.0,
                "shared_hours": 1.0,
                "shared_hours_is_non_additive": True,
                "projects_touched": ["alpha", "shared"],
                "latest_activity": "2026-07-29",
            },
            {
                "id": "goal-beta",
                "title": "Beta outcome",
                "total_hours": 1.0,
                "exclusive_hours": 0.0,
                "shared_hours": 1.0,
                "shared_hours_is_non_additive": True,
                "projects_touched": ["shared"],
                "latest_activity": "2026-07-29",
            },
        ],
    }
    serialized = json.dumps(projection)
    assert "/Users/private" not in serialized
    assert "must not escape" not in serialized


def test_projection_sanitizes_unknown_source_and_hides_empty_context():
    context, breakdown = _goal_evidence()
    provided = type(context)(
        schema_version=context.schema_version,
        goals=context.goals,
        source_metadata={"source_kind": "private-file", "path": "/secret"},
        source_fingerprint="",
    )
    projection = build_goal_projection(provided, breakdown)
    assert projection is not None
    assert projection["source_kind"] == "provided"
    assert projection["context_fingerprint"] is None

    empty = parse_goal_context(
        {"schema_version": 1, "goals": []},
        aliases={},
        source_metadata={"source_kind": "managed"},
    )
    empty_breakdown = attribute_goals([], empty)
    assert build_goal_projection(None, None) is None
    assert build_goal_projection(empty, empty_breakdown) is None


def test_projection_orders_by_raw_contribution_before_rounding():
    context = parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {"id": "alpha", "title": "Alpha", "projects": ["alpha"]},
                {"id": "beta", "title": "Beta", "projects": ["beta"]},
            ],
        },
        aliases={},
    )
    breakdown = attribute_goals(
        [
            GoalAttributionInput("alpha", date(2026, 7, 29), 3600.1),
            GoalAttributionInput("beta", date(2026, 7, 29), 3600.2),
        ],
        context,
    )
    projection = build_goal_projection(context, breakdown)

    assert projection is not None
    assert [goal["id"] for goal in projection["goals"]] == ["beta", "alpha"]
    assert [goal["total_hours"] for goal in projection["goals"]] == [1.0, 1.0]


def test_markdown_json_and_terminal_share_projection_and_order():
    context, breakdown = _goal_evidence()
    projection = build_goal_projection(context, breakdown)
    assert projection is not None
    kwargs = _render_kwargs()
    overall = "**Inferred work theme**\n- Shipped something."

    markdown = render_report(
        **kwargs,
        overall_narrative=overall,
        goal_context=context,
        goal_breakdown=breakdown,
    )
    payload = build_report_json(
        **kwargs,
        overall_narrative=overall,
        goal_context=context,
        goal_breakdown=breakdown,
    )
    console = Console(width=72, record=True, color_system=None)
    console.print(
        render_terminal_card(
            since=SINCE,
            until=UNTIL,
            sessions=[],
            rollups=kwargs["rollups"],
            usage=kwargs["usage"],
            overall_narrative=overall,
            goal_context=context,
            goal_breakdown=breakdown,
        )
    )
    card = console.export_text()

    assert payload["goals"] == projection
    assert markdown.index("## Time distribution") < markdown.index(
        "## Goal activity"
    ) < markdown.index("## What you did")
    assert "Shared (non-additive)" in markdown
    assert "Activity/contribution evidence only; not goal progress" in markdown
    assert (
        "**Unattributed:** 0.50h (14.3% of covered activity) "
        "— activity in projects mapped to no goal." in markdown
    )
    assert "**Top unmapped projects:** `unmatched` 0.50h." in markdown
    assert markdown.index("Alpha outcome") < markdown.index("Beta outcome")
    assert "Goal activity" in card
    assert "Shared*" in card
    assert "unattributed: 0.50h (14%) — projects mapped to no goal" in card
    assert "top unmapped: unmatched 0.5h" in card
    assert "Shared non-additive · activity, not progress/completion" in card
    assert card.index("Alpha outcome") < card.index("Beta outcome")


def _unwrapped_card(card: str) -> str:
    """Panel text with the border column dropped and wrapped lines rejoined.

    The card is a fixed 72-column panel, so a long disclosure line legitimately
    folds. Assertions about *what* it says should not depend on where Rich
    chose to wrap it.
    """
    return " ".join(card.replace("│", " ").split())


def _mostly_unmapped_evidence(order=None):
    """The shipped #255 shape: 205.22h covered, ~16h attributed.

    Two mapped projects carry 6.56 exclusive + 9.47 shared hours; the other
    189.19h sit in projects mapped to no goal. `ccstory` and `stock` are tied
    at rank three, so the canonical-id tie-break decides which one is listed.
    """
    context = parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {
                    "id": "goal-os",
                    "title": "Personal OS",
                    "projects": ["personal-os", "job"],
                },
                {"id": "goal-job", "title": "Job", "projects": ["job"]},
            ],
        },
        aliases={},
        source_metadata={"source_kind": "managed"},
        source_fingerprint="sha256:mostly-unmapped",
    )
    hours = {
        "personal-os": 6.56,
        "job": 9.47,
        "investment-note": 71.43,
        "kol-collector": 60.0,
        "stock": 24.88,
        "ccstory": 24.88,
        "info-collector": 8.0,
    }
    items = [
        GoalAttributionInput(project, date(2026, 8, 12), value * 3600)
        for project, value in hours.items()
    ]
    if order is not None:
        items = [items[index] for index in order]
    breakdown = attribute_goals(items, context)
    assert breakdown is not None
    return context, breakdown


def test_top_unmapped_hint_is_bounded_ranked_and_permutation_stable():
    context, breakdown = _mostly_unmapped_evidence()
    projection = build_goal_projection(context, breakdown)
    assert projection is not None

    coverage = projection["coverage"]
    assert coverage["covered_hours"] == 205.22
    assert coverage["exclusive_hours"] == 6.56
    assert coverage["shared_hours"] == 9.47
    assert coverage["unattributed_hours"] == 189.19
    assert coverage["unattributed_share"] == 0.9219
    # Bounded at three, hours descending, canonical id breaks the 24.88h tie
    # so `ccstory` makes the cut and `stock` does not.
    assert coverage["top_unmapped_projects"] == [
        {"project_id": "investment-note", "active_hours": 71.43},
        {"project_id": "kol-collector", "active_hours": 60.0},
        {"project_id": "ccstory", "active_hours": 24.88},
    ]

    for order in ([6, 5, 4, 3, 2, 1, 0], [3, 0, 6, 2, 5, 1, 4]):
        permuted_context, permuted = _mostly_unmapped_evidence(order)
        assert build_goal_projection(permuted_context, permuted) == projection


def test_every_surface_discloses_the_unattributed_majority():
    context, breakdown = _mostly_unmapped_evidence()
    kwargs = _render_kwargs()

    markdown = render_report(
        **kwargs, goal_context=context, goal_breakdown=breakdown
    )
    payload = build_report_json(
        **kwargs, goal_context=context, goal_breakdown=breakdown
    )
    console = Console(width=72, record=True, color_system=None)
    console.print(
        render_terminal_card(
            since=SINCE,
            until=UNTIL,
            sessions=[],
            rollups=kwargs["rollups"],
            usage=kwargs["usage"],
            goal_context=context,
            goal_breakdown=breakdown,
        )
    )
    card = console.export_text()

    assert (
        "**Unattributed:** 189.19h (92.2% of covered activity) "
        "— activity in projects mapped to no goal." in markdown
    )
    assert (
        "**Top unmapped projects:** `investment-note` 71.43h · "
        "`kol-collector` 60.00h · `ccstory` 24.88h." in markdown
    )
    # The coverage line fits the 72-column card without folding.
    assert "unattributed: 189.19h (92%) — projects mapped to no goal" in card
    assert (
        "top unmapped: investment-note 71.4h · kol-collector 60.0h · "
        "ccstory 24.9h" in _unwrapped_card(card)
    )
    assert (
        payload["goals"]["coverage"]["top_unmapped_projects"]
        == build_goal_projection(context, breakdown)["coverage"][
            "top_unmapped_projects"
        ]
    )
    # The hint is a ranking, not a listing: the fourth unmapped project stays
    # out of every human-facing surface and out of JSON.
    for surface in (markdown, card, json.dumps(payload)):
        assert "stock" not in surface
        assert "info-collector" not in surface


def test_full_attribution_still_shows_a_zero_coverage_line_without_a_hint():
    context = parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {"id": "goal-alpha", "title": "Alpha", "projects": ["alpha"]}
            ],
        },
        aliases={},
        source_metadata={"source_kind": "managed"},
    )
    breakdown = attribute_goals(
        [GoalAttributionInput("alpha", date(2026, 7, 29), 3600)], context
    )
    assert breakdown is not None
    kwargs = _render_kwargs()

    projection = build_goal_projection(context, breakdown)
    markdown = render_report(
        **kwargs, goal_context=context, goal_breakdown=breakdown
    )
    console = Console(width=72, record=True, color_system=None)
    console.print(
        render_terminal_card(
            since=SINCE,
            until=UNTIL,
            sessions=[],
            rollups=kwargs["rollups"],
            usage=kwargs["usage"],
            goal_context=context,
            goal_breakdown=breakdown,
        )
    )
    card = console.export_text()

    assert projection is not None
    assert projection["coverage"]["unattributed_hours"] == 0.0
    assert projection["coverage"]["top_unmapped_projects"] == []
    # Zero is a fact worth printing: a missing line would be indistinguishable
    # from a renderer that forgot the bucket.
    assert (
        "**Unattributed:** 0.00h (0.0% of covered activity) "
        "— activity in projects mapped to no goal." in markdown
    )
    assert "unattributed: 0.00h (0%) — projects mapped to no goal" in card
    assert "Top unmapped projects" not in markdown
    assert "top unmapped" not in card


def test_unmapped_hint_carries_leaf_identity_without_paths_or_sessions():
    context = parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {"id": "goal-alpha", "title": "Alpha", "projects": ["alpha"]}
            ],
        },
        aliases={},
        source_metadata={
            "source_kind": "managed",
            "path": "/Users/private/goal-source.toml",
        },
        source_fingerprint="sha256:leaf-only",
    )
    breakdown = attribute_goals(
        [
            GoalAttributionInput("alpha", date(2026, 7, 29), 3600),
            GoalAttributionInput(
                "-Users-privateowner-Side-project-investment-note",
                date(2026, 7, 29),
                7200,
            ),
        ],
        context,
    )
    assert breakdown is not None
    kwargs = _render_kwargs()

    markdown = render_report(
        **kwargs, goal_context=context, goal_breakdown=breakdown
    )
    payload = build_report_json(
        **kwargs, goal_context=context, goal_breakdown=breakdown
    )
    console = Console(width=72, record=True, color_system=None)
    console.print(
        render_terminal_card(
            since=SINCE,
            until=UNTIL,
            sessions=[],
            rollups=kwargs["rollups"],
            usage=kwargs["usage"],
            goal_context=context,
            goal_breakdown=breakdown,
        )
    )
    card = console.export_text()

    assert payload["goals"]["coverage"]["top_unmapped_projects"] == [
        {"project_id": "investment-note", "active_hours": 2.0}
    ]
    for surface in (markdown, card, json.dumps(payload)):
        assert "investment-note" in surface
        assert "privateowner" not in surface
        assert "Side-project" not in surface


def test_absent_or_zero_goal_context_adds_no_section_or_json_key():
    kwargs = _render_kwargs()
    baseline_markdown = render_report(**kwargs)
    baseline_json = build_report_json(**kwargs)

    empty = parse_goal_context(
        {"schema_version": 1, "goals": []},
        aliases={},
        source_metadata={"source_kind": "managed"},
    )
    empty_breakdown = attribute_goals([], empty)
    empty_markdown = render_report(
        **kwargs,
        goal_context=empty,
        goal_breakdown=empty_breakdown,
    )
    empty_json = build_report_json(
        **kwargs,
        goal_context=empty,
        goal_breakdown=empty_breakdown,
    )
    baseline_console = Console(width=72, record=True, color_system=None)
    baseline_console.print(
        render_terminal_card(
            since=SINCE,
            until=UNTIL,
            sessions=[],
            rollups=kwargs["rollups"],
            usage=kwargs["usage"],
        )
    )
    empty_console = Console(width=72, record=True, color_system=None)
    empty_console.print(
        render_terminal_card(
            since=SINCE,
            until=UNTIL,
            sessions=[],
            rollups=kwargs["rollups"],
            usage=kwargs["usage"],
            goal_context=empty,
            goal_breakdown=empty_breakdown,
        )
    )

    assert empty_markdown == baseline_markdown
    assert empty_console.export_text() == baseline_console.export_text()
    assert "Goal activity" not in baseline_markdown
    assert "goals" not in baseline_json
    assert "goals" not in empty_json


def test_terminal_caps_at_five_while_markdown_and_json_keep_every_goal():
    context = parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {
                    "id": f"goal-{index}",
                    "title": f"Goal {index}",
                    "projects": ["shared"],
                }
                for index in range(1, 7)
            ],
        },
        aliases={},
        source_metadata={"source_kind": "managed"},
        source_fingerprint="sha256:six-goals",
    )
    breakdown = attribute_goals(
        [GoalAttributionInput("shared", date(2026, 7, 29), 3600)],
        context,
    )
    assert breakdown is not None
    kwargs = _render_kwargs()

    markdown = render_report(
        **kwargs,
        goal_context=context,
        goal_breakdown=breakdown,
    )
    payload = build_report_json(
        **kwargs,
        goal_context=context,
        goal_breakdown=breakdown,
    )
    console = Console(width=72, record=True, color_system=None)
    console.print(
        render_terminal_card(
            since=SINCE,
            until=UNTIL,
            sessions=[],
            rollups=kwargs["rollups"],
            usage=kwargs["usage"],
            goal_context=context,
            goal_breakdown=breakdown,
        )
    )
    card = console.export_text()

    assert len(payload["goals"]["goals"]) == 6
    assert all(f"Goal {index}" in markdown for index in range(1, 7))
    assert all(f"Goal {index}" in card for index in range(1, 6))
    assert "Goal 6" not in card
    assert "+1 more in full report" in card
