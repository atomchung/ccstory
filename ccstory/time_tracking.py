"""Parse ~/.claude/projects/**/*.jsonl to estimate active time per category.

Active minutes heuristic: sum gaps between consecutive messages capped at 5 min.
Gaps > 5 min treated as "stepped away". Not precise — good enough to see
direction / distribution.

Extracted from ting/personal_os/core/time_tracking.py for ccstory v1.
The only change vs the original: classify() comes from .categorizer (generic
buckets + config.toml override) instead of hardcoded personal rules.
"""

from __future__ import annotations

import glob
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .categorizer import classify

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
    timestamps: list[float] = field(default_factory=list)

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
    """Compute active time + metadata for one session file."""
    timestamps: list[datetime] = []
    msg_count = 0
    user_msg_count = 0
    first_user_text = ""
    is_scheduled = False
    first_raw_user_seen = False

    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = d.get("type")
                if role not in ("user", "assistant"):
                    continue
                msg_count += 1
                ts = _parse_ts(d.get("timestamp"))
                if ts:
                    timestamps.append(ts)
                if role == "user":
                    content = d.get("message", {}).get("content", "")
                    text = _extract_first_user_text(content).strip()
                    if not first_raw_user_seen and text:
                        first_raw_user_seen = True
                        if text.startswith("<scheduled-task"):
                            is_scheduled = True
                    is_real_user = (
                        text
                        and not text.startswith("<")
                        and "tool_use_id" not in text
                    )
                    if is_real_user:
                        user_msg_count += 1
                        if not first_user_text:
                            first_user_text = text[:200]
    except OSError:
        return None

    if not timestamps:
        return None

    timestamps.sort()
    active_sec = 0
    for prev, curr in zip(timestamps, timestamps[1:]):
        gap = (curr - prev).total_seconds()
        active_sec += min(gap, GAP_CAP_SEC)

    try:
        proj_dir = jsonl_path.relative_to(CLAUDE_PROJECTS).parts[0]
    except ValueError:
        proj_dir = jsonl_path.parent.name

    return SessionStat(
        project=proj_dir,
        category=classify(proj_dir),
        session_id=jsonl_path.stem,
        start=timestamps[0],
        end=timestamps[-1],
        active_sec=int(active_sec),
        msg_count=msg_count,
        user_msg_count=user_msg_count,
        first_user_text=first_user_text,
        is_scheduled=is_scheduled,
        timestamps=[t.timestamp() for t in timestamps],
    )


def collect_sessions(
    since: datetime,
    until: datetime | None = None,
    engaged_only: bool = True,
) -> list[SessionStat]:
    """All sessions overlapping [since, until). until=None means now.

    `since` and `until` may be tz-aware or naive. Naive values are treated
    as UTC so the comparison against tz-aware jsonl timestamps remains
    well-defined. Callers that care about local-midnight boundaries (e.g.
    cli._parse_arg) should pass tz-aware datetimes.
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)

    stats: list[SessionStat] = []
    since_ts = since.timestamp()

    for path_str in glob.glob(str(CLAUDE_PROJECTS / "**" / "*.jsonl"), recursive=True):
        path = Path(path_str)
        # Skip nested subagent traces (double-count guard)
        if "/subagents/" in path_str:
            continue
        try:
            if path.stat().st_mtime < since_ts:
                continue
        except OSError:
            continue

        s = parse_session(path)
        if not s:
            continue
        if s.end < since:
            continue
        if until is not None and s.start >= until:
            continue
        if engaged_only and not s.engaged:
            continue
        stats.append(s)
    return stats


def wall_clock_active_sec(stats: list[SessionStat]) -> int:
    """Dedup overlapping active periods across all sessions."""
    all_ts = sorted(t for s in stats for t in s.timestamps)
    if len(all_ts) < 2:
        return 0
    active = 0
    for prev, curr in zip(all_ts, all_ts[1:]):
        gap = curr - prev
        if gap <= 0:
            continue
        active += min(gap, GAP_CAP_SEC)
    return int(active)


def wall_clock_active_min(stats: list[SessionStat]) -> float:
    return round(wall_clock_active_sec(stats) / 60, 1)


@dataclass
class CategoryRollup:
    category: str
    active_min: float
    sessions: int
    messages: int
    top_sessions: list[SessionStat] = field(default_factory=list)


def rollup_by_category(
    stats: list[SessionStat], dedup_to_wall_clock: bool = True
) -> list[CategoryRollup]:
    """Aggregate by category, optionally scaled to deduplicated wall clock."""
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
