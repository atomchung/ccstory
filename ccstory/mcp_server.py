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

v0 scope (deliberately): read-only, no fresh `claude -p` calls by default
(`get_recap`'s `allow_llm` is opt-in; `compare_to_previous` never fires an
LLM at all, matching `trends.compare_to_previous()`'s own cache-only
contract), stdio transport only. `get_trend` is intentionally not included
yet — its compact shape is the least settled part of issue #35, so it's
left for a follow-up once the other three tools' conventions have seen
real agent traffic.
"""

from __future__ import annotations

import sys
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .categorizer import load_rules, load_settings, normalize_project_name
from .recap import RecapUnavailable, build_recap, parse_window
from .time_tracking import collect_sessions, rollup_by_category
from .token_usage import collect_usage
from .trends import _resolve_sessions_from_cache
from .trends import compare_to_previous as _compare_to_previous

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
# of letting propagate. SystemExit (raised by session_summarizer._connect()
# on a corrupt cache.db) is the one that matters most: it subclasses
# BaseException, so FastMCP's own `except Exception` safety net does NOT
# catch it — an uncaught SystemExit here would kill the whole server
# process over one bad tool call instead of just failing that call.
_TOOL_ERRORS = (ValueError, RecapUnavailable, SystemExit)


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
        # Always None in v0: this tool only calls trends.compare_to_previous(),
        # never the claude -p synthesis step build_recap() layers on top —
        # matches "no fresh LLM calls" for this tool.
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
    and `allow_llm=False` never fire `claude -p`; pass `classify="content"`
    or `"hybrid"`, and/or `allow_llm=True`, to opt into LLM-assisted
    classification / narrative polish (slower, may cost tokens).
    """
    try:
        result = build_recap(
            window,
            classify=classify,
            llm_narrative=allow_llm,
            compare=False,
            artifacts=False,
            write_report=False,
            console=None,
        )
    except _TOOL_ERRORS as e:
        return {"ok": False, "error": str(e)}
    return _compact_recap(result)


@mcp.tool()
def compare_to_previous(
    window: Window = "week", classify: Classify = _DEFAULT_CLASSIFY,
) -> dict:
    """Compare one window against the immediately preceding same-length
    window: active-hours deltas per category, cost deltas. Never fires a
    fresh `claude -p` call regardless of `classify` (unlike `get_recap`,
    this tool's classification step is always cache-only by construction —
    see `trends.compare_to_previous()`), so `narrative` is always null
    here (use `get_recap` for a synthesized narrative).
    """
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
        return {"ok": False, "error": str(e)}
    if cmp is None:
        return {"ok": False, "error": "No sessions in the previous window to compare."}
    return _compact_comparison(cmp)


@mcp.tool()
def list_categories() -> dict:
    """User + default bucket rules ccstory classifies sessions into, in
    resolver priority order (first match wins)."""
    rules = load_rules()
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
