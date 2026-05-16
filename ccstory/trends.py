"""Multi-period analysis: previous-window comparison + trend mode.

ccstory recomputes from `~/.claude/projects/**/*.jsonl` on every run, so
trends are derived retroactively — no continuous logging needed. A user
installing today can see their full historical trend on day one.

The only thing that benefits from caching is per-session LLM narratives
(expensive to regenerate), and those live in `~/.ccstory/cache.db`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .time_tracking import CategoryRollup, SessionStat, rollup_by_category
from .token_usage import UsageReport, collect_usage

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
    # Session ids in the previous window — needed for synthesize_comparison
    # to fetch the prior-period summaries from cache.db.
    previous_session_ids: list[str] = field(default_factory=list)
    # 1-2 sentence cross-period narrative synthesized via claude -p (#26).
    # Optional — None when synthesis is disabled or unavailable.
    narrative: str | None = None


def previous_window(since: datetime, until: datetime) -> tuple[datetime, datetime]:
    """Same-length window immediately preceding (since, until)."""
    span = until - since
    return since - span, since


def compare_to_previous(
    current_sessions: list[SessionStat],
    current_rollups: list[CategoryRollup],
    current_usage: UsageReport,
    current_label: str,
    since: datetime,
    until: datetime,
) -> PeriodComparison | None:
    """Build a comparison record against the previous same-length window."""
    from .time_tracking import collect_sessions  # local to avoid cycle hassle

    prev_since, prev_until = previous_window(since, until)
    prev_sessions = collect_sessions(prev_since, prev_until)
    if not prev_sessions:
        return None
    prev_rollups = rollup_by_category(prev_sessions)
    prev_usage = collect_usage(prev_since, prev_until)

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
        previous_session_ids=[s.session_id for s in prev_sessions],
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
    """N calendar months ending at the current month. Most recent last."""
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
        start = datetime(y, m, 1)
        nxt = datetime(y + (m // 12), (m % 12) + 1, 1)
        end = min(now, nxt)
        out.append((f"{y}-{m:02d}", start, end))
    return out


def collect_trend(
    period: str = "week",
    count: int = 8,
    now: datetime | None = None,
) -> list[PeriodPoint]:
    """Compute per-period rollups for trend analysis.

    Efficient: scans jsonl ONCE over the full range, then buckets by period
    in-memory rather than re-scanning per window.
    """
    from .time_tracking import collect_sessions

    now = now or datetime.now()
    if period == "week":
        windows = _week_windows(now, count)
    elif period == "month":
        windows = _month_windows(now, count)
    else:
        raise ValueError(f"unsupported trend period: {period}")

    earliest = min(s for _, s, _ in windows)
    all_sessions = collect_sessions(earliest, now)

    points: list[PeriodPoint] = []
    for label, start, end in windows:
        in_window = [
            s for s in all_sessions
            if s.start.replace(tzinfo=None) >= start.replace(tzinfo=None)
            and s.start.replace(tzinfo=None) < end.replace(tzinfo=None)
        ]
        rollups = rollup_by_category(in_window)
        usage = collect_usage(start, end)
        total_h = sum(r.active_min for r in rollups) / 60
        points.append(PeriodPoint(
            label=label,
            since=start,
            until=end,
            rollups=rollups,
            total_h=total_h,
            output_tokens=usage.total_output,
            cost_usd=usage.total_cost_usd,
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
