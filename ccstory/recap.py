"""One-call recap orchestration — the library entry point behind the CLI.

`build_recap()` runs the full pipeline (collect → summarize → classify →
synthesize → compare → artifacts → render) and returns a `RecapResult`
carrying both the rich Python objects (for in-process consumers) and the
rendered markdown + JSON envelope (for report files / downstream tooling).

Part of the semi-stable integration API (#110): programmatic consumers —
dashboards, refresh scripts, the future MCP server — call this instead of
shelling out to the CLI and parsing JSON from a temp file. The CLI itself
is a thin shell over this function, so both paths stay behaviorally
identical by construction.
"""

from __future__ import annotations

import os
import re
import sqlite3
import statistics
from dataclasses import dataclass, field
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

from .artifacts import ArtifactsReport, collect_artifacts
from .categorizer import (
    duplicate_memberships,
    load_project_aliases,
    load_settings,
    normalize_project_name,
    resolve_session_bucket,
)
from .report import build_report_json, render_report
from .session_summarizer import (
    CCSTORY_LANG_ENV,
    PROJECTS_DIR as SUMMARIZER_PROJECTS_DIR,
    _classify_cache_get_many,
    _needs_llm,
    claude_bin_available,
    classify_sessions_by_content,
    get_many,
    import_from_claude_recap,
    invalidate_comparison_narratives,
    invalidate_content_buckets,
    invalidate_period_aggregates,
    language_directive,
    recent_auto_timestamps,
    summarize_session,
    synthesize_category_for_period,
    synthesize_comparison,
    synthesize_overall_for_period,
    upsert,
)
from .time_tracking import CLAUDE_PROJECTS, collect_sessions, rollup_by_category
from .token_usage import apply_prices, collect_usage, load_prices_config
from .trends import PeriodComparison, compare_to_previous

REPORTS_DIR = Path.home() / ".ccstory" / "reports"
CONFIG_PATH = Path.home() / ".ccstory" / "config.toml"

# First-run guess, used only until the cache has real timings to learn from.
# Deliberately pessimistic: with no history, over-stating is safer than
# promising a speed this machine may not deliver. `_sec_per_session()` takes
# over from the second run on (#113).
CLAUDE_P_SEC_FALLBACK = 40

_ETA_HISTORY = 60        # how many past `auto` rows to learn from
_ETA_MIN_SAMPLES = 8     # fewer gaps than this and the median is just noise
_ETA_RUN_GAP_SEC = 300   # a wider gap separates two runs, not two sessions


class RecapUnavailable(RuntimeError):
    """No Claude Code data on this machine, or no sessions in the window.

    Library counterpart of the CLI's `sys.exit(...)` for these cases: an
    empty window is an expected condition for programmatic callers (e.g.
    a refresh script running on a quiet Monday morning), so it must be
    catchable rather than process-fatal.
    """


def apply_lang_override(lang: str | None) -> None:
    """Promote a language override into the env so every prompt-assembly
    call sees it.

    ``language_directive()`` reads ``$CCSTORY_LANG`` at the top of its
    resolution chain. Setting it here (instead of threading the value
    through every callsite) keeps the surface tiny and matches the Unix
    convention that the flag is shorthand for the env var. Also flushes
    the directive's ``lru_cache`` so a re-invocation in the same Python
    process picks up the new value.
    """
    if not lang:
        return
    cleaned = lang.strip()
    if not cleaned:
        return
    os.environ[CCSTORY_LANG_ENV] = cleaned
    language_directive.cache_clear()


