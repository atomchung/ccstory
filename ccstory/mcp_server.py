"""MCP server exposing ccstory's recap/comparison/category data as tools (#35).

Lets any MCP-aware agent (Claude Desktop, Claude Code, or another local
agent) query ccstory live in conversation instead of shelling out to the
CLI and re-parsing Markdown. Every tool here is a thin wrapper over the
same semi-stable functions the Python library API exposes (`build_recap()`
in `recap.py`, `compare_to_previous()` in `trends.py`, `load_rules()` in
`categorizer.py`) — this module adds protocol plumbing and a *third*,
purposely smaller JSON shape on top, not new business logic.

Compact JSON is a distinct contract from the other two ccstory already
documents (the semi-stable function signatures, and the `--json` /
`RecapResult.to_json()` envelope): it drops the full per-session list down
to a handful of top sessions, and never returns raw transcript text. See
README "MCP server" for the full field reference.

v0 scope (deliberately): read-only, no fresh `claude -p` calls unless the
caller opts in (`get_recap`'s `allow_llm`; `compare_to_previous` and
`get_trend` never fire one at all — see their docstrings for where that
guarantee actually comes from), stdio transport only. `get_trend` landed
last, after the other three tools' conventions had settled (the ordering
issue #35 asked for), and follows them: compact points, cache-only bucket
resolution, cost figures behind the same config [prices] override step.
"""

from __future__ import annotations

import logging
import sys
from typing import Literal

# Must run before FastMCP(...) below: its __init__ unconditionally calls
# the SDK's own configure_logging(), which installs a RichHandler on the
# *root* logger and drops its effective level to INFO — a surprise side
# effect of merely importing this module (e.g. during pytest collection).
# logging.basicConfig() is a no-op once the root logger already has a
# handler, so calling it here first (matching cli.py's own WARNING
# convention) pre-empts that.
logging.basicConfig(level=logging.WARNING)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from . import recap  # noqa: E402 — module import so recap.CONFIG_PATH reads live (test monkeypatches target the attribute, not a copied value)
from .categorizer import load_rules, load_settings, normalize_project_name  # noqa: E402
from .session_summarizer import CacheUnavailable  # noqa: E402
from .recap import RecapUnavailable, build_recap, parse_window  # noqa: E402
from .time_tracking import collect_sessions, rollup_by_category  # noqa: E402
from .token_usage import apply_prices, collect_usage, load_prices_config  # noqa: E402
from .trends import _resolve_sessions_from_cache  # noqa: E402
from .trends import collect_trend  # noqa: E402
from .trends import compare_to_previous as _compare_to_previous  # noqa: E402

mcp = FastMCP("ccstory")

Window = Literal["week", "month", "all"] | str
Classify = Literal["folder", "content", "hybrid"]

# "folder" is the only classify mode that never fires an LLM call (content/
# hybrid batch-classify claude -p on cache misses) — same choice
# scripts/refresh_claude.py makes for the same reason. get_recap's
# `allow_llm` only gates narrative polish; without this default, an agent
# calling get_recap with default args could still trigger a surprise batch
# claude -p call via classification, which is exactly what issue #35 asks
# MCP tools to avoid by default.
_DEFAULT_CLASSIFY: Classify = "folder"

# Errors every tool below normalizes into {"ok": False, "error": ...} instead
# of letting propagate. CacheUnavailable is what _connect() raises on a
# corrupt/locked/newer cache.db since #119. SystemExit stays as
# belt-and-braces (it subclasses BaseException, so FastMCP's own `except
# Exception` safety net does NOT catch it — an uncaught SystemExit here
# would kill the whole server process over one bad tool call).
_TOOL_ERRORS = (ValueError, RecapUnavailable, CacheUnavailable, SystemExit)


def _normalize_error(e: BaseException) -> dict:
    """{"ok": False, "error": ...} shape shared by every tool's except clause.

    SystemExit's own str() is frequently just an exit code ("1") —
    session_summarizer._connect() attaches the actually-useful diagnostic
    via `raise SystemExit(1) from e`, which lands in __cause__, not in the
    SystemExit's own args. Prefer that when present so callers see the real
    message instead of a bare "1".
    """
    if isinstance(e, SystemExit) and e.__cause__ is not None:
        return {"ok": False, "error": str(e.__cause__)}
    return {"ok": False, "error": str(e)}


