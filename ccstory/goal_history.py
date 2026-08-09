"""Read-only consumer seam for deterministic weekly goal activity history."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, time, timedelta
from typing import Any

from .goals import (
    GoalActivitySeries,
    GoalActivityWindow,
    GoalContext,
    GoalContextError,
    build_goal_activity_series,
)
from .local_time import system_local_timezone
from .providers import collect_provider_snapshot


DEFAULT_GOAL_ACTIVITY_WEEKS = 4
MAX_GOAL_ACTIVITY_WEEKS = 24


def validate_goal_activity_weeks(weeks: object) -> int:
    """Return a valid bounded bucket count, rejecting coercion and clamping."""

    if isinstance(weeks, bool) or not isinstance(weeks, int):
        raise ValueError(
            "goal activity weeks must be an integer between 1 and "
            f"{MAX_GOAL_ACTIVITY_WEEKS}"
        )
    if not 1 <= weeks <= MAX_GOAL_ACTIVITY_WEEKS:
        raise ValueError(
            "goal activity weeks must be between 1 and "
            f"{MAX_GOAL_ACTIVITY_WEEKS}; got {weeks}"
        )
    return weeks


def completed_local_week_windows(
    weeks: object = DEFAULT_GOAL_ACTIVITY_WEEKS,
    *,
    now: datetime | None = None,
) -> tuple[tuple[str, datetime, datetime], ...]:
    """Return completed local ISO weeks, oldest first.

    Each half-open window is Monday 00:00 through the next Monday 00:00 in
    ``now``'s timezone. Calendar construction deliberately avoids assuming a
    week always contains 604800 elapsed seconds across daylight-saving changes.
    A naive injected ``now`` follows the existing trend convention and is
    interpreted in the system local timezone.
    """

    count = validate_goal_activity_weeks(weeks)
    if now is None:
        local_now = datetime.now(system_local_timezone())
    elif now.tzinfo is None:
        local_now = now.replace(tzinfo=system_local_timezone())
    else:
        local_now = now
    report_timezone = local_now.tzinfo
    if report_timezone is None:  # pragma: no cover - astimezone is tz-aware
        raise GoalContextError("could not determine the system local timezone")

    current_week_start = local_now.date() - timedelta(days=local_now.weekday())
    windows: list[tuple[str, datetime, datetime]] = []
    for offset in range(count, 0, -1):
        since_date = current_week_start - timedelta(days=7 * offset)
        until_date = since_date + timedelta(days=7)
        since = datetime.combine(since_date, time.min, tzinfo=report_timezone)
        until = datetime.combine(until_date, time.min, tzinfo=report_timezone)
        windows.append((since_date.isoformat(), since, until))
    return tuple(windows)


def build_goal_activity_projection(
    series: GoalActivitySeries,
    *,
    agent: str,
) -> dict[str, Any]:
    """Project a series into the only public JSON/MCP history shape.

    The pure attribution core records seconds. This projection exposes hours
    and an allowlisted metadata set only: no source path, source metadata,
    transcript identity, prompt, correction, or internal evidence can enter.
    """

    if not isinstance(series, GoalActivitySeries):
        raise GoalContextError(
            "goal activity projection requires a GoalActivitySeries"
        )

    def hours(seconds: float) -> float:
        return round(seconds / 3600, 2)

    def _goal_projection(goal, project_hours) -> dict[str, Any]:
        exclusive_hours = project_hours(goal.exclusive_contribution)
        shared_hours = project_hours(goal.shared_contribution)
        return {
            "id": goal.goal_id,
            "title": goal.title,
            "total_hours": round(
                math.fsum((exclusive_hours, shared_hours)), 2
            ),
            "exclusive_hours": exclusive_hours,
            "shared_hours": shared_hours,
            "shared_hours_is_non_additive": (
                goal.shared_contribution_is_non_additive
            ),
            "projects_touched": list(goal.projects_touched),
            "latest_activity": (
                goal.latest_activity.isoformat()
                if goal.latest_activity is not None
                else None
            ),
        }

    buckets: list[dict[str, Any]] = []
    for bucket in series.buckets:
        covered = bucket.covered_contribution
        exclusive_hours = hours(bucket.exclusive_contribution)
        shared_hours = hours(bucket.shared_contribution)
        unattributed_hours = hours(bucket.unattributed_contribution)
        # Derive the displayed total from the displayed additive lanes. If all
        # four values were rounded independently, a small bucket could claim a
        # global accounting invariant that its own JSON numbers did not satisfy.
        covered_hours = round(
            math.fsum(
                (exclusive_hours, shared_hours, unattributed_hours)
            ),
            2,
        )
        buckets.append(
            {
                "since": bucket.since.isoformat(),
                "until": bucket.until.isoformat(),
                "bucket_unit": bucket.bucket_unit,
                "source_kind": bucket.source_kind,
                "context_fingerprint": bucket.context_fingerprint,
                "coverage_status": bucket.coverage_status,
                "per_goal_shared_semantics": (
                    bucket.per_goal_shared_semantics
                ),
                "global_bucket_semantics": bucket.global_bucket_semantics,
                "disclaimer": bucket.disclaimer,
                "coverage": {
                    "covered_hours": covered_hours,
                    "exclusive_hours": exclusive_hours,
                    "shared_hours": shared_hours,
                    "unattributed_hours": unattributed_hours,
                    "unattributed_share": (
                        round(bucket.unattributed_contribution / covered, 4)
                        if covered
                        else 0.0
                    ),
                },
                "goals": [
                    _goal_projection(goal, hours) for goal in bucket.goals
                ],
            }
        )

    return {
        "ok": True,
        "agent": agent,
        "count": len(buckets),
        "schema_version": series.schema_version,
        "bucket_unit": series.bucket_unit,
        "source_kind": series.source_kind,
        "context_fingerprint": series.context_fingerprint,
        "coverage_status": series.coverage_status,
        "per_goal_shared_semantics": series.per_goal_shared_semantics,
        "global_bucket_semantics": series.global_bucket_semantics,
        "disclaimer": series.disclaimer,
        "buckets": buckets,
    }


def public_goal_activity_error(exc: BaseException) -> str:
    """Return a useful history error without local source/evidence details."""

    message = str(exc)
    if not isinstance(exc, GoalContextError):
        return message
    if message.startswith("No GoalContext source is configured."):
        return message
    return (
        "Goal activity history could not load or validate its selected "
        "GoalContext or bounded provider evidence."
    )


def collect_goal_activity_history(
    context: GoalContext | None,
    *,
    weeks: object = DEFAULT_GOAL_ACTIVITY_WEEKS,
    now: datetime | None = None,
    agent: str = "all",
    aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Collect one snapshot and return its sanitized weekly series projection.

    GoalContext selection is intentionally owned by the caller so the CLI and
    MCP wrappers can reuse their existing explicit/configured/managed source
    precedence. A missing source is an explicit error for this dedicated
    history surface; existing recap calls without GoalContext stay unchanged.
    """

    count = validate_goal_activity_weeks(weeks)
    if context is None:
        raise GoalContextError(
            "No GoalContext source is configured. Use --goals-file, configure "
            "[goal_context].path, or create ~/.ccstory/goals.toml."
        )

    planned = completed_local_week_windows(count, now=now)
    window_map = {
        key: (since, until) for key, since, until in planned
    }
    # The complete multi-window map is handed to the registry once. Providers
    # own a single transcript scan and return window-pure SessionStat/Slice
    # facts; no later bucket performs collection or token-coverage inference.
    snapshot = collect_provider_snapshot(window_map, agent=agent)
    windows = tuple(
        GoalActivityWindow(
            since=since,
            until=until,
            sessions=tuple(snapshot.sessions_by_window[key]),
            coverage_status="unavailable",
        )
        for key, since, until in planned
    )
    series = build_goal_activity_series(
        windows,
        context,
        aliases=aliases,
        timezone=planned[0][1].tzinfo,
    )
    assert series is not None
    return build_goal_activity_projection(series, agent=agent)