def parse_window(raw: str | None) -> tuple[datetime, datetime, str]:
    """Translate week|month|all|YYYY-MM → (since, until, label).

    Returns tz-aware datetimes in the user's local timezone. Month/week
    boundaries are local-midnight aligned, so "ccstory week" means the past
    7 days as the user perceives them — not 7 calendar days in UTC.

    Label policy: when the window endpoint is ``now`` (relative time), the
    label embeds both endpoint dates as ``YYYY-MM-DD_YYYY-MM-DD`` so two
    runs on different days don't collide on the output file. Only a fully
    past ``YYYY-MM`` keeps the compact symbolic label (#58).

    Raises ``ValueError`` on an unrecognized window string.
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
    raise ValueError(f"unrecognized window: {raw!r} (use week|month|all|YYYY-MM)")


@dataclass
class RecapResult:
    """Everything one recap run produced, in both rich and rendered forms.

    Rich objects (`sessions`, `rollups`, `usage`, …) serve in-process
    consumers and the CLI's terminal card; `markdown` / `to_json()` serve
    report files and machine consumers. `report_path` is None when the
    caller opted out of writing the report file.
    """
    label: str
    since: datetime
    until: datetime
    sessions: list
    rollups: list
    usage: object
    summaries: dict
    overall_narrative: str | None
    category_narratives: dict[str, str]
    comparison: PeriodComparison | None
    artifacts: ArtifactsReport | None
    markdown: str
    report_path: Path | None = None
    counts: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict:
        """The machine-readable envelope (`schema_version: 1`), same shape
        as the CLI's `--json` stdout — plus `report_path` when a report
        file was written."""
        payload = build_report_json(
            label=self.label,
            since=self.since,
            until=self.until,
            sessions=self.sessions,
            rollups=self.rollups,
            usage=self.usage,
            summaries=self.summaries,
            overall_narrative=self.overall_narrative,
            comparison=self.comparison,
            artifacts=self.artifacts,
            category_narratives=self.category_narratives or None,
        )
        if self.report_path is not None:
            payload["report_path"] = str(self.report_path)
        return payload


def _synthesize_overall(
    label: str,
    sessions: list,
    rollups: list,
    summaries: dict,
    console: Console,
) -> str | None:
    """Synthesize the overall goal-thread narrative for the period.

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
        "[dim]Synthesizing overall narrative (claude -p)…[/dim]"
    ):
        return synthesize_overall_for_period(
            period_key=label,
            category_hours=category_hours,
            sessions_by_category=sessions_by_cat,
        )


