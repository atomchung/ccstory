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
from .providers import (
    agent_label,
    collect_provider_snapshot,
    provider_data_roots,
)
from .report import build_report_json, render_report
from .session_summarizer import (
    CCSTORY_LANG_ENV,
    NarrativeBudget,
    _classify_cache_get_many,
    backfill_for_sessions,
    llm_available,
    classify_sessions_by_content,
    get_many,
    get_comparison_narrative_provenance,
    get_period_narrative_provenance,
    import_from_claude_recap,
    invalidate_comparison_narratives,
    invalidate_content_buckets,
    invalidate_period_aggregates,
    language_directive,
    prepare_backfill_plan,
    summary_is_synthesis_eligible,
    synthesize_category_for_period,
    synthesize_comparison,
    synthesize_overall_for_period,
)
from .time_tracking import CLAUDE_PROJECTS as CLAUDE_PROJECTS
from .time_tracking import rollup_by_category
from .token_usage import (
    apply_prices,
    load_prices_config,
)
from .trends import PeriodComparison, compare_to_previous, previous_window

REPORTS_DIR = Path.home() / ".ccstory" / "reports"
CONFIG_PATH = Path.home() / ".ccstory" / "config.toml"

class RecapUnavailable(RuntimeError):
    """No Claude Code data on this machine, or no sessions in the window.

    Library counterpart of the CLI's `sys.exit(...)` for these cases: an
    empty window is an expected condition for programmatic callers (e.g.
    a refresh script running on a quiet Monday morning), so it must be
    catchable rather than process-fatal.
    """


def _eligible_summary_items(
    session_ids: list[str], summaries: dict,
) -> list[tuple[str, str]]:
    """Return only prose safe to feed another synthesis lane."""
    return [
        (session_id, summaries[session_id].summary)
        for session_id in session_ids
        if summary_is_synthesis_eligible(summaries.get(session_id))
    ]


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
    # Appended after the existing defaulted fields so older positional
    # RecapResult(...) callers do not silently bind report_path as agent.
    agent: str = "all"
    narrative_provenance: dict = field(default_factory=dict)

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
            agent=self.agent,
            narrative_provenance=self.narrative_provenance,
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
    budget: NarrativeBudget | None = None,
) -> str | None:
    """Synthesize the overall goal-thread narrative for the period.

    Single configured-narrator call across all categories — replaces the old
    per-bucket aggregate path. Cache-friendly: only re-runs when the set
    of session ids changes since the cached narrative was written.
    """
    sessions_by_cat: dict[str, list[tuple[str, str]]] = {}
    for s in sessions:
        summ = summaries.get(s.session_id)
        if not summary_is_synthesis_eligible(summ):
            continue
        sessions_by_cat.setdefault(s.category, []).append(
            (s.session_id, summ.summary)
        )
    if not sessions_by_cat:
        return None

    category_hours = [(r.category, r.active_min / 60) for r in rollups]

    with console.status(
        "[dim]Synthesizing overall narrative (configured narrator)…[/dim]",
    ):
        return synthesize_overall_for_period(
            period_key=label,
            category_hours=category_hours,
            sessions_by_category=sessions_by_cat,
            budget=budget,
        )


def _synthesize_categories(
    label: str,
    sessions: list,
    rollups: list,
    summaries: dict,
    console: Console,
    budget: NarrativeBudget | None = None,
) -> dict[str, str]:
    """One 2-3 line narrative per bucket (#57), rollup order.

    Same input contract as the overall narrative: only sessions with a real
    summary (auto/record) feed the prompt. A bucket with none is skipped;
    a bucket whose narrator call fails is simply absent from the result.
    """
    sessions_by_cat: dict[str, list[tuple[str, str]]] = {}
    for s in sessions:
        summ = summaries.get(s.session_id)
        if not summary_is_synthesis_eligible(summ):
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
            f"{cat} (configured narrator)…[/dim]"
        ):
            narrative = synthesize_category_for_period(
                period_key=label,
                category=cat,
                session_ids=[sid for sid, _ in items],
                summaries=[text for _, text in items],
                budget=budget,
            )
        if narrative:
            out[cat] = narrative
    return out


_LOCAL_CATEGORY_FALLBACK_PROVENANCE = {
    "provider": "ccstory",
    "model": "deterministic-local-fallback",
    "source": "local_fallback",
}


