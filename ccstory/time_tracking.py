"""Estimate active time per category across registered coding-agent providers.

Active minutes heuristic: sum gaps between consecutive messages capped at 5 min.
Gaps > 5 min treated as "stepped away". Not precise — good enough to see
direction / distribution.

Extracted from ting/personal_os/core/time_tracking.py for ccstory v1.
The only change vs the original: classify() comes from .categorizer (generic
buckets + config.toml override) instead of hardcoded personal rules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePath

from .categorizer import alias_fold, normalize_project_name
from .session_identity import public_session_id

# Note: classify() is no longer called here. parse_session() leaves
# SessionStat.category empty; the caller runs categorizer.resolve_session_bucket().
# alias_fold/normalize_project_name are used only to derive the layer-2 project
# identity for the read-time (area, project) rollup (#69), never to classify.

LOG = logging.getLogger("ccstory.time_tracking")

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
GAP_CAP_SEC = 5 * 60  # gap longer than this → treat as idle

# This version names the facts that participate in a window slice.  It is
# deliberately separate from narrator prompt/cache policy versions: a moving
# ``until=now`` must not invalidate a slice whose bounded facts did not change.
WINDOW_EVIDENCE_POLICY_VERSION = 1
SESSION_SLICE_ID_VERSION = 1


@dataclass(frozen=True)
class ActiveInterval:
    """One inferred, half-open interval of activity in epoch seconds."""

    start: float
    end: float

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("active interval end must be after start")


@dataclass(frozen=True)
class WindowEvidence:
    """Authoritative evidence and active-time facts bounded to one window.

    Providers populate the text/message fields in 0.8-D.  The generic builder
    below is intentionally useful before then: it records only the bounded
    timestamp/interval facts already available from ``SessionStat`` and never
    substitutes an unbounded transcript excerpt.
    """

    timestamps: tuple[float, ...]
    active_intervals: tuple[ActiveInterval, ...]
    msg_count: int
    user_msg_count: int
    first_user_text: str
    latest_user_text: str
    final_assistant_text: str
    excerpt: str
    evidence_fingerprint: str


@dataclass
class SessionSlice:
    """A window-pure view of one physical provider session.

    ``physical_session_id`` remains the public/correction identity.  The
    internal ``session_id`` differs only for a boundary-clipped slice, so an
    auto summary cannot be reused across different bounded evidence.
    """

    physical_session_id: str
    session_id: str
    agent: str
    project: str
    category: str
    start: datetime
    end: datetime
    active_sec: int
    msg_count: int
    user_msg_count: int
    timestamps: list[float]
    active_intervals: tuple[ActiveInterval, ...]
    window_since: datetime
    window_until: datetime
    boundary_clipped: bool
    evidence_excerpt: str
    evidence_fingerprint: str
    evidence: WindowEvidence
    # Keep SessionStat provenance available for the later recap integration
    # without making callers recover provider-specific paths from an id.
    is_scheduled: bool = False
    cwd: str = ""
    path: Path | None = None
    native_title: str = ""
    category_source: str = ""
    physical_engaged: bool = False

    @property
    def active_min(self) -> float:
        return round(self.active_sec / 60, 1)

    @property
    def engaged(self) -> bool:
        """Preserve the collection-time engagement decision for this slice."""

        return self.physical_engaged


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


def _as_utc(value: datetime) -> datetime:
    """Normalize a public window bound without using the host timezone."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validate_window(since: datetime, until: datetime) -> tuple[datetime, datetime]:
    since_utc = _as_utc(since)
    until_utc = _as_utc(until)
    if until_utc <= since_utc:
        raise ValueError("window until must be after since")
    return since_utc, until_utc


def active_intervals_for_timestamps(
    timestamps: list[float] | tuple[float, ...],
) -> tuple[ActiveInterval, ...]:
    """Infer normal activity intervals from one physical session's events."""

    ordered = sorted(set(timestamps))
    intervals: list[ActiveInterval] = []
    for previous, current in zip(ordered, ordered[1:]):
        if current <= previous:
            continue
        intervals.append(ActiveInterval(previous, min(current, previous + GAP_CAP_SEC)))
    return tuple(intervals)


def clip_active_intervals(
    intervals: tuple[ActiveInterval, ...],
    since: datetime,
    until: datetime,
) -> tuple[ActiveInterval, ...]:
    """Intersect inferred intervals with the half-open ``[since, until)`` window."""

    since_utc, until_utc = _validate_window(since, until)
    window_start = since_utc.timestamp()
    window_end = until_utc.timestamp()
    clipped: list[ActiveInterval] = []
    for interval in intervals:
        start = max(interval.start, window_start)
        end = min(interval.end, window_end)
        if end > start:
            clipped.append(ActiveInterval(start, end))
    return tuple(clipped)


