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

from .categorizer import color_for
from .session_summarizer import SessionSummary
from .time_tracking import CategoryRollup, SessionStat
from .token_usage import UsageReport, fmt_tokens


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


def render_report(
    label: str,
    since: datetime,
    until: datetime,
    sessions: list[SessionStat],
    rollups: list[CategoryRollup],
    usage: UsageReport,
    summaries: dict[str, SessionSummary],
    period_aggregates: dict[str, str] | None = None,
) -> str:
    """Produce the full markdown report."""
    period_aggregates = period_aggregates or {}
    total_min = sum(r.active_min for r in rollups)
    total_h = total_min / 60
    total_msgs = sum(r.messages for r in rollups)

    lines: list[str] = []
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
