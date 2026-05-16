"""Markdown report + Rich-based terminal card for ccstory.

The markdown report is the source of truth — re-runnable, copy-pasteable,
versionable. The terminal card is a screenshot-friendly summary printed
when the CLI finishes.
"""

from __future__ import annotations

from datetime import datetime

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .categorizer import color_for, load_settings, normalize_project_name
from .session_summarizer import SessionSummary
from .time_tracking import CategoryRollup, SessionStat
from .token_usage import UsageReport, fmt_tokens
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
    top_focus = rollups[0].category if rollups else ""
    buckets = [r.category for r in rollups]
    lines = ["---"]
    lines.append(f"date_start: {since.date().isoformat()}")
    lines.append(f"date_end: {until.date().isoformat()}")
    lines.append(f"active_hours: {total_h:.1f}")
    if top_focus:
        lines.append(f"top_focus: {top_focus}")
    lines.append("buckets: [" + ", ".join(buckets) + "]")
    lines.append(f"cost_usd: {usage.total_cost_usd:.2f}")
    lines.append(f"output_tokens: {usage.total_output}")
    lines.append("---")
    return lines


def render_report(
    label: str,
    since: datetime,
    until: datetime,
    sessions: list[SessionStat],
    rollups: list[CategoryRollup],
    usage: UsageReport,
    summaries: dict[str, SessionSummary],
    period_aggregates: dict[str, str] | None = None,
    comparison: PeriodComparison | None = None,
    flavor: str = "plain",
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
    period_aggregates = period_aggregates or {}
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
            f"| {r.category} | {h_m} | {pct:.0f}% | {r.sessions} | {r.messages:,} |"
        )
    lines.append("")

    if comparison:
        lines.append(render_comparison_markdown(comparison))

    # Per-category narrative + top sessions
    lines.append("## What you did, by category")
    lines.append("")
    for r in rollups:
        lines.append(f"### {r.category}")
        narrative = period_aggregates.get(r.category)
        if narrative:
            lines.append("")
            lines.append(narrative)
        lines.append("")
        for s in r.top_sessions:
            summ = summaries.get(s.session_id)
            text = summ.summary if summ else s.first_user_text[:100]
            time_str = s.start.strftime("%Y-%m-%d %H:%M")
            mins = int(s.active_sec // 60)
            if flavor == "obsidian":
                leaf = normalize_project_name(s.project) or s.project
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
            f"| {model} | {mu.turns} | {fmt_tokens(mu.output_tokens)} | "
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


def _colored_bar(pct: float, color: str, width: int = 28) -> Text:
    """Solid+empty block bar with per-bucket color."""
    filled = int(round(pct * width))
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="grey39")
    return bar


def render_terminal_card(
    since: datetime,
    until: datetime,
    sessions: list[SessionStat],
    rollups: list[CategoryRollup],
    usage: UsageReport,
    summaries: dict | None = None,
    period_aggregates: dict[str, str] | None = None,
    report_path: str | None = None,
    comparison: PeriodComparison | None = None,
) -> Panel:
    """Rich Panel summarizing the recap. Designed for screenshot sharing."""
    summaries = summaries or {}
    period_aggregates = period_aggregates or {}
    total_min = sum(r.active_min for r in rollups)
    total_h = total_min / 60

    # --- Highlight row: biggest bucket + top session in it ---
    highlight_block: list = []
    if rollups:
        top_r = rollups[0]
        top_color = color_for(top_r.category)
        top_pct = (top_r.active_min / total_min * 100) if total_min else 0
        headline = Text()
        headline.append("★ Top focus  ", style="bold bright_white")
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
        Text("Active",   style="dim"),  Text(f"{total_h:.1f}h", style="bold bright_white"),
        Text("Sessions", style="dim"),  Text(f"{len(sessions)}", style="bold bright_white"),
        Text("Output",   style="dim"),  Text(fmt_tokens(usage.total_output), style="bold bright_white"),
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
                Text(f"{r.active_min/60:.1f}h", style="bright_white"),
                Text(f"{pct*100:.0f}%", style="dim"),
            )

    parts: list = []
    parts.extend(highlight_block)
    parts.append(metrics)
    parts.append(Text(""))
    parts.append(Text("Time by category", style="bold underline"))
    parts.append(bars)

    if period_aggregates:
        parts.append(Text(""))
        parts.append(Text("What you did", style="bold underline"))
        for r in rollups:
            narrative = period_aggregates.get(r.category)
            if narrative:
                color = color_for(r.category)
                line = Text()
                line.append("• ", style="dim")
                line.append(r.category, style=f"bold {color}")
                line.append(f"  {narrative}", style="dim")
                parts.append(line)

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
        title=f"[bold bright_white]Claude Code Recap[/bold bright_white] [dim]·[/dim] [cyan]{title_range}[/cyan]",
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
        return Text("new", style="bright_green")
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
    table = Table.grid(padding=(0, 1))
    table.add_column(width=14)
    table.add_column(justify="right", width=8, style="bright_white")
    table.add_column(justify="right", width=8, style="dim")
    table.add_column(width=12)
    # Totals row first
    table.add_row(
        Text("total", style="bold"),
        Text(f"{cmp.current_total_h:.1f}h", style="bold bright_white"),
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
            f"| `{d.category}` | {d.current_min/60:.1f}h | "
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
        Text(sparkline(total_series), style="bold bright_white"),
        Text(f"{total_series[-1]:.1f}h", style="bold bright_white"),
        Text(f"avg {sum(total_series)/len(total_series):.1f}h", style="dim"),
        _delta_text(total_series[-1], total_series[-2] if len(total_series) > 1 else 0),
    )
    for cat, series in cat_series.items():
        color = color_for(cat)
        prev = series[-2] if len(series) > 1 else 0
        table.add_row(
            Text(cat, style=color),
            Text(sparkline(series), style=color),
            Text(f"{series[-1]:.1f}h", style="bright_white"),
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
        Text(f"{output_series[-1]:.1f}M", style="bright_white"),
        Text(f"avg {sum(output_series)/len(output_series):.1f}M", style="dim"),
        _delta_text(output_series[-1], output_series[-2] if len(output_series) > 1 else 0, unit="M"),
    )
    extra.add_row(
        Text("cost", style="bold"),
        Text(sparkline(cost_series), style="green"),
        Text(f"${cost_series[-1]:,.0f}", style="bright_white"),
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
        title=f"[bold bright_white]Claude Code Trend[/bold bright_white] "
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
            f"| `{cat}` | `{sparkline(series)}` | "
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
            f"| `{p.label}` | {p.total_h:.1f} | "
            f"{p.output_tokens/1_000_000:.2f} | ${p.cost_usd:,.0f} |"
        )
    lines.append("")
    return "\n".join(lines)
