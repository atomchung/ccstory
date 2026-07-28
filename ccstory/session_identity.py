"""The seam between public session identity and internal evidence identity.

Before 0.8 a single ``session_id`` served four unrelated purposes at once:
the id published in JSON/MCP, the key a human ``record`` row is stored under,
the key an automatic summary is cached under, and the key the content
classifier caches under. That conflation is safe only while one physical
session maps to exactly one set of facts.

``time_tracking.SessionSlice`` (#188, 0.8-C) breaks that assumption: one
physical session that crosses a window boundary produces two slices whose
bounded evidence differs, so each slice needs its own automatic-cache
identity while both still describe the same physical session the user knows.

This module names the two lanes explicitly so no consumer has to guess:

``public``    the physical provider session id. Public output, authoritative
              human ``record`` rows, and corrections live here. It is the only
              identity that may reach a terminal card, Markdown report, JSON
              envelope, MCP result, or share projection.

``evidence``  the identity of one bounded set of facts. Automatic caches
              (generated summaries, content classification) live here so a
              clipped slice can never reuse prose generated from a different
              window's evidence.

For a plain ``SessionStat`` the two ids are the same string, which is why
threading these helpers through existing call sites is behavior-preserving.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, is_dataclass, replace
from typing import Any

# Only a human-authored row is authoritative about the physical session as a
# whole. Everything else (``auto``, ``fallback``, ``skipped``, and rows
# imported from ~/.claude/session_summaries.db that were not records) is a
# statement about one specific set of bounded evidence.
AUTHORITATIVE_SUMMARY_SOURCE = "record"


@dataclass(frozen=True)
class SessionIdentity:
    """The two identities one session object carries."""

    public_id: str
    evidence_id: str
    boundary_clipped: bool = False

    @property
    def is_window_slice(self) -> bool:
        """Whether automatic caches must be scoped below the physical session."""

        return self.public_id != self.evidence_id


def public_session_id(session: Any) -> str:
    """Return the physical session id safe to publish and to correct against.

    ``SessionSlice`` carries it explicitly; every other session object already
    stores the physical id in ``session_id``.
    """

    physical = getattr(session, "physical_session_id", "") or ""
    if physical:
        return physical
    return getattr(session, "session_id", "") or ""


def evidence_session_id(session: Any) -> str:
    """Return the automatic-cache key for this session's bounded evidence."""

    return getattr(session, "session_id", "") or ""


def session_identity(session: Any) -> SessionIdentity:
    """Resolve both identity lanes for one session or slice."""

    return SessionIdentity(
        public_id=public_session_id(session),
        evidence_id=evidence_session_id(session),
        boundary_clipped=bool(getattr(session, "boundary_clipped", False)),
    )


def physical_session_view(session: Any) -> Any:
    """Present ``session`` under its physical identity for provider lookups.

    Where a transcript lives on disk is a fact about the whole physical
    session, not about one window of it. Provider adapters resolve a missing
    ``path`` by reading ``sess.session_id``, so a clipped slice handed over
    unchanged would look up an internal slice id, miss, and report "transcript
    gone" instead of "transcript found".

    Anything that is not a clipped slice is returned unchanged, so this costs
    nothing on the ordinary path. The returned copy is for provider lookup
    only and must not be cached or published: its ``session_id`` deliberately
    no longer names its own evidence.
    """

    physical = public_session_id(session)
    if not physical or physical == evidence_session_id(session):
        return session
    if is_dataclass(session) and not isinstance(session, type):
        return replace(session, session_id=physical)
    return session


def public_session_ids(sessions: Iterable[Any]) -> list[str]:
    """Public ids in input order, dropping session objects with no id."""

    return [sid for sid in (public_session_id(s) for s in sessions) if sid]


def evidence_session_ids(sessions: Iterable[Any]) -> list[str]:
    """Automatic-cache keys in input order, dropping session objects with no id."""

    return [sid for sid in (evidence_session_id(s) for s in sessions) if sid]


def resolve_session_summaries(
    sessions: Sequence[Any],
    *,
    fetch: Callable[[list[str]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Map each session's evidence id to the summary that may describe it.

    Precedence, per the 0.8 contract:

    1. a human ``record`` stored at the physical id wins for every slice of
       that session — a correction the user wrote about their work does not
       stop being true because a report window cut the session in half;
    2. otherwise the row cached at this slice's own evidence id is used;
    3. otherwise there is no summary, and the caller falls back to the bounded
       evidence carried by the slice itself.

    Step 3 is the correctness-first rule: a generated full-session summary is
    never reused for a clipped slice, because it describes facts that partly
    lie outside the requested window.

    The result is keyed by evidence id, which is ``session.session_id`` for
    every session object, so existing ``summaries.get(s.session_id)`` consumers
    keep working unchanged. Returned ``SessionSummary`` rows keep their own
    ``session_id`` field, which honestly names the row the text came from.
    """

    if fetch is None:
        from .session_summarizer import get_many as fetch

    identities = [session_identity(s) for s in sessions]

    wanted: list[str] = []
    seen: set[str] = set()
    for identity in identities:
        # A contained session asks for one id twice; the ordered dedupe keeps
        # this a single query regardless of how many slices share a physical id.
        for key in (identity.evidence_id, identity.public_id):
            if key and key not in seen:
                seen.add(key)
                wanted.append(key)
    if not wanted:
        return {}

    rows = fetch(wanted)

    resolved: dict[str, Any] = {}
    for identity in identities:
        if not identity.evidence_id:
            continue
        authoritative = rows.get(identity.public_id)
        if (
            authoritative is not None
            and getattr(authoritative, "source", "") == AUTHORITATIVE_SUMMARY_SOURCE
        ):
            resolved[identity.evidence_id] = authoritative
            continue
        own = rows.get(identity.evidence_id)
        if own is not None:
            resolved[identity.evidence_id] = own
    return resolved
