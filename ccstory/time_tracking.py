"""Estimate active time per category across registered coding-agent providers.

Active minutes heuristic: sum gaps between consecutive messages capped at 5 min.
Gaps > 5 min treated as "stepped away". Not precise — good enough to see
direction / distribution.

Extracted from ting/personal_os/core/time_tracking.py for ccstory v1.
The only change vs the original: classify() comes from .categorizer (generic
buckets + config.toml override) instead of hardcoded personal rules.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePath

from .categorizer import alias_fold, normalize_project_name

# Note: classify() is no longer called here. parse_session() leaves
# SessionStat.category empty; the caller runs categorizer.resolve_session_bucket().
# alias_fold/normalize_project_name are used only to derive the layer-2 project
# identity for the read-time (area, project) rollup (#69), never to classify.

LOG = logging.getLogger("ccstory.time_tracking")

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
GAP_CAP_SEC = 5 * 60  # gap longer than this → treat as idle


@dataclass
class SessionStat:
    project: str
    category: str
    session_id: str
    start: datetime
    end: datetime
    active_sec: int
    msg_count: int
    user_msg_count: int = 0
    first_user_text: str = ""
    is_scheduled: bool = False
    # Working directory recorded in the transcript. Drives artifact/repo
    # attribution (artifacts.py); empty for pre-cwd-era transcripts.
    cwd: str = ""
    timestamps: list[float] = field(default_factory=list)
    # Which resolver layer set `.category`. One of:
    #   "" (unresolved) | "user_rule" | "llm_cache" | "llm_fresh" | "fallback"
    # parse_session() leaves this empty; the caller is expected to run
    # categorizer.resolve_session_bucket() before consuming `.category`.
    category_source: str = ""
    # Which coding agent produced this session — one of providers.list_providers().
    # Defaults to "claude" so SessionStats built by older callers (and by
    # tests that construct them by hand) keep their historical meaning.
    agent: str = "claude"
    # Transcript this stat was parsed from. Lets the summary backfill find the
    # file without re-deriving each agent's on-disk layout.
    path: Path | None = None
    native_title: str = ""

    @property
    def active_min(self) -> float:
        return round(self.active_sec / 60, 1)

    @property
    def engaged(self) -> bool:
        """Did the user actually engage (vs auto-fired / API batch run)?"""
        if self.is_scheduled:
            return self.user_msg_count >= 1
        if self.user_msg_count >= 2:
            return True
        if self.user_msg_count == 1 and self.active_sec >= 60:
            return True
        return False


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _extract_first_user_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                return c.get("text", "") or ""
    return ""


def parse_session(jsonl_path: Path) -> SessionStat | None:
    """Compute active time + metadata for one Claude Code session file.

    Kept as a module-level function because it is part of the semi-stable
    library surface (#110); the parsing itself now lives in
    ``providers.claude.ClaudeCodeProvider``.
    """
    from .providers.claude import ClaudeCodeProvider

    return ClaudeCodeProvider().parse_session(jsonl_path)


def collect_sessions(
    since: datetime,
    until: datetime | None = None,
    engaged_only: bool = True,
    agent: str = "all",
) -> list[SessionStat]:
    """All sessions overlapping [since, until). until=None means now.

    Integration API (semi-stable, #110) — see README "Library usage".

    `since` and `until` may be tz-aware or naive. Naive values are treated
    as UTC so the comparison against tz-aware jsonl timestamps remains
    well-defined. Callers that care about local-midnight boundaries (e.g.
    cli._parse_arg) should pass tz-aware datetimes.

    `agent` selects which coding agent's sessions to include: ``all``
    (default), or any id from ``providers.list_providers()``. Sessions from
    different agents overlap in wall-clock time — sum `.active_sec` across
    them and you get parallel work counted twice; use
    `wall_clock_active_sec` for any figure presented as a duration.
    """
    from .providers import collect_multi_agent_sessions

    return collect_multi_agent_sessions(
        since, until, engaged_only=engaged_only, agent=agent
    )


def collect_sessions_for_windows(
    windows: Mapping[str, tuple[datetime, datetime]],
    engaged_only: bool = True,
    agent: str = "all",
) -> dict[str, list[SessionStat]]:
    """Collect several bounded windows with one provider scan.

    A normal recap needs both its current window and the immediately preceding
    one for comparison. Calling :func:`collect_sessions` twice means every
    provider re-enumerates and reparses much of the same immutable transcript
    set. Scan their enclosing range once, then apply the exact overlap rule
    that each individual collection uses. Sessions crossing a boundary stay
    in both windows, preserving the existing comparison semantics.

    This is intentionally an in-memory, invocation-local snapshot. It never
    treats an mtime as a durable truth for token/session data, which is
    especially important for resumed Codex rollout files.
    """
    if not windows:
        return {}

    earliest = min(since for since, _until in windows.values())
    latest = max(until for _since, until in windows.values())
    scanned = collect_sessions(
        earliest, latest, engaged_only=engaged_only, agent=agent,
    )

    by_window: dict[str, list[SessionStat]] = {}
    for key, (since, until) in windows.items():
        by_window[key] = [
            session
            for session in scanned
            if session.end >= since and session.start < until
        ]
    return by_window


def _is_subagent_path(path: PurePath) -> bool:
    """Detect the exact ``subagents`` component on any pathlib flavor."""
    return "subagents" in path.parts


def wall_clock_active_sec(stats: list[SessionStat]) -> int:
    """Return the union of every session's inferred active intervals.

    Each adjacent timestamp pair contributes at most ``GAP_CAP_SEC`` starting
    at the earlier event. Building intervals per session first is essential:
    flattening all timestamps and then measuring adjacent gaps invents active
    time between unrelated sessions and can make ``wall_clock`` exceed the raw
    sum (which in turn produces an impossible parallelism factor below 1).
    """
    intervals: list[tuple[float, float]] = []
    for session in stats:
        timestamps = sorted(set(session.timestamps))
        for prev, curr in zip(timestamps, timestamps[1:]):
            if curr <= prev:
                continue
            intervals.append((prev, min(curr, prev + GAP_CAP_SEC)))

    if not intervals:
        return 0

    intervals.sort()
    active = 0.0
    merged_start, merged_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= merged_end:
            merged_end = max(merged_end, end)
            continue
        active += merged_end - merged_start
        merged_start, merged_end = start, end
    active += merged_end - merged_start
    return int(active)


def wall_clock_active_min(stats: list[SessionStat]) -> float:
    return round(wall_clock_active_sec(stats) / 60, 1)


@dataclass
class ProjectRollup:
    """Layer-2 (#69): one project's slice within an area.

    Computed at read time from the sessions already in hand — no cache row,
    no fingerprint, no migration. ``project`` is the alias-folded normalized
    leaf (``categorizer.project_identity``); the physical fact of which folder
    the work happened in, never a content/LLM override.
    """
    project: str
    active_min: float
    sessions: int
    messages: int


@dataclass
class CategoryRollup:
    category: str
    active_min: float
    sessions: int
    messages: int
    top_sessions: list[SessionStat] = field(default_factory=list)
    # Layer-2 rollup, biggest project first. Additive (#69): layer-1 fields
    # above are untouched, so every existing consumer keeps its numbers.
    projects: list[ProjectRollup] = field(default_factory=list)


def _rollup_projects(
    items: list[SessionStat],
    scale: float,
    aliases: dict[str, str] | None,
) -> list[ProjectRollup]:
    """Second-level (area → project) rollup for one area's sessions.

    Uses the same wall-clock ``scale`` as the parent area so project hours sum
    back to the area total. Groups by the alias-folded project leaf.
    """
    groups: dict[str, list[SessionStat]] = defaultdict(list)
    for s in items:
        leaf = alias_fold(normalize_project_name(s.project) or s.project, aliases)
        groups[leaf].append(s)
    out: list[ProjectRollup] = []
    for proj, sess in groups.items():
        proj_sec = sum(i.active_sec for i in sess) * scale
        out.append(
            ProjectRollup(
                project=proj,
                active_min=round(proj_sec / 60, 1),
                sessions=len(sess),
                messages=sum(i.msg_count for i in sess),
            )
        )
    out.sort(key=lambda p: p.active_min, reverse=True)
    return out


def rollup_by_category(
    stats: list[SessionStat],
    dedup_to_wall_clock: bool = True,
    aliases: dict[str, str] | None = None,
) -> list[CategoryRollup]:
    """Aggregate by category (layer 1), with a read-time per-project breakdown
    (layer 2, #69) attached to each rollup.

    Integration API (semi-stable, #110) — see README "Library usage".

    ``aliases`` is the optional ``[projects]`` fold map (``categorizer.
    load_project_aliases``); pass it so layer-2 groups variant folder names
    under one canonical project. Layer-1 numbers are independent of it — the
    ``projects`` field is purely additive, so trend/compare (which omit it)
    keep byte-identical area totals.
    """
    buckets: dict[str, list[SessionStat]] = defaultdict(list)
    for s in stats:
        buckets[s.category].append(s)

    raw_total = sum(s.active_sec for s in stats)
    if dedup_to_wall_clock and raw_total > 0:
        scale = wall_clock_active_sec(stats) / raw_total
    else:
        scale = 1.0

    rollups: list[CategoryRollup] = []
    for cat, items in buckets.items():
        items.sort(key=lambda x: x.active_sec, reverse=True)
        cat_sec = sum(i.active_sec for i in items) * scale
        rollups.append(
            CategoryRollup(
                category=cat,
                active_min=round(cat_sec / 60, 1),
                sessions=len(items),
                messages=sum(i.msg_count for i in items),
                top_sessions=items[:5],
                projects=_rollup_projects(items, scale, aliases),
            )
        )
    rollups.sort(key=lambda r: r.active_min, reverse=True)
    return rollups


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cutoff = datetime.now() - timedelta(days=7)
    sessions = collect_sessions(cutoff)
    roll = rollup_by_category(sessions)
    total_min = sum(r.active_min for r in roll)
    print(
        f"\n=== Past 7 days: {total_min:.0f} active min "
        f"({total_min/60:.1f} h) across {len(sessions)} sessions ===\n"
    )
    for r in roll:
        pct = (r.active_min / total_min * 100) if total_min else 0
        print(
            f"  {r.category:14s} {r.active_min:7.1f} min  {pct:5.1f}%  "
            f"{r.sessions:3d} sess  {r.messages:5d} msg"
        )
