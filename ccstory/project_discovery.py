"""Read-only consumer seam for observed workspace/project discovery (#222).

Exposes only the canonical project identities already authoritative for a
recap and for `GoalContext` v1 — the same normalization + alias fold, over
the same provider collection/snapshot seam, that `ccstory.time_tracking` and
`ccstory.goals` already use. This module intentionally does not know about
inferred contribution intent, session-to-project reassignment, or any of
`ccstory.project_attribution`'s future accepted/conflict/abstained candidate
evidence (#224) — it must never import that module. It also never persists a
project registry or cache: every call re-scans the existing provider seam for
exactly the window it was asked about.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from .categorizer import project_identity
from .providers import collect_provider_snapshot
from .time_tracking import SessionSlice, SessionStat

#: Terminal display cap. Independent from `JSON_HARD_MAX` so a huge
#: population never floods a terminal card, while `--json` callers still see
#: more (up to their own explicit ceiling) without a second flag.
DEFAULT_DISPLAY_CAP = 20

#: `--json` output is bounded too (issue #222: "JSON must still obey an
#: explicit hard maximum rather than silently become unbounded").
JSON_HARD_MAX = 200

_WINDOW_KEY = "project-discovery"

#: Deterministic close-spelling suggestion tuning. Guidance only — a
#: suggestion never rewrites a caller's input (see `find_close_project_candidates`).
_CANDIDATE_LIMIT = 3
_CANDIDATE_CUTOFF = 0.6


@dataclass(frozen=True)
class ObservedProject:
    """One canonical project identity already observed in local session history.

    Deliberately minimal: only what safely distinguishes one workspace from
    another for configuration purposes. Never a transcript path or session id
    (issue #222 non-goal) — those stay internal to the provider layer.
    """

    project_id: str
    last_seen: datetime
    session_count: int
    agents: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "last_seen": self.last_seen.isoformat(),
            "session_count": self.session_count,
            "agents": list(self.agents),
        }


def collect_observed_projects(
    since: datetime,
    until: datetime,
    *,
    agent: str = "all",
    aliases: Mapping[str, str] | None = None,
) -> tuple[ObservedProject, ...]:
    """Collect the canonical project identities observed in ``[since, until)``.

    Scans through the existing provider collection/snapshot seam exactly once
    for this one call — no persistent registry, no second transcript index.
    Grouping uses the identical canonical normalization + alias fold that
    recap and GoalContext already use (`categorizer.project_identity`), so a
    variant folder-leaf and a nested-workspace path collapse onto the same row
    as everywhere else in ccstory, and a project touched by more than one
    coding agent collapses onto one row listing every agent that touched it.

    Order is deterministic regardless of provider/filesystem iteration order:
    most-recently-observed project first, canonical ``project_id`` as the
    explicit tie-break. Every provider-owned identifier (transcript path,
    session id, prompt text) is dropped during aggregation and never reaches
    the returned rows.
    """

    alias_map = dict(aliases) if aliases else {}
    snapshot = collect_provider_snapshot({_WINDOW_KEY: (since, until)}, agent=agent)
    sessions: Sequence[SessionStat | SessionSlice] = snapshot.sessions_by_window.get(
        _WINDOW_KEY, ()
    )

    groups: dict[str, list[SessionStat | SessionSlice]] = {}
    for session in sessions:
        canonical = project_identity(session.project, aliases=alias_map)
        groups.setdefault(canonical, []).append(session)

    observed = [
        ObservedProject(
            project_id=project_id,
            last_seen=max(member.end for member in members),
            session_count=len(members),
            agents=tuple(sorted({member.agent for member in members})),
        )
        for project_id, members in groups.items()
    ]
    observed.sort(
        key=lambda project: (-project.last_seen.timestamp(), project.project_id)
    )
    return tuple(observed)


def bounded_observed_projects(
    projects: Sequence[ObservedProject], cap: int,
) -> tuple[tuple[ObservedProject, ...], int]:
    """Return ``(capped, total_count)``. ``projects`` must already be ordered.

    This only truncates a population that ``collect_observed_projects`` (or
    an equivalent caller) already sorted deterministically — it never
    re-sorts, so callers control ordering exactly once.
    """

    total = len(projects)
    return tuple(projects[:cap]), total


def observed_project_ids(projects: Sequence[ObservedProject]) -> tuple[str, ...]:
    """The bare canonical id set, in the same deterministic order."""

    return tuple(project.project_id for project in projects)


def find_close_project_candidates(
    candidate: str,
    observed_ids: Sequence[str],
    *,
    limit: int = _CANDIDATE_LIMIT,
    cutoff: float = _CANDIDATE_CUTOFF,
) -> tuple[str, ...]:
    """Deterministic close-spelling suggestions — guidance only, never applied.

    Callers must never use this to rewrite a stored value; it exists purely
    so `ccstory goal set` can point a user at a likely typo. ``observed_ids``
    is deduplicated and sorted before scoring so the result never depends on
    the caller's iteration order — `difflib.get_close_matches` itself already
    breaks ratio ties by comparing candidate strings (tuple comparison), so
    this sort documents that guarantee rather than changing the outcome.
    """

    pool = sorted(set(observed_ids) - {candidate})
    if not pool:
        return ()
    return tuple(difflib.get_close_matches(candidate, pool, n=limit, cutoff=cutoff))


__all__ = [
    "DEFAULT_DISPLAY_CAP",
    "JSON_HARD_MAX",
    "ObservedProject",
    "collect_observed_projects",
    "bounded_observed_projects",
    "observed_project_ids",
    "find_close_project_candidates",
]
