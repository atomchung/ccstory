"""Markdown report + Rich-based terminal card for ccstory.

The markdown report is the source of truth — re-runnable, copy-pasteable,
versionable. The terminal card is a screenshot-friendly summary printed
when the CLI finishes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .artifacts import ArtifactsReport
from .categorizer import colors_for, load_settings, normalize_project_name
from .providers import agent_label
from .session_summarizer import SessionSummary
from .time_tracking import CategoryRollup, SessionStat, wall_clock_active_sec
from .token_usage import (
    UsageReport,
    fmt_tokens,
    get_snapshot_date,
    pricing_snapshot_warning,
)
from .trends import PeriodComparison, PeriodPoint, sparkline, trend_by_category

# Supported markdown flavors for render_report().
VALID_FLAVORS = ("plain", "obsidian")

# ----- Multi-agent breakdown (#133) -------------------------------------------
#
# Deliberately shares-only. Agents run *concurrently* — a Codex review and a
# Claude Code session can occupy the same ten minutes — so per-agent hours are
# not additive and printing them next to the card's single Total invites the
# reader to add numbers that describe overlapping intervals. Measured on real
# data: raw per-agent time summed to 163h over a week whose deduplicated wall
# clock was 64.5h — 2.5× over. The one duration ccstory reports stays
# `wall_clock_active_sec` over all sessions; agents get a relative share of raw
# interaction time instead, which is meaningful precisely because it does not
# claim to be a duration.


@dataclass
class AgentShare:
    """One agent's slice of a window. Shares are fractions in [0, 1]."""
    agent: str
    label: str
    sessions: int
    messages: int
    time_share: float
    session_share: float


def agent_breakdown(sessions: list[SessionStat]) -> list[AgentShare]:
    """Per-agent share of raw interaction time and of session count.

    Sorted by time share, biggest first. The two shares routinely disagree —
    many short Codex reviews against fewer long Claude Code sessions — and the
    disagreement is the point, so both are reported.
    """
    raw_by_agent: dict[str, int] = {}
    sessions_by_agent: dict[str, int] = {}
    msgs_by_agent: dict[str, int] = {}
    for s in sessions:
        name = getattr(s, "agent", "claude") or "claude"
        raw_by_agent[name] = raw_by_agent.get(name, 0) + s.active_sec
        sessions_by_agent[name] = sessions_by_agent.get(name, 0) + 1
        msgs_by_agent[name] = msgs_by_agent.get(name, 0) + s.msg_count

    raw_total = sum(raw_by_agent.values())
    n_total = len(sessions)
    out = [
        AgentShare(
            agent=name,
            label=agent_label(name),
            sessions=sessions_by_agent[name],
            messages=msgs_by_agent[name],
            time_share=(raw / raw_total) if raw_total else 0.0,
            session_share=(sessions_by_agent[name] / n_total) if n_total else 0.0,
        )
        for name, raw in raw_by_agent.items()
    ]
    out.sort(key=lambda a: (-a.time_share, a.agent))
    return out


def render_agent_breakdown_markdown(sessions: list[SessionStat]) -> list[str]:
    """The "Coding agents" section, or [] when only one agent is in play.

    A single-agent window has nothing to compare, and a 100% row would be pure
    noise for the Claude-Code-only user who is still ccstory's default reader.
    """
    shares = agent_breakdown(sessions)
    if len(shares) < 2:
        return []

    lines = ["## Coding agents", ""]
    lines.append("| Agent | Time share | Sessions | Messages |")
    lines.append("|---|---:|---:|---:|")
    for a in shares:
        lines.append(
            f"| {_md_cell(a.label)} (`{a.agent}`) | {a.time_share*100:.0f}% | "
            f"{a.sessions} ({a.session_share*100:.0f}%) | {a.messages:,} |"
        )
    lines.append("")
    wall_h = wall_clock_active_sec(sessions) / 3600
    raw_h = sum(s.active_sec for s in sessions) / 3600
    lines.append(
        f"**{wall_h:.1f}h** wall-clock across all agents · "
        f"**{parallelism_factor(sessions):.1f}× parallel** "
        f"(raw per-agent time sums to {raw_h:.1f}h)."
    )
    lines.append("")
    lines.append(
        "> Share = each agent's raw interaction time relative to the others'. "
        "Agents run in parallel, so a share is **not** a duration and the "
        "shares do not add up to the total active time above."
    )
    lines.append("")
    return lines


def parallelism_factor(sessions: list[SessionStat]) -> float:
    """How much agent time overlapped: raw summed time ÷ wall-clock time.

    1.0 means strictly sequential work; 2.8 means the raw per-agent totals
    describe 2.8× more time than actually elapsed.
    """
    wall = wall_clock_active_sec(sessions)
    if wall <= 0:
        return 1.0
    return sum(s.active_sec for s in sessions) / wall


def _format_date_range(since: datetime, until: datetime) -> str:
    """Human-readable date range. Same year/month collapsed for compactness."""
    def _month_day(value: datetime) -> str:
        return f"{value.strftime('%b')} {value.day}"

    def _full(value: datetime) -> str:
        return f"{_month_day(value)}, {value.year}"

    if since.date() == until.date():
        return _full(since)
    if since.year == until.year and since.month == until.month:
        return f"{_month_day(since)} – {until.day}, {until.year}"
    if since.year == until.year:
        return f"{_month_day(since)} – {_full(until)}"
    return f"{_full(since)} – {_full(until)}"


def _top_session_text(rollup: CategoryRollup, summaries: dict, max_chars: int = 70) -> str:
    """One-line summary of the longest session in a category. Always single-line."""
    if not rollup.top_sessions:
        return ""
    top = rollup.top_sessions[0]
    summ = summaries.get(top.session_id) if summaries else None
    text = summ.summary if summ else (top.first_user_text or "(no summary)")
    # Always collapse to one line — newlines/extra whitespace mangle the panel
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


