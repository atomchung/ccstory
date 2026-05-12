"""Interactive `ccstory init` — auto-suggest category buckets from recent
sessions via a single `claude -p` call.

Why a single call: each invocation costs ~7s + a turn of the user's plan.
N-per-project would be wasteful; one batch prompt is plenty for a few dozen
projects.

Flow:
  1. Scan sessions in the past N days (default 30).
  2. Group by normalized project leaf, keep up to 2 sample first_user_text.
  3. Build one prompt asking claude -p to propose a category for each.
  4. Parse claude's TOML output, show diff vs existing config.
  5. On user confirmation, back up existing config and write the new one.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from .categorizer import CONFIG_PATH, color_for, normalize_project_name
from .session_summarizer import CLAUDE_BIN, claude_bin_available
from .time_tracking import collect_sessions

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


def run_init(days: int = 30, dry_run: bool = False,
             auto_yes: bool = False, console: Console | None = None) -> int:
    """Entry point invoked by `ccstory init`."""
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
        "[dim]Asking claude -p to suggest category buckets (one shot, ~15s)…[/dim]"
    ):
        out = _call_claude_p(_format_prompt(samples))
    if not out:
        console.print("[red]✗[/red] claude -p failed. See `ccstory init -v` for details.")
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
    if not auto_yes:
        if not Confirm.ask("\nWrite this to ~/.ccstory/config.toml?",
                           console=console, default=True):
            console.print("[yellow]Skipped.[/yellow]")
            return 0

    backup = _write_config(CONFIG_PATH, proposal)
    console.print(
        f"\n[green]✓[/green] Wrote {CONFIG_PATH}"
        + (f"  [dim](backup: {backup.name})[/dim]" if backup else "")
    )
    console.print(
        "[dim]Re-run `ccstory week` to see your sessions categorized.[/dim]"
    )
    return 0
