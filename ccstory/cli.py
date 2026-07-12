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
import json as _json
import logging
import os
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
from .artifacts import collect_artifacts
from .categorizer import (
    add_category_keywords,
    color_for,
    ensure_default_config,
    list_user_categories,
    load_settings,
    normalize_project_name,
    preview_classification,
    remove_category_keywords,
    resolve_session_bucket,
)
from .report import (
    VALID_FLAVORS,
    build_report_json,
    build_trend_json,
    print_terminal_card,
    render_report,
    render_trend_card,
    render_trend_markdown,
)
from .session_summarizer import (
    CCSTORY_LANG_ENV,
    PROJECTS_DIR as SUMMARIZER_PROJECTS_DIR,
    _classify_cache_get_many,
    claude_bin_available,
    classify_sessions_by_content,
    get_many,
    import_from_claude_recap,
    invalidate_comparison_narratives,
    invalidate_content_buckets,
    invalidate_period_aggregates,
    language_directive,
    missing_ids,
    summarize_session,
    synthesize_category_for_period,
    synthesize_comparison,
    synthesize_overall_for_period,
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

VALID_OUTPUT_FORMATS = ("auto", "markdown", "card", "json")


def apply_lang_override(lang: str | None) -> None:
    """Promote ``--lang`` into the env so every prompt-assembly call sees it.

    ``language_directive()`` reads ``$CCSTORY_LANG`` at the top of its
    resolution chain. Setting it here (instead of threading the value
    through every callsite) keeps the CLI surface tiny and matches the
    Unix convention that the flag is shorthand for the env var.
    Also flushes the directive's ``lru_cache`` so a re-invocation in
    the same Python process picks up the new value.
    """
    if not lang:
        return
    cleaned = lang.strip()
    if not cleaned:
        return
    os.environ[CCSTORY_LANG_ENV] = cleaned
    language_directive.cache_clear()


def resolve_output_format(arg: str, *, env: dict | None = None, isatty: bool | None = None) -> str:
    """Resolve --format=auto to a concrete format.

    Claude Code chat renders Markdown but mangles Rich panels (ANSI escapes
    drop, table alignment breaks). When `CLAUDECODE=1` is in the environment
    or stdout is not a tty (piped, redirected, headless), prefer Markdown.
    Otherwise keep the Rich card the terminal user already expects.

    Validation of `arg` is the parser's job (`choices=VALID_OUTPUT_FORMATS`):
    any non-"auto" string is returned verbatim so the helper stays a pure
    dispatch decision instead of duplicating argparse error handling.
    """
    if arg != "auto":
        return arg
    env = os.environ if env is None else env
    if env.get("CLAUDECODE") == "1":
        return "markdown"
    if isatty is None:
        isatty = sys.stdout.isatty()
    if not isatty:
        return "markdown"
    return "card"


def _parse_arg(raw: str | None) -> tuple[datetime, datetime, str]:
    """Translate week|month|all|YYYY-MM → (since, until, label).

    Returns tz-aware datetimes in the user's local timezone. Month/week
    boundaries are local-midnight aligned, so "ccstory week" means the past
    7 days as the user perceives them — not 7 calendar days in UTC.

    Label policy: when the window endpoint is ``now`` (relative time), the
    label embeds both endpoint dates as ``YYYY-MM-DD_YYYY-MM-DD`` so two
    runs on different days don't collide on the output file. Only a fully
    past ``YYYY-MM`` keeps the compact symbolic label (#58).
    """
    now = datetime.now().astimezone()  # tz-aware local
    local_tz = now.tzinfo
    def _range_label(a: datetime, b: datetime) -> str:
        return f"{a:%Y-%m-%d}_{b:%Y-%m-%d}"

    if raw is None or raw == "month":
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return since, now, _range_label(since, now)
    if raw == "week":
        since = now - timedelta(days=7)
        return since, now, _range_label(since, now)
    if raw == "all":
        return datetime(2000, 1, 1, tzinfo=local_tz), now, f"all-thru-{now:%Y-%m-%d}"
    m = re.match(r"^(\d{4})-(\d{2})$", raw)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        since = datetime(year, month, 1, tzinfo=local_tz)
        nxt = datetime(year + (month // 12), (month % 12) + 1, 1,
                       tzinfo=local_tz)
        until = min(now, nxt)
        # In-progress month: endpoint is `now`, so the window is relative —
        # use a range label. Fully past month: keep the compact `YYYY-MM`.
        if until < nxt:
            return since, until, _range_label(since, until)
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


def _synthesize_overall_with_progress(
    label: str,
    sessions: list,
    rollups: list,
    summaries: dict,
    console: Console,
) -> str | None:
    """Synthesize ONE 3-sentence overall narrative for the period.

    Single `claude -p` call across all categories — replaces the old
    per-bucket aggregate path. Cache-friendly: only re-runs when the set
    of session ids changes since the cached narrative was written.
    """
    sessions_by_cat: dict[str, list[tuple[str, str]]] = {}
    for s in sessions:
        summ = summaries.get(s.session_id)
        if not summ or summ.source not in ("auto", "record"):
            continue
        sessions_by_cat.setdefault(s.category, []).append(
            (s.session_id, summ.summary)
        )
    if not sessions_by_cat:
        return None

    category_hours = [(r.category, r.active_min / 60) for r in rollups]

    with console.status(
        "[dim]Synthesizing 3-sentence overall narrative (claude -p)…[/dim]"
    ):
        return synthesize_overall_for_period(
            period_key=label,
            category_hours=category_hours,
            sessions_by_category=sessions_by_cat,
        )


def _synthesize_categories_with_progress(
    label: str,
    sessions: list,
    rollups: list,
    summaries: dict,
    console: Console,
) -> dict[str, str]:
    """One 2-3 line narrative per bucket (#57), rollup order.

    Same input contract as the overall narrative: only sessions with a real
    summary (auto/record) feed the prompt. A bucket with none is skipped;
    a bucket whose claude -p fails is simply absent from the result.
    """
    sessions_by_cat: dict[str, list[tuple[str, str]]] = {}
    for s in sessions:
        summ = summaries.get(s.session_id)
        if not summ or summ.source not in ("auto", "record"):
            continue
        sessions_by_cat.setdefault(s.category, []).append(
            (s.session_id, summ.summary)
        )
    cats = [r.category for r in rollups if r.category in sessions_by_cat]
    out: dict[str, str] = {}
    for i, cat in enumerate(cats, 1):
        items = sessions_by_cat[cat]
        with console.status(
            f"[dim]Synthesizing bucket narrative {i}/{len(cats)} — "
            f"{cat} (claude -p)…[/dim]"
        ):
            narrative = synthesize_category_for_period(
                period_key=label,
                category=cat,
                session_ids=[sid for sid, _ in items],
                summaries=[text for _, text in items],
            )
        if narrative:
            out[cat] = narrative
    return out


def _resolve_all_sessions(
    sessions: list,
    summaries: dict,
    mode: str,
    fallback: str,
    console: Console,
) -> None:
    """Resolve every session's bucket via the unified resolver, batching LLM
    for cache misses. Mutates ``sessions[*].category`` and ``.category_source``.

    Two-pass design:
      Pass 1: cache + folder rule walk-through (single SQL query for cache)
      Pass 2: one batched ``claude -p`` for sessions marked ``needs_llm``,
              when summaries exist and mode allows LLM.

    Sessions that still have no resolution (folder mode, or LLM unavailable,
    or missing summary) collapse to ``fallback`` so ``.category`` is never
    empty downstream.
    """
    if not sessions:
        return

    # Pass 1: bulk fetch cache, then resolver per session.
    cache_map = _classify_cache_get_many([s.session_id for s in sessions])
    needs_llm: list = []
    for s in sessions:
        bucket, source = resolve_session_bucket(
            s.project, cache_map.get(s.session_id), mode=mode, fallback=fallback,
        )
        if source == "needs_llm":
            needs_llm.append(s)
        else:
            s.category = bucket
            s.category_source = source

    # Pass 2: batch LLM for cache misses (only when mode != folder).
    if needs_llm and mode != "folder":
        items: list[tuple[str, str, str]] = []
        for s in needs_llm:
            summ = summaries.get(s.session_id)
            if not summ or not summ.summary:
                continue
            leaf = normalize_project_name(s.project) or s.project
            items.append((s.session_id, leaf, summ.summary))

        mapping: dict[str, str] = {}
        if items:
            total_chunks = (len(items) + 79) // 80
            chunk_suffix = (
                f" (1 batch)" if total_chunks == 1
                else f" (0/{total_chunks} batches)"
            )
            with console.status(
                f"[dim]Content-classifying {len(items)} session(s)"
                f"{chunk_suffix}…[/dim]"
            ) as status:
                def _tick(done: int, total: int) -> None:
                    if total > 1:
                        status.update(
                            f"[dim]Content-classifying {len(items)} session(s)"
                            f" ({done}/{total} batches)…[/dim]"
                        )
                mapping = classify_sessions_by_content(
                    items, on_chunk_complete=_tick,
                )

        for s in needs_llm:
            new_bucket = mapping.get(s.session_id)
            if new_bucket:
                s.category = new_bucket
                s.category_source = "llm_fresh"
            else:
                # No summary, LLM unavailable, or parse failure → fallback.
                s.category = fallback
                s.category_source = "fallback"
        if mapping:
            console.print(
                f"[green]✓[/green] [dim]content-classified {len(mapping)} "
                f"session(s) via claude -p[/dim]\n"
            )
    else:
        # Folder mode (or no LLM path) → assign fallback to leftovers.
        for s in needs_llm:
            s.category = fallback
            s.category_source = "fallback"


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
    from .init_categories import DEEP_DEFAULT_DAYS, DEEP_DEFAULT_MAX, run_init
    p = argparse.ArgumentParser(
        prog="ccstory init",
        description=(
            "Set up classification. Three modes — picked interactively when no\n"
            "flag is given, or selected via flag:\n"
            "  Quick   Infer categories from folder names + sample messages\n"
            "          (~10s, 1 claude -p call). Best when folder names are\n"
            "          descriptive.\n"
            "  Deep    Classify recent sessions individually, write per-session\n"
            "          cache + majority-vote folder rules (~1 min, last 7d,\n"
            "          cap 200). Best when folder names are brand-name or\n"
            "          catch-all.\n"
            "  Skip    Scaffold template config, no LLM. Built-in keyword\n"
            "          defaults take over."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--quick", action="store_const", const="quick", dest="mode",
        help="Infer categories from folder names + samples (~10s)",
    )
    mode_group.add_argument(
        "--deep", action="store_const", const="deep", dest="mode",
        help="Classify recent sessions individually (~1 min)",
    )
    mode_group.add_argument(
        "--skip", action="store_const", const="skip", dest="mode",
        help="Use built-in keyword defaults only (no LLM)",
    )
    p.add_argument("--days", type=int, default=30,
                   help="Quick mode: how many past days to scan for folder "
                        "samples (default 30)")
    p.add_argument("--deep-days", type=int, default=DEEP_DEFAULT_DAYS,
                   help=f"Deep mode: time range to sample (default "
                        f"{DEEP_DEFAULT_DAYS})")
    p.add_argument("--max", dest="deep_max", type=int, default=DEEP_DEFAULT_MAX,
                   help=f"Deep mode: session cap (default {DEEP_DEFAULT_MAX})")
    p.add_argument("--dry-run", action="store_true",
                   help="Print proposal but don't write config.toml")
    args = p.parse_args(argv)

    # --days/--deep-days/--max only make sense with their respective mode;
    # error early instead of silently using a default users didn't intend.
    if args.mode is None and (
        argv and any(a.startswith(("--deep-days", "--max")) for a in argv)
    ):
        p.error("--deep-days / --max require --deep")
    return run_init(
        mode=args.mode,
        days=args.days,
        deep_days=args.deep_days,
        deep_max=args.deep_max,
        dry_run=args.dry_run,
        console=console,
    )


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


def _run_trend(argv: list[str]) -> int:
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
    p.add_argument("--classify", choices=["folder", "content", "hybrid"],
                   default="hybrid",
                   help="Bucket resolution mode — must match what `ccstory "
                        "week` is using to keep vocabulary aligned across "
                        "trend, week, and vs-previous views.")
    p.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    p.add_argument("--format", dest="output_format",
                   choices=VALID_OUTPUT_FORMATS, default="auto",
                   help="Output style; see `ccstory --help`.")
    p.add_argument("--json", dest="output_format", action="store_const",
                   const="json", help="Shorthand for --format=json.")
    p.add_argument("--lang", dest="lang", default=None,
                   help="Narrative response language for this run "
                        "(e.g. \"Traditional Chinese\"). Overrides "
                        "$CCSTORY_LANG, config.toml, CLAUDE.md, "
                        "settings.json, and system locale.")
    args = p.parse_args(argv)

    apply_lang_override(args.lang)
    output_format = resolve_output_format(args.output_format)
    console = Console(stderr=(output_format in ("markdown", "json")))

    period = "month" if args.months else "week"
    count = args.months or args.weeks or 8

    # Trend must use the same fallback bucket as the main flow — otherwise a
    # user who set default_bucket = "other" gets "coding" leaking into trend
    # for cache-miss sessions. Bug surfaced in codex review of PR-A.
    fallback_bucket = load_settings(CONFIG_PATH).get("default_bucket", "coding")
    with console.status(
        f"[dim]Computing trend over last {count} {period}s…[/dim]"
    ):
        points = collect_trend(
            period=period, count=count,
            mode=args.classify, fallback=fallback_bucket,
        )
    if not any(p.total_h for p in points):
        sys.exit("No engaged sessions across the trend window.")

    args.reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.reports_dir / f"trend-{period}-{count}.md"
    md = render_trend_markdown(points, period)
    out_path.write_text(md, encoding="utf-8")

    if output_format == "json":
        payload = build_trend_json(points, period)
        sys.stdout.write(_json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        console.print(f"[dim]Full report → {out_path}[/dim]")
        console.print(f"[dim]Prices as of {get_snapshot_date()}[/dim]")
    elif output_format == "markdown":
        sys.stdout.write(md)
        if not md.endswith("\n"):
            sys.stdout.write("\n")
        console.print(f"[dim]Full report → {out_path}[/dim]")
        console.print(f"[dim]Prices as of {get_snapshot_date()}[/dim]")
    else:
        console.print(render_trend_card(points, period))
        console.print(f"[dim]Full report → {out_path}[/dim]")
        console.print(f"[dim]Prices as of {get_snapshot_date()}[/dim]")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]

    # Manual dispatch for subcommands — keeps default `ccstory week`
    # / `ccstory month` flow simple positional. `init` / `category` only
    # emit progress lines, so a stdout Console is fine; `trend` and the
    # default recap path resolve --format themselves and may switch to
    # a stderr Console so the markdown report can own stdout.
    if raw and raw[0] == "trend":
        logging.basicConfig(level=logging.WARNING)
        return _run_trend(raw[1:])
    if raw and raw[0] == "init":
        logging.basicConfig(level=logging.WARNING)
        return _run_init(raw[1:], Console())
    if raw and raw[0] == "category":
        logging.basicConfig(level=logging.WARNING)
        return _run_category(raw[1:], Console())

    parser = argparse.ArgumentParser(
        prog="ccstory",
        description="Claude Code usage recap with narrative. "
                    "ccusage tells you the bill; ccstory tells the story.",
        epilog=(
            "Subcommands:\n"
            "  ccstory init [--quick | --deep | --skip] [--dry-run]\n"
            "      Set up category classification. Interactive picker by\n"
            "      default; pass a mode flag to skip the prompt. Writes\n"
            "      ~/.ccstory/config.toml.\n"
            "  ccstory trend [--weeks N | --months N]\n"
            "      Per-bucket sparklines + burn-% over N periods.\n"
            "  ccstory category {list,set,unset} ...\n"
            "      Edit project-bucket rules from the CLI.\n"
            "\n"
            "Examples:\n"
            "  ccstory week                  # last 7 days, instant fallback\n"
            "                                # narratives + overall synthesis\n"
            "  ccstory week --llm-narrative  # polish per-session via claude -p\n"
            "                                # (slow; shows ETA before batch)\n"
            "  ccstory week --no-aggregate   # skip overall synthesis\n"
            "  ccstory week --refresh        # re-classify cached sessions in window\n"
            "  ccstory 2026-04               # specific month\n"
            "  ccstory trend --months 6      # 6-month sparkline view\n"
            "  ccstory init --quick          # folder-name LLM, no prompt\n"
            "  ccstory init --deep           # session-level LLM, no prompt\n"
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
                        help="Skip the 3-sentence overall narrative "
                             "(one claude -p call across all buckets)")
    parser.add_argument("--no-compare", action="store_true",
                        help="Skip the vs-previous-window comparison block")
    parser.add_argument("--narrative", choices=["overall", "per-category", "both"],
                        default="overall",
                        help="Narrative depth. `overall` (default) = one "
                             "3-sentence synthesis. `per-category` = 2-3 "
                             "lines per bucket instead (one claude -p per "
                             "bucket, cached until the bucket's session set "
                             "changes). `both` = overall first, then "
                             "per-bucket sections.")
    parser.add_argument("--no-artifacts", action="store_true",
                        help="Skip the What-shipped section (git commits / "
                             "merged PRs / releases / stars / PyPI downloads "
                             "for repos worked on this window). Also "
                             "disable persistently via config.toml "
                             "[artifacts] enabled = false.")
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
    parser.add_argument("--format", dest="output_format",
                        choices=VALID_OUTPUT_FORMATS, default="auto",
                        help="Output style. `card` = Rich panel (terminal). "
                             "`markdown` = full Markdown report to stdout "
                             "(Claude Code chat / pipe friendly). "
                             "`json` = machine-readable report to stdout. "
                             "`auto` (default) = markdown when CLAUDECODE=1 "
                             "or stdout is non-tty, else card.")
    parser.add_argument("--json", dest="output_format", action="store_const",
                        const="json",
                        help="Shorthand for --format=json (ccusage-style). "
                             "stdout = one JSON object; progress on stderr.")
    parser.add_argument("--lang", dest="lang", default=None,
                        help="Narrative response language for this run "
                             "(e.g. \"Traditional Chinese\", \"日本語\"). "
                             "Overrides $CCSTORY_LANG, ~/.ccstory/config.toml "
                             "`language`, ~/.claude/CLAUDE.md, "
                             "~/.claude/settings.json `language`, and system "
                             "locale. Persist the choice by setting "
                             "`language = \"...\"` in ~/.ccstory/config.toml.")
    parser.add_argument("--version", action="version",
                        version=f"ccstory {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(raw)

    # --lang promotes into $CCSTORY_LANG before any prompt assembly runs,
    # so every claude -p call this invocation makes sees the override.
    apply_lang_override(args.lang)

    # Resolve --format before building the console: in markdown/json mode,
    # all progress / status output must go to stderr so stdout is a clean
    # stream the chat (or downstream tools) can consume.
    output_format = resolve_output_format(args.output_format)
    console = Console(stderr=(output_format in ("markdown", "json")))

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
        # since/until are tz-aware local; collect_usage normalizes to UTC.
        usage = collect_usage(since, until)

    console.print(
        f"[green]✓[/green] {len(sessions)} sessions · "
        f"{usage.assistant_turns:,} turns\n"
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
    overall_narrative: str | None = None
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

    # Resolver pass — single point where every session's bucket gets assigned.
    # Reads LLM cache once, batches uncached sessions into one claude -p call
    # when summaries are available. Same priority chain runs in compare_to_
    # previous() so cross-window comparison stays symmetric (fixes #61).
    settings = load_settings(CONFIG_PATH)
    fallback_bucket = settings.get("default_bucket", "coding")
    _resolve_all_sessions(
        sessions, summaries, args.classify, fallback_bucket, console,
    )
    rollups = rollup_by_category(sessions)
    console.print(
        f"[green]✓[/green] [dim]resolved into {len(rollups)} categories[/dim]\n"
    )

    category_narratives: dict[str, str] = {}
    if not args.minimal:
        if (
            not args.no_aggregate
            and summaries
            and args.narrative in ("overall", "both")
        ):
            overall_narrative = _synthesize_overall_with_progress(
                label, sessions, rollups, summaries, console,
            )
            if overall_narrative:
                console.print(
                    "[green]✓[/green] [dim]synthesized overall narrative"
                    "[/dim]\n"
                )
        if summaries and args.narrative in ("per-category", "both"):
            category_narratives = _synthesize_categories_with_progress(
                label, sessions, rollups, summaries, console,
            )
            if category_narratives:
                console.print(
                    f"[green]✓[/green] [dim]synthesized "
                    f"{len(category_narratives)} bucket narrative(s)[/dim]\n"
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
                mode=args.classify,
                fallback=fallback_bucket,
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

    artifacts = None
    if not args.no_artifacts:
        # Local git is fast; gh / pypistats are network-bound but individually
        # capped by timeouts, and every miss degrades to "column unavailable".
        with console.status("[dim]Collecting shipped artifacts (git / gh / PyPI)…[/dim]"):
            artifacts = collect_artifacts(sessions, since, until, settings)

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
        overall_narrative=overall_narrative,
        comparison=comparison,
        flavor=args.flavor,
        artifacts=artifacts,
        category_narratives=category_narratives or None,
    )
    out_path.write_text(md, encoding="utf-8")

    if output_format == "json":
        # stdout = one JSON object (#83). The markdown report file is still
        # written above — the report stays the source of truth; JSON is a
        # view for downstream tooling.
        payload = build_report_json(
            label=label,
            since=since,
            until=until,
            sessions=sessions,
            rollups=rollups,
            usage=usage,
            summaries=summaries,
            overall_narrative=overall_narrative,
            comparison=comparison,
            artifacts=artifacts,
            category_narratives=category_narratives or None,
        )
        # Injected here, not in build_report_json — the path is a CLI-run
        # concern (label + --reports-dir), and downstream consumers need it
        # since stdout no longer carries the "Full report →" breadcrumb.
        payload["report_path"] = str(out_path)
        sys.stdout.write(_json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        console.print(f"[dim]Full report → {out_path}[/dim]")
        console.print(f"[dim]Prices as of {get_snapshot_date()}[/dim]")
    elif output_format == "markdown":
        # stdout = clean markdown stream. The Rich console already routes to
        # stderr in this branch so progress / status lines don't pollute it.
        sys.stdout.write(md)
        if not md.endswith("\n"):
            sys.stdout.write("\n")
        console.print(f"[dim]Full report → {out_path}[/dim]")
        console.print(f"[dim]Prices as of {get_snapshot_date()}[/dim]")
    else:
        print_terminal_card(
            since=since,
            until=until,
            sessions=sessions,
            rollups=rollups,
            usage=usage,
            summaries=summaries,
            overall_narrative=overall_narrative,
            report_path=str(out_path),
            comparison=comparison,
            artifacts=artifacts,
            console=console,
        )
        console.print(f"[dim]Prices as of {get_snapshot_date()}[/dim]")
    # Breadcrumb — most users discover wrong buckets in the rendered report
    # and have no idea what to do about it. Point them at the rule-edit CLI
    # right here. Hide when --classify=folder since those users opted out of
    # any reclassification path anyway.
    if args.classify != "folder":
        console.print(
            "[dim]Bucket looks wrong? "
            "`ccstory category set <bucket> <keyword>` to pin a project, "
            "then re-run with `--refresh`.[/dim]"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
