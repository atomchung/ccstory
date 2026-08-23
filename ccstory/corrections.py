"""Durable user corrections for one physical session's summary/category.

Issue #191's product framing: a session correction is a trust escape hatch
for a materially wrong summary or category, not a routine editing workflow.
ccstory's generated/imported cache rows can be wrong — bounded excerpts,
narrator drift, fallback text, stale cache identity, or an ambiguous project
folder — and the user needs a first-class, local, durable way to say "no,
here is the true fact" without editing generated Markdown (ephemeral) or a
folder/category rule (#69, too broad for one exceptional session).

Corrections key on the *public* physical-session id (see
``session_identity.public_session_id``), never a window-slice evidence id:
a correction the user wrote about their work does not stop applying because
a report window later clips that session into slices. Project attribution
(which canonical project a session belongs to) is explicitly out of scope —
that binding belongs to #224's ``attributed_projects``, not this module.

Scope of this slice only (issue #191 PR A):

- the ``session_corrections`` storage primitives: :func:`set_session_correction`,
  :func:`get_session_corrections`, :func:`unset_session_correction`;
- the pure resolution functions :func:`resolve_summary` / :func:`resolve_category`,
  which layer correction precedence on top of a caller-supplied fallback
  value and never touch the cache, a transcript, or a model themselves.

Explicitly out of scope (see issue #191's PR split):

- comparing a correction's ``base_evidence_fingerprint`` against live
  evidence and deciding when to flip ``status`` to ``evidence_changed``, plus
  the accept/rebase operation that clears it — that integration depends on
  #188's evidence fingerprints (now on ``main``) but is its own PR B;
- any aggregate/comparison cache invalidation keyed on correction identity,
  and the shared ``ResolvedSessionView`` #238 also depends on — PR C;
- the ``ccstory correct`` / ``ccstory corrections`` CLI — PR D;
- exposing correction provenance in terminal/Markdown/JSON/MCP output — PR E.

Nothing in this module is wired into ``recap``, ``report``, the CLI, or MCP
yet; :func:`resolve_summary` / :func:`resolve_category` are ready for a later
slice to call once bulk resolved-view semantics are approved.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import categorizer
from .session_summarizer import cache_session

# session_corrections.field — closed MVP enum (issue #191 "MVP CLI" section).
# Enforced here in Python rather than a SQLite CHECK constraint, matching how
# session_summarizer.py enforces its own source vocabulary.
FIELD_SUMMARY = "summary"
FIELD_CATEGORY = "category"
KNOWN_FIELDS = frozenset({FIELD_SUMMARY, FIELD_CATEGORY})

# session_corrections.status — never a scalar bool: "current" is the normal
# state; "evidence_changed" means the transcript/window-bounded evidence this
# correction was created against has since grown, and the correction stays
# authoritative but needs a compact review warning (issue #191 PR B territory
# to compute; this module only stores and never silently overwrites it).
STATUS_CURRENT = "current"
STATUS_EVIDENCE_CHANGED = "evidence_changed"
KNOWN_STATUSES = frozenset({STATUS_CURRENT, STATUS_EVIDENCE_CHANGED})

# Resolved-output provenance tag for a value that came from a user
# correction, matching the issue's explicit `source: "user_correction"`
# provenance example. Sibling to session_summarizer.SOURCE_* / categorizer's
# "user_rule"/"llm_cache"/"builtin_rule"/"fallback" source tags.
SOURCE_USER_CORRECTION = "user_correction"
# Resolved-output provenance tag when neither a correction nor a fallback
# value exists at all (e.g. an unknown session id).
SOURCE_NONE = "none"

# set_session_correction()'s outcome classification, so a caller (the future
# PR D CLI) can render "created" / "replaced" / "unchanged" distinctly rather
# than a single ambiguous "ok".
OUTCOME_CREATED = "created"
OUTCOME_REPLACED = "replaced"
OUTCOME_UNCHANGED = "unchanged"

# Values are free user text (summary) or a short bucket name (category).
# Bounded generously enough for real corrections while rejecting an
# accidental full-transcript paste; counted in Python str length (code
# points), matching how the rest of ccstory bounds text (e.g.
# session_summarizer._CLAUDE_MD_MAX_CHARS).
MAX_SUMMARY_VALUE_CHARS = 2000
MAX_CATEGORY_VALUE_CHARS = 100
MAX_NOTE_CHARS = 500


class CorrectionError(ValueError):
    """Base class for a rejected ``session_corrections`` write.

    Subclasses ``ValueError`` so existing broad ``except ValueError`` callers
    keep working; a caller that wants to render a clean message (the future
    PR D CLI) can catch this base class specifically instead.
    """


class UnknownFieldError(CorrectionError):
    """``field`` is not in the closed MVP enum (``summary``, ``category``)."""


class InvalidCorrectionValueError(CorrectionError):
    """``session_id``, ``value``, or ``note`` fails blank/length validation."""


class UnknownCategoryError(CorrectionError):
    """A category value is outside the effective vocabulary, uncorrected.

    Raised only when ``allow_new_category`` is not passed — see
    :func:`set_session_correction`.
    """


@dataclass(frozen=True)
class SessionCorrection:
    """One stored row: a user's assertion about one physical session's field.

    ``status`` is never mutated silently by this module — only an explicit
    :func:`set_session_correction` write (create or replace) sets it, always
    to ``STATUS_CURRENT``; a true no-op write leaves it untouched. Deciding
    when live evidence has drifted enough to mark ``STATUS_EVIDENCE_CHANGED``
    is issue #191 PR B's job, not this dataclass's or this module's.
    """

    session_id: str
    field: str  # FIELD_SUMMARY | FIELD_CATEGORY
    value: str
    created_at: float
    updated_at: float
    base_evidence_fingerprint: str
    status: str  # STATUS_CURRENT | STATUS_EVIDENCE_CHANGED
    note: str | None = None


@dataclass(frozen=True)
class CorrectionWriteResult:
    """The row after a :func:`set_session_correction` write, plus what happened."""

    correction: SessionCorrection
    outcome: str  # OUTCOME_CREATED | OUTCOME_REPLACED | OUTCOME_UNCHANGED


@dataclass(frozen=True)
class ResolvedField:
    """One field's final resolved value plus where it came from.

    ``status`` is only meaningful when ``source == SOURCE_USER_CORRECTION``
    (it mirrors the winning correction's ``status``); it is ``None``
    whenever a non-correction value or no value at all was resolved, since
    only a stored correction carries the evidence-changed review state.
    """

    value: str | None
    source: str
    status: str | None = None


def _validate_session_id(session_id: str) -> str:
    cleaned = (session_id or "").strip()
    if not cleaned:
        raise InvalidCorrectionValueError("session_id cannot be blank")
    return cleaned


def _validate_field(field: str) -> str:
    if field not in KNOWN_FIELDS:
        raise UnknownFieldError(
            f"unknown correction field {field!r}; expected one of "
            f"{sorted(KNOWN_FIELDS)}"
        )
    return field


def _validate_value(field: str, value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise InvalidCorrectionValueError(
            f"{field} correction value cannot be blank"
        )
    limit = (
        MAX_CATEGORY_VALUE_CHARS if field == FIELD_CATEGORY
        else MAX_SUMMARY_VALUE_CHARS
    )
    if len(cleaned) > limit:
        raise InvalidCorrectionValueError(
            f"{field} correction value exceeds {limit} character limit "
            f"(got {len(cleaned)})"
        )
    return cleaned


def _validate_note(note: str | None) -> str | None:
    if note is None:
        return None
    cleaned = note.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_NOTE_CHARS:
        raise InvalidCorrectionValueError(
            f"correction note exceeds {MAX_NOTE_CHARS} character limit "
            f"(got {len(cleaned)})"
        )
    return cleaned


def effective_category_vocabulary(config_path: Path | None = None) -> set[str]:
    """The category names a correction may reference without an override.

    Built-in bucket names (``categorizer.DEFAULT_RULES``) unioned with the
    user's configured ``[categories]`` rule names — the exact same universe
    ``categorizer.classify()`` can resolve a session to. Deliberately does
    *not* include synthetic display-only labels like ``"uncategorized"``
    that no folder/content rule ever assigns.
    """
    return {rule.name for rule in categorizer.load_rules(config_path)}


def _row_to_correction(row: tuple) -> SessionCorrection:
    (
        session_id, field, value, created_at, updated_at,
        base_evidence_fingerprint, status, note,
    ) = row
    return SessionCorrection(
        session_id=session_id,
        field=field,
        value=value,
        created_at=created_at,
        updated_at=updated_at,
        base_evidence_fingerprint=base_evidence_fingerprint,
        status=status,
        note=note,
    )


def set_session_correction(
    session_id: str,
    field: str,
    value: str,
    evidence_fingerprint: str,
    note: str | None = None,
    *,
    allow_new_category: bool = False,
    config_path: Path | None = None,
) -> CorrectionWriteResult:
    """Create or replace one physical session's correction for ``field``.

    Idempotent: resubmitting the exact same ``(value, note,
    evidence_fingerprint)`` a session already has is a true no-op — the
    stored row (including its ``status`` and timestamps) is left byte-for-
    byte untouched, so a correction already marked ``STATUS_EVIDENCE_CHANGED``
    is never silently cleared by a resubmission that addressed nothing. Any
    actual change (value, note, or a rebased fingerprint) is an explicit
    write that resets ``status`` to ``STATUS_CURRENT`` — the user just
    reasserted this correction, so it is fresh again.

    Generated/imported cache rows (``session_summaries``,
    ``session_content_buckets``) are a completely separate table and this
    function never reads or writes them — a correction never overwrites the
    underlying auto/provided row it is layered on top of.

    Raises :class:`UnknownFieldError` for a field outside the MVP enum,
    :class:`InvalidCorrectionValueError` for a blank/oversized
    ``session_id``/``value``/``note``, and :class:`UnknownCategoryError` for
    a ``category`` value outside :func:`effective_category_vocabulary` unless
    ``allow_new_category=True`` — a correction may deliberately name a bucket
    that is not yet (or will never be) a global folder/category rule (#69).
    """
    session_id = _validate_session_id(session_id)
    field = _validate_field(field)
    cleaned_value = _validate_value(field, value)
    if field == FIELD_CATEGORY and not allow_new_category:
        vocabulary = effective_category_vocabulary(config_path)
        if cleaned_value not in vocabulary:
            raise UnknownCategoryError(
                f"{cleaned_value!r} is not a known category "
                f"({', '.join(sorted(vocabulary))}); "
                "pass allow_new_category=True to correct this session to a "
                "category with no matching folder/content rule yet"
            )
    cleaned_note = _validate_note(note)
    evidence_fingerprint = evidence_fingerprint or ""

    now = time.time()
    with cache_session() as conn:
        existing = conn.execute(
            """SELECT value, created_at, updated_at, base_evidence_fingerprint,
                      status, note
               FROM session_corrections WHERE session_id = ? AND field = ?""",
            (session_id, field),
        ).fetchone()

        if existing is None:
            created_at = now
            updated_at = now
            status = STATUS_CURRENT
            outcome = OUTCOME_CREATED
        else:
            (prev_value, prev_created_at, prev_updated_at,
             prev_fingerprint, prev_status, prev_note) = existing
            unchanged = (
                prev_value == cleaned_value
                and prev_fingerprint == evidence_fingerprint
                and (prev_note or None) == cleaned_note
            )
            created_at = prev_created_at
            if unchanged:
                # Nothing this call could learn changed: preserve the row
                # exactly, including a prior STATUS_EVIDENCE_CHANGED — this
                # call did not address that state, so it must not clear it.
                updated_at = prev_updated_at
                status = prev_status
                outcome = OUTCOME_UNCHANGED
            else:
                updated_at = now
                status = STATUS_CURRENT
                outcome = OUTCOME_REPLACED

        if outcome != OUTCOME_UNCHANGED:
            conn.execute(
                """INSERT INTO session_corrections
                       (session_id, field, value, created_at, updated_at,
                        base_evidence_fingerprint, status, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id, field) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at,
                       base_evidence_fingerprint = excluded.base_evidence_fingerprint,
                       status = excluded.status,
                       note = excluded.note""",
                (session_id, field, cleaned_value, created_at, updated_at,
                 evidence_fingerprint, status, cleaned_note),
            )
            conn.commit()

    return CorrectionWriteResult(
        correction=SessionCorrection(
            session_id=session_id,
            field=field,
            value=cleaned_value,
            created_at=created_at,
            updated_at=updated_at,
            base_evidence_fingerprint=evidence_fingerprint,
            status=status,
            note=cleaned_note,
        ),
        outcome=outcome,
    )


def get_session_corrections(
    session_ids: list[str],
) -> dict[str, dict[str, SessionCorrection]]:
    """Bulk-fetch corrections for many sessions at once.

    Returns ``{session_id: {field: SessionCorrection}}``; a session with no
    corrections is simply absent from the outer mapping (never an empty
    inner dict), matching ``session_summarizer.get_many``'s "missing means
    absent" convention. Safe to call with a single-element list for one
    session. Blank/empty ids in the input are dropped rather than erroring —
    this is a read path, not a validating write.
    """
    ids = [sid.strip() for sid in session_ids if sid and sid.strip()]
    if not ids:
        return {}
    with cache_session() as conn:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""SELECT session_id, field, value, created_at, updated_at,
                       base_evidence_fingerprint, status, note
                FROM session_corrections
                WHERE session_id IN ({placeholders})""",
            ids,
        ).fetchall()
    result: dict[str, dict[str, SessionCorrection]] = {}
    for row in rows:
        correction = _row_to_correction(row)
        result.setdefault(correction.session_id, {})[correction.field] = correction
    return result


def unset_session_correction(session_id: str, field: str) -> bool:
    """Delete one session's correction for ``field``. Returns whether a row existed.

    Never touches ``session_summaries`` / ``session_content_buckets`` — after
    this call, resolution falls back to whatever generated/imported/
    deterministic value already lived underneath the correction, unmodified.
    """
    session_id = _validate_session_id(session_id)
    field = _validate_field(field)
    with cache_session() as conn:
        cur = conn.execute(
            "DELETE FROM session_corrections WHERE session_id = ? AND field = ?",
            (session_id, field),
        )
        conn.commit()
        return cur.rowcount > 0


def _resolve(
    field: str,
    fallback_value: str | None,
    fallback_source: str,
    correction: SessionCorrection | None,
) -> ResolvedField:
    """Shared precedence: an applicable correction always outranks fallback.

    Pure — no cache, transcript, or model access. Raises
    :class:`UnknownFieldError` if ``correction`` is for a different field
    than the resolver being called; a caller fetches per-field corrections
    from :func:`get_session_corrections`, so a mismatch here is a caller
    bug, not a legitimate "no correction" state (that is ``None``).
    """
    if correction is not None:
        if correction.field != field:
            raise UnknownFieldError(
                f"resolve_{field} received a {correction.field!r} "
                f"correction; expected {field!r}"
            )
        return ResolvedField(
            value=correction.value,
            source=SOURCE_USER_CORRECTION,
            status=correction.status,
        )
    return ResolvedField(value=fallback_value, source=fallback_source)


def resolve_summary(
    auto_or_record: Any,
    correction: SessionCorrection | None,
) -> ResolvedField:
    """Resolve one session's final summary text.

    ``auto_or_record`` is whatever the existing (non-correction) precedence
    chain already resolved — typically a ``session_summarizer.SessionSummary``
    (read structurally via ``.summary`` / ``.source`` so this module needs no
    import of that dataclass), or ``None`` when nothing is cached at all.
    A present ``correction`` always wins over it, per the issue's contract:
    "user correction always wins resolved output."
    """
    if auto_or_record is None:
        fallback_value, fallback_source = None, SOURCE_NONE
    else:
        fallback_value = getattr(auto_or_record, "summary", None)
        fallback_source = getattr(auto_or_record, "source", SOURCE_NONE)
    return _resolve(FIELD_SUMMARY, fallback_value, fallback_source, correction)


def resolve_category(
    rule_or_llm: tuple[str | None, str] | None,
    correction: SessionCorrection | None,
) -> ResolvedField:
    """Resolve one session's final category.

    ``rule_or_llm`` is whatever the existing (non-correction) precedence
    chain already resolved — the ``(bucket, source)`` pair
    ``categorizer.resolve_session_bucket`` returns — or ``None`` when
    nothing resolved at all. A present ``correction`` always wins over it,
    per the issue's contract: "user correction always wins resolved output."
    A session-level category correction never touches folder/category rules
    (#69) or any other session — this function does not either.
    """
    if rule_or_llm is None:
        fallback_value, fallback_source = None, SOURCE_NONE
    else:
        fallback_value, fallback_source = rule_or_llm
    return _resolve(FIELD_CATEGORY, fallback_value, fallback_source, correction)