def _compact_recap(result) -> dict:
    top = sorted(result.sessions, key=lambda s: -s.active_min)[:5]
    return {
        "ok": True,
        "label": result.label,
        "since": result.since.isoformat(),
        "until": result.until.isoformat(),
        "active_hours": round(sum(r.active_min for r in result.rollups) / 60, 2),
        "categories": [
            {
                "name": r.category,
                "active_hours": round(r.active_min / 60, 2),
                "narrative": result.category_narratives.get(r.category),
            }
            for r in result.rollups
        ],
        "top_focus": result.overall_narrative,
        "top_sessions": [
            {
                "id": s.session_id,
                "project": normalize_project_name(s.project) or s.project,
                "active_hours": round(s.active_min / 60, 2),
                "summary": (
                    result.summaries[s.session_id].summary
                    if s.session_id in result.summaries else None
                ),
            }
            for s in top
        ],
        "cost_usd": round(result.usage.total_cost_usd, 2),
        "report_path": str(result.report_path) if result.report_path else None,
    }


def _compact_comparison(cmp) -> dict:
    return {
        "ok": True,
        "current_label": cmp.current_label,
        "previous_label": cmp.previous_label,
        "current_active_hours": round(cmp.current_total_h, 2),
        "previous_active_hours": round(cmp.previous_total_h, 2),
        "current_cost_usd": round(cmp.current_cost_usd, 2),
        "previous_cost_usd": round(cmp.previous_cost_usd, 2),
        # Always None in v0: this tool never runs claude -p synthesis —
        # matches "no fresh LLM calls" for this tool (see its docstring).
        "narrative": cmp.narrative,
        "deltas": [
            {
                "category": d.category,
                "current_hours": round(d.current_min / 60, 2),
                "previous_hours": round(d.previous_min / 60, 2),
                "pct_change": (
                    round(d.pct_change, 1) if d.pct_change is not None else None
                ),
            }
            for d in cmp.deltas
        ],
    }


@mcp.tool()
def get_recap(
    window: Window = "month",
    classify: Classify = _DEFAULT_CLASSIFY,
    allow_llm: bool = False,
) -> dict:
    """Recap totals, per-category breakdown, and the overall narrative for
    one window (week | month | all | YYYY-MM). Read-only, compact JSON —
    top 5 sessions only, not the full list. Default `classify="folder"`
    and `allow_llm=False` never fire `claude -p` — this gates *every*
    synthesis step (per-session polish, the overall narrative, and the
    per-category narratives), so `top_focus` and each category's
    `narrative` are null unless `allow_llm=True`. Pass `classify="content"`
    or `"hybrid"`, and/or `allow_llm=True`, to opt into LLM-assisted
    classification / narrative synthesis (slower, may cost tokens).
    """
    try:
        result = build_recap(
            window,
            classify=classify,
            llm_narrative=allow_llm,
            aggregate=allow_llm,
            narrative=("both" if allow_llm else "overall"),
            compare=False,
            artifacts=False,
            write_report=False,
            console=None,
        )
    except _TOOL_ERRORS as e:
        return _normalize_error(e)
    return _compact_recap(result)


