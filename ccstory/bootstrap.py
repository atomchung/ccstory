"""Lightweight process entry point for ccstory.

Top-level ``--help`` and ``--version`` only need argparse and package
metadata.  Keep those paths independent of the recap/rendering stack so a
basic information request does not pay to import Rich, transcript providers,
or the narrative pipeline.

All other argv is delegated unchanged to :mod:`ccstory.cli`.  The full CLI
also uses :func:`build_top_level_parser`, keeping option order, help text, and
argparse behavior in one canonical definition.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .version import resolve_version


BUNDLED_PROVIDER_NAMES = ("claude", "codex", "antigravity")
VALID_OUTPUT_FORMATS = ("auto", "markdown", "card", "json")
VALID_REPORT_FLAVORS = ("plain", "obsidian")

_SUBCOMMANDS = frozenset(("trend", "init", "category", "mcp"))
_INFORMATION_FLAGS = frozenset(("-h", "--help", "--version"))


def build_top_level_parser(
    *,
    provider_names: Sequence[str] = BUNDLED_PROVIDER_NAMES,
    reports_dir: Path | None = None,
    report_flavors: Sequence[str] = VALID_REPORT_FLAVORS,
    version: str | None = None,
) -> argparse.ArgumentParser:
    """Return the canonical parser for the default recap command.

    ``provider_names`` stays injectable so in-process callers that extend the
    provider registry keep seeing their registered choices through
    ``ccstory.cli.main``.  A fresh process uses the bundled provider names
    without importing their implementations merely to format help.
    """
    provider_choices = tuple(provider_names)
    flavor_choices = tuple(report_flavors)
    default_reports_dir = (
        reports_dir
        if reports_dir is not None
        else Path.home() / ".ccstory" / "reports"
    )
    resolved_version = resolve_version() if version is None else version

    parser = argparse.ArgumentParser(
        prog="ccstory",
        description="AI coding-agent usage recap with narrative. "
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
            "  ccstory mcp\n"
            "      Serve recap/comparison/category data over MCP (stdio) so\n"
            "      other agents can query ccstory live. Requires the optional\n"
            "      `mcp` extra: pip install 'ccstory[mcp]'.\n"
            "\n"
            "Examples:\n"
            "  ccstory week                  # last 7 days, per-category local\n"
            "                                # narratives with LLM enhancement when available\n"
            "  ccstory week --llm-narrative  # polish per-session via configured narrator\n"
            "                                # (shared 90s LLM budget)\n"
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
                        help="Polish per-session narratives via configured local narrator "
                             "(10-session probe, then up to 40 sessions/call; "
                             "shared 90s LLM budget). Default is an instant "
                             "first/last-message fallback. Re-run on a past "
                             "window to upgrade those fallbacks to polished "
                             "summaries; already-polished sessions are reused "
                             "unless their prompt version is stale. Add "
                             "--refresh to force-regenerate them all.")
    parser.add_argument("--no-aggregate", action="store_true",
                        help="Skip the overall goal-thread narrative "
                             "(one configured narrator call across all buckets)")
    parser.add_argument("--no-compare", action="store_true",
                        help="Skip the vs-previous-window comparison block")
    parser.add_argument("--narrative", choices=["overall", "per-category", "both"],
                        default="per-category",
                        help="Narrative depth. `per-category` (default) = a header + "
                             "bullets per bucket; an unavailable narrator uses a "
                             "deterministic local fallback. `overall` = 2-4 "
                             "goal threads (bold header + bullets) across "
                             "all buckets. Each category can use one configured narrator call per "
                             "bucket, cached until the bucket's session set "
                             "changes). `both` = overall first, then "
                             "per-bucket sections.")
    parser.add_argument("--no-artifacts", action="store_true",
                        help="Skip the Repo activity section (git commits / "
                             "merged PRs / releases / stars / PyPI downloads "
                             "for repos worked on this window). Also "
                             "disable persistently via config.toml "
                             "[artifacts] enabled = false.")
    parser.add_argument("--for", dest="flavor", choices=flavor_choices,
                        default="plain",
                        help="Markdown variant for the saved report. "
                             "`obsidian` adds YAML front-matter + [[wikilinks]] "
                             "so the report drops into a PKM vault cleanly.")
    parser.add_argument("--no-compare-narrative", action="store_true",
                        help="Skip the 1-2 sentence narrator synthesis "
                             "under the comparison table (numeric deltas "
                             "still render)")
    parser.add_argument("--classify", choices=["folder", "content", "hybrid"],
                        default="hybrid",
                        help="How to bucket sessions. `folder` uses only "
                             "config + folder-name rules. `content` runs a "
                             "batch narrator call over each session's narrative. "
                             "`hybrid` (default) keeps the folder bucket when "
                             "a user rule in config.toml matched, otherwise "
                             "falls back to content classification.")
    parser.add_argument("--refresh", action="store_true",
                        help="Re-do this window's cached work. Wipes the "
                             "content-classification cache for the sessions "
                             "inside [since, until] (use after editing "
                             "[categories] rules); with --llm-narrative, also "
                             "force-regenerates every per-session summary in "
                             "the window via the configured narrator.")
    parser.add_argument("--refresh-all", action="store_true",
                        help="Wipe the entire content-classification cache, "
                             "not just this window. Implies --refresh.")
    parser.add_argument("--reports-dir", type=Path, default=default_reports_dir,
                        help=f"Markdown report output dir (default: {default_reports_dir})")
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
    parser.add_argument("--agent", choices=["all", *provider_choices],
                        default="all",
                        help="Which coding agent's sessions to include: "
                             f"`all` (default), or one of "
                             f"{', '.join(provider_choices)}.")
    parser.add_argument("--version", action="version",
                        version=f"ccstory {resolved_version}")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _is_top_level_information_request(argv: Sequence[str]) -> bool:
    """Whether argv can be answered by the lightweight top-level parser."""
    if not argv or argv[0] in _SUBCOMMANDS:
        return False

    # Once argparse sees ``--``, later values are positional even when they
    # look like flags.  Respect that delimiter before deciding to intercept.
    try:
        option_end = argv.index("--")
    except ValueError:
        option_end = len(argv)
    return any(arg in _INFORMATION_FLAGS for arg in argv[:option_end])


def main(argv: list[str] | None = None) -> int:
    """Serve top-level information cheaply; delegate every real command."""
    raw = list(argv) if argv is not None else sys.argv[1:]
    if _is_top_level_information_request(raw):
        # argparse's help/version actions raise SystemExit with their canonical
        # output and exit code, exactly as the former eager CLI path did.
        build_top_level_parser().parse_args(raw)
        return 0

    from .cli import main as cli_main

    return cli_main(raw)
