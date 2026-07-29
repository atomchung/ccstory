"""ccstory CLI entry. Usage:

    python -m ccstory                  # default month
    python -m ccstory week
    python -m ccstory month
    python -m ccstory 2026-04
    python -m ccstory all

Flags:
    --llm-narrative    Polish per-session narratives via configured local narrator
                       (slow, opt-in; shared 90s LLM budget). Re-run on a
                       past window to upgrade cached fallbacks / stale auto
                       summaries; add --refresh to force-regenerate all.
    --minimal          Skip per-session narrative entirely (fastest)
                       (deprecated alias: --no-summary)
    --no-aggregate     Skip the overall work-theme narrative
    --reports-dir PATH Override default ~/.ccstory/reports/

The recap pipeline itself lives in `ccstory.recap.build_recap()` — this
module is argument parsing, output-format resolution, and rendering on top
of that library entry point.
"""

from __future__ import annotations

import argparse
import json as _json
import logging
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .bootstrap import (
    VALID_OUTPUT_FORMATS,
    build_top_level_parser,
    parser_version_for_argv,
    prepare_information_streams,
)
from .providers import agent_label, list_providers
from .categorizer import (
    add_category_keywords,
    colors_for,
    ensure_default_config,
    load_project_aliases,
    list_user_categories,
    load_settings,
    normalize_project_name,
    preview_classification,
    remove_category_keywords,
)
from .goal_store import (
    ManagedTomlGoalSource,
    managed_goal_context_path,
    resolve_goal_context_source,
)
from .goals import GoalContextError
from .recap import (
    CONFIG_PATH,
    REPORTS_DIR,
    RecapUnavailable,
    _agent_data_roots,
    apply_lang_override,
    build_recap,
)
from .report import (
    VALID_FLAVORS,
    build_trend_json,
    print_terminal_card,
    render_trend_card,
    render_trend_markdown,
)
from .session_summarizer import (
    CacheUnavailable,
    invalidate_comparison_narratives,
    invalidate_period_aggregates,
)
from .time_tracking import CLAUDE_PROJECTS
from .token_usage import apply_prices, get_snapshot_date, load_prices_config
from .trends import collect_trend

LOG = logging.getLogger("ccstory.cli")

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


