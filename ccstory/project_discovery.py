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

Issue #254 adds two decided facts per identity — a deterministic folder-layer
category and a relevance verdict — and a pure partition helper. Both stay
inside this module's existing contract: zero model calls, no new evidence
source, and no row is ever dropped during collection.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .categorizer import project_identity, resolve_session_bucket
from .providers import collect_provider_snapshot, list_providers
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

# ----- Relevance verdicts (#254) --------------------------------------------
#
# Discovery exists so an owner can configure goals against a real workspace
# (#222). Two identity classes can never be that target, and both are
# decidable from evidence already in hand — no model call, no heuristic
# scoring, no per-store tuning:
#
#   * a workspace whose root is operating-system scratch space, which the
#     next reboot or cleanup sweep deletes; and
#   * the per-day placeholder workspace a coding agent synthesizes for a
#     chat the user started without opening a folder.
#
# `RELEVANCE_RELEVANT` is the only verdict the default view keeps; `--all`
# restores the complete population unchanged.

#: The project is a genuine, configurable workspace.
RELEVANCE_RELEVANT = "relevant"

#: Every session for this project ran under a temporary filesystem root.
RELEVANCE_EPHEMERAL_ROOT = "ephemeral_root"

#: The canonical id has the `<agent>-YYYY-MM-DD-<leaf>` shape a coding agent
#: produces for a folderless chat.
RELEVANCE_SYNTHETIC_DATED_ID = "synthetic_dated_id"

#: Verdicts the default relevance view hides, in stable reporting order.
FILTERED_RELEVANCE_REASONS: tuple[str, ...] = (
    RELEVANCE_EPHEMERAL_ROOT,
    RELEVANCE_SYNTHETIC_DATED_ID,
)

#: Absolute filesystem roots that are scratch space by construction, as
#: leading path components. Matched component-wise, never as a bare string
#: prefix, so a real repository named `tmp-runner` (or living in
#: `~/code/tmpdata`) is untouched — only a workspace whose root genuinely
#: *is* `/tmp`, `/private/tmp`, `/var/tmp`, `/private/var/tmp`, or the macOS
#: per-user `TMPDIR` under `/var/folders` matches.
_EPHEMERAL_ROOTS: tuple[tuple[str, ...], ...] = (
    ("tmp",),
    ("private", "tmp"),
    ("var", "tmp"),
    ("private", "var", "tmp"),
    ("var", "folders"),
    ("private", "var", "folders"),
)

#: Structural shape of a provider-synthesized dated workspace id. Verified
#: against real local history: the Codex desktop app parks a folderless chat
#: in `~/Documents/<Agent>/<YYYY-MM-DD>/<leaf>` (observed leaves: `new-chat`,
#: `codex`), which `categorizer.normalize_project_name` renders as
#: `codex-2026-08-21-new-chat` once the `Documents` path stem is dropped.
#: Deliberately narrow: the leading token must be a registered coding agent,
#: the date must be a real calendar date, and a non-empty leaf must follow —
#: so an ordinary project named `release-2026-08-21-notes` (no agent token)
#: or `codex-plugin` (no date) is never touched.
_SYNTHETIC_DATED_ID_RE = re.compile(
    r"^(?P<agent>[a-z0-9]+)-(?P<date>\d{4}-\d{2}-\d{2})-(?P<leaf>.+)$"
)


@dataclass(frozen=True)
class ObservedProject:
    """One canonical project identity already observed in local session history.

    Deliberately minimal: only what safely distinguishes one workspace from
    another for configuration purposes. Never a transcript path or session id
    (issue #222 non-goal) — those stay internal to the provider layer.

    `category` / `category_source` and `relevance` (#254) are decided facts
    about the identity, not new evidence: both are derived from what this
    module already holds, and neither widens the disclosure surface — a
    relevance verdict is a boolean-grade classification, not the path it was
    read from.
    """

    project_id: str
    last_seen: datetime
    session_count: int
    agents: tuple[str, ...]
    #: Deterministic folder-layer bucket (`user_rule > builtin_rule >
    #: fallback`); never the content-classification cache, never a model.
    category: str = ""
    #: Which of those three layers produced `category`.
    category_source: str = ""
    #: One of `RELEVANCE_RELEVANT` / `FILTERED_RELEVANCE_REASONS`.
    relevance: str = RELEVANCE_RELEVANT

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "last_seen": self.last_seen.isoformat(),
            "session_count": self.session_count,
            "agents": list(self.agents),
            "category": self.category,
            "category_source": self.category_source,
            "relevance": self.relevance,
        }


def _agent_id_tokens() -> frozenset[str]:
    """Lowercased registered coding-agent names, as leading id tokens.

    Read from the provider registry rather than hard-coded so a newly
    registered provider's own dated scratch workspaces are recognized without
    a second edit here.
    """

    return frozenset(name.lower() for name in list_providers())


def _cwd_path_components(cwd: str) -> tuple[str, ...]:
    """Absolute POSIX components of a recorded workspace cwd, else ``()``.

    Some providers record an editor-supplied ``file://`` URI instead of a
    plain path; the scheme is stripped so both spellings compare identically.
    A relative or empty cwd yields ``()``, which callers treat as "no path
    evidence" rather than as a negative match.
    """

    raw = (cwd or "").strip()
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    if not raw.startswith("/"):
        return ()
    return tuple(part for part in raw.split("/") if part)


