"""Interactive `ccstory init` — three modes for setting up classification.

  - Quick   ([Y], ~10s): LLM looks at folder names + sample first_user_text
            to propose `bucket → [folders]` rules. Writes config.toml.
  - Deep    ([n], ~1min): LLM analyses recent sessions individually.
            Writes session_content_buckets cache + majority-vote folder rules.
            Default range: last 7 days, capped at 200 sessions.
  - Skip    ([s], instant): Just write a template config with empty
            [categories]. Built-in DEFAULT_RULES + fallback take over.

Why three modes: Quick covers users with self-descriptive folder names
(`stock-dashboard`, `rednote-analysis`). Deep covers brand-name folders
(`ccstory`, `personal-os`) where folder text gives the LLM nothing. Skip
covers users who don't want any LLM call.

Picking n in current init writes nothing — equivalent to never running it.
This module replaces that trap with three explicit modes.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from .categorizer import (
    CONFIG_PATH,
    color_for,
    ensure_default_config,
    normalize_project_name,
)
from .session_summarizer import (
    CLAUDE_BIN,
    claude_bin_available,
    classify_sessions_by_content,
    get_many,
)
from .time_tracking import SessionStat, collect_sessions

LOG = logging.getLogger("ccstory.init")

MAX_PROJECTS_IN_PROMPT = 40
MAX_SAMPLES_PER_PROJECT = 2
SAMPLE_CHARS = 160


_INIT_PROMPT = """You are helping a developer organize their Claude Code projects into category buckets for usage analytics.

Given each project (folder name + a couple of first-user-message excerpts from recent sessions), assign ONE category. Use these defaults when they fit:

  - coding       (software development, libraries, tools, plugins, infra)
  - investment   (stock research, portfolio, financial data, trading)
  - writing      (blog posts, content, documentation, newsletters)
  - other        (playground, experiments, miscellaneous)

You MAY introduce a new bucket if you see a clear pattern across multiple projects (e.g. "design", "research", "learning"). Keep total buckets small (≤ 6). Reuse the same bucket name consistently.

Projects:

{projects}

Output ONLY valid TOML in this exact shape (no prose, no fences):

[categories]
"bucket_name" = ["project-leaf-1", "project-leaf-2"]
"another_bucket" = ["project-leaf-3"]

Group every project under exactly one bucket. Use the LEAF name shown (the part after `→`).
"""


def _collect_project_samples(days: int) -> dict[str, list[str]]:
    """{leaf: [first_user_text, ...]} from sessions in past `days` days."""
    since = datetime.now() - timedelta(days=days)
    sessions = collect_sessions(since)
    by_leaf: dict[str, list[str]] = defaultdict(list)
    for s in sessions:
        if not s.first_user_text:
            continue
        leaf = normalize_project_name(s.project) or s.project
        if len(by_leaf[leaf]) >= MAX_SAMPLES_PER_PROJECT:
            continue
        # collapse whitespace, cap
        text = " ".join(s.first_user_text.split())[:SAMPLE_CHARS]
        if text not in by_leaf[leaf]:
            by_leaf[leaf].append(text)
    return dict(by_leaf)


def _format_prompt(samples: dict[str, list[str]]) -> str:
    lines: list[str] = []
    # Sort by recency-of-use proxy = number of samples; cap to MAX_PROJECTS
    items = sorted(samples.items(), key=lambda kv: -len(kv[1]))[:MAX_PROJECTS_IN_PROMPT]
    for leaf, texts in items:
        lines.append(f"\nfolder → {leaf}")
        for t in texts:
            lines.append(f"  - {t}")
    return _INIT_PROMPT.format(projects="\n".join(lines))


def _call_claude_p(prompt: str, timeout: int = 120) -> str | None:
    if not claude_bin_available():
        return None
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", "--output-format", "text",
             "--no-session-persistence", prompt],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if r.returncode != 0:
            LOG.warning("claude -p failed: %s", r.stderr[:200])
            return None
        return r.stdout.strip()
    except (subprocess.SubprocessError, OSError) as e:
        LOG.warning("claude -p errored: %s", e)
        return None


def _parse_toml_categories(text: str) -> dict[str, list[str]] | None:
    """Extract `[categories]` table from claude's response. Tolerate fences."""
    cleaned = text.strip()
    # Strip code fences if any
    for fence in ("```toml", "```TOML", "```"):
        cleaned = cleaned.replace(fence, "")
    cleaned = cleaned.strip()
    try:
        import tomllib  # py 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return None
    try:
        data = tomllib.loads(cleaned)
    except Exception:
        return None
    cats = data.get("categories")
    if not isinstance(cats, dict):
        return None
    out: dict[str, list[str]] = {}
    for k, v in cats.items():
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            out[str(k)] = v
    return out or None