def _local_category_fallback(
    rollup: object,
    summaries: dict,
) -> str:
    """Return a deterministic, local-only narrative for one category.

    Category synthesis is an enhancement, not a condition for presenting a
    category's work.  In particular, session summaries often start as local
    ``fallback`` rows and a configured narrator can be unavailable or exhaust
    its budget.  Keep the category visible in those cases without inventing
    model output or persisting a synthetic aggregate in the narrator cache.
    """
    category = str(rollup.category)
    sessions = int(rollup.sessions)
    session_label = "session" if sessions == 1 else "sessions"
    header = (
        f"{category}: {sessions} {session_label} · "
        f"{rollup.active_min:.0f} active min."
    )

    details: list[str] = []
    for session in rollup.top_sessions:
        summary = summaries.get(session.session_id)
        # Local fallback rows are derived from the raw first/last user turns.
        # They remain useful in the detailed session list, but must not be
        # promoted to a category-level recap where they could expose commands,
        # paths, or an unbounded prompt.  Only current generated summaries or
        # authoritative human records are suitable for this higher-level
        # surface.
        text = (
            summary.summary.strip()
            if summary_is_synthesis_eligible(summary) and summary.summary
            else ""
        )
        if text:
            details.append(text)
        if len(details) == 4:
            break
    if not details:
        projects = [project.project for project in rollup.projects[:3]]
        if projects:
            details.append("Projects: " + ", ".join(projects) + ".")
        else:
            details.append("No generated session summary was available.")

    return f"{header}\n\n" + "\n".join(f"- {text}" for text in details)


def _fill_local_category_fallbacks(
    rollups: list,
    summaries: dict,
    category_narratives: dict[str, str],
    narrative_provenance: dict[str, object],
) -> None:
    """Fill missing per-category prose and disclose its local provenance."""
    categories = narrative_provenance["categories"]
    assert isinstance(categories, dict)
    for rollup in rollups:
        if category_narratives.get(rollup.category):
            continue
        category_narratives[rollup.category] = _local_category_fallback(
            rollup, summaries,
        )
        categories[rollup.category] = {
            **_LOCAL_CATEGORY_FALLBACK_PROVENANCE,
            "reason": "no_generated_category_narrative",
        }