def _window_evidence_fingerprint(
    *,
    timestamps: tuple[float, ...],
    active_intervals: tuple[ActiveInterval, ...],
    msg_count: int,
    user_msg_count: int,
    first_user_text: str,
    latest_user_text: str,
    final_assistant_text: str,
    excerpt: str,
) -> str:
    """Hash exactly the bounded facts, never a moving window endpoint."""

    payload = {
        "policy_version": WINDOW_EVIDENCE_POLICY_VERSION,
        "timestamps": timestamps,
        "active_intervals": [(item.start, item.end) for item in active_intervals],
        "msg_count": msg_count,
        "user_msg_count": user_msg_count,
        "first_user_text": first_user_text,
        "latest_user_text": latest_user_text,
        "final_assistant_text": final_assistant_text,
        "excerpt": excerpt,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_window_evidence(
    *,
    timestamps: list[float] | tuple[float, ...],
    active_intervals: tuple[ActiveInterval, ...],
    msg_count: int,
    user_msg_count: int,
    first_user_text: str = "",
    latest_user_text: str = "",
    final_assistant_text: str = "",
    excerpt: str = "",
) -> WindowEvidence:
    """Create a self-identifying bounded evidence value.

    This helper is the provider-neutral cache boundary: it does not inspect a
    transcript, and callers in 0.8-D must pass only authoritative events from
    the requested half-open window.
    """

    normalized_timestamps = tuple(sorted(float(item) for item in timestamps))
    if msg_count < 0 or user_msg_count < 0 or user_msg_count > msg_count:
        raise ValueError("invalid bounded message counts")
    return WindowEvidence(
        timestamps=normalized_timestamps,
        active_intervals=active_intervals,
        msg_count=msg_count,
        user_msg_count=user_msg_count,
        first_user_text=first_user_text,
        latest_user_text=latest_user_text,
        final_assistant_text=final_assistant_text,
        excerpt=excerpt,
        evidence_fingerprint=_window_evidence_fingerprint(
            timestamps=normalized_timestamps,
            active_intervals=active_intervals,
            msg_count=msg_count,
            user_msg_count=user_msg_count,
            first_user_text=first_user_text,
            latest_user_text=latest_user_text,
            final_assistant_text=final_assistant_text,
            excerpt=excerpt,
        ),
    )


def _validate_bounded_evidence(
    evidence: WindowEvidence,
    since: datetime,
    until: datetime,
    expected_intervals: tuple[ActiveInterval, ...],
) -> None:
    """Reject an adapter evidence object that would leak a window boundary."""

    since_utc, until_utc = _validate_window(since, until)
    lower = since_utc.timestamp()
    upper = until_utc.timestamp()
    if any(
        timestamp < lower or timestamp >= upper for timestamp in evidence.timestamps
    ):
        raise ValueError("window evidence contains an out-of-window timestamp")
    if any(
        interval.start < lower or interval.end > upper
        for interval in evidence.active_intervals
    ):
        raise ValueError("window evidence contains an out-of-window interval")
    if evidence.active_intervals != expected_intervals:
        raise ValueError(
            "window evidence intervals do not match clipped physical intervals"
        )
    expected_fingerprint = _window_evidence_fingerprint(
        timestamps=evidence.timestamps,
        active_intervals=evidence.active_intervals,
        msg_count=evidence.msg_count,
        user_msg_count=evidence.user_msg_count,
        first_user_text=evidence.first_user_text,
        latest_user_text=evidence.latest_user_text,
        final_assistant_text=evidence.final_assistant_text,
        excerpt=evidence.excerpt,
    )
    if evidence.evidence_fingerprint != expected_fingerprint:
        raise ValueError("window evidence fingerprint does not match its facts")


def session_slice_for_window(
    session: SessionStat,
    since: datetime,
    until: datetime,
    *,
    evidence: WindowEvidence | None = None,
) -> SessionSlice | None:
    """Build one window-pure slice without reopening a provider transcript.

    The optional ``evidence`` parameter is the explicit hand-off for 0.8-D.
    Before provider-specific bounded extraction lands, the default contains
    only clipped timestamp and active-interval facts; notably, it never copies
    full-session user or assistant prose into a clipped slice.
    """

    since_utc, until_utc = _validate_window(since, until)
    physical_timestamps = tuple(float(item) for item in session.timestamps)
    bounded_timestamps = tuple(
        item
        for item in physical_timestamps
        if since_utc.timestamp() <= item < until_utc.timestamp()
    )
    intervals = clip_active_intervals(
        active_intervals_for_timestamps(physical_timestamps), since_utc, until_utc
    )
    if evidence is None:
        evidence = make_window_evidence(
            timestamps=bounded_timestamps,
            active_intervals=intervals,
            # Timestamp activity is not a provider-neutral message inventory.
            # 0.8-D supplies authoritative role/message counts; zero here is
            # explicitly unknown rather than a fabricated count.
            msg_count=0,
            user_msg_count=0,
        )
    _validate_bounded_evidence(evidence, since_utc, until_utc, intervals)

    # A physical gap may cross the whole window without a direct event inside
    # it.  Its clipped interval is still real window activity and must survive.
    if not evidence.timestamps and not evidence.active_intervals:
        return None

    physical_start = _as_utc(session.start)
    physical_end = _as_utc(session.end)
    boundary_clipped = physical_start < since_utc or physical_end >= until_utc
    if boundary_clipped:
        identity_material = "\x00".join(
            (
                str(SESSION_SLICE_ID_VERSION),
                session.agent,
                session.session_id,
                evidence.evidence_fingerprint,
            )
        ).encode("utf-8")
        internal_id = "slice-" + hashlib.sha256(identity_material).hexdigest()[:24]
    else:
        internal_id = session.session_id

    fact_times = list(evidence.timestamps)
    time_bounds = fact_times + [
        value
        for interval in evidence.active_intervals
        for value in (interval.start, interval.end)
    ]
    start = datetime.fromtimestamp(min(time_bounds), tz=timezone.utc)
    end = datetime.fromtimestamp(max(time_bounds), tz=timezone.utc)

    return SessionSlice(
        physical_session_id=session.session_id,
        session_id=internal_id,
        agent=session.agent,
        project=session.project,
        category=session.category,
        start=start,
        end=end,
        active_sec=int(
            sum(item.end - item.start for item in evidence.active_intervals)
        ),
        msg_count=evidence.msg_count,
        user_msg_count=evidence.user_msg_count,
        timestamps=fact_times,
        active_intervals=evidence.active_intervals,
        window_since=since_utc,
        window_until=until_utc,
        boundary_clipped=boundary_clipped,
        evidence_excerpt=evidence.excerpt,
        evidence_fingerprint=evidence.evidence_fingerprint,
        evidence=evidence,
        is_scheduled=session.is_scheduled,
        cwd=session.cwd,
        path=session.path,
        native_title=session.native_title,
        category_source=session.category_source,
        physical_engaged=session.engaged,
    )


def validate_window_session(
    session: SessionStat | SessionSlice,
    since: datetime,
    until: datetime,
) -> None:
    """Reject a session that does not honor its bounded ``[since, until)``.

    `session_slice_for_window()` already establishes these invariants for the
    slices it builds, but `SessionSlice` and `SessionStat` are plain
    dataclasses that any caller may construct directly. This is the entry
    point for a consumer that receives already-bounded session facts from
    somewhere else and must not trust them blindly.

    Raises ``ValueError`` describing the first violated invariant.
    """

    if not isinstance(session, (SessionStat, SessionSlice)):
        raise ValueError(
            "bounded window sessions must be SessionStat or SessionSlice values"
        )
    since_utc, until_utc = _validate_window(since, until)
    lower = since_utc.timestamp()
    upper = until_utc.timestamp()

    if session.start.tzinfo is None or session.end.tzinfo is None:
        raise ValueError("bounded window sessions must have timezone-aware bounds")

    if isinstance(session, SessionSlice):
        if session.window_since.tzinfo is None or session.window_until.tzinfo is None:
            raise ValueError("SessionSlice bounds must be timezone-aware")
        expected_active_sec = int(
            math.fsum(
                interval.end - interval.start
                for interval in session.active_intervals
            )
        )
        if session.active_sec != expected_active_sec:
            raise ValueError(
                "SessionSlice active_sec must match its bounded intervals"
            )
        if (
            not isinstance(session.evidence, WindowEvidence)
            or tuple(session.timestamps) != session.evidence.timestamps
            or session.evidence_fingerprint != session.evidence.evidence_fingerprint
        ):
            raise ValueError(
                "SessionSlice fields must match its bounded evidence"
            )
        try:
            _validate_bounded_evidence(
                session.evidence,
                since_utc,
                until_utc,
                session.active_intervals,
            )
        except ValueError as exc:
            raise ValueError(
                "SessionSlice contains invalid bounded evidence"
            ) from exc
        if (
            session.window_since.timestamp() != lower
            or session.window_until.timestamp() != upper
        ):
            raise ValueError("SessionSlice bounds must match its window")
        if any(
            interval.start < lower or interval.end > upper
            for interval in session.active_intervals
        ):
            raise ValueError("SessionSlice contains out-of-window activity")
    else:
        expected_active_sec = int(
            math.fsum(
                interval.end - interval.start
                for interval in active_intervals_for_timestamps(session.timestamps)
            )
        )
        if session.active_sec != expected_active_sec:
            raise ValueError("SessionStat active_sec must match its intervals")
        if session.start.timestamp() < lower or session.end.timestamp() >= upper:
            raise ValueError(
                "a cross-window SessionStat must be supplied as a bounded "
                "SessionSlice"
            )

    if any(
        timestamp < lower or timestamp >= upper for timestamp in session.timestamps
    ):
        raise ValueError("bounded window session contains an out-of-window timestamp")


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


def wall_clock_active_sec(
    stats: Sequence[SessionStat | SessionSlice],
) -> int:
    """Return the union of every session's inferred active intervals.

    Each adjacent timestamp pair contributes at most ``GAP_CAP_SEC`` starting
    at the earlier event. Building intervals per session first is essential:
    flattening all timestamps and then measuring adjacent gaps invents active
    time between unrelated sessions and can make ``wall_clock`` exceed the raw
    sum (which in turn produces an impossible parallelism factor below 1).
    """
    intervals: list[tuple[float, float]] = []
    for session in stats:
        # A SessionSlice has already intersected its physical intervals with a
        # report window.  Re-deriving them from timestamps would reintroduce
        # out-of-window time at either boundary.  Legacy SessionStat objects
        # deliberately retain their historical timestamp-derived semantics.
        if isinstance(session, SessionSlice):
            intervals.extend(
                (item.start, item.end) for item in session.active_intervals
            )
            continue
        intervals.extend(
            (item.start, item.end)
            for item in active_intervals_for_timestamps(session.timestamps)
        )

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


def wall_clock_active_min(
    stats: Sequence[SessionStat | SessionSlice],
) -> float:
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
    top_sessions: list[SessionStat | SessionSlice] = field(default_factory=list)
    # Layer-2 rollup, biggest project first. Additive (#69): layer-1 fields
    # above are untouched, so every existing consumer keeps its numbers.
    projects: list[ProjectRollup] = field(default_factory=list)


def _rollup_projects(
    items: Sequence[SessionStat | SessionSlice],
    scale: float,
    aliases: dict[str, str] | None,
) -> list[ProjectRollup]:
    """Second-level (area → project) rollup for one area's sessions.

    Uses the same wall-clock ``scale`` as the parent area so project hours sum
    back to the area total. Groups by the alias-folded project leaf.
    """
    groups: dict[str, list[SessionStat | SessionSlice]] = defaultdict(list)
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
    # Ties broken by name, not left to dict insertion order. Two projects
    # with equal rounded hours are common — a 0.1h resolution collides
    # easily — and without this the order fell out of whichever provider was
    # registered first and whichever file the OS listed first, so the same
    # data rendered differently between runs (#188 0.8-G).
    out.sort(key=lambda p: (-p.active_min, p.project))
    return out


def rollup_by_category(
    stats: Sequence[SessionStat | SessionSlice],
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
    buckets: dict[str, list[SessionStat | SessionSlice]] = defaultdict(list)
    for s in stats:
        buckets[s.category].append(s)

    raw_total = sum(s.active_sec for s in stats)
    if dedup_to_wall_clock and raw_total > 0:
        scale = wall_clock_active_sec(stats) / raw_total
    else:
        scale = 1.0

    rollups: list[CategoryRollup] = []
    for cat, items in buckets.items():
        # Same deterministic-tie-break reasoning as `_rollup_projects`, and
        # the same ordering `report.top_focus_narrative` already uses — this
        # list becomes `top_sessions`, which reaches public output.
        items.sort(
            key=lambda x: (-x.active_sec, x.start, public_session_id(x)),
        )
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
    rollups.sort(key=lambda r: (-r.active_min, r.category))
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
