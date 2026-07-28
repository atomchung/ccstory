"""Deterministic, invocation-local sampling over one batch of sessions (#188, 0.8-E).

A recap or narrator often cannot afford to look at every session in a large
window — either because a downstream LLM call has a character budget, or
because showing every session would bury the few that actually matter. The
only precedent for that trade-off before this module was
``init_categories.sample_sessions_for_deep``: a per-day quota that spreads
picks across recency but has no notion of provider/project coverage, no
budget-aware skipping, and no way to say *why* a session was kept.

``SessionSketch`` is a small, cheap-to-compare projection of one session or
window slice. ``SamplingPlan`` is the deterministic result of applying a
fixed, explicit selection policy to a batch of sketches: the same population
— regardless of the order it arrives in — always produces the same plan,
field for field. Both are built fresh for one invocation, held only in
memory, and never cached or written to disk: there is no table, no
embedding, no vector index, and nothing here calls a clock or opens a
transcript.

The #188 spec for this slice deliberately rejects a diversity algorithm like
MMR (maximal marginal relevance, which needs embeddings and a similarity
metric) in favor of a fixed pipeline of coverage passes — provider, then
project, then local day, then a handful of bounded "salience" hints, then a
plain fill by active time. Every stage that must choose among several
candidates breaks ties with the same explicit tuple (see
``_tie_break_key``), which is what makes the whole plan reproducible.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from .session_identity import evidence_session_id, public_session_id
from .session_summarizer import SessionSummary
from .time_tracking import SessionSlice, SessionStat

# Bumped only when the *policy* below changes which candidates a plan keeps,
# or the order it reports them in — never for a docstring or comment edit.
# A caller that caches a plan keyed by this version can then trust that an
# unchanged version means an unchanged plan, mirroring how
# `WINDOW_EVIDENCE_POLICY_VERSION`/`SESSION_SLICE_ID_VERSION` in
# time_tracking.py version facts rather than prose.
POLICY_VERSION = 1

# The fixed, bounded vocabulary of salience reason codes. Order matters here:
# it is the priority order the policy's coverage pass uses when filling
# salience slots, and it is the order `SessionSketch.salience` reports
# matches in, so two sketches built from differently-ordered evidence never
# disagree about tuple order.
SALIENCE_CODES: tuple[str, ...] = ("error", "test", "security", "artifact", "outcome")

# Fixed, deliberately simple patterns. These are a sampling heuristic, not a
# content classifier: getting one wrong only changes which session fills a
# bounded "interesting session" slot, never a published fact about the
# session (see `SessionSketch.salience`'s docstring). Keeping the patterns
# here — rather than an open-ended keyword list a caller could supply — is
# what keeps the policy reproducible without a config file to drift out of
# sync between callers.
_SALIENCE_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "error": re.compile(
        r"\berror\w*\b|\bexception\w*\b|\btraceback\w*\b|\bcrash\w*\b"
        r"|\bfail\w*\b|\bbug\w*\b",
        re.IGNORECASE,
    ),
    "test": re.compile(
        r"\btest\w*\b|\bpytest\b|\bunittest\b|\bregression\w*\b|\bcoverage\b",
        re.IGNORECASE,
    ),
    "security": re.compile(
        r"\bsecurity\b|\bvulnerab\w*\b|\bcve-\d+\b|\bexploit\w*\b"
        r"|\bcredential\w*\b|\bsecret\w*\b|\bleak\w*\b",
        re.IGNORECASE,
    ),
    "artifact": re.compile(
        r"\bartifact\w*\b|\bscreenshot\w*\b|\bdeliverable\w*\b"
        r"|\bexport\w*\b|\breport\w*\b",
        re.IGNORECASE,
    ),
    "outcome": re.compile(
        r"\bfixed\b|\bresolved\b|\bshipped\b|\bmerged\b|\bdeployed\b"
        r"|\bcompleted\b|\bdone\b|\bsuccess\w*\b",
        re.IGNORECASE,
    ),
}

# Reason-code vocabulary for `SamplingPlan.reasons`. Fixed and closed so a
# plan's trace can never grow a code derived from transcript text.
REASON_PROVIDER_TOP = "provider_top"
REASON_PROJECT_TOP = "project_top"
REASON_DAY_TOP = "day_top"
REASON_BUDGET_FILL = "budget_fill"
REASON_SALIENCE_PREFIX = "salience:"

REASON_VOCABULARY: frozenset[str] = frozenset(
    {REASON_PROVIDER_TOP, REASON_PROJECT_TOP, REASON_DAY_TOP, REASON_BUDGET_FILL}
    | {f"{REASON_SALIENCE_PREFIX}{code}" for code in SALIENCE_CODES}
)

# The coverage dimensions `SamplingPlan.coverage_targets`/`coverage_hits`
# report on. Fixed rather than derived from the population so an empty
# population still reports all three dimensions at 0/0 instead of omitting
# them, which keeps the plan's shape stable regardless of input.
COVERAGE_DIMENSIONS: tuple[str, ...] = ("provider", "project", "day")


@dataclass(frozen=True)
class SessionSketch:
    """A cheap, comparable projection of one session or window slice.

    Built fresh for one invocation and discarded afterward — see the module
    docstring. Two identities are carried per the public/evidence seam in
    ``session_identity.py``: ``internal_id`` is the evidence id sampling
    reasons about (so a boundary-clipped window slice is never confused with
    a sibling slice of the same physical session), and ``physical_id`` is
    carried along only so a later stage can map a decision back to something
    safe to show a user — this module itself never publishes it anywhere.

    ``salience`` is a bounded set of heuristic reason codes only, never the
    text that triggered them. Treat it like a die roll that happened to come
    up "this session's bounded evidence mentions an error": useful for
    deciding what to sample, not a claim about the session's actual content,
    and never something a report or terminal card may surface as a fact.
    """

    internal_id: str
    physical_id: str
    provider: str
    project: str
    category: str
    local_day: date
    start: datetime
    active_sec: int
    msg_count: int
    evidence_chars: int
    summary_source: str | None
    salience: tuple[str, ...]


def _local_day(start: datetime) -> date:
    """Resolve the calendar day ``start`` falls on, in the local timezone.

    Grouping sessions by day is only useful if "day" means the day the user
    experienced, not the UTC day a transcript happens to store — #22 fixed
    exactly this off-by-one for report windows (see tests/test_timezone.py),
    and a per-session sketch must not reintroduce it here. This reuses the
    ``dt.astimezone()`` idiom already used for "now" in trends.py/recap.py
    rather than inventing a second convention: a naive ``start`` is presumed
    to already be host-local wall-clock time (Python attaches the local zone
    without shifting it); an aware ``start`` — what
    ``time_tracking._parse_ts`` always produces — converts to the host's
    local zone before truncating to a date.
    """

    return start.astimezone().date()


def _bounded_evidence_text(
    session: SessionStat | SessionSlice, summary: SessionSummary | None,
) -> str:
    """Return the one bounded text this sketch may derive size/salience from.

    Precedence follows how much the caller can trust the text as *the*
    bounded description of this session:

    1. a trusted ``summary`` — already reconciled by the caller, typically
       via ``session_identity.resolve_session_summaries`` — is the shortest
       faithful description available and exactly what a narrator would
       quote, so it wins when present;
    2. otherwise a window slice's own ``evidence_excerpt``, already clipped
       to the window by ``session_slice_for_window`` and never containing
       out-of-window prose;
    3. otherwise a plain session's ``first_user_text``, the only bounded
       text a ``SessionStat`` carries without reopening its transcript.

    The returned string is used only to compute a length and to search for
    fixed salience patterns; nothing in this module stores it on the sketch
    (see ``SessionSketch.salience``'s docstring) or on the plan.
    """

    if summary is not None and summary.summary:
        return summary.summary
    excerpt = getattr(session, "evidence_excerpt", "")
    if excerpt:
        return excerpt
    return getattr(session, "first_user_text", "") or ""


def _detect_salience(text: str) -> tuple[str, ...]:
    """Match bounded, fixed patterns against ``text`` and name what matched.

    Returns codes in the fixed ``SALIENCE_CODES`` order regardless of where
    in ``text`` each pattern matched, so two sketches built from
    differently-ordered evidence never disagree about tuple order.
    """

    if not text:
        return ()
    return tuple(
        code for code in SALIENCE_CODES if _SALIENCE_PATTERNS[code].search(text)
    )


def build_session_sketch(
    session: SessionStat | SessionSlice,
    *,
    summary: SessionSummary | None = None,
) -> SessionSketch:
    """Project one session or window slice into a comparable ``SessionSketch``.

    ``internal_id``/``physical_id`` reuse ``session_identity``'s two lanes
    rather than re-deriving them: ``evidence_session_id`` is the key the rest
    of sampling reasons about, and ``public_session_id`` is carried along
    only so a later stage can map a decision back to something safe to show
    a user. ``provider`` reads ``session.agent`` — the same field the rest of
    the codebase calls "agent" — because ``ccstory/providers/`` and this
    module's contract both talk about "providers"; there is no behavior
    difference, only a naming one.
    """

    text = _bounded_evidence_text(session, summary)
    return SessionSketch(
        internal_id=evidence_session_id(session),
        physical_id=public_session_id(session),
        provider=session.agent,
        project=session.project,
        category=session.category,
        local_day=_local_day(session.start),
        start=session.start,
        active_sec=int(session.active_sec),
        msg_count=int(session.msg_count),
        evidence_chars=len(text),
        summary_source=summary.source if summary is not None else None,
        salience=_detect_salience(text),
    )


@dataclass(frozen=True)
class SamplingPlan:
    """The deterministic result of applying the sampling policy to a batch.

    Every field is part of the plan's stable surface, not just
    ``selected_ids``: a caller that only compared ``selected_ids`` across two
    runs could miss a regression in ``reasons`` or ``coverage_hits``, so
    tests for this module compare the whole dataclass.
    """

    policy_version: int
    population_size: int
    ordered_ids: tuple[str, ...]
    selected_ids: tuple[str, ...]
    reasons: Mapping[str, tuple[str, ...]]
    char_budget: int | None
    coverage_targets: Mapping[str, int]
    coverage_hits: Mapping[str, int]
    unselected_count: int


def _tie_break_key(sketch: SessionSketch) -> tuple[int, float, str, str]:
    """The one fallback ordering every ranking step in this module reuses.

    Every stage below ranks candidates by a stage-specific primary score (a
    provider's, project's, or day's total active time — or, here, the
    candidate's own activity). Two distinct candidates can always tie on
    that score, and a tie broken by input order or by dict/set iteration
    order would make the whole plan depend on how the caller happened to
    list its sessions. Reusing this exact tuple everywhere — active time
    first, then start (as ``start.timestamp()`` rather than the ``datetime``
    itself, so two equal instants expressed with different tzinfo still
    compare equal instead of raising ``TypeError``), then provider, then the
    evidence id, which is always unique in one population — guarantees a
    total order no matter which stage the tie came from.
    """

    return (
        -sketch.active_sec,
        sketch.start.timestamp(),
        sketch.provider,
        sketch.internal_id,
    )


def _normalized_project_key(project: str) -> str:
    """A Unicode-stable sort key for a project name.

    NFC-normalizing then casefolding before comparing means two project name
    strings that render identically but differ in Unicode form (combining
    marks vs. a precomposed character, or letter case) always land in the
    same relative sort position. This module does not fold them into one
    *group* — that grouping decision belongs to ``categorizer.alias_fold``, a
    different concern owned by another module — it only makes the display
    order of otherwise-equal-looking names well-defined and stable.
    """

    return unicodedata.normalize("NFC", project).casefold()


def _try_select(
    sketch: SessionSketch,
    reason: str,
    *,
    selected: dict[str, SessionSketch],
    reasons: dict[str, list[str]],
    remaining_budget: int | None,
    max_selected: int | None,
) -> int | None:
    """Attempt to add ``sketch`` to the plan under ``reason``.

    Recording a reason and adding a candidate are almost the same operation,
    but not quite: a session already selected by an earlier stage can still
    be *true* for a later stage's criterion too (e.g. it is both its
    provider's and its project's top pick), and that should show up in
    ``reasons`` at no extra budget cost. A session that fails the capacity or
    budget check below, on the other hand, must contribute nothing — it must
    never appear in ``reasons`` while being absent from ``selected``, or the
    two would go out of sync.

    Returns the (possibly unchanged) remaining budget for the caller to carry
    into the next candidate. The caller's loop must keep scanning either way
    (#188 step 8): a candidate that does not fit is skipped, never
    truncated, and never stops the rest of the scan.
    """

    if sketch.internal_id not in selected:
        if max_selected is not None and len(selected) >= max_selected:
            return remaining_budget
        if remaining_budget is not None and sketch.evidence_chars > remaining_budget:
            return remaining_budget
        selected[sketch.internal_id] = sketch
        if remaining_budget is not None:
            remaining_budget -= sketch.evidence_chars

    codes = reasons.setdefault(sketch.internal_id, [])
    if reason not in codes:
        codes.append(reason)
    return remaining_budget


def build_sampling_plan(
    sketches: Sequence[SessionSketch],
    *,
    char_budget: int | None = None,
    max_selected: int | None = None,
    policy_version: int = POLICY_VERSION,
) -> SamplingPlan:
    """Apply the fixed #188 sampling policy to one batch of sketches.

    The policy is a pipeline of coverage passes, ordered so its result never
    depends on the order ``sketches`` arrived in:

    1. canonicalize — dedupe by ``internal_id`` (the same tie-break used
       everywhere else decides a duplicate, never input order) and sort the
       population once; this order is exactly ``ordered_ids``;
    2. one top-by-activity candidate per provider, providers visited by
       total active time (descending) then name;
    3. one top-by-activity candidate per project, projects visited by total
       active time (descending) then a Unicode-normalized name;
    4. one top-by-activity candidate for each local day *not already
       covered* by an earlier step — unlike providers/projects, a day that
       already has a selected session on it is left alone rather than
       spending budget on a second one, matching the #188 spec's own
       wording ("cover **remaining** local days");
    5. one top-by-activity, not-yet-selected candidate per bounded salience
       code, in ``SALIENCE_CODES`` order — "when not already selected" in
       the spec, so, like day coverage, this step only fills gaps;
    6. fill any remaining capacity in the same order as ``ordered_ids``.

    Every step that must break a tie uses ``_tie_break_key`` — see its
    docstring for why one shared tuple matters. When ``char_budget`` is set,
    a candidate that does not fit is skipped and the scan continues (#188
    step 8); it is never truncated, and one oversized candidate never stops
    the rest of the plan from being built.
    """

    # Step 1: canonicalize. A duplicate `internal_id` in the input (the same
    # sketch handed in twice, or two callers disagreeing about one session)
    # must not let input order decide which copy wins, so ties use the same
    # rule as every other step below.
    by_id: dict[str, SessionSketch] = {}
    for sketch in sketches:
        existing = by_id.get(sketch.internal_id)
        if existing is None or _tie_break_key(sketch) < _tie_break_key(existing):
            by_id[sketch.internal_id] = sketch
    population = sorted(by_id.values(), key=_tie_break_key)
    ordered_ids = tuple(sketch.internal_id for sketch in population)

    selected: dict[str, SessionSketch] = {}
    reasons: dict[str, list[str]] = {}
    remaining_budget = char_budget

    # Step 2: provider coverage.
    by_provider: dict[str, list[SessionSketch]] = defaultdict(list)
    for sketch in population:
        by_provider[sketch.provider].append(sketch)
    provider_totals = {
        provider: sum(s.active_sec for s in items)
        for provider, items in by_provider.items()
    }
    for provider in sorted(by_provider, key=lambda p: (-provider_totals[p], p)):
        top = min(by_provider[provider], key=_tie_break_key)
        remaining_budget = _try_select(
            top,
            REASON_PROVIDER_TOP,
            selected=selected,
            reasons=reasons,
            remaining_budget=remaining_budget,
            max_selected=max_selected,
        )

    # Step 3: project coverage.
    by_project: dict[str, list[SessionSketch]] = defaultdict(list)
    for sketch in population:
        by_project[sketch.project].append(sketch)
    project_totals = {
        project: sum(s.active_sec for s in items)
        for project, items in by_project.items()
    }
    for project in sorted(
        by_project,
        key=lambda p: (-project_totals[p], _normalized_project_key(p), p),
    ):
        top = min(by_project[project], key=_tie_break_key)
        remaining_budget = _try_select(
            top,
            REASON_PROJECT_TOP,
            selected=selected,
            reasons=reasons,
            remaining_budget=remaining_budget,
            max_selected=max_selected,
        )

    # Step 4: cover remaining local days only. A day already touched by a
    # provider- or project-driven pick is left alone; only a day with no
    # selected session yet gets its own top candidate added.
    by_day: dict[date, list[SessionSketch]] = defaultdict(list)
    for sketch in population:
        by_day[sketch.local_day].append(sketch)
    day_totals = {
        day: sum(s.active_sec for s in items) for day, items in by_day.items()
    }
    covered_days = {sketch.local_day for sketch in selected.values()}
    for day in sorted(by_day, key=lambda d: (-day_totals[d], d)):
        if day in covered_days:
            continue
        top = min(by_day[day], key=_tie_break_key)
        remaining_budget = _try_select(
            top,
            REASON_DAY_TOP,
            selected=selected,
            reasons=reasons,
            remaining_budget=remaining_budget,
            max_selected=max_selected,
        )
        if top.internal_id in selected:
            covered_days.add(day)

    # Step 5: bounded salience coverage — at most one new candidate per fixed
    # code, chosen only from sessions not already selected.
    for code in SALIENCE_CODES:
        candidates = [
            sketch
            for sketch in population
            if code in sketch.salience and sketch.internal_id not in selected
        ]
        if not candidates:
            continue
        top = min(candidates, key=_tie_break_key)
        remaining_budget = _try_select(
            top,
            f"{REASON_SALIENCE_PREFIX}{code}",
            selected=selected,
            reasons=reasons,
            remaining_budget=remaining_budget,
            max_selected=max_selected,
        )

    # Step 6: fill remaining capacity by active time, in the same order as
    # `ordered_ids`.
    for sketch in population:
        if sketch.internal_id in selected:
            continue
        remaining_budget = _try_select(
            sketch,
            REASON_BUDGET_FILL,
            selected=selected,
            reasons=reasons,
            remaining_budget=remaining_budget,
            max_selected=max_selected,
        )

    selected_ids = tuple(
        sketch.internal_id for sketch in population if sketch.internal_id in selected
    )
    coverage_targets = {
        "provider": len(by_provider),
        "project": len(by_project),
        "day": len(by_day),
    }
    coverage_hits = {
        "provider": len({s.provider for s in selected.values()}),
        "project": len({s.project for s in selected.values()}),
        "day": len({s.local_day for s in selected.values()}),
    }

    return SamplingPlan(
        policy_version=policy_version,
        population_size=len(population),
        ordered_ids=ordered_ids,
        selected_ids=selected_ids,
        reasons={sid: tuple(codes) for sid, codes in reasons.items()},
        char_budget=char_budget,
        coverage_targets=coverage_targets,
        coverage_hits=coverage_hits,
        unselected_count=len(population) - len(selected),
    )