def _run_init(argv: list[str], console: Console) -> int:
    from .init_categories import DEEP_DEFAULT_DAYS, DEEP_DEFAULT_MAX, run_init
    p = argparse.ArgumentParser(
        prog="ccstory init",
        description=(
            "Set up classification. Three modes — picked interactively when no\n"
            "flag is given, or selected via flag:\n"
            "  Quick   Infer categories from folder names + sample messages\n"
            "          (~10s, 1 configured narrator call). Best when folder names are\n"
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
        colors = colors_for(sorted(cats))
        for bucket in sorted(cats):
            color = colors[bucket]
            table.add_row(
                f"[{color}]{bucket}[/{color}]",
                ", ".join(cats[bucket]),
            )
        console.print(table)
        return 0

    try:
        if args.action == "set":
            categories, moved = add_category_keywords(args.bucket, args.keywords)
            # A moved-from bucket that emptied out is dropped from `categories`
            # by add_category_keywords — union it back in so colors_for() has
            # every bucket this command is about to print a color for.
            colors = colors_for(sorted(set(categories) | {args.bucket} | {p for _, p in moved}))
            kw_render = ", ".join(f"`{k}`" for k in args.keywords)
            console.print(
                f"[green]✓[/green] Added {kw_render} → "
                f"[{colors[args.bucket]}]{args.bucket}[/{colors[args.bucket]}]"
            )
            for kw, prev in moved:
                console.print(
                    f"  [yellow]moved[/yellow] `{kw}` "
                    f"from [{colors[prev]}]{prev}[/{colors[prev]}]"
                )
        else:  # unset
            categories, missing = remove_category_keywords(
                args.bucket, args.keywords,
            )
            kept = [k for k in args.keywords if k.lower() not in missing]
            if kept:
                # args.bucket itself may have emptied out and been dropped
                # from `categories` — same union as above.
                colors = colors_for(sorted(set(categories) | {args.bucket}))
                kw_render = ", ".join(f"`{k}`" for k in kept)
                console.print(
                    f"[green]✓[/green] Removed {kw_render} from "
                    f"[{colors[args.bucket]}]{args.bucket}[/{colors[args.bucket]}]"
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


def _run_goal(argv: list[str], console: Console) -> int:
    """``ccstory goal {set,list,unset}`` — mutate only the managed store."""

    p = argparse.ArgumentParser(
        prog="ccstory goal",
        description=(
            "Manage local GoalContext v1 definitions in "
            "~/.ccstory/goals.toml."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    set_p = sub.add_parser("set", help="Create or replace one goal by id")
    set_p.add_argument("goal_id")
    set_p.add_argument("--title", required=True)
    set_p.add_argument(
        "--project",
        dest="projects",
        action="append",
        required=True,
        help="Linked project; repeat for multiple projects",
    )
    set_p.add_argument("--valid-from", default=None, metavar="YYYY-MM-DD")
    set_p.add_argument("--valid-until", default=None, metavar="YYYY-MM-DD")

    list_p = sub.add_parser("list", help="List managed goals")
    list_p.add_argument(
        "--json",
        action="store_true",
        help="Print the versioned GoalContext schema as JSON",
    )

    unset_p = sub.add_parser("unset", help="Remove one goal by id")
    unset_p.add_argument("goal_id")
    args = p.parse_args(argv)

    path = managed_goal_context_path()
    aliases = load_project_aliases(CONFIG_PATH)
    source = ManagedTomlGoalSource(path, aliases=aliases)
    try:
        if args.command == "set":
            context, replaced = source.upsert(
                args.goal_id,
                title=args.title,
                projects=args.projects,
                valid_from=args.valid_from,
                valid_until=args.valid_until,
            )
            goal = next(
                goal
                for goal in context.goals
                if goal.id == args.goal_id.strip()
            )
            action = "Updated" if replaced else "Set"
            console.print(
                f"[green]✓[/green] {action} goal `{goal.id}` · "
                f"{goal.title} · {', '.join(goal.projects)}"
            )
            return 0

        if args.command == "unset":
            removed = source.delete(args.goal_id)
            if removed:
                console.print(
                    f"[green]✓[/green] Removed goal `{args.goal_id.strip()}`"
                )
            else:
                console.print(
                    f"[yellow]not found:[/yellow] goal "
                    f"`{args.goal_id.strip()}`; nothing changed"
                )
            return 0

        context = source.read()
        if args.json:
            sys.stdout.write(
                _json.dumps(
                    context.to_schema_dict(),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            return 0
        if not context.goals:
            console.print(
                "[dim]No managed goals. Add one with: "
                "ccstory goal set <goal-id> --title <title> "
                "--project <project>[/dim]"
            )
            return 0
        table = Table(title=f"Goals · {path}", title_style="bold")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Title")
        table.add_column("Projects", style="dim")
        table.add_column("Valid", style="dim", no_wrap=True)
        for goal in context.goals:
            valid = (
                f"{goal.valid_from.isoformat() if goal.valid_from else '…'}"
                " → "
                f"{goal.valid_until.isoformat() if goal.valid_until else '…'}"
            )
            table.add_row(
                goal.id,
                goal.title,
                ", ".join(goal.projects),
                valid,
            )
        console.print(table)
        return 0
    except GoalContextError as exc:
        console.print(f"[red]✗[/red] {exc}")
        return 1


def _run_trend(argv: list[str]) -> int:
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
    p.add_argument("--agent", choices=["all", *list_providers()], default="all",
                   help="Which coding agent's sessions to include — mirror "
                        "what `ccstory week` uses so the trend line and the "
                        "week describe the same population.")
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

    roots = _agent_data_roots(args.agent)
    if not any(root.exists() for _, root in roots):
        where = " or ".join(
            f"{agent_label(name)} at {root}" for name, root in roots
        )
        sys.exit(f"No session data ({where}). Have you used it yet?")

    # Load prices after parsing so any pricing-related flag is readable here.
    prices, snapshot, provenance = load_prices_config(CONFIG_PATH)
    apply_prices(prices, snapshot, provenance)

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
            mode=args.classify, fallback=fallback_bucket, agent=args.agent,
        )
    if not any(p.total_h for p in points):
        sys.exit("No engaged sessions across the trend window.")

    args.reports_dir.mkdir(parents=True, exist_ok=True)
    agent_suffix = "" if args.agent == "all" else f"-{args.agent}"
    out_path = args.reports_dir / f"trend-{period}-{count}{agent_suffix}.md"
    md = render_trend_markdown(points, period, agent=args.agent)
    out_path.write_text(md, encoding="utf-8")

    if output_format == "json":
        payload = build_trend_json(points, period, agent=args.agent)
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
        console.print(render_trend_card(points, period, agent=args.agent))
        console.print(f"[dim]Full report → {out_path}[/dim]")
        console.print(f"[dim]Prices as of {get_snapshot_date()}[/dim]")
    return 0


def _run_mcp(argv: list[str]) -> int:
    """`ccstory mcp` — serve recap/comparison/category data over MCP (#35).

    Stdio transport, read-only. Deferred import: `mcp` is an optional
    dependency (`pip install 'ccstory[mcp]'`) so a plain `pip install
    ccstory` stays lightweight for CLI-only users who never touch this.

    Takes no flags in v0 — but still parses `argv` rather than ignoring it,
    so `ccstory mcp --help` (or a mistyped flag) prints usage instead of
    silently entering the stdio blocking read loop, which looks identical
    to a hang from the terminal.
    """
    if argv:
        if argv[0] in ("-h", "--help"):
            print(
                "usage: ccstory mcp\n\n"
                "Serve recap/comparison/category data over MCP (stdio), "
                "read-only.\nTakes no arguments. See README \"MCP server\" "
                "for client setup.",
                file=sys.stderr,
            )
            return 0
        print(f"ccstory mcp: unrecognized argument(s): {' '.join(argv)}",
              file=sys.stderr)
        return 1
    try:
        from .mcp_server import run
    except ImportError as e:
        print(
            "ccstory mcp requires the optional `mcp` dependency: "
            "pip install 'ccstory[mcp]'",
            file=sys.stderr,
        )
        print(f"  (underlying: {e})", file=sys.stderr)
        return 1
    run()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `_dispatch` plus the one cross-cutting error trap.

    `CacheUnavailable` can surface from any subcommand — every cache access
    funnels through `session_summarizer._connect()`. Catching it here keeps
    the CLI contract (full message to stderr, exit 1) while letting library
    callers of `build_recap()` catch it as a normal exception instead of
    dying on a `SystemExit` they cannot intercept (#119).
    """
    try:
        return _dispatch(argv)
    except CacheUnavailable as e:
        print(str(e), file=sys.stderr)
        return 1


def _dispatch(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]

    # Manual dispatch for subcommands — keeps default `ccstory week`
    # / `ccstory month` flow simple positional. `init` / `category` / `goal` only
    # emit progress lines, so a stdout Console is fine; `trend` and the
    # default recap path resolve --format themselves and may switch to
    # a stderr Console so the markdown report can own stdout. `mcp` owns
    # stdout for the whole process (protocol stream) — no Console at all.
    if raw and raw[0] == "trend":
        logging.basicConfig(level=logging.WARNING)
        return _run_trend(raw[1:])
    if raw and raw[0] == "init":
        logging.basicConfig(level=logging.WARNING)
        return _run_init(raw[1:], Console())
    if raw and raw[0] == "category":
        logging.basicConfig(level=logging.WARNING)
        return _run_category(raw[1:], Console())
    if raw and raw[0] == "goal":
        logging.basicConfig(level=logging.WARNING)
        return _run_goal(raw[1:], Console())
    if raw and raw[0] == "mcp":
        logging.basicConfig(level=logging.WARNING)
        return _run_mcp(raw[1:])

    version = parser_version_for_argv(raw)
    parser = build_top_level_parser(
        version=version,
        provider_names=list_providers(),
        reports_dir=REPORTS_DIR,
        report_flavors=VALID_FLAVORS,
    )
    prepare_information_streams(raw, parser, version=version)
    args = parser.parse_args(raw)

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

    try:
        goal_context = resolve_goal_context_source(
            args.goals_file,
            config_path=CONFIG_PATH,
            aliases=load_project_aliases(CONFIG_PATH),
        )
    except GoalContextError as e:
        sys.exit(str(e))

    # `build_recap` re-checks this; the early exit is only so a first-time
    # user gets the message before the first-run preview prints. Derive it
    # from the registry so a third-provider-only install is not rejected.
    roots = _agent_data_roots(args.agent)
    if not any(root.exists() for _, root in roots):
        where = " or ".join(
            f"{agent_label(name)} at {root}" for name, root in roots
        )
        sys.exit(f"No session data ({where}). Have you used it yet?")

    _print_first_run_preview(console)

    try:
        result = build_recap(
            args.window,
            minimal=args.minimal,
            llm_narrative=args.llm_narrative,
            narrative=args.narrative,
            aggregate=not args.no_aggregate,
            compare=not args.no_compare,
            compare_narrative=not args.no_compare_narrative,
            artifacts=not args.no_artifacts,
            classify=args.classify,
            refresh=args.refresh,
            refresh_all=args.refresh_all,
            flavor=args.flavor,
            lang=args.lang,
            agent=args.agent,
            reports_dir=args.reports_dir,
            console=console,
            goal_context=goal_context,
        )
    except (ValueError, RecapUnavailable) as e:
        sys.exit(str(e))

    out_path = result.report_path
    md = result.markdown

    if output_format == "json":
        # stdout = one JSON object (#83). The markdown report file is still
        # written by build_recap — the report stays the source of truth;
        # JSON is a view for downstream tooling. The envelope carries
        # report_path since stdout no longer shows the "Full report →"
        # breadcrumb.
        payload = result.to_json()
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
            since=result.since,
            until=result.until,
            sessions=result.sessions,
            rollups=result.rollups,
            usage=result.usage,
            summaries=result.summaries,
            overall_narrative=result.overall_narrative,
            report_path=str(out_path),
            comparison=result.comparison,
            artifacts=result.artifacts,
            agent=result.agent,
            goal_context=result.goal_context,
            goal_breakdown=result.goal_breakdown,
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