_BOLD_HEADER_RE = re.compile(r"^\*\*(.+)\*\*$")
_INNER_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _narrative_headers(narrative: str) -> list[str]:
    """Pull the bold thread headers out of a goal-thread overall narrative.

    `session_summarizer._OVERALL_PROMPT` (#98) shapes the overall narrative
    as 2-4 blocks, each a `**bold header**` line (the concrete win) followed
    by 1-3 `- bullet` supporting lines. That's right for the full markdown
    report, but dumping the whole thing verbatim into the terminal card (a
    "screenshot-friendly summary" per this module's docstring) prints the
    literal `**`/`-` markup and runs many lines long. This extracts just the
    headers so the card can show the wins compactly; full detail stays one
    line away via the "Full report" footer.

    A header line is allowed to contain its own nested `**emphasis**` (e.g.
    around a version number) — `_INNER_BOLD_RE` unwraps those too, since the
    outer match's greedy `.+` would otherwise capture the inner `**` marks
    verbatim and leak them into the card, reproducing the exact bug this
    function exists to avoid.

    Returns `[]` if no `**...**`-wrapped lines are found — e.g. an older
    cached narrative from before #98's format, or the LLM drifting off
    spec — so the caller falls back to rendering the raw text.
    """
    headers = []
    for line in narrative.splitlines():
        m = _BOLD_HEADER_RE.match(line.strip())
        if m:
            headers.append(_INNER_BOLD_RE.sub(r"\1", m.group(1)).strip())
    return headers


_YAML_IMPLICIT_NON_STRING = frozenset({
    # YAML 1.1 booleans (Obsidian / Dataview parsers tend to use 1.1 semantics)
    "y", "Y", "yes", "Yes", "YES",
    "n", "N", "no", "No", "NO",
    "true", "True", "TRUE",
    "false", "False", "FALSE",
    "on", "On", "ON",
    "off", "Off", "OFF",
    # null
    "null", "Null", "NULL", "~",
})