def _synthesize_categories(
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


def _sec_per_session() -> tuple[float, bool]:
    """How long one `claude -p` summary actually takes on this machine.

    Learns from the cache rather than guessing: a backfill writes one `auto`
    row per call, so gaps between consecutive rows are real timings for
    exactly the work being predicted. Gaps wider than `_ETA_RUN_GAP_SEC` fall
    between separate runs, not between sessions, so they are dropped. The
    median, not the mean, keeps one stalled call from skewing the estimate.

    Returns `(seconds, measured)`; `measured` is False when there is not yet
    enough history, so the caller can label the number honestly instead of
    presenting a guess as a measurement.
    """
    try:
        stamps = recent_auto_timestamps(_ETA_HISTORY)
    except sqlite3.Error:
        return CLAUDE_P_SEC_FALLBACK, False
    gaps = [
        b - a for a, b in zip(stamps, stamps[1:]) if b - a < _ETA_RUN_GAP_SEC
    ]
    if len(gaps) < _ETA_MIN_SAMPLES:
        return CLAUDE_P_SEC_FALLBACK, False
    return statistics.median(gaps), True


def _backfill_summaries(
    sessions,
    console: Console,
    use_llm: bool = False,
    force: bool = False,
) -> dict[str, int]:
    """Resolve narratives for sessions in this window.

    Default path is the instant first/last-user-message fallback for never-seen
    sessions. Pass `use_llm=True` to opt into `claude -p`: it upgrades
    `fallback` rows to `auto` and regenerates stale `auto` rows (older
    prompt_version) — or, with `force=True`, every in-window `auto`. The
    user gets an ETA warning, split into new vs regenerated, before the
    batch starts.
    """
    by_id = {s.session_id: s for s in sessions if getattr(s, "session_id", None)}
    ids = list(by_id.keys())
    existing = get_many(ids)
    if use_llm:
        todo = [sid for sid in ids if _needs_llm(existing.get(sid), force)]
    else:
        todo = [sid for sid in ids if existing.get(sid) is None]
    regen = sum(1 for sid in todo if existing.get(sid) is not None)
    counts = {"summarized": 0, "fallback": 0, "skipped": 0,
              "regenerated": regen, "already": len(ids) - len(todo)}
    if not todo:
        return counts

    if use_llm:
        sec, measured = _sec_per_session()
        eta_min = max(1, int((len(todo) * sec + 59) // 60))
        breakdown = f"{len(todo) - regen} new"
        if regen:
            breakdown += f" + {regen} regenerated"
        basis = "measured on this machine" if measured else "first-run estimate"
        console.print(
            f"[yellow]![/yellow] {len(todo)} session(s) to summarize "
            f"({breakdown}). [bold]`claude -p` ETA ~{eta_min} min[/bold] "
            f"(~{sec:.0f}s/session, {basis}). "
            f"Press Ctrl+C to abort, or rerun without --llm-narrative "
            f"for an instant first/last-message fallback.\n"
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
        task = progress.add_task(progress_desc, total=len(todo))
        for sid in todo:
            sess = by_id[sid]
            if getattr(sess, "agent", "claude") == "antigravity":
                jsonl_path = Path.home() / ".gemini" / "antigravity" / "brain" / sid / ".system_generated" / "logs" / "transcript.jsonl"
            else:
                jsonl_path = SUMMARIZER_PROJECTS_DIR / sess.project / f"{sid}.jsonl"
                if not jsonl_path.exists():
                    matches = list(SUMMARIZER_PROJECTS_DIR.rglob(f"{sid}.jsonl"))
                    if matches:
                        jsonl_path = matches[0]

            if not jsonl_path.exists():
                # Don't clobber a cached summary when the jsonl has since
                # gone missing; only record a skip for never-seen ids.
                if existing.get(sid) is None:
                    upsert(sid, "(jsonl not found)", "skipped",
                           project=sess.project)
                counts["skipped"] += 1
                progress.advance(task)
                continue
            result = summarize_session(sid, jsonl_path, use_llm=use_llm,
                                       force=force)
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


def build_recap(
    window: str = "month",
    *,
    minimal: bool = False,
    llm_narrative: bool = False,
    narrative: str = "overall",
    aggregate: bool = True,
    compare: bool = True,
    compare_narrative: bool = True,
    artifacts: bool = True,
    classify: str = "hybrid",
    refresh: bool = False,
    refresh_all: bool = False,
    flavor: str = "plain",
    lang: str | None = None,
    agent: str = "all",
    reports_dir: Path | None = None,
    write_report: bool = True,
    console: Console | None = None,
) -> RecapResult:
    """Run the full recap pipeline for one window and return the result."""
    if console is None:
        console = Console(quiet=True)

    apply_lang_override(lang)

    antigravity_brain = Path.home() / ".gemini" / "antigravity" / "brain"
    if not CLAUDE_PROJECTS.exists() and not antigravity_brain.exists():
        raise RecapUnavailable(
            f"No Claude Code data or Antigravity data at {CLAUDE_PROJECTS} or {antigravity_brain}. "
            "Have you used Claude Code or Antigravity yet?"
        )

    # Load user price overrides (config [prices] table). No-op if absent.
    prices, snapshot = load_prices_config(CONFIG_PATH)
    apply_prices(prices, snapshot)

    for needle, areas in duplicate_memberships(CONFIG_PATH):
        console.print(
            f"[yellow]![/yellow] [dim]config: project '{needle}' is listed "
            f"under multiple areas ({', '.join(areas)}); using '{areas[0]}' "
            f"(first wins). Remove it from the others to silence this.[/dim]"
        )

    since, until, label = parse_window(window)
    console.print(
        f"[dim]Window:[/dim] [bold]{since.date()} → {until.date()}[/bold] "
        f"[dim]({label})[/dim]\n"
    )

    with console.status("[dim]Parsing sessions and token usage…[/dim]"):
        sessions = collect_sessions(since, until, agent=agent)
        if not sessions:
            raise RecapUnavailable("No engaged sessions in this window.")
        # since/until are tz-aware local; collect_usage normalizes to UTC.
        usage = collect_usage(since, until)

    console.print(
        f"[green]✓[/green] {len(sessions)} sessions · "
        f"{usage.assistant_turns:,} turns\n"
    )

    # `refresh` wipes the content-classification cache so the rules that
    # just changed actually take effect. Without this, sessions that were
    # claude-classified before the rule edit keep their old bucket. Done
    # AFTER session collection so we know exactly which ids to scope to.
    if refresh_all:
        c_n = invalidate_content_buckets(None)
        a_n = invalidate_period_aggregates(None)
        m_n = invalidate_comparison_narratives()
        console.print(
            f"[yellow]Refreshed[/yellow] [dim]{c_n} cached bucket(s), "
            f"{a_n} aggregate(s), {m_n} comparison narrative(s) — "
            f"global wipe[/dim]\n"
        )
    elif refresh:
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
    counts: dict[str, int] = {}
    overall_narrative: str | None = None
    if not minimal:
        imported = import_from_claude_recap()
        if imported:
            console.print(
                f"[green]✓[/green] [dim]imported {imported} cached "
                f"summarie(s) from ~/.claude/session_summaries.db "
                f"(/recap)[/dim]\n"
            )
        if llm_narrative and not claude_bin_available():
            console.print(
                "[yellow]![/yellow] [dim]`claude` not on PATH — "
                "--llm-narrative will fall back to first/last user messages[/dim]\n"
            )
        counts = _backfill_summaries(
            sessions, console, use_llm=llm_narrative,
            force=(refresh or refresh_all),
        )
        regen = counts.get("regenerated", 0)
        regen_note = f" · regenerated={regen}" if regen else ""
        console.print(
            f"[green]✓[/green] [dim]summarized={counts['summarized']} · "
            f"fallback={counts['fallback']} · skipped={counts['skipped']}"
            f"{regen_note} · cached={counts['already']}[/dim]\n"
        )
        # Regenerating per-session summaries changes the inputs to the
        # "What you did" overall synthesis without changing the session-id
        # set its cache is keyed on, so invalidate it for this label (unless
        # refresh already wiped it above) to avoid a stale aggregate.
        if regen and not (refresh or refresh_all):
            invalidate_period_aggregates(label)
            invalidate_comparison_narratives()
        summaries = get_many([s.session_id for s in sessions])

    # Resolver pass — single point where every session's bucket gets assigned.
    # Reads LLM cache once, batches uncached sessions into one claude -p call
    # when summaries are available. Same priority chain runs in compare_to_
    # previous() so cross-window comparison stays symmetric (fixes #61).
    settings = load_settings(CONFIG_PATH)
    fallback_bucket = settings.get("default_bucket", "coding")
    _resolve_all_sessions(
        sessions, summaries, classify, fallback_bucket, console,
    )
    # aliases feed the layer-2 (area → project) rollup (#69); layer-1 area
    # totals are independent of it.
    rollups = rollup_by_category(
        sessions, aliases=load_project_aliases(CONFIG_PATH),
    )
    console.print(
        f"[green]✓[/green] [dim]resolved into {len(rollups)} categories[/dim]\n"
    )

    category_narratives: dict[str, str] = {}
    if not minimal:
        if aggregate and summaries and narrative in ("overall", "both"):
            overall_narrative = _synthesize_overall(
                label, sessions, rollups, summaries, console,
            )
            if overall_narrative:
                console.print(
                    "[green]✓[/green] [dim]synthesized overall narrative"
                    "[/dim]\n"
                )
        if summaries and narrative in ("per-category", "both"):
            category_narratives = _synthesize_categories(
                label, sessions, rollups, summaries, console,
            )
            if category_narratives:
                console.print(
                    f"[green]✓[/green] [dim]synthesized "
                    f"{len(category_narratives)} bucket narrative(s)[/dim]\n"
                )

    comparison = None
    if compare and window != "all":
        with console.status("[dim]Computing previous-window comparison…[/dim]"):
            comparison = compare_to_previous(
                current_sessions=sessions,
                current_rollups=rollups,
                current_usage=usage,
                current_label=label,
                since=since,
                until=until,
                mode=classify,
                fallback=fallback_bucket,
            )
        if comparison and compare_narrative and summaries:
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

    artifacts_report = None
    if artifacts:
        # Local git is fast; gh / pypistats are network-bound but individually
        # capped by timeouts, and every miss degrades to "column unavailable".
        with console.status("[dim]Collecting shipped artifacts (git / gh / PyPI)…[/dim]"):
            artifacts_report = collect_artifacts(sessions, since, until, settings)

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
        flavor=flavor,
        artifacts=artifacts_report,
        category_narratives=category_narratives or None,
    )

    report_path: Path | None = None
    if write_report:
        out_dir = reports_dir if reports_dir is not None else REPORTS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / f"recap-{label}.md"
        report_path.write_text(md, encoding="utf-8")

    return RecapResult(
        label=label,
        since=since,
        until=until,
        sessions=sessions,
        rollups=rollups,
        usage=usage,
        summaries=summaries,
        overall_narrative=overall_narrative,
        category_narratives=category_narratives,
        comparison=comparison,
        artifacts=artifacts_report,
        markdown=md,
        report_path=report_path,
        counts=counts,
    )