def _has_ephemeral_root(session: SessionStat | SessionSlice) -> bool:
    """Did this one session's workspace live under a temporary root?

    Prefers the recorded ``cwd``. Pre-cwd-era transcripts leave it empty, so
    the encoded project folder name is the documented fallback:
    `providers.projects.encode_project_dir` maps every path separator to
    ``-``, so an absolute temp root survives as a full ``-tmp-`` /
    ``-private-tmp-`` style prefix. The comparison is against that whole
    prefix, so an ordinary `-Users-alice-code-tmp-runner` never matches.
    """

    components = _cwd_path_components(getattr(session, "cwd", "") or "")
    if components:
        return any(
            components[: len(root)] == root for root in _EPHEMERAL_ROOTS
        )

    encoded = session.project or ""
    for root in _EPHEMERAL_ROOTS:
        prefix = "-" + "-".join(root)
        if encoded == prefix or encoded.startswith(prefix + "-"):
            return True
    return False


def is_synthetic_dated_project_id(project_id: str) -> bool:
    """Is this canonical id a coding agent's per-day folderless-chat workspace?

    Structure only, and every part of the structure has to hold: a registered
    agent token, a real calendar date in `YYYY-MM-DD`, then a non-empty leaf.
    """

    match = _SYNTHETIC_DATED_ID_RE.match(project_id)
    if match is None:
        return False
    if match.group("agent") not in _agent_id_tokens():
        return False
    try:
        date.fromisoformat(match.group("date"))
    except ValueError:
        return False
    return True


def _classify_relevance(
    project_id: str, members: Sequence[SessionStat | SessionSlice],
) -> str:
    """The deterministic relevance verdict for one grouped project.

    The ephemeral-root verdict needs *every* member session to have run in
    scratch space. That direction is deliberate: an alias that folds a
    throwaway checkout onto a real project, or a repository that happens to
    have been copied into `/tmp` once, keeps the project visible — the filter
    may never be the reason an owner cannot find a workspace they actually
    work in.
    """

    if members and all(_has_ephemeral_root(member) for member in members):
        return RELEVANCE_EPHEMERAL_ROOT
    if is_synthetic_dated_project_id(project_id):
        return RELEVANCE_SYNTHETIC_DATED_ID
    return RELEVANCE_RELEVANT


def _resolve_folder_category(
    members: Sequence[SessionStat | SessionSlice], config_path: Path | None,
) -> tuple[str, str]:
    """Resolve one project's category through the folder layer only (#254).

    Runs `categorizer.resolve_session_bucket(..., mode="folder")`, whose
    documented chain is exactly `user_rule > builtin_rule > fallback` (#214).
    Folder mode never consults the per-session content-classification cache
    and never reaches a narrator, so discovery keeps its zero-model-call
    contract while still showing how a project relates to report categories.

    The resolver reads a raw encoded project folder — the same input shape a
    recap feeds it — so the group's lexicographically smallest member folder
    is the representative. That choice is independent of provider and
    filesystem iteration order, which keeps the column deterministic.
    """

    representative = min(member.project for member in members)
    bucket, source = resolve_session_bucket(
        representative, None, mode="folder", config_path=config_path,
    )
    return bucket or "", source


def collect_observed_projects(
    since: datetime,
    until: datetime,
    *,
    agent: str = "all",
    aliases: Mapping[str, str] | None = None,
    config_path: Path | None = None,
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

    Every row also carries its deterministic folder-layer category and its
    relevance verdict (#254). Nothing is dropped here: filtering is a
    separate, explicit step (`partition_by_relevance`) so callers that need
    the complete population — `ccstory goal set`'s observed check above all —
    keep seeing every observed identity.
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

    observed: list[ObservedProject] = []
    for project_id, members in groups.items():
        category, category_source = _resolve_folder_category(members, config_path)
        observed.append(
            ObservedProject(
                project_id=project_id,
                last_seen=max(member.end for member in members),
                session_count=len(members),
                agents=tuple(sorted({member.agent for member in members})),
                category=category,
                category_source=category_source,
                relevance=_classify_relevance(project_id, members),
            )
        )
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


def partition_by_relevance(
    projects: Sequence[ObservedProject],
) -> tuple[tuple[ObservedProject, ...], tuple[ObservedProject, ...]]:
    """Split an ordered population into ``(relevant, filtered)`` (#254).

    Pure and order-preserving on both sides: the verdict was already decided
    per row at collection time, so this never re-sorts, never re-reads a
    transcript, and never changes which rows exist — a caller can always
    reconstruct the complete listing by concatenating what it was given.
    """

    relevant = tuple(
        project for project in projects
        if project.relevance == RELEVANCE_RELEVANT
    )
    filtered = tuple(
        project for project in projects
        if project.relevance != RELEVANCE_RELEVANT
    )
    return relevant, filtered


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
    "FILTERED_RELEVANCE_REASONS",
    "JSON_HARD_MAX",
    "RELEVANCE_EPHEMERAL_ROOT",
    "RELEVANCE_RELEVANT",
    "RELEVANCE_SYNTHETIC_DATED_ID",
    "ObservedProject",
    "collect_observed_projects",
    "bounded_observed_projects",
    "is_synthetic_dated_project_id",
    "observed_project_ids",
    "partition_by_relevance",
    "find_close_project_candidates",
]