def _md_cell(value: str) -> str:
    """Escape characters that break a GFM table cell.

    Pipes terminate cells; literal newlines split rows. Bucket names and
    LLM-produced narratives can contain either, so any user-controlled
    string heading into a table cell goes through here.
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _yaml_scalar(value: str) -> str:
    """Quote a string so it is safe as a YAML scalar.

    Bare strings made of a leading letter plus [A-Za-z0-9_-] pass through
    unquoted, unless they collide with a YAML implicit scalar that would
    deserialize as bool/null. Everything else (digits-led, dates, dotted
    numbers, punctuation, booleans, nulls) is emitted JSON-style — JSON
    strings are valid YAML scalars.
    """
    if (
        value
        and value[0].isalpha()
        and all(c.isalnum() or c in "-_" for c in value)
        and value not in _YAML_IMPLICIT_NON_STRING
    ):
        return value
    return json.dumps(value, ensure_ascii=False)


def _obsidian_wikilink_target(name: str) -> str:
    """Sanitize a string so it can sit inside `[[...]]` without breaking the link.

    Newlines, `[`, `]`, `|`, `#`, `^` all have meaning in Obsidian wikilink
    syntax (alias separator, block/heading refs, link terminator). Replace
    them with `-` to keep the link parseable.
    """
    cleaned = name.replace("\r", " ").replace("\n", " ")
    for ch in ("[", "]", "|", "#", "^"):
        cleaned = cleaned.replace(ch, "-")
    cleaned = cleaned.strip()
    return cleaned or "untitled"


def _obsidian_frontmatter(
    since: datetime,
    until: datetime,
    rollups: list[CategoryRollup],
    usage: UsageReport,
) -> list[str]:
    """YAML front-matter for the Obsidian flavor.

    Front-matter properties are queryable in Obsidian's Dataview / Bases —
    e.g. `WHERE top_focus = "coding"` to pull recap notes by bucket.
    """
    total_min = sum(r.active_min for r in rollups)
    total_h = total_min / 60
    # Don't rely on caller-side sort; compute the top by active_min directly.
    # Tiebreak by category name so output is deterministic regardless of
    # caller-side iteration order.
    top_focus = (
        max(rollups, key=lambda r: (r.active_min, r.category)).category
        if rollups
        else ""
    )
    buckets = [r.category for r in rollups]
    lines = ["---"]
    lines.append(f"date_start: {since.date().isoformat()}")
    lines.append(f"date_end: {until.date().isoformat()}")
    lines.append(f"active_hours: {total_h:.1f}")
    if top_focus:
        lines.append(f"top_focus: {_yaml_scalar(top_focus)}")
    lines.append(
        "buckets: [" + ", ".join(_yaml_scalar(b) for b in buckets) + "]"
    )
    lines.append(f"cost_usd: {usage.total_cost_usd:.2f}")
    lines.append(f"output_tokens: {usage.total_output}")
    lines.append("---")
    return lines


def _stars_cell(stars: int | None, delta: int | None) -> str:
    if stars is None:
        return "–"
    if delta is None or delta == 0:
        return f"{stars:,}"
    sign = "+" if delta > 0 else ""
    return f"{stars:,} ({sign}{delta})"


def render_artifacts_markdown(arts: ArtifactsReport) -> str:
    """"What shipped" section: per-repo output metrics + PyPI downloads."""
    lines: list[str] = []
    lines.append("## What shipped")
    lines.append("")
    if arts.repos:
        lines.append("| Repo | Commits | PRs merged | Releases | Stars |")
        lines.append("|---|---:|---:|---|---:|")
        for r in arts.repos:
            prs = str(r.prs_merged) if r.prs_merged is not None else "–"
            rels = _md_cell(", ".join(r.releases)) if r.releases else "–"
            lines.append(
                f"| {_md_cell(r.name)} | {r.commits} | {prs} | {rels} | "
                f"{_stars_cell(r.stars, r.stars_delta)} |"
            )
        lines.append("")
    for p in arts.pypi:
        window_label = p.window.replace("_", " ")
        lines.append(
            f"- PyPI **{_md_cell(p.package)}**: {p.downloads:,} downloads ({window_label})"
        )
    if arts.pypi:
        lines.append("")
    lines.append(
        "> Commits count all branches (unmerged work is still output). "
        "Stars delta is vs the last snapshot before this window; "
        "PyPI numbers are pypistats.org rolling buckets, not this exact window."
    )
    lines.append("")
    return "\n".join(lines)


def render_report(
    label: str,
    since: datetime,
    until: datetime,
    sessions: list[SessionStat],
    rollups: list[CategoryRollup],
    usage: UsageReport,
    summaries: dict[str, SessionSummary],
    overall_narrative: str | None = None,
    comparison: PeriodComparison | None = None,
    flavor: str = "plain",
    artifacts: ArtifactsReport | None = None,
    category_narratives: dict[str, str] | None = None,
) -> str:
    """Produce the full markdown report.

    `flavor`:
      - "plain" (default): vanilla markdown, works in any viewer
      - "obsidian": adds YAML front-matter + [[wikilinks]] around project
        names in per-session lines, so notes drop into a PKM vault with
        live cross-linking on day one
    """
    if flavor not in VALID_FLAVORS:
        raise ValueError(f"unsupported flavor: {flavor!r} (use one of {VALID_FLAVORS})")
    total_min = sum(r.active_min for r in rollups)
    total_h = total_min / 60
    total_msgs = sum(r.messages for r in rollups)

    lines: list[str] = []
    if flavor == "obsidian":
        lines.extend(_obsidian_frontmatter(since, until, rollups, usage))
        lines.append("")

    date_range = _format_date_range(since, until)
    lines.append(f"# Claude Code Recap · {date_range}")
    lines.append("")
    lines.append(f"> Period label: `{label}` · Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"> Window: {since.date()} → {until.date()}")
    lines.append(
        f"> **{total_h:.1f}h active** · {len(sessions)} sessions · {total_msgs:,} messages"
    )
    lines.append("")

    if rollups:
        top_r = rollups[0]
        top_pct = (top_r.active_min / total_min * 100) if total_min else 0
        top_text = _top_session_text(top_r, summaries)
        lines.append(
            f"**★ Top focus: `{top_r.category}` — {top_r.active_min/60:.1f}h "
            f"({top_pct:.0f}% of active time)**"
        )
        if top_text:
            lines.append(f"> {top_text}")
        lines.append("")

    # Time distribution
    lines.append("## Time distribution")
    lines.append("")
    lines.append("| Category | Time | % | Sessions | Messages |")
    lines.append("|---|---:|---:|---:|---:|")
    for r in rollups:
        pct = (r.active_min / total_min * 100) if total_min else 0
        h_m = f"{int(r.active_min // 60)}h {int(r.active_min % 60):02d}m"
        lines.append(
            f"| {_md_cell(r.category)} | {h_m} | {pct:.0f}% | {r.sessions} | {r.messages:,} |"
        )
    lines.append("")

    lines.extend(render_agent_breakdown_markdown(sessions))

    if comparison:
        lines.append(render_comparison_markdown(comparison))

    # Overall narrative (goal-thread synthesis across the whole period)
    if overall_narrative:
        lines.append("## What you did")
        lines.append("")
        lines.append(overall_narrative)
        lines.append("")

    # Per-bucket narratives (#57) — rollup order so the biggest bucket leads
    if category_narratives:
        lines.append("## What you did, by category")
        lines.append("")
        for r in rollups:
            narrative = category_narratives.get(r.category)
            if not narrative:
                continue
            lines.append(f"### {r.category}")
            lines.append("")
            lines.append(narrative)
            lines.append("")

    # Shipped-output metrics — the "what did the time produce" half (#90)
    if artifacts:
        lines.append(render_artifacts_markdown(artifacts))

    # Per-category session breakdown
    lines.append("## Sessions, by category")
    lines.append("")
    for r in rollups:
        lines.append(f"### {r.category}")
        lines.append("")
        # Layer-2 (#69): indented top-3 projects by active hours. Full list is
        # in --json; this keeps the markdown scannable.
        if r.projects:
            top3 = r.projects[:3]
            proj_bits = " · ".join(
                f"**{_md_cell(p.project)}** {p.active_min / 60:.1f}h" for p in top3
            )
            extra = len(r.projects) - len(top3)
            if extra > 0:
                proj_bits += f" · +{extra} more"
            lines.append(f"_Projects:_ {proj_bits}")
            lines.append("")
        for s in r.top_sessions:
            summ = summaries.get(s.session_id)
            text = summ.summary if summ else s.first_user_text[:100]
            time_str = s.start.strftime("%Y-%m-%d %H:%M")
            mins = int(s.active_sec // 60)
            if flavor == "obsidian":
                leaf = normalize_project_name(s.project) or s.project
                leaf = _obsidian_wikilink_target(leaf)
                lines.append(
                    f"- **{time_str}** · {mins}m · {s.msg_count} msg · "
                    f"[[{leaf}]] — {text}"
                )
            else:
                lines.append(
                    f"- **{time_str}** · {mins}m · {s.msg_count} msg — {text}"
                )
        lines.append("")

    # Token usage
    lines.append("## Token & API-equivalent cost")
    lines.append("")
    lines.append(
        f"**{usage.assistant_turns:,}** assistant turns · "
        f"**{fmt_tokens(usage.total_tokens)}** total tokens · "
        f"cache hit **{usage.cache_hit_ratio*100:.0f}%**"
    )
    lines.append("")
    lines.append("| Type | Tokens |")
    lines.append("|---|---:|")
    lines.append(f"| Input (fresh) | {fmt_tokens(usage.total_input)} |")
    lines.append(f"| Cache creation | {fmt_tokens(usage.total_cache_creation)} |")
    lines.append(f"| Cache read | {fmt_tokens(usage.total_cache_read)} |")
    lines.append(f"| Output | {fmt_tokens(usage.total_output)} |")
    lines.append("")
    lines.append("| Model | Turns | Output | Cost (USD) |")
    lines.append("|---|---:|---:|---:|")
    for model, mu in sorted(
        usage.by_model.items(), key=lambda x: -x[1].total_tokens
    ):
        lines.append(
            f"| {_md_cell(model)} | {mu.turns} | {fmt_tokens(mu.output_tokens)} | "
            f"${mu.cost_usd:,.2f} |"
        )
    lines.append("")
    lines.append(f"- **API-equivalent cost**: ${usage.total_cost_usd:,.2f}")
    lines.append(
        f"- **Without cache it would be**: ${usage.total_cost_uncached_usd:,.2f} "
        f"(cache saved ${usage.cache_savings_usd:,.2f})"
    )
    lines.append("")
    others = [a.label for a in agent_breakdown(sessions) if a.agent != "claude"]
    if others:
        # Time now spans every agent; token accounting still reads Claude Code
        # transcripts only. Say so rather than letting the reader divide a
        # multi-agent hour count into a single-agent cost.
        lines.append(
            f"> ⚠️ Token and cost figures cover Claude Code only — "
            f"{', '.join(others)} usage is in the time breakdown above but not "
            "in these numbers."
        )
    lines.append(
        "> For exact cost / billing-window breakdowns, pair with "
        "[ccusage](https://github.com/ryoppippi/ccusage). ccstory tells the story; "
        "ccusage tells the bill."
    )
    lines.append(f"> Pricing snapshot: `{get_snapshot_date()}`.")
    pricing_warning = pricing_snapshot_warning(until)
    if pricing_warning:
        lines.append(
            f"> ⚠️ {pricing_warning} "
            "[Check current pricing](https://platform.claude.com/docs/en/pricing)."
        )
    lines.append("")

    return "\n".join(lines)


# ----- JSON output (#83) -------------------------------------------------------

# Bump when a field is renamed/removed or its meaning changes. Additive
# fields do NOT bump the version — consumers must tolerate unknown keys.
JSON_SCHEMA_VERSION = 1


def _session_summary_text(s: SessionStat, summaries: dict) -> str:
    """Same one-liner precedence the markdown report uses."""
    summ = summaries.get(s.session_id) if summaries else None
    return summ.summary if summ else s.first_user_text[:100]


def build_report_json(
    label: str,
    since: datetime,
    until: datetime,
    sessions: list[SessionStat],
    rollups: list[CategoryRollup],
    usage: UsageReport,
    summaries: dict[str, SessionSummary],
    overall_narrative: str | None = None,
    comparison: PeriodComparison | None = None,
    artifacts: ArtifactsReport | None = None,
    category_narratives: dict[str, str] | None = None,
) -> dict:
    """Machine-readable envelope mirroring the markdown report's content.

    Consumed by downstream tooling (dashboards, bots, sync scripts) — field
    names are a public contract governed by JSON_SCHEMA_VERSION.
    """
    total_min = sum(r.active_min for r in rollups)
    category_narratives = category_narratives or {}
    payload: dict = {
        "schema_version": JSON_SCHEMA_VERSION,
        "kind": "recap",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "window": {
            "label": label,
            "since": since.isoformat(),
            "until": until.isoformat(),
        },
        "totals": {
            "active_hours": round(total_min / 60, 2),
            "sessions": len(sessions),
            "messages": sum(r.messages for r in rollups),
            "assistant_turns": usage.assistant_turns,
            "cache_hit_ratio": round(usage.cache_hit_ratio, 4),
            "tokens": {
                "input_fresh": usage.total_input,
                "cache_creation": usage.total_cache_creation,
                "cache_read": usage.total_cache_read,
                "output": usage.total_output,
                "total": usage.total_tokens,
            },
            "cost_usd": round(usage.total_cost_usd, 2),
            "cost_uncached_usd": round(usage.total_cost_uncached_usd, 2),
            "cache_savings_usd": round(usage.cache_savings_usd, 2),
        },
        "pricing_snapshot": get_snapshot_date(),
        "buckets": [
            {
                "name": r.category,
                "active_hours": round(r.active_min / 60, 2),
                "share": round(r.active_min / total_min, 4) if total_min else 0.0,
                "sessions": r.sessions,
                "messages": r.messages,
                # Additive (#57): null unless the run used
                # --narrative per-category|both.
                "narrative": category_narratives.get(r.category),
                # Additive layer-2 (#69): full per-project breakdown, biggest
                # first. schema_version stays 1 — consumers tolerate unknown
                # keys, and layer-1 fields above are unchanged.
                "projects": [
                    {
                        "name": p.project,
                        "active_hours": round(p.active_min / 60, 2),
                        "sessions": p.sessions,
                        "messages": p.messages,
                    }
                    for p in r.projects
                ],
            }
            for r in rollups
        ],
        "sessions": [
            {
                "id": s.session_id,
                "project": normalize_project_name(s.project) or s.project,
                "bucket": s.category,
                # Additive (#133): which coding agent produced this session.
                # "claude" for every pre-multi-agent consumer's data.
                "agent": getattr(s, "agent", "claude") or "claude",
                "start": s.start.isoformat(),
                "end": s.end.isoformat(),
                "active_min": s.active_min,
                "messages": s.msg_count,
                "summary": _session_summary_text(s, summaries),
                # Provenance so consumers can filter real LLM summaries
                # ("auto") from first-message fallbacks — seeding a downstream
                # cache with fallback text would poison it.
                "summary_source": (
                    summaries[s.session_id].source
                    if summaries and s.session_id in summaries
                    else "first_message"
                ),
            }
            for s in sorted(sessions, key=lambda x: x.start)
        ],
        # Additive (#133). `time_share` is a relative weight of raw
        # interaction time, NOT hours: agents run concurrently, so per-agent
        # durations would double-count the overlap. `parallelism` is how many
        # times over the raw sum exceeds `totals.active_hours`.
        "agents": [
            {
                "agent": a.agent,
                "label": a.label,
                "sessions": a.sessions,
                "messages": a.messages,
                "time_share": round(a.time_share, 4),
                "session_share": round(a.session_share, 4),
            }
            for a in agent_breakdown(sessions)
        ],
        "parallelism": round(parallelism_factor(sessions), 2),
        "by_model": [
            {
                "model": model,
                "turns": mu.turns,
                "output_tokens": mu.output_tokens,
                "cost_usd": round(mu.cost_usd, 2),
            }
            for model, mu in sorted(
                usage.by_model.items(), key=lambda x: -x[1].total_tokens
            )
        ],
        "narrative": {"overall": overall_narrative},
    }
    if comparison:
        payload["comparison"] = {
            "previous_label": comparison.previous_label,
            "current_total_h": round(comparison.current_total_h, 2),
            "previous_total_h": round(comparison.previous_total_h, 2),
            "current_output_tokens": comparison.current_output_tokens,
            "previous_output_tokens": comparison.previous_output_tokens,
            "current_cost_usd": round(comparison.current_cost_usd, 2),
            "previous_cost_usd": round(comparison.previous_cost_usd, 2),
            "narrative": comparison.narrative,
            "deltas": [
                {
                    "bucket": d.category,
                    "current_min": d.current_min,
                    "previous_min": d.previous_min,
                    "delta_min": round(d.delta_min, 1),
                    "pct_change": round(d.pct_change, 1)
                    if d.pct_change is not None else None,
                }
                for d in comparison.deltas
            ],
        }
    else:
        payload["comparison"] = None
    if artifacts:
        payload["artifacts"] = {
            "repos": [
                {
                    "name": r.name,
                    "github": r.github,
                    "commits": r.commits,
                    "commit_subjects": r.commit_subjects,
                    "prs_merged": r.prs_merged,
                    "releases": r.releases,
                    "stars": r.stars,
                    "stars_delta": r.stars_delta,
                }
                for r in artifacts.repos
            ],
            "pypi": [
                {"package": p.package, "downloads": p.downloads, "window": p.window}
                for p in artifacts.pypi
            ],
            "totals": {
                "commits": artifacts.total_commits,
                "prs_merged": artifacts.total_prs,
                "releases": artifacts.total_releases,
            },
        }
    else:
        payload["artifacts"] = None
    return payload


def build_trend_json(points: list[PeriodPoint], period: str) -> dict:
    """Machine-readable trend series (per-period totals + bucket hours)."""
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "kind": "trend",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "period": period,
        "pricing_snapshot": get_snapshot_date(),
        "points": [
            {
                "label": p.label,
                "since": p.since.isoformat(),
                "until": p.until.isoformat(),
                "total_hours": round(p.total_h, 2),
                "output_tokens": p.output_tokens,
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


def _colored_bar(pct: float, color: str, width: int = 28) -> Text:
    """Solid+empty block bar with per-bucket color."""
    filled = int(round(pct * width))
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="dim")
    return bar


def render_terminal_card(
    since: datetime,
    until: datetime,
    sessions: list[SessionStat],
    rollups: list[CategoryRollup],
    usage: UsageReport,
    summaries: dict | None = None,
    overall_narrative: str | None = None,
    report_path: str | None = None,
    comparison: PeriodComparison | None = None,
    artifacts: ArtifactsReport | None = None,
) -> Panel:
    """Rich Panel summarizing the recap. Designed for screenshot sharing."""
    summaries = summaries or {}
    total_min = sum(r.active_min for r in rollups)
    total_h = total_min / 60

    # One collision-free color map for every bucket shown anywhere in this
    # card (bars, by-project table, comparison deltas) — colors_for() avoids
    # two different buckets landing on the same color, which color_for()
    # can't since it resolves each bucket independently.
    all_categories = [r.category for r in rollups]
    if comparison:
        all_categories += [d.category for d in comparison.deltas]
    colors = colors_for(all_categories)

    # --- Highlight row: biggest bucket + top session in it ---
    highlight_block: list = []
    if rollups:
        top_r = rollups[0]
        top_color = colors[top_r.category]
        top_pct = (top_r.active_min / total_min * 100) if total_min else 0
        headline = Text()
        headline.append("★ Top focus  ", style="bold")
        headline.append(top_r.category, style=f"bold {top_color}")
        headline.append(f"  {top_r.active_min/60:.1f}h", style="bold")
        headline.append(f"  ({top_pct:.0f}% of active time)", style="dim")
        highlight_block.append(headline)
        top_text = _top_session_text(top_r, summaries)
        if top_text:
            sub = Text(no_wrap=True, overflow="ellipsis")
            sub.append("  ↳ ", style="dim")
            sub.append(top_text, style="italic")
            highlight_block.append(sub)
        highlight_block.append(Text(""))

    # --- Metrics row ---
    metrics = Table.grid(padding=(0, 2))
    for _ in range(6):
        metrics.add_column()
    metrics.add_row(
        Text("Active",   style="dim"),  Text(f"{total_h:.1f}h", style="bold"),
        Text("Sessions", style="dim"),  Text(f"{len(sessions)}", style="bold"),
        Text("Output",   style="dim"),  Text(fmt_tokens(usage.total_output), style="bold"),
    )
    cache_color = "green" if usage.cache_hit_ratio >= 0.9 else "yellow"
    metrics.add_row(
        Text("Turns",   style="dim"),  Text(f"{usage.assistant_turns:,}", style="bold"),
        Text("Cache",   style="dim"),  Text(f"{usage.cache_hit_ratio*100:.0f}%", style=f"bold {cache_color}"),
        Text("Cost",    style="dim"),  Text(f"${usage.total_cost_usd:,.0f}", style="bold green"),
    )

    # --- Bar chart, colored per bucket ---
    bars = Table.grid(padding=(0, 1))
    bars.add_column(width=14)
    bars.add_column(no_wrap=True)
    bars.add_column(justify="right", width=7)
    bars.add_column(justify="right", width=5)
    if total_min > 0:
        for r in rollups:
            pct = r.active_min / total_min
            color = colors[r.category]
            bars.add_row(
                Text(r.category, style=f"bold {color}"),
                _colored_bar(pct, color),
                Text(f"{r.active_min/60:.1f}h", style="bold"),
                Text(f"{pct*100:.0f}%", style="dim"),
            )

    # --- Layer-2 (#69): top projects per multi-project area ---
    # Kept in its own grid below the bars so the layer-1 bar chart stays
    # pixel-identical, and project names get a column wide enough not to wrap.
    # Single-project areas are skipped — their breakdown is just themselves.
    split_areas = [r for r in rollups if total_min > 0 and len(r.projects) >= 2]
    proj_table: Table | None = None
    if split_areas:
        proj_table = Table.grid(padding=(0, 1))
        proj_table.add_column(width=12, no_wrap=True, overflow="ellipsis")
        proj_table.add_column(no_wrap=True, overflow="ellipsis", width=52)
        for r in split_areas:
            color = colors[r.category]
            top3 = r.projects[:3]
            summary = " · ".join(
                f"{p.project} {p.active_min/60:.1f}h" for p in top3
            )
            extra = len(r.projects) - len(top3)
            if extra > 0:
                summary += f" · +{extra}"
            proj_table.add_row(
                Text(r.category, style=color), Text(summary, style="dim"),
            )

    parts: list = []
    parts.extend(highlight_block)
    parts.append(metrics)
    parts.append(Text(""))
    parts.append(Text("Time by category", style="bold underline"))
    parts.append(bars)

    if proj_table is not None:
        parts.append(Text(""))
        parts.append(Text("By project", style="bold underline"))
        parts.append(proj_table)

    # --- Multi-agent shares (#133) --- no hours here on purpose; see the
    # module-level note above agent_breakdown().
    shares = agent_breakdown(sessions)
    if len(shares) >= 2:
        agent_table = Table.grid(padding=(0, 1))
        agent_table.add_column(width=14, no_wrap=True, overflow="ellipsis")
        agent_table.add_column(no_wrap=True)
        for a in shares:
            agent_table.add_row(
                Text(a.label, style="bold"),
                Text(
                    f"{a.time_share*100:.0f}% of agent time · "
                    f"{a.sessions} sessions ({a.session_share*100:.0f}%)",
                    style="dim",
                ),
            )
        parts.append(Text(""))
        parts.append(Text("Coding agents", style="bold underline"))
        parts.append(agent_table)
        note = Text()
        note.append(f"{parallelism_factor(sessions):.1f}× parallel", style="bold")
        note.append("  shares are weights, not hours", style="dim")
        parts.append(note)
        parts.append(
            Text("Tokens / cost above are Claude Code only.", style="dim yellow")
        )

    if overall_narrative:
        parts.append(Text(""))
        parts.append(Text("What you did", style="bold underline"))
        headers = _narrative_headers(overall_narrative)
        if headers:
            # Goal-thread narrative (#98): show each thread's bold header
            # (the concrete win) only — supporting bullets are one line away
            # via the "Full report" footer. Table.grid gives wrapped headers
            # a hanging indent instead of restarting at column 0.
            did_table = Table.grid(padding=(0, 1))
            did_table.add_column(width=2)
            did_table.add_column()
            for h in headers:
                did_table.add_row(Text("•", style="dim"), Text(h, style="bold"))
            parts.append(did_table)
        else:
            # Pre-#98 cached narrative (plain prose) — render as before.
            parts.append(Text(overall_narrative, style="dim"))

    if artifacts and artifacts.repos:
        parts.append(Text(""))
        shipped = Text()
        shipped.append("Shipped  ", style="bold")
        bits = [f"{artifacts.total_commits} commits"]
        if artifacts.total_prs:
            bits.append(f"{artifacts.total_prs} PRs merged")
        if artifacts.total_releases:
            bits.append(f"{artifacts.total_releases} release"
                        + ("s" if artifacts.total_releases > 1 else ""))
        shipped.append(" · ".join(bits), style="bold green")
        shipped.append(f"  across {len(artifacts.repos)} repos", style="dim")
        parts.append(shipped)

    if comparison:
        parts.extend(render_comparison_block(comparison, colors))

    if report_path:
        parts.append(Text(""))
        footer = Text()
        footer.append("Full report → ", style="dim")
        footer.append(report_path, style="dim underline")
        parts.append(footer)

    pricing_warning = pricing_snapshot_warning(until)
    if pricing_warning:
        parts.append(Text(f"⚠️ {pricing_warning}", style="yellow"))

    title_range = _format_date_range(since, until)
    # Only rebrand the card when the window genuinely spans several agents —
    # a Claude-Code-only user should keep seeing the name they installed.
    card_title = "AI Coding Recap" if len(shares) >= 2 else "Claude Code Recap"
    return Panel(
        Group(*parts),
        title=f"[bold]{card_title}[/bold] [dim]·[/dim] [cyan]{title_range}[/cyan]",
        subtitle="[dim]ccstory[/dim]",
        border_style="cyan",
        padding=(1, 2),
        width=72,
    )


def print_terminal_card(*args, console: Console | None = None, **kwargs) -> None:
    """Print the Rich Panel to console."""
    console = console or Console()
    console.print(render_terminal_card(*args, **kwargs))


# ----- Comparison rendering (feature A) ---------------------------------------

def _delta_text(current: float, previous: float, unit: str = "h", fmt: str = ".1f") -> Text:
    """Compact ▲/▼ delta string with green/red coloring."""
    diff = current - previous
    if previous == 0:
        return Text("new", style="green")
    pct = (diff / previous * 100) if previous else 0
    arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "·")
    color = "green" if diff > 0 else ("red" if diff < 0 else "dim")
    return Text(f"{arrow} {pct:+.0f}%", style=color)


def render_comparison_block(cmp: PeriodComparison, colors: dict[str, str]) -> list:
    """Renderable Rich elements: title + small comparison table for the panel.

    `colors` is the bucket→color map from the enclosing card (see
    render_terminal_card) so a bucket's color here matches its bar in the
    "Time by category" section above — always pass the full-card map, not
    one computed from just `cmp.deltas`, or a bucket that appears in both
    places could get two different colors in the same card.
    """
    parts: list = []
    parts.append(Text(""))
    parts.append(Text(f"vs previous window  ({cmp.previous_label})",
                      style="bold underline"))
    if cmp.narrative:
        # Match "What you did" body style (dim italic) so the two narrative
        # blocks read as one visual family — previously this one was just
        # `italic` (no dim) and shouted louder than the overall recap.
        parts.append(Text(cmp.narrative, style="dim italic"))
    table = Table.grid(padding=(0, 1))
    table.add_column(width=14)
    table.add_column(justify="right", width=8, style="bold")
    table.add_column(justify="right", width=8, style="dim")
    table.add_column(width=12)
    # Totals row first
    table.add_row(
        Text("total", style="bold"),
        Text(f"{cmp.current_total_h:.1f}h", style="bold"),
        Text(f"{cmp.previous_total_h:.1f}h", style="dim"),
        _delta_text(cmp.current_total_h, cmp.previous_total_h),
    )
    for d in cmp.deltas:
        color = colors[d.category]
        table.add_row(
            Text(d.category, style=color),
            Text(f"{d.current_min/60:.1f}h"),
            Text(f"{d.previous_min/60:.1f}h"),
            _delta_text(d.current_min, d.previous_min),
        )
    parts.append(table)
    parts.append(Text(
        f"Output: {fmt_tokens(cmp.current_output_tokens)} "
        f"(was {fmt_tokens(cmp.previous_output_tokens)})  ·  "
        f"Cost: ${cmp.current_cost_usd:,.0f} "
        f"(was ${cmp.previous_cost_usd:,.0f})",
        style="dim",
    ))
    return parts


def render_comparison_markdown(cmp: PeriodComparison) -> str:
    """Markdown block: comparison table for the long report."""
    lines = []
    lines.append("## vs previous window")
    lines.append("")
    lines.append(f"_Compared to_ `{cmp.previous_label}`")
    lines.append("")
    if cmp.narrative:
        # Multiline narratives must keep the `> ` prefix on every line, or
        # only the first line renders as a blockquote.
        for nl in cmp.narrative.splitlines() or [""]:
            lines.append(f"> {nl}" if nl else ">")
        lines.append("")
    lines.append("| Metric | Current | Previous | Change |")
    lines.append("|---|---:|---:|---:|")
    def fmt_pct(c: float, p: float) -> str:
        if p == 0:
            return "new"
        pct = (c - p) / p * 100
        arrow = "▲" if c > p else ("▼" if c < p else "·")
        return f"{arrow} {pct:+.0f}%"
    lines.append(
        f"| **Total active** | {cmp.current_total_h:.1f}h | "
        f"{cmp.previous_total_h:.1f}h | {fmt_pct(cmp.current_total_h, cmp.previous_total_h)} |"
    )
    lines.append(
        f"| Output tokens | {fmt_tokens(cmp.current_output_tokens)} | "
        f"{fmt_tokens(cmp.previous_output_tokens)} | "
        f"{fmt_pct(cmp.current_output_tokens, cmp.previous_output_tokens)} |"
    )
    lines.append(
        f"| API-equiv cost | ${cmp.current_cost_usd:,.0f} | "
        f"${cmp.previous_cost_usd:,.0f} | "
        f"{fmt_pct(cmp.current_cost_usd, cmp.previous_cost_usd)} |"
    )
    for d in cmp.deltas:
        lines.append(
            f"| `{_md_cell(d.category)}` | {d.current_min/60:.1f}h | "
            f"{d.previous_min/60:.1f}h | {fmt_pct(d.current_min, d.previous_min)} |"
        )
    lines.append("")
    lines.append(
        "> Cross-period token comparison uses **output tokens** (Claude's "
        "actual production). `total_tokens` is dominated by `cache_read` and "
        "inflates with turn count / system prompt size — not comparable across periods."
    )
    lines.append("")
    return "\n".join(lines)


# ----- Trend rendering (feature B) --------------------------------------------

def render_trend_card(points: list[PeriodPoint], period: str) -> Panel:
    """Rich Panel showing per-bucket sparklines over N periods."""
    if not points:
        return Panel(Text("No data in window."), title="ccstory trend")

    cat_series = trend_by_category(points)
    colors = colors_for(list(cat_series.keys()))
    total_series = [p.total_h for p in points]
    output_series = [p.output_tokens / 1_000_000 for p in points]
    cost_series = [p.cost_usd for p in points]
    labels = [p.label for p in points]

    table = Table.grid(padding=(0, 1))
    table.add_column(width=14)              # category
    table.add_column(no_wrap=True, width=len(labels) + 2)  # spark
    table.add_column(justify="right", width=8)  # latest
    table.add_column(justify="right", width=10)  # avg
    table.add_column(width=12)              # delta

    table.add_row(
        Text("total", style="bold underline"),
        Text(sparkline(total_series), style="bold"),
        Text(f"{total_series[-1]:.1f}h", style="bold"),
        Text(f"avg {sum(total_series)/len(total_series):.1f}h", style="dim"),
        _delta_text(total_series[-1], total_series[-2] if len(total_series) > 1 else 0),
    )
    for cat, series in cat_series.items():
        color = colors[cat]
        prev = series[-2] if len(series) > 1 else 0
        table.add_row(
            Text(cat, style=color),
            Text(sparkline(series), style=color),
            Text(f"{series[-1]:.1f}h", style="bold"),
            Text(f"avg {sum(series)/len(series):.1f}h", style="dim"),
            _delta_text(series[-1], prev),
        )

    # bottom axis: period labels as small hint
    axis_hint = Text(no_wrap=True, overflow="ellipsis")
    axis_hint.append("        ", style="dim")
    axis_hint.append(f"oldest {labels[0]}  →  latest {labels[-1]}", style="dim italic")

    extra = Table.grid(padding=(0, 1))
    extra.add_column(width=14)
    extra.add_column(no_wrap=True, width=len(labels) + 2)
    extra.add_column(justify="right", width=8)
    extra.add_column(justify="right", width=10)
    extra.add_column(width=12)
    extra.add_row(
        Text("output", style="bold"),
        Text(sparkline(output_series), style="cyan"),
        Text(f"{output_series[-1]:.1f}M", style="bold"),
        Text(f"avg {sum(output_series)/len(output_series):.1f}M", style="dim"),
        _delta_text(output_series[-1], output_series[-2] if len(output_series) > 1 else 0, unit="M"),
    )
    extra.add_row(
        Text("cost", style="bold"),
        Text(sparkline(cost_series), style="green"),
        Text(f"${cost_series[-1]:,.0f}", style="bold"),
        Text(f"avg ${sum(cost_series)/len(cost_series):,.0f}", style="dim"),
        _delta_text(cost_series[-1], cost_series[-2] if len(cost_series) > 1 else 0, unit="$"),
    )

    # Quota burn (% of prorated monthly cap) — only show if configured > 0
    settings = load_settings()
    quota = settings["monthly_quota_usd"]
    if quota > 0:
        burn_series = [p.quota_pct(quota) * 100 for p in points]
        burn_color = "red" if burn_series[-1] >= 100 else "yellow" if burn_series[-1] >= 60 else "green"
        extra.add_row(
            Text("burn %", style="bold"),
            Text(sparkline(burn_series), style=burn_color),
            Text(f"{burn_series[-1]:.0f}%", style=f"bold {burn_color}"),
            Text(f"avg {sum(burn_series)/len(burn_series):.0f}%", style="dim"),
            _delta_text(burn_series[-1], burn_series[-2] if len(burn_series) > 1 else 0),
        )

    body_parts: list = [
        Text("Hours by bucket", style="bold underline"),
        table,
        Text(""),
        Text("Overall", style="bold underline"),
        extra,
        Text(""),
        axis_hint,
    ]
    pricing_warning = pricing_snapshot_warning(points[-1].until)
    if pricing_warning:
        body_parts.extend((
            Text(""),
            Text(f"⚠️ {pricing_warning}", style="yellow"),
        ))
    body = Group(*body_parts)
    return Panel(
        body,
        title=f"[bold]Claude Code Trend[/bold] "
              f"[dim]·[/dim] [cyan]last {len(points)} {period}s[/cyan]",
        subtitle="[dim]ccstory trend[/dim]",
        border_style="cyan",
        padding=(1, 2),
        width=78,
    )


def render_trend_markdown(points: list[PeriodPoint], period: str) -> str:
    """Markdown table mirroring the trend card."""
    if not points:
        return "# ccstory trend\n\nNo data.\n"
    cat_series = trend_by_category(points)
    labels = [p.label for p in points]
    lines = [f"# Claude Code Trend · last {len(points)} {period}s", ""]
    lines.append(f"_Window labels (oldest → newest)_: {', '.join(labels)}")
    lines.append("")
    lines.append("| Bucket | Spark | Latest | Average | Δ vs previous |")
    lines.append("|---|---|---:|---:|---:|")
    total_series = [p.total_h for p in points]
    def pct(c, p):
        if p == 0: return "new"
        return f"{(c-p)/p*100:+.0f}%"
    lines.append(
        f"| **total** | `{sparkline(total_series)}` | "
        f"{total_series[-1]:.1f}h | {sum(total_series)/len(total_series):.1f}h | "
        f"{pct(total_series[-1], total_series[-2] if len(total_series)>1 else 0)} |"
    )
    for cat, series in cat_series.items():
        prev = series[-2] if len(series) > 1 else 0
        lines.append(
            f"| `{_md_cell(cat)}` | `{sparkline(series)}` | "
            f"{series[-1]:.1f}h | {sum(series)/len(series):.1f}h | "
            f"{pct(series[-1], prev)} |"
        )
    lines.append("")
    lines.append("Output tokens (M) and cost (USD) per period:")
    lines.append("")
    lines.append("| Period | Active (h) | Output (M tok) | Cost (USD) |")
    lines.append("|---|---:|---:|---:|")
    for p in points:
        lines.append(
            f"| `{_md_cell(p.label)}` | {p.total_h:.1f} | "
            f"{p.output_tokens/1_000_000:.2f} | ${p.cost_usd:,.0f} |"
        )
    lines.append("")
    lines.append(f"> Pricing snapshot: `{get_snapshot_date()}`.")
    pricing_warning = pricing_snapshot_warning(points[-1].until)
    if pricing_warning:
        lines.append(
            f"> ⚠️ {pricing_warning} "
            "[Check current pricing](https://platform.claude.com/docs/en/pricing)."
        )
    lines.append("")
    return "\n".join(lines)
