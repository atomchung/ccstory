"""ccstory CLI entry. Usage:

    python -m ccstory                  # default month
    python -m ccstory week
    python -m ccstory month
    python -m ccstory 2026-04
    python -m ccstory all

Flags:
    --llm-narrative    Polish per-session narratives via `claude -p`
                       (slow, opt-in; shows ETA before batch)
    --minimal          Skip per-session narrative entirely (fastest)
                       (deprecated alias: --no-summary)
    --no-aggregate     Skip per-category aggregate narrative
    --reports-dir PATH Override default ~/.ccstory/reports/
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from . import __version__
from .categorizer import (
    add_category_keywords,
    color_for,
    ensure_default_config,
    list_user_categories,
    normalize_project_name,
    preview_classification,
    remove_category_keywords,
    user_rule_match,
)
from .report import (
    VALID_FLAVORS,
    print_terminal_card,
    render_report,
    render_trend_card,
    render_trend_markdown,
)
from .session_summarizer import (
    PROJECTS_DIR as SUMMARIZER_PROJECTS_DIR,
    aggregate_for_period,
    claude_bin_available,
    classify_sessions_by_content,
    get_many,
    import_from_claude_recap,
    invalidate_comparison_narratives,
    invalidate_content_buckets,
    invalidate_period_aggregates,
    missing_ids,
    summarize_session,
    synthesize_comparison,
    upsert,
)
from .time_tracking import CLAUDE_PROJECTS, collect_sessions, rollup_by_category
from .token_usage import (
    apply_prices,
    collect_usage,
    get_snapshot_date,
    load_prices_config,
)
from .trends import collect_trend, compare_to_previous

LOG = logging.getLogger("ccstory.cli")
REPORTS_DIR = Path.home() / ".ccstory" / "reports"
CONFIG_PATH = Path.home() / ".ccstory" / "config.toml"


def _parse_arg(raw: str | None) -> tuple[datetime, datetime, str]:
    """Translate week|month|all|YYYY-MM → (since, until, label).

    Returns tz-aware datetimes in the user's local timezone. Month/week
    boundaries are local-midnight aligned, so "ccstory week" means the past
    7 days as the user perceives them — not 7 calendar days in UTC.
    """
    now = datetime.now().astimezone()  # tz-aware local
    local_tz = now.tzinfo
    if raw is None or raw == "month":
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return since, now, since.strftime("%Y-%m")
    if raw == "week":
        since = now - timedelta(days=7)
        iso = since.isocalendar()
        return since, now, f"{iso[0]}-W{iso[1]:02d}"
    if raw == "all":
        return datetime(2000, 1, 1, tzinfo=local_tz), now, "all"
    m = re.match(r"^(\d{4})-(\d{2})$", raw)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        since = datetime(year, month, 1, tzinfo=local_tz)
        nxt = datetime(year + (month // 12), (month % 12) + 1, 1,
                       tzinfo=local_tz)
        until = min(now, nxt)
        return since, until, raw
    sys.exit(f"unrecognized window: {raw!r} (use week|month|all|YYYY-MM)")


def _print_first_run_preview(console: Console) -> None:
    """If no config exists yet, show how default rules classified projects."""
    created = ensure_default_config()
    if not created:
        return
    if not CLAUDE_PROJECTS.exists():
        return
    projects = [d.name for d in CLAUDE_PROJECTS.iterdir() if d.is_dir()]
    unique: dict[str, str] = {}
    for raw in projects:
        leaf = normalize_project_name(raw) or raw
        unique.setdefault(leaf, raw)
    preview = preview_classification(list(unique.values()))

    table = Table(title="First run — default bucket preview", title_style="bold")
    table.add_column("Bucket", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Sample projects", style="dim")
    for bucket, items in sorted(preview.items(), key=lambda x: -len(x[1])):
        sample = ", ".join(leaf for leaf, _ in items[:4])
        if len(items) > 4:
            sample += f", …+{len(items) - 4}"
        table.add_row(bucket, str(len(items)), sample)
    console.print(table)
    console.print(
        "[dim]Customize buckets: edit ~/.ccstory/config.toml[/dim]\n"
    )


def _aggregate_with_progress(
    label: str,
    sessions: list,
    rollups: list,
    summaries: dict,
    console: Console,
) -> dict[str, str]:
    """Synthesize a 2-3 line narrative per bucket via one claude -p call each.

    Cache-friendly: aggregate_for_period only re-runs claude -p when the set
    of session ids in a bucket changes since the cached aggregate was written.
    """
    sessions_by_cat: dict[str, list] = {}
    for s in sessions:
        sessions_by_cat.setdefault(s.category, []).append(s)

    out: dict[str, str] = {}
    buckets_with_data = [r for r in rollups
                         if any(summaries.get(s.session_id) for s in sessions_by_cat.get(r.category, []))]
    if not buckets_with_data:
        return out

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task(
            "Synthesizing per-bucket narrative (claude -p)",
            total=len(buckets_with_data),
        )
        for r in buckets_with_data:
            sids: list[str] = []
            texts: list[str] = []
            for s in sessions_by_cat.get(r.category, []):
                summ = summaries.get(s.session_id)
                if summ and summ.source in ("auto", "record"):
                    sids.append(s.session_id)
                    texts.append(summ.summary)
            if not texts:
                progress.advance(task)
                continue
            narrative = aggregate_for_period(label, r.category, sids, texts)
            if narrative:
                out[r.category] = narrative
                progress.update(task, description=f"[dim]↳ {r.category}: synthesized[/dim]")
            progress.advance(task)
    return out


def _apply_content_classification(
    sessions: list,
    summaries: dict,
    mode: str,
    console: Console,
) -> None:
    """Mutate `sessions[*].category` based on content (#25).

    - mode="content": every session re-bucketed by content
    - mode="hybrid": only sessions whose folder didn't match a USER rule
      (i.e. an explicit override in config.toml) get re-bucketed
    """
    if mode == "hybrid":
        eligible = [s for s in sessions if user_rule_match(s.project) is None]
    else:
        eligible = list(sessions)
    if not eligible:
        return

    items: list[tuple[str, str, str]] = []
    for s in eligible:
        summ = summaries.get(s.session_id)
        if not summ or not summ.summary:
            continue
        leaf = normalize_project_name(s.project) or s.project
        items.append((s.session_id, leaf, summ.summary))
    if not items:
        return

    with console.status(
        f"[dim]Content-classifying {len(items)} session(s) (one batch "
        f"claude -p call)…[/dim]"
    ):
        mapping = classify_sessions_by_content(items)
    if not mapping:
        return

    changed = 0
    for s in eligible:
        new_bucket = mapping.get(s.session_id)
        if new_bucket and new_bucket != s.category:
            s.category = new_bucket
            changed += 1
    console.print(
        f"[green]✓[/green] [dim]content-classified {len(mapping)} session(s), "
        f"{changed} re-bucketed[/dim]\n"
    )


CLAUDE_P_SEC_PER_SESSION = 40  # rough cold-start average on M1 Pro


def _backfill_with_progress(
    sessions,
    console: Console,
    use_llm: bool = False,
) -> dict[str, int]:
    """Resolve narratives for sessions not yet in DB.

    Default path is the instant first-user-msg fallback. Pass `use_llm=True`
    to opt into `claude -p` polish per session (slow); the user gets an ETA
    warning before the batch starts.
    """
    by_id = {s.session_id: s for s in sessions if getattr(s, "session_id", None)}
    miss = missing_ids(list(by_id.keys()))
    counts = {"summarized": 0, "fallback": 0, "skipped": 0,
              "already": len(by_id) - len(miss)}
    if not miss:
        return counts

    if use_llm:
        eta_min = max(1, (len(miss) * CLAUDE_P_SEC_PER_SESSION + 59) // 60)
        console.print(
            f"[yellow]![/yellow] Found {len(miss)} un-summarized "
            f"session(s). [bold]`claude -p` ETA ~{eta_min} min[/bold] "
            f"(~{CLAUDE_P_SEC_PER_SESSION}s/session cold start). "
            f"Press Ctrl+C to abort, or rerun without --llm-narrative "
            f"for an instant first-user-msg fallback.\n"
        )
        progress_desc = "Summarizing sessions via claude -p"
    else:
        progress_desc = "Generating fallback narratives"

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task(progress_desc, total=len(miss))
        for sid in miss:
            sess = by_id[sid]
            jsonl_path = SUMMARIZER_PROJECTS_DIR / sess.project / f"{sid}.jsonl"
            if not jsonl_path.exists():
                matches = list(SUMMARIZER_PROJECTS_DIR.rglob(f"{sid}.jsonl"))
                if matches:
                    jsonl_path = matches[0]
                else:
                    upsert(sid, "(jsonl not found)", "skipped", project=sess.project)
                    counts["skipped"] += 1
                    progress.advance(task)
                    continue
            result = summarize_session(sid, jsonl_path, use_llm=use_llm)
            if result and result.source == "auto":
                counts["summarized"] += 1
                latest = result.summary
                progress.update(task, description=f"[dim]↳ {latest[:50]}[/dim]")
            elif result and result.source == "fallback":
                counts["fallback"] += 1
            else:
                counts["skipped"] += 1
            progress.advance(task)
    return counts


def _run_init(argv: list[str], console: Console) -> int:
    from .init_categories import run_init
    p = argparse.ArgumentParser(
        prog="ccstory init",
        description="Scan recent sessions and auto-suggest category buckets "
                    "via one claude -p call.",
    )
    p.add_argument("--days", type=int, default=30,
                   help="How many past days to scan (default 30)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print proposal but don't write config.toml")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip the confirmation prompt")
    args = p.parse_args(argv)
    return run_init(days=args.days, dry_run=args.dry_run,
                    auto_yes=args.yes, console=console)


def _run_category(argv: list[str], console: Console) -> int:
    """`ccstory category {list,set,unset}` — edit user bucket rules.

    `set` / `unset` mutate the `[categories]` block in `~/.ccstory/config.toml`
    and invalidate any cache that was written against the previous rule shape
    (per-period aggregates + cross-period comparison narratives). The per-
    session content-classification cache is NOT cleared — those rows are
    keyed by session id, not by rule, and reusing them is correct. Use
    `ccstory <window> --refresh` if you want a full re-classify.
    """
    p = argparse.ArgumentParser(
        prog="ccstory category",
        description="Edit project-bucket rules in ~/.ccstory/config.toml.",
        epilog=(
            "Examples:\n"
            "  ccstory category set research ai-project-research\n"
            "  ccstory category set work company-repo internal-tool\n"
            "  ccstory category unset writing blog\n"
            "  ccstory category list"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="action", required=True)

    p_set = sub.add_parser("set", help="Add keywords to a bucket")
    p_set.add_argument("bucket")
    p_set.add_argument("keywords", nargs="+")

    p_unset = sub.add_parser("unset", help="Remove keywords from a bucket")
    p_unset.add_argument("bucket")
    p_unset.add_argument("keywords", nargs="+")

    sub.add_parser("list", help="Show all user-defined bucket rules")

    args = p.parse_args(argv)

    if args.action == "list":
        cats = list_user_categories()
        if not cats:
            console.print(
                "[dim]No user-defined rules yet. "
                "Built-in defaults (coding/investment/writing/other) apply.[/dim]"
            )
            console.print(
                "[dim]Add one with: ccstory category set <bucket> <keyword>…[/dim]"
            )
            return 0
        table = Table(
            title=f"User rules · {CONFIG_PATH}",
            title_style="bold",
        )
        table.add_column("Bucket", style="bold")
        table.add_column("Keywords", style="dim")
        for bucket in sorted(cats):
            color = color_for(bucket)
            table.add_row(
                f"[{color}]{bucket}[/{color}]",
                ", ".join(cats[bucket]),
            )
        console.print(table)
        return 0

    try:
        if args.action == "set":
            categories, moved = add_category_keywords(args.bucket, args.keywords)
            kw_render = ", ".join(f"`{k}`" for k in args.keywords)
            console.print(
                f"[green]✓[/green] Added {kw_render} → "
                f"[{color_for(args.bucket)}]{args.bucket}[/{color_for(args.bucket)}]"
            )
            for kw, prev in moved:
                console.print(
                    f"  [yellow]moved[/yellow] `{kw}` "
                    f"from [{color_for(prev)}]{prev}[/{color_for(prev)}]"
                )
        else:  # unset
            categories, missing = remove_category_keywords(
                args.bucket, args.keywords,
            )
            kept = [k for k in args.keywords if k.lower() not in missing]
            if kept:
                kw_render = ", ".join(f"`{k}`" for k in kept)
                console.print(
                    f"[green]✓[/green] Removed {kw_render} from "
                    f"[{color_for(args.bucket)}]{args.bucket}[/{color_for(args.bucket)}]"
                )
            for kw in missing:
                console.print(
                    f"  [yellow]not found:[/yellow] `{kw}` was not in `{args.bucket}`"
                )
    except ValueError as e:
        console.print(f"[red]✗[/red] {e}")
        return 1

    # Rule changes can shift bucket assignments retroactively, so any
    # cached narrative that was written against the old shape is now stale.
    # The per-session content classification cache (session_content_buckets)
    # stays — those rows are keyed by session id and remain correct.
    agg_n = invalidate_period_aggregates()
    cmp_n = invalidate_comparison_narratives()
    if agg_n or cmp_n:
        bits = []
        if agg_n:
            bits.append(f"{agg_n} per-bucket narrative(s)")
        if cmp_n:
            bits.append(f"{cmp_n} vs-previous narrative(s)")
        console.print(
            f"[dim]Invalidated {' + '.join(bits)} so they regenerate next run.[/dim]"
        )
    console.print(
        f"[dim]To re-classify sessions whose bucket may have changed, run "
        f"`ccstory week --refresh`.[/dim]"
    )
    return 0


def _run_trend(argv: list[str], console: Console) -> int:
    if not CLAUDE_PROJECTS.exists():
        sys.exit(f"No Claude Code data at {CLAUDE_PROJECTS}.")
    prices, snapshot = load_prices_config(CONFIG_PATH)
    apply_prices(prices, snapshot)
    p = argparse.ArgumentParser(
        prog="ccstory trend",
        description="Show per-bucket sparklines over N periods.",
    )
    p.add_argument("--weeks", type=int, default=None,
                   help="Number of 7-day windows (default 8)")
    p.add_argument("--months", type=int, default=None,
                   help="Number of calendar months")
    p.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    args = p.parse_args(argv)

    period = "month" if args.months else "week"
    count = args.months or args.weeks or 8

    with console.status(
        f"[dim]Computing trend over last {count} {period}s…[/dim]"
    ):
        points = collect_trend(period=period, count=count)
    if not any(p.total_h for p in points):
        sys.exit("No engaged sessions across the trend window.")

    args.reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.reports_dir / f"trend-{period}-{count}.md"
    out_path.write_text(render_trend_markdown(points, period), encoding="utf-8")

    console.print(render_trend_card(points, period))
    console.print(f"[dim]Full report → {out_path}[/dim]")
    console.print(f"[dim]Prices as of {get_snapshot_date()}[/dim]")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    console = Console()

    # Manual dispatch for subcommands — keeps default `ccstory week`
    # / `ccstory month` flow simple positional.
    if raw and raw[0] == "trend":
        logging.basicConfig(level=logging.WARNING)
        return _run_trend(raw[1:], console)
    if raw and raw[0] == "init":
        logging.basicConfig(level=logging.WARNING)
        return _run_init(raw[1:], console)
    if raw and raw[0] == "category":
        logging.basicConfig(level=logging.WARNING)
        return _run_category(raw[1:], console)

    parser = argparse.ArgumentParser(
        prog="ccstory",
        description="Claude Code usage recap with narrative. "
                    "ccusage tells you the bill; ccstory tells the story.",
        epilog=(
            "Subcommands:\n"
            "  ccstory init [--days N] [--dry-run] [-y]\n"
            "      Scan recent sessions and propose category buckets via\n"
            "      one claude -p call. Writes ~/.ccstory/config.toml.\n"
            "  ccstory trend [--weeks N | --months N]\n"
            "      Per-bucket sparklines + burn-% over N periods.\n"
            "  ccstory category {list,set,unset} ...\n"
            "      Edit project-bucket rules from the CLI.\n"
            "\n"
            "Examples:\n"
            "  ccstory week                  # last 7 days, instant fallback\n"
            "                                # narratives + aggregate synthesis\n"
            "  ccstory week --llm-narrative  # polish per-session via claude -p\n"
            "                                # (slow; shows ETA before batch)\n"
            "  ccstory week --no-aggregate   # skip aggregate synthesis\n"
            "  ccstory week --refresh        # re-classify cached sessions in window\n"
            "  ccstory 2026-04               # specific month\n"
            "  ccstory trend --months 6      # 6-month sparkline view\n"
            "  ccstory init -y               # auto-categorize (no prompt)\n"
            "  ccstory category set research ai-project-research"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("window", nargs="?", default="month",
                        help="week | month | all | YYYY-MM (default: month)")
    parser.add_argument("--minimal", action="store_true",
                        help="Skip per-session narrative entirely — numbers "
                             "only, no per-session lines (fastest path)")
    parser.add_argument("--no-summary", action="store_true",
                        help=argparse.SUPPRESS)  # deprecated alias for --minimal
    parser.add_argument("--llm-narrative", action="store_true",
                        help="Polish per-session narratives via `claude -p` "
                             "(slow ~40s/session cold start; shows ETA "
                             "before batch). Default is an instant "
                             "first-user-msg fallback.")
    parser.add_argument("--no-aggregate", action="store_true",
                        help="Skip the per-bucket aggregate narrative "
                             "(one claude -p call per non-empty bucket)")
    parser.add_argument("--no-compare", action="store_true",
                        help="Skip the vs-previous-window comparison block")
    parser.add_argument("--for", dest="flavor", choices=VALID_FLAVORS,
                        default="plain",
                        help="Markdown variant for the saved report. "
                             "`obsidian` adds YAML front-matter + [[wikilinks]] "
                             "so the report drops into a PKM vault cleanly.")
    parser.add_argument("--no-compare-narrative", action="store_true",
                        help="Skip the 1-2 sentence claude -p synthesis "
                             "under the comparison table (numeric deltas "
                             "still render)")
    parser.add_argument("--classify", choices=["folder", "content", "hybrid"],
                        default="hybrid",
                        help="How to bucket sessions. `folder` uses only "
                             "config + folder-name rules. `content` runs a "
                             "batch claude -p over each session's narrative. "
                             "`hybrid` (default) keeps the folder bucket when "
                             "a user rule in config.toml matched, otherwise "
                             "falls back to content classification.")
    parser.add_argument("--refresh", action="store_true",
                        help="Re-classify cached sessions in this window. "
                             "Wipes the content-classification cache for the "
                             "sessions inside [since, until] and lets the "
                             "next pass redo them. Use after editing "
                             "[categories] rules.")
    parser.add_argument("--refresh-all", action="store_true",
                        help="Wipe the entire content-classification cache, "
                             "not just this window. Implies --refresh.")
    parser.add_argument("--reports-dir", type=Path, default=REPORTS_DIR,
                        help=f"Markdown report output dir (default: {REPORTS_DIR})")
    parser.add_argument("--version", action="version",
                        version=f"ccstory {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(raw)

    # Deprecation: --no-summary is the old name for --minimal. The flag's
    # documented behavior ("skip claude -p") never matched the code path
    # (which skips the entire narrative pipeline), so --minimal is the
    # honest name. Keep the old flag for one minor release as an alias.
    if args.no_summary and not args.minimal:
        print(
            "ccstory: warning: --no-summary is deprecated and will be removed "
            "in a future release. Use --minimal instead.",
            file=sys.stderr,
        )
        args.minimal = True

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not CLAUDE_PROJECTS.exists():
        sys.exit(f"No Claude Code data at {CLAUDE_PROJECTS}. "
                 "Have you used Claude Code yet?")

    # Load user price overrides (config [prices] table). No-op if absent.
    prices, snapshot = load_prices_config(CONFIG_PATH)
    apply_prices(prices, snapshot)

    _print_first_run_preview(console)

    since, until, label = _parse_arg(args.window)
    console.print(
        f"[dim]Window:[/dim] [bold]{since.date()} → {until.date()}[/bold] "
        f"[dim]({label})[/dim]\n"
    )

    with console.status("[dim]Parsing sessions and token usage…[/dim]"):
        sessions = collect_sessions(since, until)
        if not sessions:
            sys.exit("No engaged sessions in this window.")
        rollups = rollup_by_category(sessions)
        # since/until are tz-aware local; collect_usage normalizes to UTC.
        usage = collect_usage(since, until)

    console.print(
        f"[green]✓[/green] {len(sessions)} sessions · "
        f"{len(rollups)} categories · {usage.assistant_turns:,} turns\n"
    )

    # `--refresh` wipes the content-classification cache so the rules that
    # just changed actually take effect. Without this, sessions that were
    # claude-classified before the rule edit keep their old bucket. Done
    # AFTER session collection so we know exactly which ids to scope to.
    if args.refresh_all:
        c_n = invalidate_content_buckets(None)
        a_n = invalidate_period_aggregates(None)
        m_n = invalidate_comparison_narratives()
        console.print(
            f"[yellow]Refreshed[/yellow] [dim]{c_n} cached bucket(s), "
            f"{a_n} aggregate(s), {m_n} comparison narrative(s) — "
            f"global wipe[/dim]\n"
        )
    elif args.refresh:
        sids = [s.session_id for s in sessions]
        c_n = invalidate_content_buckets(sids)
        a_n = invalidate_period_aggregates(label)
        m_n = invalidate_comparison_narratives()
        console.print(
            f"[yellow]Refreshed[/yellow] [dim]{c_n} cached bucket(s) in this "
            f"window, {a_n} aggregate(s) for `{label}`, "
            f"{m_n} comparison narrative(s)[/dim]\n"
        )

    summaries: dict = {}
    period_aggregates: dict[str, str] = {}
    if not args.minimal:
        imported = import_from_claude_recap()
        if imported:
            console.print(
                f"[green]✓[/green] [dim]imported {imported} cached "
                f"summarie(s) from ~/.claude/session_summaries.db "
                f"(/recap)[/dim]\n"
            )
        if args.llm_narrative and not claude_bin_available():
            console.print(
                "[yellow]![/yellow] [dim]`claude` not on PATH — "
                "--llm-narrative will fall back to first user message[/dim]\n"
            )
        counts = _backfill_with_progress(
            sessions, console, use_llm=args.llm_narrative,
        )
        console.print(
            f"[green]✓[/green] [dim]summarized={counts['summarized']} · "
            f"fallback={counts['fallback']} · skipped={counts['skipped']} · "
            f"cached={counts['already']}[/dim]\n"
        )
        summaries = get_many([s.session_id for s in sessions])

        # Session-level content classification (#25). Folder-only buckets
        # mis-attribute monorepo / mixed-purpose sessions; one batch
        # claude -p call resolves each session by its actual content.
        if args.classify != "folder" and summaries:
            _apply_content_classification(
                sessions, summaries, args.classify, console,
            )
            # Re-roll up so per-bucket totals reflect the new categories
            rollups = rollup_by_category(sessions)

        if not args.no_aggregate and summaries:
            period_aggregates = _aggregate_with_progress(
                label, sessions, rollups, summaries, console,
            )
            if period_aggregates:
                console.print(
                    f"[green]✓[/green] [dim]aggregated "
                    f"{len(period_aggregates)} bucket(s)[/dim]\n"
                )

    comparison = None
    if not args.no_compare and args.window != "all":
        with console.status("[dim]Computing previous-window comparison…[/dim]"):
            comparison = compare_to_previous(
                current_sessions=sessions,
                current_rollups=rollups,
                current_usage=usage,
                current_label=label,
                since=since,
                until=until,
            )
        if (
            comparison
            and not args.no_compare_narrative
            and not args.no_summary
            and summaries
        ):
            prev_summaries = get_many(comparison.previous_session_ids)
            with console.status(
                "[dim]Synthesizing week-over-week narrative (claude -p)…[/dim]"
            ):
                comparison.narrative = synthesize_comparison(
                    current_key=label,
                    previous_key=comparison.previous_label,
                    current_summaries=[
                        (s.session_id, summaries[s.session_id].summary)
                        for s in sessions
                        if s.session_id in summaries
                    ],
                    previous_summaries=[
                        (sid, prev_summaries[sid].summary)
                        for sid in comparison.previous_session_ids
                        if sid in prev_summaries
                    ],
                    deltas=[
                        (d.category, d.current_min, d.previous_min)
                        for d in comparison.deltas
                    ],
                )

    args.reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.reports_dir / f"recap-{label}.md"
    md = render_report(
        label=label,
        since=since,
        until=until,
        sessions=sessions,
        rollups=rollups,
        usage=usage,
        summaries=summaries,
        period_aggregates=period_aggregates,
        comparison=comparison,
        flavor=args.flavor,
    )
    out_path.write_text(md, encoding="utf-8")

    print_terminal_card(
        since=since,
        until=until,
        sessions=sessions,
        rollups=rollups,
        usage=usage,
        summaries=summaries,
        period_aggregates=period_aggregates,
        report_path=str(out_path),
        comparison=comparison,
        console=console,
    )
    console.print(f"[dim]Prices as of {get_snapshot_date()}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