def _render_proposal(console: Console, proposal: dict[str, list[str]]) -> None:
    table = Table(title="Proposed category buckets", title_style="bold")
    table.add_column("Bucket", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Projects", style="dim")
    for bucket, projects in sorted(proposal.items(), key=lambda kv: -len(kv[1])):
        color = color_for(bucket)
        table.add_row(
            f"[{color}]{bucket}[/{color}]",
            str(len(projects)),
            ", ".join(projects),
        )
    console.print(table)


def _write_config(path: Path, proposal: dict[str, list[str]],
                  preserve_header: str = "") -> Path | None:
    """Backup existing then write new config. Returns backup path if any."""
    backup: Path | None = None
    if path.exists():
        backup = path.with_suffix(f".toml.bak-{int(time.time())}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# ccstory category overrides — generated by `ccstory init`",
        "# Backup of previous config (if any) saved alongside this file.",
        "#",
        "# Built-in defaults: coding, investment, writing, other.",
        "# Unmatched projects fall back to `coding`.",
        "",
    ]
    if preserve_header:
        lines.append(preserve_header.strip())
        lines.append("")
    lines.append("[categories]")
    for bucket, projects in sorted(proposal.items(), key=lambda kv: -len(kv[1])):
        # Build TOML list manually so we get nice formatting
        plist = ", ".join(json.dumps(p) for p in projects)
        lines.append(f'"{bucket}" = [{plist}]')
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return backup


# ---------------------------------------------------------------------------
# Quick mode  — LLM looks at folder names + sample first_user_text
# ---------------------------------------------------------------------------

def run_quick_mode(
    days: int = 30,
    dry_run: bool = False,
    console: Console | None = None,
) -> int:
    """LLM proposes `bucket → [folders]` from folder names + sample text.

    Time: ~10s (one `claude -p` call). Writes `[categories]` to config.toml.
    Behaviour-equivalent to the pre-PR-B `ccstory init` flow, minus the
    select-N trap (this function always writes when LLM succeeds; the
    enclosing dispatcher decides whether to call us at all).
    """
    console = console or Console()

    if not claude_bin_available():
        console.print(
            "[red]✗[/red] `claude` CLI not on PATH. Install Claude Code first."
        )
        return 1

    console.print(
        f"[bold]Scanning past {days} days of sessions…[/bold]"
    )
    samples = _collect_project_samples(days)
    if not samples:
        console.print("[yellow]No engaged sessions found.[/yellow] "
                      "Use ccstory normally for a while, then re-run init.")
        return 0
    console.print(
        f"[green]✓[/green] Found {len(samples)} unique projects "
        f"with sample messages.\n"
    )

    with console.status(
        "[dim]Asking claude -p to suggest category buckets (one shot, ~10s)…[/dim]"
    ):
        out = _call_claude_p(_format_prompt(samples))
    if not out:
        console.print(
            "[red]✗[/red] claude -p failed. See `ccstory init -v` for details."
        )
        return 1

    proposal = _parse_toml_categories(out)
    if not proposal:
        console.print("[red]✗[/red] Could not parse claude's response as TOML.")
        console.print("[dim]Raw response below:[/dim]")
        console.print(out[:800])
        return 1

    _render_proposal(console, proposal)
    console.print(
        f"\n[dim]Existing config: {CONFIG_PATH} "
        f"({'will be backed up' if CONFIG_PATH.exists() else 'not present yet'})[/dim]"
    )

    if dry_run:
        console.print("\n[dim]--dry-run set, not writing.[/dim]")
        return 0

    backup = _write_config(CONFIG_PATH, proposal)
    console.print(
        f"\n[green]✓[/green] Wrote {CONFIG_PATH}"
        + (f"  [dim](backup: {backup.name})[/dim]" if backup else "")
    )
    console.print(
        "[dim]Re-run `ccstory week` to see your sessions categorized.[/dim]\n"
        "[dim]For better accuracy on brand-name folders, "
        "try [bold]ccstory init --deep[/bold].[/dim]"
    )
    return 0


# ---------------------------------------------------------------------------
# Deep mode  — LLM analyses sampled sessions individually
# ---------------------------------------------------------------------------

# Sane defaults: 7 days covers a full activity cycle; cap 200 keeps the
# one-batch claude -p call under ~3 minutes on cold start.
DEEP_DEFAULT_DAYS = 7
DEEP_DEFAULT_MAX = 200


def sample_sessions_for_deep(
    sessions: list[SessionStat],
    days: int = DEEP_DEFAULT_DAYS,
    max_n: int = DEEP_DEFAULT_MAX,
) -> list[SessionStat]:
    """Pick up to ``max_n`` sessions spread across ``days`` days.

    Strategy: each calendar day gets a quota of ``max_n // days``. Within a
    day, pick the top-``quota`` by ``active_sec``. Days with fewer sessions
    leave overflow; the remaining slots are filled by the highest-``active_sec``
    overflow sessions from any day.

    Result is biased toward both *recency coverage* (every day represented
    when possible) and *signal weighting* (active sessions over noise).
    """
    if not sessions:
        return []
    if days <= 0:
        days = 1
    by_date: dict = defaultdict(list)
    for s in sessions:
        by_date[s.start.date()].append(s)

    quota_per_day = max(1, max_n // days)
    sampled: list[SessionStat] = []
    overflow: list[SessionStat] = []
    for date in sorted(by_date.keys()):
        day_sorted = sorted(by_date[date], key=lambda s: -s.active_sec)
        sampled.extend(day_sorted[:quota_per_day])
        overflow.extend(day_sorted[quota_per_day:])
    if len(sampled) < max_n:
        overflow.sort(key=lambda s: -s.active_sec)
        sampled.extend(overflow[: max_n - len(sampled)])
    return sampled[:max_n]


def _aggregate_folder_rules(
    sessions: list[SessionStat],
    bucket_by_session: dict[str, str],
) -> dict[str, list[str]]:
    """Majority-vote folder → bucket from session-level LLM results.

    Returns ``{bucket: [folder_leaf, ...]}`` suitable for ``_write_config``.
    For each project leaf, pick the most common bucket the LLM assigned to
    its sessions; ties broken by first occurrence in the iteration.
    """
    by_folder: dict[str, Counter] = defaultdict(Counter)
    for s in sessions:
        bucket = bucket_by_session.get(s.session_id)
        if not bucket:
            continue
        leaf = normalize_project_name(s.project) or s.project
        by_folder[leaf][bucket] += 1

    rules: dict[str, list[str]] = defaultdict(list)
    for leaf, counter in by_folder.items():
        majority_bucket = counter.most_common(1)[0][0]
        rules[majority_bucket].append(leaf)
    return dict(rules)


def run_deep_mode(
    days: int = DEEP_DEFAULT_DAYS,
    max_n: int = DEEP_DEFAULT_MAX,
    dry_run: bool = False,
    console: Console | None = None,
) -> int:
    """Sample sessions, batch-LLM-classify, write cache + majority folder rules.

    Time: ~1-3 min depending on session count. Writes both:
      - ``session_content_buckets`` cache rows (so future ``ccstory week``
        runs hit cache for these sessions)
      - ``[categories]`` folder rules in config.toml (majority-vote per leaf)
    """
    console = console or Console()

    if not claude_bin_available():
        console.print(
            "[red]✗[/red] `claude` CLI not on PATH. Install Claude Code first."
        )
        return 1

    # Codex review caught this: raw `days` from CLI flows into both
    # `timedelta` here and `sample_sessions_for_deep` below. The latter
    # clamps internally; we have to clamp here too so `--deep-days 0`
    # doesn't silently sample "from now".
    if days <= 0:
        days = DEEP_DEFAULT_DAYS
        console.print(
            f"[yellow]![/yellow] --deep-days must be ≥ 1; clamped to "
            f"{DEEP_DEFAULT_DAYS}."
        )
    if max_n <= 0:
        max_n = DEEP_DEFAULT_MAX
        console.print(
            f"[yellow]![/yellow] --max must be ≥ 1; clamped to "
            f"{DEEP_DEFAULT_MAX}."
        )

    since = datetime.now() - timedelta(days=days)
    console.print(
        f"[bold]Collecting sessions in the last {days} day(s)…[/bold]"
    )
    sessions = collect_sessions(since)
    if not sessions:
        console.print("[yellow]No engaged sessions found.[/yellow] "
                      "Use ccstory normally for a while, then re-run init.")
        return 0

    sampled = sample_sessions_for_deep(sessions, days=days, max_n=max_n)
    console.print(
        f"[green]✓[/green] Sampled {len(sampled)}/{len(sessions)} sessions "
        f"across {days} day(s) (cap: {max_n}).\n"
    )

    # Pull summaries we already have; fall back to first_user_text for the rest.
    summaries = get_many([s.session_id for s in sampled])
    items: list[tuple[str, str, str]] = []
    for s in sampled:
        leaf = normalize_project_name(s.project) or s.project
        summ_row = summaries.get(s.session_id)
        summary = summ_row.summary if summ_row else (s.first_user_text or "")
        if not summary:
            continue
        items.append((s.session_id, leaf, summary))
    if not items:
        console.print("[yellow]No usable summaries.[/yellow]")
        return 0

    eta = max(1, len(items) // 80 + 1)  # ~80 sessions/batch, ~1 min/batch
    console.print(
        f"[dim]Asking claude -p to classify {len(items)} session(s) "
        f"(~{eta} min)…[/dim]"
    )
    with console.status("[dim]Running batch LLM…[/dim]"):
        # force_refresh=True: deep mode wants fresh judgments even if cache exists
        mapping = classify_sessions_by_content(items, force_refresh=True)
    if not mapping:
        console.print("[red]✗[/red] claude -p classification failed or returned nothing.")
        return 1

    folder_rules = _aggregate_folder_rules(sampled, mapping)
    if not folder_rules:
        console.print("[yellow]No folder rules could be derived from the LLM mapping.[/yellow]")
        return 0

    _render_proposal(console, folder_rules)
    console.print(
        f"\n[dim]LLM classified {len(mapping)} session(s); aggregated into "
        f"{sum(len(v) for v in folder_rules.values())} folder rules.[/dim]"
    )
    console.print(
        f"[dim]Existing config: {CONFIG_PATH} "
        f"({'will be backed up' if CONFIG_PATH.exists() else 'not present yet'})[/dim]"
    )

    if dry_run:
        console.print("\n[dim]--dry-run set, not writing.[/dim]")
        return 0

    backup = _write_config(CONFIG_PATH, folder_rules)
    console.print(
        f"\n[green]✓[/green] Wrote {CONFIG_PATH}"
        + (f"  [dim](backup: {backup.name})[/dim]" if backup else "")
    )
    console.print(
        f"[green]✓[/green] Cached {len(mapping)} session classifications "
        f"(future `ccstory week` runs skip LLM for these).\n"
        "[dim]Re-run `ccstory week` for a cache-hit fast report.[/dim]"
    )
    return 0


# ---------------------------------------------------------------------------
# Skip mode  — keyword default only, no LLM
# ---------------------------------------------------------------------------

def run_skip_mode(
    dry_run: bool = False,
    console: Console | None = None,
) -> int:
    """Write the template config (empty ``[categories]``) and exit.

    Built-in DEFAULT_RULES + fallback bucket take over for actual
    classification. This is the explicit equivalent of \"don't run init\",
    but with the side effect of materialising config.toml so the user has
    something concrete to edit later.
    """
    console = console or Console()
    if dry_run:
        console.print("[dim]--dry-run set, would scaffold default config.[/dim]")
        return 0
    created = ensure_default_config()
    if created:
        console.print(
            f"[green]✓[/green] Wrote template {CONFIG_PATH} (no LLM call)."
        )
    else:
        console.print(
            f"[dim]Existing {CONFIG_PATH} kept; built-in defaults apply.[/dim]"
        )
    console.print(
        "[dim]Built-in buckets: investment / writing / coding / other. "
        "Unmatched folders fall back to `coding`.\n"
        "Add custom rules with [bold]ccstory category set <bucket> <keyword>[/bold] "
        "or re-run [bold]ccstory init[/bold] for LLM-assisted setup.[/dim]"
    )
    return 0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _prompt_for_mode(console: Console) -> str:
    """Interactive [Y]Quick / [n]Deep / [s]Skip selector. Returns mode key.

    Copy wording follows the codex naming-review guidance: name what each
    mode *does* (the action), not its internal mechanic. Users pick on the
    speed↔accuracy axis, so the labels surface that trade-off in the time
    annotation.
    """
    console.print(
        "\nSet up classification — all three modes write [bold]folder-level "
        "rules[/bold] to ~/.ccstory/config.toml. The difference is how "
        "thoroughly the LLM looks before assigning each folder.\n"
        "  [bold][Y][/bold] Quick     — Read folder names + a few first "
        "messages [dim](~10s)[/dim]\n"
        "  [bold][n][/bold] Deep      — Read each session's content "
        "[dim](~1 min, last "
        f"{DEEP_DEFAULT_DAYS}d, cap {DEEP_DEFAULT_MAX}; better for "
        "catch-all repos like ccstory / scratch)[/dim]\n"
        "  [bold][s][/bold] Skip      — Built-in keyword defaults only "
        "[dim](no LLM)[/dim]\n"
    )
    choice = Prompt.ask(
        "Choose",
        choices=["y", "Y", "n", "N", "s", "S"],
        default="Y",
        show_choices=False,
        console=console,
    ).lower()
    return {"y": "quick", "n": "deep", "s": "skip"}[choice]


def run_init(
    mode: str | None = None,
    days: int = 30,
    deep_days: int = DEEP_DEFAULT_DAYS,
    deep_max: int = DEEP_DEFAULT_MAX,
    dry_run: bool = False,
    console: Console | None = None,
) -> int:
    """Set up classification — entry point invoked by ``ccstory init``.

    Three modes, picked interactively (``mode=None``) or via flag:

    ``"quick"`` — Infer categories from folder names + sample messages.
        One ``claude -p`` call (~10s). Writes folder→bucket rules to
        ``~/.ccstory/config.toml``. Best when folder names are descriptive
        (``stock-dashboard``, ``rednote-analysis``).

    ``"deep"`` — Classify recent sessions individually.
        Samples up to ``deep_max`` sessions from the last ``deep_days``,
        one batched ``claude -p`` call (~1 min). Writes per-session cache
        AND majority-vote folder rules. Best when folder names are
        brand-name or catch-all (``ccstory``, ``~/scratch``).

    ``"skip"`` — Scaffold template config, no LLM.
        Built-in DEFAULT_RULES + fallback bucket take over. Equivalent of
        "don't run init" but materialises the file so it's discoverable.

    ``days`` controls Quick mode's sample-collection lookback (only used to
    decide which folders have recent activity). ``deep_days`` / ``deep_max``
    control Deep mode's sampling window and session cap.
    """
    console = console or Console()
    if mode is None:
        mode = _prompt_for_mode(console)
    if mode == "quick":
        return run_quick_mode(days=days, dry_run=dry_run, console=console)
    if mode == "deep":
        return run_deep_mode(
            days=deep_days, max_n=deep_max, dry_run=dry_run, console=console,
        )
    if mode == "skip":
        return run_skip_mode(dry_run=dry_run, console=console)
    console.print(f"[red]✗[/red] Unknown init mode: {mode!r}")
    return 2