@mcp.tool()
def compare_to_previous(
    window: Window = "week", classify: Classify = _DEFAULT_CLASSIFY,
) -> dict:
    """Compare one window against the immediately preceding same-length
    window: active-hours deltas per category, cost deltas. Not supported
    for `window="all"` (there is no meaningful previous window for an
    open-ended range).

    Never fires a fresh `claude -p` call — unlike `get_recap`, this is not
    conditional on any parameter here: both windows' sessions are always
    resolved cache-only (this tool's own choice, not an intrinsic property
    of the classify mode), so `narrative` is always null (use `get_recap`
    with `allow_llm=True` for a synthesized narrative).
    """
    if window == "all":
        return {
            "ok": False,
            "error": "compare_to_previous does not support window='all' "
                     "(no meaningful previous window to compare against).",
        }
    try:
        since, until, label = parse_window(window)
        sessions = collect_sessions(since, until)
        if not sessions:
            return {"ok": False, "error": "No engaged sessions in this window."}
        fallback_bucket = load_settings().get("default_bucket", "coding")
        # Sessions come back from collect_sessions() with .category unset —
        # resolve_session_bucket() (via the cache-only helper, same one
        # compare_to_previous() below uses for the *previous* window) has
        # to run before rollup_by_category() groups them, or every session
        # silently lands in the same empty-string bucket.
        _resolve_sessions_from_cache(sessions, mode=classify, fallback=fallback_bucket)
        rollups = rollup_by_category(sessions)
        # Same price-override step build_recap() (recap.py) and _run_trend()
        # (cli.py) both take before computing cost — skipping it would leave
        # collect_usage() pricing off of whatever DEFAULT_PRICES/config
        # overrides some *other* call in this process happened to apply.
        prices, snapshot = load_prices_config(recap.CONFIG_PATH)
        apply_prices(prices, snapshot)
        usage = collect_usage(since, until)
        cmp = _compare_to_previous(
            current_sessions=sessions,
            current_rollups=rollups,
            current_usage=usage,
            current_label=label,
            since=since,
            until=until,
            mode=classify,
            fallback=fallback_bucket,
        )
    except _TOOL_ERRORS as e:
        return _normalize_error(e)
    if cmp is None:
        return {"ok": False, "error": "No sessions in the previous window to compare."}
    return _compact_comparison(cmp)


@mcp.tool()
def get_trend(
    period: Literal["week", "month"] = "week",
    count: int = 8,
    classify: Classify = _DEFAULT_CLASSIFY,
) -> dict:
    """Per-period activity series over the last `count` weeks or months —
    active hours, cost, and per-category hours for each window, oldest
    first. `count` is clamped to 1..24.

    Never fires a fresh `claude -p` call — like `compare_to_previous`,
    this holds for every parameter combination: `collect_trend()` resolves
    buckets cache-only by design (cache-miss sessions land in the fallback
    bucket), so `classify="hybrid"` here only changes which cache layers
    are consulted, never whether an LLM runs.
    """
    try:
        n = max(1, min(int(count), 24))
        # Same price-override step every cost-reporting entry point takes
        # (build_recap, _run_trend, compare_to_previous above) — without it
        # the per-point cost_usd would depend on whatever prices some other
        # call in this server process happened to leave applied (#115).
        prices, snapshot = load_prices_config(recap.CONFIG_PATH)
        apply_prices(prices, snapshot)
        fallback_bucket = load_settings().get("default_bucket", "coding")
        points = collect_trend(
            period=period, count=n, mode=classify, fallback=fallback_bucket,
        )
    except _TOOL_ERRORS as e:
        return _normalize_error(e)
    return {
        "ok": True,
        "period": period,
        "count": n,
        "points": [
            {
                "label": p.label,
                "since": p.since.isoformat(),
                "until": p.until.isoformat(),
                "active_hours": round(p.total_h, 2),
                "cost_usd": round(p.cost_usd, 2),
                "buckets": [
                    {
                        "name": r.category,
                        "active_hours": round(r.active_min / 60, 2),
                        "sessions": r.sessions,
                    }
                    for r in p.rollups
                ],
            }
            for p in points
        ],
    }


@mcp.tool()
def list_categories() -> dict:
    """User + default bucket rules ccstory classifies sessions into, in
    resolver priority order (first match wins)."""
    try:
        rules = load_rules()
    except _TOOL_ERRORS as e:
        return _normalize_error(e)
    return {
        "ok": True,
        "categories": [{"name": r.name, "needles": r.needles} for r in rules],
    }


def run() -> None:
    """`ccstory mcp` entry point — serve the tools above over stdio."""
    print("ccstory mcp: listening on stdio", file=sys.stderr)
    try:
        mcp.run()
    except KeyboardInterrupt:
        print("ccstory mcp: stopped", file=sys.stderr)
