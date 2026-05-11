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

from .session_summarizer import SessionSummary
from .time_tracking import CategoryRollup, SessionStat
from .token_usage import UsageReport, fmt_tokens


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
    lines.append(f"# Claude Code Recap · {label}")
    lines.append("")
    lines.append(f"> Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"> Window: {since.date()} → {until.date()}")
    lines.append(
        f"> **{total_h:.1f}h active** · {len(sessions)} sessions · {total_msgs:,} messages"
    )
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


def _bar(pct: float, width: int = 28) -> str:
    """Solid+empty block bar — kept colorless for screenshot consistency."""
    filled = int(round(pct * width))
    return "█" * filled + "░" * (width - filled)


def render_terminal_card(
    label: str,
    sessions: list[SessionStat],
    rollups: list[CategoryRollup],
    usage: UsageReport,
    period_aggregates: dict[str, str] | None = None,
    report_path: str | None = None,
) -> Panel:
    """Rich Panel summarizing the recap. Designed for screenshot sharing."""
    period_aggregates = period_aggregates or {}
    total_min = sum(r.active_min for r in rollups)
    total_h = total_min / 60

    metrics = Table.grid(padding=(0, 2))
    metrics.add_column(justify="left", style="dim")
    metrics.add_column(justify="left", style="bold")
    metrics.add_column(justify="left", style="dim")
    metrics.add_column(justify="left", style="bold")
    metrics.add_column(justify="left", style="dim")
    metrics.add_column(justify="left", style="bold")
    metrics.add_row(
        "Active",   f"{total_h:.1f}h",
        "Sessions", f"{len(sessions)}",
        "Output",   fmt_tokens(usage.total_output),
    )
    metrics.add_row(
        "Turns",    f"{usage.assistant_turns:,}",
        "Cache",    f"{usage.cache_hit_ratio*100:.0f}%",
        "Cost",     f"${usage.total_cost_usd:,.0f}",
    )

    bars = Table.grid(padding=(0, 1))
    bars.add_column(style="bold", width=14)
    bars.add_column(no_wrap=True)
    bars.add_column(justify="right", style="dim", width=7)
    bars.add_column(justify="right", style="dim", width=5)
    if total_min > 0:
        for r in rollups:
            pct = r.active_min / total_min
            bars.add_row(
                r.category,
                _bar(pct),
                f"{r.active_min/60:.1f}h",
                f"{pct*100:.0f}%",
            )

    parts = [metrics, Text(""), Text("Time by category", style="bold"), bars]

    if period_aggregates:
        parts.append(Text(""))
        parts.append(Text("What you did", style="bold"))
        for r in rollups:
            narrative = period_aggregates.get(r.category)
            if narrative:
                parts.append(Text(f"• {r.category}: {narrative}", style="dim"))

    if report_path:
        parts.append(Text(""))
        parts.append(Text(f"Full report → {report_path}", style="dim"))

    return Panel(
        Group(*parts),
        title=f"[bold]Claude Code Recap · {label}[/bold]",
        subtitle="[dim]ccstory[/dim]",
        border_style="cyan",
        padding=(1, 2),
        width=72,
    )


def print_terminal_card(*args, console: Console | None = None, **kwargs) -> None:
    """Print the Rich Panel to console."""
    console = console or Console()
    console.print(render_terminal_card(*args, **kwargs))
