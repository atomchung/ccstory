"""Multi-period analysis: previous-window comparison + trend mode.

ccstory recomputes from `~/.claude/projects/**/*.jsonl` on every run, so
trends are derived retroactively — no continuous logging needed. A user
installing today can see their full historical trend on day one.

The only thing that benefits from caching is per-session LLM narratives
(expensive to regenerate), and those live in `~/.ccstory/cache.db`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .categorizer import builtin_or_fallback, resolve_session_bucket
from .local_time import system_local_timezone
from .providers import collect_provider_snapshot
from .session_identity import evidence_session_id, public_session_id
from .time_tracking import CategoryRollup, SessionSlice, SessionStat, rollup_by_category
from .token_usage import UsageReport, collect_usage


def _resolve_sessions_from_cache(
    sessions: Sequence[SessionStat | SessionSlice],
    mode: str,
    fallback: str,
) -> None:
    """Mutate `sessions[*].category` + `.category_source` using cache only.

    Used by ``compare_to_previous`` and ``collect_trend``: these paths must not
    fire fresh LLM calls (would surprise the user with cost). A ``needs_llm``
    cache miss is resolved the same way ``recap._resolve_all_sessions`` does
    once fresh content classification is off the table: hybrid mode still
    owes the deterministic built-in folder tier (``builtin_or_fallback`` —
    #214) before the scalar fallback; content mode collapses straight to
    fallback, staying content-only.

    Reads cache once for all sessions to avoid N SQLite queries. Content
    classification is an automatic derivation, so it is keyed by evidence
    identity: two slices of one physical session describe different work and
    may legitimately land in different buckets.
    """
    from .session_summarizer import _classify_cache_get_many
    if not sessions:
        return
    cache_map = _classify_cache_get_many(
        [evidence_session_id(s) for s in sessions]
    )
    for s in sessions:
        bucket, source = resolve_session_bucket(
            s.project,
            cache_map.get(evidence_session_id(s)),
            mode=mode,
            fallback=fallback,
        )
        if bucket is None:
            # needs_llm signal collapsed in cache-only mode: hybrid applies
            # the built-in tier first, content goes straight to fallback.
            bucket, source = builtin_or_fallback(
                s.project, mode=mode, fallback=fallback,
            )
        s.category = bucket
        s.category_source = source

# 8-step sparkline. Wider range than the common 8 just below makes height
# differences readable even when values are close.
SPARK_BARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int | None = None) -> str:
    """Render a single-line block sparkline. Empty input → empty string."""
    if not values:
        return ""
    if width and width != len(values):
        # If we want a fixed width, sample / pad — keep things simple and just
        # take last `width` points (most recent on the right).
        values = values[-width:]
    mn, mx = min(values), max(values)
    if mx == mn:
        # Flat line at mid-height; "no change" reads better than a flat 0.
        return SPARK_BARS[3] * len(values) if mx > 0 else SPARK_BARS[0] * len(values)
    rng = mx - mn
    return "".join(
        SPARK_BARS[min(len(SPARK_BARS) - 1, int((v - mn) / rng * len(SPARK_BARS)))]
        for v in values
    )


# ----- Previous-window comparison ---------------------------------------------

@dataclass
class CategoryDelta:
    category: str
    current_min: float
    previous_min: float

    @property
    def delta_min(self) -> float:
        return self.current_min - self.previous_min

    @property
    def pct_change(self) -> float | None:
        if self.previous_min <= 0:
            return None  # cannot compute %; show "new"
        return (self.current_min - self.previous_min) / self.previous_min * 100


@dataclass
class PeriodComparison:
    current_label: str
    previous_label: str
    deltas: list[CategoryDelta]
    current_total_h: float
    previous_total_h: float
    current_output_tokens: int
    previous_output_tokens: int
    current_cost_usd: float
    previous_cost_usd: float
    # Public physical session ids in the previous window. Kept as the stable,
    # publishable identity; ``previous_summary_keys()`` is what cache.db
    # lookups must use.
    previous_session_ids: list[str] = field(default_factory=list)
    # 1-2 sentence cross-period narrative synthesized via claude -p (#26).
    # Optional — None when synthesis is disabled or unavailable.
    narrative: str | None = None
    # Additive MCP cost-integrity metadata. Kept after the pre-0.7.1 fields so
    # positional construction of this semi-stable dataclass stays compatible.
    current_provider_coverage: dict[str, str] = field(default_factory=dict)
    previous_provider_coverage: dict[str, str] = field(default_factory=dict)
    current_unpriced_models: list[str] = field(default_factory=list)
    previous_unpriced_models: list[str] = field(default_factory=list)
    # The previous window's session objects. Additive and appended last so
    # positional construction of this semi-stable dataclass stays compatible.
    # A bare id list cannot recover the physical identity of a window slice,
    # so the comparison lane needs the objects to resolve summaries correctly.
    previous_sessions: list = field(default_factory=list)

    def previous_summary_keys(self) -> list[str]:
        """cache.db keys for the previous window's summaries, in order.

        Prefers the carried session objects (which know both identity lanes)
        and falls back to ``previous_session_ids`` for callers that built this
        comparison without them — for those, public and evidence identity are
        the same string anyway.
        """
        if self.previous_sessions:
            return [evidence_session_id(s) for s in self.previous_sessions]
        return list(self.previous_session_ids)


def previous_window(since: datetime, until: datetime) -> tuple[datetime, datetime]:
    """Same-length window immediately preceding (since, until)."""
    span = until - since
    return since - span, since


def compare_to_previous(
    current_sessions: Sequence[SessionStat | SessionSlice],
    current_rollups: list[CategoryRollup],
    current_usage: UsageReport,
    current_label: str,
    since: datetime,
    until: datetime,
    mode: str = "hybrid",
    fallback: str = "coding",
    agent: str = "all",
    previous_sessions: Sequence[SessionStat | SessionSlice] | None = None,
    previous_usage: UsageReport | None = None,
) -> PeriodComparison | None:
    """Build a comparison record against the previous same-length window.

    Bug #61 fix: previous-window sessions go through the same priority chain
    as current-window sessions (``resolve_session_bucket``), reading the LLM
    cache that earlier ``ccstory week`` runs populated. Cache miss falls to
    ``fallback`` rather than firing fresh LLM calls — comparison mode must
    not surprise the user with token spend.

    ``mode``, ``fallback`` and ``agent`` should mirror the caller's main-flow
    settings so current and previous windows resolve to the same vocabulary —
    and, under an ``--agent`` filter, cover the same set of agents.
    """
    from .time_tracking import collect_sessions  # local to avoid cycle hassle

    prev_since, prev_until = previous_window(since, until)
    # A recap can supply an invocation-local snapshot that already scanned
    # both adjacent windows. Keep the standalone API's old collection path
    # for callers that only have the current window in hand.
    prev_sessions = previous_sessions
    if prev_sessions is None:
        prev_sessions = collect_sessions(prev_since, prev_until, agent=agent)
    if not prev_sessions:
        return None
    _resolve_sessions_from_cache(prev_sessions, mode=mode, fallback=fallback)
    prev_rollups = rollup_by_category(prev_sessions)
    prev_usage = previous_usage or collect_usage(
        prev_since,
        prev_until,
        agent=agent,
        active_agents={session.agent for session in prev_sessions},
    )

    cats = {r.category for r in current_rollups} | {r.category for r in prev_rollups}
    cur_by_cat = {r.category: r.active_min for r in current_rollups}
    prev_by_cat = {r.category: r.active_min for r in prev_rollups}
    deltas = [
        CategoryDelta(
            category=cat,
            current_min=cur_by_cat.get(cat, 0.0),
            previous_min=prev_by_cat.get(cat, 0.0),
        )
        for cat in cats
    ]
    deltas.sort(key=lambda d: -d.current_min)

    return PeriodComparison(
        current_label=current_label,
        previous_label=f"{prev_since.date()} → {prev_until.date()}",
        deltas=deltas,
        current_total_h=sum(r.active_min for r in current_rollups) / 60,
        previous_total_h=sum(r.active_min for r in prev_rollups) / 60,
        current_output_tokens=current_usage.total_output,
        previous_output_tokens=prev_usage.total_output,
        current_cost_usd=current_usage.total_cost_usd,
        previous_cost_usd=prev_usage.total_cost_usd,
        current_provider_coverage=current_usage.provider_coverage,
        previous_provider_coverage=prev_usage.provider_coverage,
        current_unpriced_models=current_usage.unpriced_models,
        previous_unpriced_models=prev_usage.unpriced_models,
        previous_session_ids=[public_session_id(s) for s in prev_sessions],
        previous_sessions=list(prev_sessions),
    )


# ----- Trend mode --------------------------------------------------------------

@dataclass
class PeriodPoint:
    label: str
    since: datetime
    until: datetime
    rollups: list[CategoryRollup]
    total_h: float
    output_tokens: int
    cost_usd: float
    provider_coverage: dict[str, str] = field(default_factory=dict)
    unpriced_models: list[str] = field(default_factory=list)

    def quota_pct(self, monthly_quota_usd: float) -> float:
        """API-equiv cost as % of the prorated monthly quota (1.0 = 100%)."""
        if monthly_quota_usd <= 0:
            return 0.0
        days = max(1.0, (self.until - self.since).total_seconds() / 86400)
        prorated = monthly_quota_usd * (days / 30.0)
        return self.cost_usd / prorated if prorated else 0.0


def _week_windows(now: datetime, count: int) -> list[tuple[str, datetime, datetime]]:
    """N rolling 7-day windows ending at `now`. Most recent last."""
    out = []
    for i in range(count - 1, -1, -1):
        end = now - timedelta(days=7 * i)
        start = end - timedelta(days=7)
        iso = end.isocalendar()
        label = f"{iso[0]}-W{iso[1]:02d}"
        out.append((label, start, end))
    return out


def _month_windows(now: datetime, count: int) -> list[tuple[str, datetime, datetime]]:
    """N calendar months ending at the current month. Most recent last.

    Month boundaries inherit `now.tzinfo` so a tz-aware `now` produces
    tz-aware window starts/ends. Naive `now` produces naive windows.
    """
    out = []
    # Walk back N months
    year, month = now.year, now.month
    months = []
    for _ in range(count):
        months.append((year, month))
        if month == 1:
            year, month = year - 1, 12
        else:
            month -= 1
    months.reverse()
    for y, m in months:
        start = datetime(y, m, 1, tzinfo=now.tzinfo)
        nxt = datetime(y + (m // 12), (m % 12) + 1, 1, tzinfo=now.tzinfo)
        end = min(now, nxt)
        out.append((f"{y}-{m:02d}", start, end))
    return out


def collect_trend(
    period: str = "week",
    count: int = 8,
    now: datetime | None = None,
    mode: str = "hybrid",
    fallback: str = "coding",
    agent: str = "all",
) -> list[PeriodPoint]:
    """Compute per-period rollups for trend analysis.

    Window-pure (#188): every period is handed to one
    ``collect_provider_snapshot()`` call as a single window map, exactly like
    a recap's current/previous pair. A session crossing a period boundary
    therefore contributes its own bounded ``SessionSlice`` facts to each
    period it touches, instead of being assigned wholesale to whichever
    period holds its physical start time. A session fully inside one period
    keeps its plain ``SessionStat`` and its output is unchanged — the
    snapshot only converts a session that actually crosses a boundary.

    Resolves every session's bucket through the same priority chain as
    ``ccstory week`` (cache-only; no fresh LLM in trend mode), then groups by
    window. Per-period token/cost figures come from
    ``ProviderSnapshot.usage_by_window`` rather than a separate
    ``collect_usage()`` call per period, so the whole range's transcripts are
    scanned once instead of once per period.

    ``agent`` mirrors the recap's ``--agent`` filter so a trend and a week over
    the same range describe the same population — without it, a Claude-only
    week would sit next to an every-agent trend line.
    """
    # A trend walks back over historical periods, so its timezone must carry
    # historical DST rules rather than the offset in effect right now (#233).
    now = now or datetime.now(system_local_timezone())
    if now.tzinfo is None:
        # Naive caller input is local wall time, per the existing convention.
        now = now.replace(tzinfo=system_local_timezone())
    if period == "week":
        windows = _week_windows(now, count)
    elif period == "month":
        windows = _month_windows(now, count)
    else:
        raise ValueError(f"unsupported trend period: {period}")

    window_map = {label: (start, end) for label, start, end in windows}
    snapshot = collect_provider_snapshot(window_map, agent=agent)

    # Bulk-resolve every period's sessions in one cache lookup. A
    # boundary-crossing session now appears as two distinct slice objects —
    # one per period it touches, each under its own evidence id
    # (session_identity.py) — so flattening across periods first keeps this
    # the single SQL query the original single-pass design relied on,
    # rather than one per window.
    all_sessions = [
        session
        for sessions in snapshot.sessions_by_window.values()
        for session in sessions
    ]
    _resolve_sessions_from_cache(all_sessions, mode=mode, fallback=fallback)

    points: list[PeriodPoint] = []
    for label, start, end in windows:
        in_window = snapshot.sessions_by_window[label]
        rollups = rollup_by_category(in_window)
        usage = snapshot.usage_by_window[label]
        total_h = sum(r.active_min for r in rollups) / 60
        points.append(PeriodPoint(
            label=label,
            since=start,
            until=end,
            rollups=rollups,
            total_h=total_h,
            output_tokens=usage.total_output,
            cost_usd=usage.total_cost_usd,
            provider_coverage=usage.provider_coverage,
            unpriced_models=usage.unpriced_models,
        ))
    return points


def trend_by_category(points: list[PeriodPoint]) -> dict[str, list[float]]:
    """{ category: [active_h_per_period, ...] } aligned to `points` order."""
    cats: set[str] = set()
    for p in points:
        cats.update(r.category for r in p.rollups)
    out: dict[str, list[float]] = {}
    for cat in cats:
        series = []
        for p in points:
            cat_min = next((r.active_min for r in p.rollups if r.category == cat), 0.0)
            series.append(cat_min / 60)
        out[cat] = series
    # sort by total hours desc so biggest categories appear first
    return dict(sorted(out.items(), key=lambda kv: -sum(kv[1])))