def _resolve_all_sessions(
    sessions: list,
    summaries: dict,
    mode: str,
    fallback: str,
    console: Console,
    budget: NarrativeBudget | None = None,
) -> None:
    """Resolve every session's bucket via the unified resolver, batching LLM
    for cache misses. Mutates ``sessions[*].category`` and ``.category_source``.

    Two-pass design:
      Pass 1: cache + folder rule walk-through (single SQL query for cache)
      Pass 2: one batched local narrator call for sessions marked ``needs_llm``,
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
            if not summary_is_synthesis_eligible(summ) or not summ.summary:
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
                    items, on_chunk_complete=_tick, budget=budget,
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
                f"session(s) via configured narrator[/dim]\n"
            )
    else:
        # Folder mode (or no LLM path) → assign fallback to leftovers.
        for s in needs_llm:
            s.category = fallback
            s.category_source = "fallback"


def _backfill_summaries(
    sessions,
    console: Console,
    use_llm: bool = False,
    force: bool = False,
    budget: NarrativeBudget | None = None,
) -> dict[str, int]:
    """Resolve narratives for sessions in this window.

    Default path is the instant first/last-user-message fallback for never-seen
    sessions. Pass `use_llm=True` to opt into the configured narrator: it upgrades
    `fallback` rows to `auto` and regenerates stale `auto` rows (older
    prompt_version) — or, with `force=True`, every in-window `auto`. The
    user sees the shared time budget, split into new vs regenerated, before
    the adaptive batch sequence starts.
    """
    plan = prepare_backfill_plan(
        sessions, use_llm=use_llm, force=force,
    )
    ids = plan.ids
    existing = plan.existing
    todo = plan.todo
    if not todo:
        return {
            "summarized": 0, "fallback": 0, "skipped": 0,
            "regenerated": 0, "already": len(ids),
        }

    if use_llm:
        regen = sum(1 for sid in todo if existing.get(sid) is not None)
        breakdown = f"{len(todo) - regen} new"
        if regen:
            breakdown += f" + {regen} regenerated"
        console.print(
            f"[yellow]![/yellow] {len(todo)} session(s) to summarize "
            f"({breakdown}). Starts with a 10-session probe; LLM work has a "
            f"[bold]90s total budget[/bold] and 45s per-call deadline. "
            "Unfinished sessions use local fallback.\n"
        )
        progress_desc = "Summarizing sessions via configured narrator"
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
        def _progress(done: int, _total: int, _sid: str, source: str) -> None:
            progress.update(task, description=f"[dim]completed {done}/{len(todo)} ({source})[/dim]")
            progress.advance(task)

        def _chunk_progress(done: int, total: int) -> None:
            progress.update(
                task,
                description=f"[dim]narrator batch {done}/{total} complete[/dim]",
            )

        return backfill_for_sessions(
            sessions, on_progress=_progress, use_llm=use_llm, force=force,
            budget=budget,
            on_chunk_complete=_chunk_progress if use_llm else None,
            prepared_plan=plan,
        )


def _agent_data_roots(agent: str) -> list[tuple[str, Path]]:
    """(agent, transcript root) pairs for the selected ``--agent`` filter."""
    return provider_data_roots(agent)


def build_recap(
    window: str = "month",
    *,
    minimal: bool = False,
    llm_narrative: bool = False,
    narrative: str = "per-category",
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
    """Run the full recap pipeline for one window and return the result.

    This is the one-call library entry point (#110): everything the CLI's
    default flow does — session collection, summary backfill, bucket
    resolution, narrative synthesis, previous-window comparison, artifact
    collection, markdown render — behind a single function. Parameters
    mirror the CLI flags one-to-one:

      window            week | month | all | YYYY-MM   (positional arg)
      minimal           --minimal        skip the narrative pipeline entirely
      llm_narrative     --llm-narrative  polish per-session summaries via the
                                         configured local narrator (90s shared budget)
      narrative         --narrative      per-category (default) | overall | both
      aggregate         --no-aggregate   False skips the overall synthesis
      compare           --no-compare     False skips the vs-previous block
      compare_narrative --no-compare-narrative
      artifacts         --no-artifacts   False skips the Repo activity scan
      classify          --classify       folder | content | hybrid
      refresh           --refresh        wipe this window's caches first
      refresh_all       --refresh-all    wipe ALL classification caches
      flavor            --for            plain | obsidian markdown variant
      lang              --lang           narrative language override
      agent             --agent          all | registered provider id — which
                                         coding agent's sessions to include
      reports_dir       --reports-dir    None → ~/.ccstory/reports
      write_report      (CLI always writes)  False skips the report file

    `console` controls progress output: pass a Rich Console to see status
    lines / progress bars (the CLI passes its own; scripts typically pass
    ``Console(stderr=True)``), or leave None for silence.

    Side effects: writes the markdown report (unless ``write_report=False``)
    and updates ccstory's own caches (summaries, classifications, period
    aggregates) in ``~/.ccstory/cache.db`` — same as a CLI run.

    Raises ``ValueError`` for an unrecognized window or an unknown ``agent``,
    ``RecapUnavailable`` when there is no session data / no engaged
    sessions in the window, and ``session_summarizer.CacheUnavailable``
    when ``~/.ccstory/cache.db`` cannot be opened (corrupt, locked, or
    written by a newer ccstory) — all normal exceptions a host process
    can catch (#119).
    """
    if console is None:
        console = Console(quiet=True)

    apply_lang_override(lang)

    roots = _agent_data_roots(agent)
    if not any(root.exists() for _, root in roots):
        where = " or ".join(f"{agent_label(name)} at {root}" for name, root in roots)
        raise RecapUnavailable(f"No session data ({where}). Have you used it yet?")

    # Load user price overrides (config [prices] table). No-op if absent.
    prices, snapshot, provenance = load_prices_config(CONFIG_PATH)
    apply_prices(prices, snapshot, provenance)

    # Config validation (#69): a project listed under two areas is ambiguous.
    # The resolver keeps the first (exact-membership, config order); surface
    # the shadowed ones once so the user can clean up rather than silently
    # losing a membership.
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

    previous_sessions = None
    previous_usage = None
    with console.status("[dim]Parsing sessions and token usage…[/dim]"):
        if compare and window != "all":
            prev_since, prev_until = previous_window(since, until)
            snapshot = collect_provider_snapshot(
                windows={
                    "current": (since, until),
                    "previous": (prev_since, prev_until),
                },
                agent=agent,
            )
        else:
            snapshot = collect_provider_snapshot(
                windows={"current": (since, until)},
                agent=agent,
            )
        sessions = snapshot.sessions_by_window["current"]
        if not sessions:
            raise RecapUnavailable("No engaged sessions in this window.")
        usage = snapshot.usage_by_window["current"]
        if "previous" in snapshot.sessions_by_window:
            previous_sessions = snapshot.sessions_by_window["previous"]
            previous_usage = snapshot.usage_by_window["previous"]

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
    narrative_budget = NarrativeBudget() if not minimal else None
    overall_narrative: str | None = None
    if not minimal:
        imported = import_from_claude_recap()
        if imported:
            console.print(
                f"[green]✓[/green] [dim]imported {imported} cached "
                f"summarie(s) from ~/.claude/session_summaries.db "
                f"(/recap)[/dim]\n"
            )
        if llm_narrative and not llm_available():
            console.print(
                "[yellow]![/yellow] [dim]no configured narrative CLI is available — "
                "--llm-narrative will fall back to first/last user messages[/dim]\n"
            )
        counts = _backfill_summaries(
            sessions, console, use_llm=llm_narrative,
            force=(refresh or refresh_all),
            budget=narrative_budget,
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
        budget=narrative_budget,
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
    narrative_provenance: dict[str, object] = {"overall": None, "categories": {}, "comparison": None}
    if not minimal:
        if aggregate and summaries and narrative in ("overall", "both"):
            overall_narrative = _synthesize_overall(
                label, sessions, rollups, summaries, console,
                budget=narrative_budget,
            )
            if overall_narrative:
                narrative_provenance["overall"] = get_period_narrative_provenance(label)
                console.print(
                    "[green]✓[/green] [dim]synthesized overall narrative"
                    "[/dim]\n"
                )
        if narrative in ("per-category", "both"):
            generated_count = 0
            if summaries:
                category_narratives = _synthesize_categories(
                    label, sessions, rollups, summaries, console,
                    budget=narrative_budget,
                )
                generated_count = len(category_narratives)
                narrative_provenance["categories"] = {
                    category: get_period_narrative_provenance(label, category)
                    for category in category_narratives
                }
            _fill_local_category_fallbacks(
                rollups, summaries, category_narratives, narrative_provenance,
            )
            fallback_count = len(category_narratives) - generated_count
            console.print(
                f"[green]✓[/green] [dim]category narratives: "
                f"{generated_count} synthesized · {fallback_count} local fallback[/dim]\n"
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
                agent=agent,
                previous_sessions=previous_sessions,
                previous_usage=previous_usage,
            )
        if comparison and compare_narrative and summaries:
            prev_summaries = get_many(comparison.previous_session_ids)
            with console.status(
                "[dim]Synthesizing week-over-week narrative (configured narrator)…[/dim]"
            ):
                comparison.narrative = synthesize_comparison(
                    current_key=label,
                    previous_key=comparison.previous_label,
                    current_summaries=_eligible_summary_items(
                        [s.session_id for s in sessions], summaries,
                    ),
                    previous_summaries=_eligible_summary_items(
                        comparison.previous_session_ids, prev_summaries,
                    ),
                    deltas=[
                        (d.category, d.current_min, d.previous_min)
                        for d in comparison.deltas
                    ],
                    budget=narrative_budget,
                )
                if comparison.narrative:
                    narrative_provenance["comparison"] = get_comparison_narrative_provenance(
                        label, comparison.previous_label,
                    )

    if narrative_budget is not None:
        narrative_provenance["budget"] = narrative_budget.status()
        if narrative_budget.stopped_reason == "budget_exhausted":
            console.print(
                "[yellow]![/yellow] [dim]LLM analysis partial: "
                f"stopped at {narrative_budget.total_sec:.0f}s budget; "
                "remaining session work used local fallback and later prose was skipped[/dim]\n"
            )
        elif narrative_budget.timed_out_calls:
            console.print(
                "[yellow]![/yellow] [dim]LLM analysis partial: one or more "
                "calls reached the 45s deadline; affected session work used "
                "local fallback[/dim]\n"
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
        agent=agent,
        narrative_provenance=narrative_provenance,
    )

    report_path: Path | None = None
    if write_report:
        out_dir = reports_dir if reports_dir is not None else REPORTS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        agent_suffix = "" if agent == "all" else f"-{agent}"
        report_path = out_dir / f"recap-{label}{agent_suffix}.md"
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
        agent=agent,
        report_path=report_path,
        counts=counts,
        narrative_provenance=narrative_provenance,
    )
