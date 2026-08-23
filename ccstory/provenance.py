"""Provider-neutral interaction-mode classification (#136, slice 1 of 4).

The recap has historically read all wall-clock session time as if a human
were driving every minute of it.  This module introduces the shared model
the issue proposes instead of a lossy ``attended: bool``: an explicit
:class:`InteractionMode` classification with a confidence level and a named
evidence trail, plus a pure, deterministic resolver that turns
already-collected authoritative session fields into that classification.

Scope of this slice only:

- the :class:`InteractionMode` / :class:`InteractionProvenance` model;
- :class:`InteractionSignals`, the normalized resolver input, and
  :func:`interaction_signals_from_session`, which reads it off the fields
  ``SessionStat``/``SessionSlice`` already carry;
- :func:`resolve_interaction_provenance` / :func:`resolve_session_provenance`,
  a pure, deterministic, session/window-pure classifier;
- :func:`evaluate_fixtures`, a reusable labeled-fixture evaluation harness
  that scores the resolver's policy (precision/recall/confusion per mode)
  against a set of hand-labeled cases.

Explicitly out of scope (see issue #136's PR split): provider transcript
parsing (stays in ``ccstory/providers/``), and wiring any of the above into
``recap``, ``report``, JSON, MCP output, or the existing
``SessionStat.engaged``/headline-time wording.  Nothing in this module is
called from those surfaces yet — ``resolve_session_provenance`` is the
callable, session/window-pure entry point later slices (recap, trend, MCP,
direct library callers, and the #224 project-attribution engine) adopt
without any change here.

Field-coverage gap (#136): ``SYSTEM_REVIEW`` (guardian / auto-review /
control-plane sessions) has no authoritative source on ``SessionStat`` /
``SessionSlice`` today.  ``resolve_interaction_provenance`` still accepts and
correctly classifies an explicit ``is_system_review`` signal, so the mode and
this evaluation harness are ready the day a provider adapter supplies one;
``interaction_signals_from_session`` always passes ``is_system_review=None``
because no bundled provider currently exposes that signal.  Wiring one is a
provider-adapter slice, not this one.

Deliberately not consulted: ``active_sec`` / ``SessionStat.engaged``.
Interaction mode answers *who or what drove the session*; ``engaged``
answers *whether it clears an existing wall-clock-time threshold*.  Folding
the two together is exactly the mistake flagged while landing #240: routing
Codex's automation marker into ``is_scheduled`` would have silently relaxed
that threshold for 41 real sessions instead of separating them out. This
resolver only ever reads ``is_scheduled``/``is_delegated``/``delegation_source``/
``is_system_review``/``user_msg_count`` and never touches engagement or
active-time math.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from .time_tracking import SessionSlice, SessionStat


class InteractionMode(StrEnum):
    """How a physical session was driven, independent of what it did."""

    INTERACTIVE = "interactive"  # direct user turns drive work
    DELEGATED = "delegated"  # another agent/session drives it
    SCHEDULED = "scheduled"  # automation trigger
    SYSTEM_REVIEW = "system_review"  # guardian/auto-review/control plane
    UNKNOWN = "unknown"


ProvenanceConfidence = Literal["high", "medium", "low"]

# Highest-priority mode wins when more than one authoritative signal fires on
# the same session (see _positive_categories below). Every provider's
# evidence list in the #136 brief puts a structural marker (scheduled
# envelope, guardian/auto-review metadata, child-path identity) ahead of
# direct user turns, so a non-interactive marker always outranks a raw
# message count here. SYSTEM_REVIEW leads because a guardian/control-plane
# pass is, by definition, not the primary work session even when it also
# carries a scheduled or delegated marker. SCHEDULED outranks DELEGATED as a
# documented default (a timer firing involves zero in-the-moment agent
# choice, one step further from human intent than an agent choosing to
# delegate); today's bundled providers never set both at once — Claude is
# the sole source of ``is_scheduled`` and Codex the sole source of
# ``is_delegated`` — so this ordering is forward-compatible groundwork, not
# something observable in current data.
_MODE_PRIORITY: tuple[InteractionMode, ...] = (
    InteractionMode.SYSTEM_REVIEW,
    InteractionMode.SCHEDULED,
    InteractionMode.DELEGATED,
    InteractionMode.INTERACTIVE,
)


@dataclass(frozen=True)
class InteractionProvenance:
    """One session's classified drive mode, with its evidence trail.

    ``signals`` holds only metadata name/value descriptors (for example
    ``"is_scheduled=true"`` or ``"delegation_source=codex_delegation"``),
    never prompt or transcript content.
    """

    mode: InteractionMode
    confidence: ProvenanceConfidence
    signals: tuple[str, ...]


@dataclass(frozen=True)
class InteractionSignals:
    """Normalized, provider-neutral input to :func:`resolve_interaction_provenance`.

    Every field is tri-state where the underlying fact can genuinely be
    absent: ``None`` means "not reported," distinct from a reported
    negative (``False`` / ``0``). The resolver never treats a missing
    signal as evidence either way — that is how ``UNKNOWN`` stays a
    first-class result instead of a fallback guess.

    ``delegation_source`` stays a plain string rather than tri-state
    because its only meaningful states are "no source recorded" (``""``)
    and "recorded as this provider-specific tag," mirroring
    ``SessionStat.delegation_source``.
    """

    is_scheduled: bool | None = None
    is_delegated: bool | None = None
    delegation_source: str = ""
    is_system_review: bool | None = None
    user_msg_count: int | None = None


def _bool_token(value: bool) -> str:
    return "true" if value else "false"


def _positive_categories(
    signals: InteractionSignals,
) -> list[tuple[InteractionMode, bool, tuple[str, ...]]]:
    """Every mode with affirmative evidence, as ``(mode, high_quality, tokens)``.

    A lone direct user turn (``user_msg_count == 1``) is also the normal
    shape of a scheduled or delegated session (its single triggering
    message), so it is added as INTERACTIVE evidence only when no
    structural marker already explains the session. Two or more user turns
    is independent evidence of a real back-and-forth and always competes —
    that is what correctly surfaces a human resuming a delegated or
    scheduled session as a (lower-confidence) conflict instead of a silent
    misclassification.
    """

    structural: list[tuple[InteractionMode, bool, tuple[str, ...]]] = []

    if signals.is_system_review is True:
        structural.append(
            (InteractionMode.SYSTEM_REVIEW, True, ("is_system_review=true",))
        )
    if signals.is_scheduled is True:
        structural.append(
            (InteractionMode.SCHEDULED, True, ("is_scheduled=true",))
        )
    if signals.is_delegated is True:
        source_known = bool(signals.delegation_source)
        tokens: tuple[str, ...] = ("is_delegated=true",)
        if source_known:
            tokens += (f"delegation_source={signals.delegation_source}",)
        structural.append((InteractionMode.DELEGATED, source_known, tokens))

    found = list(structural)
    count = signals.user_msg_count
    if count is not None and count >= 2:
        found.append(
            (InteractionMode.INTERACTIVE, True, (f"user_msg_count={count}",))
        )
    elif count is not None and count >= 1 and not structural:
        found.append(
            (InteractionMode.INTERACTIVE, False, (f"user_msg_count={count}",))
        )

    return found


def _unknown_provenance(signals: InteractionSignals) -> InteractionProvenance:
    """UNKNOWN with confidence set by how much authoritative data we have.

    ``UNKNOWN`` is a first-class result, not an error path: nothing here
    guesses a mode from partial data. "low" means metadata is absent
    outright. "medium" means every reported field is concretely known and
    simply describes a session with no evidence for any mode — for example,
    zero user messages and no scheduled/delegated/system marker.
    """

    reported: list[str] = []
    if signals.is_scheduled is not None:
        reported.append(f"is_scheduled={_bool_token(signals.is_scheduled)}")
    if signals.is_delegated is not None:
        reported.append(f"is_delegated={_bool_token(signals.is_delegated)}")
    if signals.is_system_review is not None:
        reported.append(f"is_system_review={_bool_token(signals.is_system_review)}")
    if signals.user_msg_count is not None:
        reported.append(f"user_msg_count={signals.user_msg_count}")

    confidence: ProvenanceConfidence = "medium" if reported else "low"
    return InteractionProvenance(
        mode=InteractionMode.UNKNOWN,
        confidence=confidence,
        signals=tuple(reported),
    )


def resolve_interaction_provenance(
    signals: InteractionSignals,
) -> InteractionProvenance:
    """Deterministically classify one session's drive mode.

    Pure function: the same ``signals`` value always returns an equal
    ``InteractionProvenance``, regardless of call order or any other
    resolution happening before or after it, and regardless of which order
    its fields were supplied in (``InteractionSignals`` is a fixed-shape
    dataclass, so there is no field ordering to depend on in the first
    place). Never inspects prompt or transcript text — every signal token
    is a metadata name/value pair read off already-collected fields.

    When more than one mode has affirmative evidence, the higher-priority
    mode (see ``_MODE_PRIORITY``) wins and confidence drops — to "medium"
    for two conflicting categories, "low" for three or more. ``signals``
    then carries every contending category's own tokens (priority order),
    not just the winner's, plus a trailing ``conflict=...`` token listing
    every contending mode in priority order — so the evidence trail always
    shows what was overridden, not only what won.
    """

    positive = _positive_categories(signals)
    if not positive:
        return _unknown_provenance(signals)

    by_mode = {mode: (high_quality, tokens) for mode, high_quality, tokens in positive}
    winner = next(mode for mode in _MODE_PRIORITY if mode in by_mode)
    high_quality, tokens = by_mode[winner]

    if len(by_mode) == 1:
        confidence: ProvenanceConfidence = "high" if high_quality else "medium"
        return InteractionProvenance(mode=winner, confidence=confidence, signals=tokens)

    # Conflict: keep every contending category's own tokens (priority order),
    # not just the winner's, so the evidence trail still shows what was
    # overridden — a debugger reading `signals` should never have to guess
    # why confidence dropped.
    conflict_modes = tuple(mode for mode in _MODE_PRIORITY if mode in by_mode)
    all_tokens: tuple[str, ...] = ()
    for mode in conflict_modes:
        all_tokens += by_mode[mode][1]
    conflict_token = "conflict=" + "+".join(mode.value for mode in conflict_modes)
    confidence = "medium" if len(by_mode) == 2 else "low"
    return InteractionProvenance(
        mode=winner,
        confidence=confidence,
        signals=all_tokens + (conflict_token,),
    )


def interaction_signals_from_session(
    session: SessionStat | SessionSlice,
) -> InteractionSignals:
    """Read the session/window-pure authoritative fields already collected.

    Only reads existing ``SessionStat``/``SessionSlice`` attributes — no
    provider transcript is reopened and no prompt/transcript text is
    inspected. ``is_system_review`` always comes back ``None`` today: no
    bundled provider currently surfaces a guardian/auto-review/control-plane
    marker on either dataclass (see the module docstring's field-coverage
    note).

    ``SessionStat.is_scheduled`` / ``is_delegated`` are plain ``bool`` with
    a ``False`` default, so "confirmed not scheduled/delegated" and "never
    inspected by a parser for this" are indistinguishable at this layer.
    That is an existing shape of those two fields, not something this
    adapter introduces or works around.
    """

    return InteractionSignals(
        is_scheduled=session.is_scheduled,
        is_delegated=session.is_delegated,
        delegation_source=session.delegation_source,
        is_system_review=None,
        user_msg_count=session.user_msg_count,
    )


def resolve_session_provenance(
    session: SessionStat | SessionSlice,
) -> InteractionProvenance:
    """Callable session/window-pure entry point for future consumers.

    Not called from recap, report, JSON, MCP, or any other output surface
    in this slice — this is the stable seam those consumers adopt later
    without any change to this module.
    """

    return resolve_interaction_provenance(interaction_signals_from_session(session))


@dataclass(frozen=True)
class LabeledFixture:
    """One synthetic, hand-labeled case for :func:`evaluate_fixtures`.

    ``category`` groups fixtures for reporting (for example ``"direct"``,
    ``"delegated"``, ``"scheduled"``, ``"guardian"``, ``"mixed_resumed"``,
    ``"missing_metadata"``, ``"conflicting_signals"``); it is free text the
    resolver never reads.
    """

    name: str
    signals: InteractionSignals
    expected_mode: InteractionMode
    category: str = ""


@dataclass(frozen=True)
class FixtureResult:
    fixture: str
    category: str
    expected_mode: InteractionMode
    actual: InteractionProvenance
    correct: bool


@dataclass(frozen=True)
class ConfusionCell:
    expected_mode: InteractionMode
    predicted_mode: InteractionMode
    count: int


@dataclass(frozen=True)
class ModeMetrics:
    """Precision/recall for one mode over an evaluated fixture set.

    ``precision``/``recall`` are ``None`` (not zero) when their denominator
    is zero, so "no fixtures exercised this mode" stays distinguishable
    from "exercised and scored 0".
    """

    mode: InteractionMode
    support: int  # fixtures actually labeled this mode
    predicted: int  # fixtures the resolver predicted this mode for
    true_positives: int
    precision: float | None
    recall: float | None


@dataclass(frozen=True)
class EvaluationReport:
    fixture_count: int
    correct_count: int
    accuracy: float
    confusion: tuple[ConfusionCell, ...]
    per_mode: tuple[ModeMetrics, ...]
    results: tuple[FixtureResult, ...]


def _mode_metrics(
    mode: InteractionMode, confusion: tuple[ConfusionCell, ...]
) -> ModeMetrics:
    support = sum(cell.count for cell in confusion if cell.expected_mode == mode)
    predicted = sum(cell.count for cell in confusion if cell.predicted_mode == mode)
    true_positives = sum(
        cell.count
        for cell in confusion
        if cell.expected_mode == mode and cell.predicted_mode == mode
    )
    return ModeMetrics(
        mode=mode,
        support=support,
        predicted=predicted,
        true_positives=true_positives,
        precision=round(true_positives / predicted, 6) if predicted else None,
        recall=round(true_positives / support, 6) if support else None,
    )


def evaluate_fixtures(fixtures: Sequence[LabeledFixture]) -> EvaluationReport:
    """Run the resolver over labeled fixtures and score it deterministically.

    Order-independent: fixtures only ever combine through counting and
    fixed-key sorted aggregation, so shuffling ``fixtures`` cannot change
    the returned ``confusion``, ``per_mode``, ``accuracy``, or ``results``
    (``results`` is itself sorted by fixture name, not input position).
    """

    if not fixtures:
        raise ValueError("evaluate_fixtures requires at least one fixture")

    name_counts = Counter(fixture.name for fixture in fixtures)
    duplicates = sorted(name for name, count in name_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate fixture names: {', '.join(duplicates)}")

    confusion_counts: dict[tuple[InteractionMode, InteractionMode], int] = defaultdict(
        int
    )
    results: list[FixtureResult] = []
    for fixture in fixtures:
        actual = resolve_interaction_provenance(fixture.signals)
        confusion_counts[(fixture.expected_mode, actual.mode)] += 1
        results.append(
            FixtureResult(
                fixture=fixture.name,
                category=fixture.category,
                expected_mode=fixture.expected_mode,
                actual=actual,
                correct=actual.mode == fixture.expected_mode,
            )
        )

    confusion = tuple(
        ConfusionCell(expected_mode=expected, predicted_mode=predicted, count=count)
        for (expected, predicted), count in sorted(
            confusion_counts.items(),
            key=lambda item: (item[0][0].value, item[0][1].value),
        )
    )
    per_mode = tuple(_mode_metrics(mode, confusion) for mode in InteractionMode)
    correct_count = sum(1 for result in results if result.correct)

    return EvaluationReport(
        fixture_count=len(fixtures),
        correct_count=correct_count,
        accuracy=round(correct_count / len(fixtures), 6),
        confusion=confusion,
        per_mode=per_mode,
        results=tuple(sorted(results, key=lambda result: result.fixture)),
    )
