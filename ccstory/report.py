"""Markdown report + Rich-based terminal card for ccstory.

The markdown report is the source of truth — re-runnable, copy-pasteable,
versionable. The terminal card is a screenshot-friendly summary printed
when the CLI finishes.
"""

from __future__ import annotations

import json
from datetime import datetime

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .artifacts import ArtifactsReport
from .categorizer import color_for, load_settings, normalize_project_name
from .session_summarizer import SessionSummary
from .time_tracking import CategoryRollup, SessionStat
from .token_usage import UsageReport, fmt_tokens, get_snapshot_date
from .trends import PeriodComparison, PeriodPoint, sparkline, trend_by_category

# Supported markdown flavors for render_report().
VALID_FLAVORS = ("plain", "obsidian")


def _format_date_range(since: datetime, until: datetime) -> str:
    """Human-readable date range. Same year/month collapsed for compactness."""
    if since.date() == until.date():
        return since.strftime("%b %-d, %Y")
    if since.year == until.year and since.month == until.month:
        return f"{since.strftime('%b %-d')} – {until.strftime('%-d, %Y')}"
    if since.year == until.year:
        return f"{since.strftime('%b %-d')} – {until.strftime('%b %-d, %Y')}"
    return f"{since.strftime('%b %-d, %Y')} – {until.strftime('%b %-d, %Y')}"


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

    if comparison:
        lines.append(render_comparison_markdown(comparison))

    # Overall narrative (3-sentence synthesis across the whole period)
    if overall_narrative:
        lines.append("## What you did")
        lines.append("")
        lines.append(overall_narrative)
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
    lines.append(
        "> For exact cost / billing-window breakdowns, pair with "
        "[ccusage](https://github.com/ryoppippi/ccusage). ccstory tells the story; "
        "ccusage tells the bill."
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
) -> dict:
    """Machine-readable envelope mirroring the markdown report's content.

    Consumed by downstream tooling (dashboards, bots, sync scripts) — field
    names are a public contract governed by JSON_SCHEMA_VERSION.
    """
    total_min = sum(r.active_min for r in rollups)
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
            }
            for r in rollups
        ],
        "sessions": [
            {
                "id": s.session_id,
                "project": normalize_project_name(s.project) or s.project,
                "bucket": s.category,
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

    # --- Highlight row: biggest bucket + top session in it ---
    highlight_block: list = []
    if rollups:
        top_r = rollups[0]
        top_color = color_for(top_r.category)
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
            color = color_for(r.category)
            bars.add_row(
                Text(r.category, style=f"bold {color}"),
                _colored_bar(pct, color),
                Text(f"{r.active_min/60:.1f}h", style="bold"),
                Text(f"{pct*100:.0f}%", style="dim"),
            )

    parts: list = []
    parts.extend(highlight_block)
    parts.append(metrics)
    parts.append(Text(""))
    parts.append(Text("Time by category", style="bold underline"))
    parts.append(bars)

    if overall_narrative:
        parts.append(Text(""))
        parts.append(Text("What you did", style="bold underline"))
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
        parts.extend(render_comparison_block(comparison))

    if report_path:
        parts.append(Text(""))
        footer = Text()
        footer.append("Full report → ", style="dim")
        footer.append(report_path, style="dim underline")
        parts.append(footer)

    title_range = _format_date_range(since, until)
    return Panel(
        Group(*parts),
        title=f"[bold]Claude Code Recap[/bold] [dim]·[/dim] [cyan]{title_range}[/cyan]",
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


def render_comparison_block(cmp: PeriodComparison) -> list:
    """Renderable Rich elements: title + small comparison table for the panel."""
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
        color = color_for(d.category)
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
        color = color_for(cat)
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

    body = Group(
        Text("Hours by bucket", style="bold underline"),
        table,
        Text(""),
        Text("Overall", style="bold underline"),
        extra,
        Text(""),
        axis_hint,
    )
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
    return "\n".join(lines)
