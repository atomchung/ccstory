"""Session-level summaries cached to SQLite, generated through local CLIs.

This is the differentiator: ccusage gives numbers, ccstory gives a one-line
narrative per session.  Narrative work uses the first available configured
local coding-agent CLI; no API key or ccstory-operated service is involved.

Extracted from ting/personal_os/core/session_summarizer.py. Simplified for v1:
  - Single source ("auto") — dropped the personal_os curated "record" source
  - DB lives at ~/.ccstory/cache.db (not polluting Claude Code's own dir)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

LOG = logging.getLogger("ccstory.summarizer")
DB_PATH = Path.home() / ".ccstory" / "cache.db"
RECAP_DB_PATH = Path.home() / ".claude" / "session_summaries.db"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_MD_PATH = Path.home() / ".claude" / "CLAUDE.md"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CCSTORY_CONFIG_PATH = Path.home() / ".ccstory" / "config.toml"
CCSTORY_LANG_ENV = "CCSTORY_LANG"
CLAUDE_BIN = "claude"
CODEX_BIN = "codex"
ANTIGRAVITY_BIN = Path.home() / ".local" / "bin" / "agy"

# The CLI accepts free-form language names, but the short codes people
# naturally type (for example ``--lang en``) are ambiguous to a narrative
# model when most of the source material is in another language. Normalize
# only common, unambiguous codes and leave every other value intact.
_LANGUAGE_ALIASES = {
    "en": "English",
    "en-us": "English",
    "en-gb": "English",
    "zh": "Chinese",
    "zh-tw": "Traditional Chinese",
    "zh-hk": "Traditional Chinese",
    "zh-cn": "Simplified Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
}

# Bump whenever the per-session narrative prompt (_PROMPT_TEMPLATE) is changed
# materially, or when retuning it for a newer/better default `claude` model.
# Cached "auto" summaries carrying a lower prompt_version are treated as stale
# and regenerated on the next `--llm-narrative` run. Keep this an int so the
# comparison `stored < PROMPT_VERSION` is monotonic.
PROMPT_VERSION = 3
CACHE_SCHEMA_VERSION = 4


_CLAUDE_MD_MAX_CHARS = 500


def _build_language_line(language: str) -> str:
    """Render the single-line `Respond in <X>.` directive used for explicit
    language picks (CLI flag, env, ccstory config, settings.json, locale)."""
    normalized = language.strip().replace("_", "-").casefold()
    language = _LANGUAGE_ALIASES.get(normalized, language.strip())
    return (
        f"Respond in {language}. "
        f"The input summaries may be in a different language — still respond ONLY in {language}, translating concepts as needed. "
        "Keep the same length / format limits regardless of language."
    )


@lru_cache(maxsize=1)
def language_directive(path: Path | None = None) -> str:
    """Build the prompt block that tells `claude -p` what language to use.

    Resolution chain (high → low priority):
      1. ``$CCSTORY_LANG`` env var — shell-scoped or set by ``--lang``.
      2. ``~/.ccstory/config.toml`` ``language`` field — ccstory's own
         persistent override (top-level, written by hand or by
         ``ccstory category set`` rewrites that preserve it).
      3. ``~/.claude/CLAUDE.md`` — pasted verbatim so any custom
         directives the user wrote there stick (not just language).
      4. ``~/.claude/settings.json`` ``"language"`` field — set by
         Claude Code's ``/config`` UI (issue #55).
      5. System locale (``$LANG`` / ``locale.getlocale()``) — only
         when it resolves to a non-English language; English locales
         fall through to step 6.
      6. Hardcoded ``Respond in English.``

    Steps 1, 2, 4, 5 render the short single-line directive. Step 3
    pastes the markdown verbatim because CLAUDE.md can carry richer
    directives than just a language hint.

    Cached because every prompt assembly calls it; flushed only on
    process restart, which matches the edit cadence of the inputs.
    """
    env_lang = os.environ.get(CCSTORY_LANG_ENV, "").strip()
    if env_lang:
        return _build_language_line(env_lang)

    ccstory_lang = _read_ccstory_language()
    if ccstory_lang:
        return _build_language_line(ccstory_lang)

    target = path or CLAUDE_MD_PATH
    try:
        text = target.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        text = ""
    if text:
        excerpt = text[:_CLAUDE_MD_MAX_CHARS]
        return (
            "The user's ~/.claude/CLAUDE.md begins below between the markers. "
            "If it specifies a response language, respect it; otherwise default "
            "to English. Keep the same length / format limits regardless of "
            "language.\n"
            "--- CLAUDE.md ---\n"
            f"{excerpt}\n"
            "--- end ---"
        )
    settings_lang = _read_settings_language()
    if settings_lang:
        return _build_language_line(settings_lang)
    locale_lang = _detect_system_locale()
    if locale_lang:
        return _build_language_line(locale_lang)
    return "Respond in English."


def _read_settings_language(path: Path | None = None) -> str | None:
    """Return the top-level ``language`` field from ``~/.claude/settings.json``.

    Returns ``None`` when the file is missing, malformed JSON, or the
    field is absent / empty / non-string. Errors are swallowed silently
    because this is a soft fallback — a broken settings file should
    degrade to English, not crash ccstory.
    """
    target = path or CLAUDE_SETTINGS_PATH
    try:
        text = target.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    lang = data.get("language") if isinstance(data, dict) else None
    if isinstance(lang, str) and lang.strip():
        return lang.strip()
    return None


def _read_ccstory_language(path: Path | None = None) -> str | None:
    """Return the top-level ``language`` field from ``~/.ccstory/config.toml``.

    Mirrors ``_read_settings_language`` but reads ccstory's own config so
    users who don't touch Claude Code's settings.json (or override its
    value just for ccstory output) have a place to set it. Errors degrade
    silently to ``None`` so a malformed config falls through to lower
    layers rather than crashing.
    """
    target = path or CCSTORY_CONFIG_PATH
    if not target.exists():
        return None
    try:
        import tomllib  # py 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return None
    try:
        with target.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return None
    lang = data.get("language") if isinstance(data, dict) else None
    if isinstance(lang, str) and lang.strip():
        return lang.strip()
    return None


def _read_ccstory_config(path: Path | None = None) -> dict:
    """Return ccstory's TOML config, or an empty mapping on malformed input."""
    target = path or CCSTORY_CONFIG_PATH
    if not target.exists():
        return {}
    try:
        import tomllib  # py 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return {}
    try:
        with target.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def narrative_backends(path: Path | None = None) -> tuple[NarrativeBackend, ...]:
    """Resolve explicit local narrator backends from ``[narrative]`` config.

    Defaults are deliberately economical and ordered by the requested coding
    agent preference: Claude Sonnet, Codex GPT-5.6 Terra, then Antigravity
    Gemini Flash low.  Invalid entries are ignored rather than silently
    creating a model-less call; an empty ``providers`` list explicitly opts
    out of all LLM work.
    """
    block = _read_ccstory_config(path).get("narrative")
    if not isinstance(block, dict):
        return _DEFAULT_NARRATIVE_BACKENDS
    providers = block.get("providers")
    if providers is None:
        selected = [backend.provider for backend in _DEFAULT_NARRATIVE_BACKENDS]
    elif isinstance(providers, list) and all(isinstance(v, str) for v in providers):
        selected = [v.strip().lower() for v in providers]
    else:
        LOG.warning("ignoring malformed [narrative].providers; using defaults")
        return _DEFAULT_NARRATIVE_BACKENDS

    defaults = {backend.provider: backend for backend in _DEFAULT_NARRATIVE_BACKENDS}
    resolved: list[NarrativeBackend] = []
    seen: set[str] = set()
    for provider in selected:
        if provider in seen:
            continue
        seen.add(provider)
        default = defaults.get(provider)
        if default is None:
            LOG.warning("ignoring unsupported narrative provider %r", provider)
            continue
        override = block.get(provider)
        override = override if isinstance(override, dict) else {}
        model = override.get("model", default.model)
        if not isinstance(model, str) or not model.strip():
            LOG.warning("ignoring narrative provider %r without a model", provider)
            continue
        effort = override.get("effort", default.effort)
        if effort is not None and (not isinstance(effort, str) or not effort.strip()):
            LOG.warning("ignoring invalid effort for narrative provider %r", provider)
            continue
        resolved.append(NarrativeBackend(provider, model.strip(), effort.strip() if effort else None))
    return tuple(resolved)


def narrative_config_fingerprint() -> str:
    """Stable cache identity for the configured backend order and models."""
    return _cache_fingerprint(
        "narrative-backends",
        *(
            f"{backend.provider}:{backend.model}:{backend.effort or ''}"
            for backend in narrative_backends()
        ),
    )


_LOCALE_NAMES: dict[str, str] = {
    "zh_TW": "Traditional Chinese",
    "zh_HK": "Traditional Chinese",
    "zh_CN": "Simplified Chinese",
    "zh_SG": "Simplified Chinese",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt_BR": "Brazilian Portuguese",
    "pt": "Portuguese",
    "ru": "Russian",
    "ar": "Arabic",
    "hi": "Hindi",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "nl": "Dutch",
    "pl": "Polish",
    "tr": "Turkish",
}


def _detect_system_locale() -> str | None:
    """Return a human-readable language name inferred from the OS locale.

    Resolution order: ``locale.getlocale()`` then ``$LC_ALL`` / ``$LANG``.
    Returns ``None`` for unset / ``C`` / ``POSIX`` / English locales —
    the caller falls back to the hardcoded English directive in those
    cases, matching pre-locale-fallback behavior so existing English
    users see no change.
    """
    lang_tag = ""
    try:
        import locale as _locale
        lang_tag = _locale.getlocale()[0] or ""
    except (ValueError, ImportError):
        lang_tag = ""
    if not lang_tag:
        lang_tag = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    if not lang_tag:
        return None
    base = lang_tag.split(".", 1)[0].split("@", 1)[0]
    if base in ("", "C", "POSIX"):
        return None
    primary = base.split("_", 1)[0].lower()
    if primary == "en":
        return None
    return _LOCALE_NAMES.get(base) or _LOCALE_NAMES.get(primary) or base


@dataclass
class SessionSummary:
    session_id: str
    summary: str
    source: str  # "auto" | "skipped" | "fallback"
    project: str | None = None
    created_at: float = 0.0
    # Version of the per-session prompt (_PROMPT_TEMPLATE / PROMPT_VERSION)
    # that produced an "auto" summary. Used to detect staleness so a later
    # release can refresh summaries when the prompt — or the model it's
    # tuned for — improves. 0 / None for fallback/skipped/legacy rows.
    prompt_version: int = 0
    # The backend that actually wrote an ``auto`` summary.  ``None`` is kept
    # for extractive fallback, skipped, imported, and pre-provenance cache
    # rows; callers must never infer a model for those records.
    narrator_provider: str | None = None
    narrator_model: str | None = None
    narrator_fingerprint: str = ""


@dataclass(frozen=True)
class NarrativeBackend:
    """One configured local CLI backend, in fallback priority order."""

    provider: str
    model: str
    effort: str | None = None


@dataclass(frozen=True)
class NarrativeCall:
    """Successful LLM response plus the exact configured provenance."""

    stdout: str
    provider: str
    model: str


@dataclass
class NarrativeBudget:
    """One wall-clock allowance shared by every narrator lane in a recap.

    This is deliberately invocation-local: it controls responsiveness without
    persisting transcript-derived timing data.  A cache hit consumes no budget.
    """

    total_sec: float = 90.0
    batch_deadline_sec: float = 45.0
    started_at: float | None = None
    completed_calls: int = 0
    timed_out_calls: int = 0
    stopped_reason: str | None = None
    batch_durations: list[float] = field(default_factory=list)
    # Execution metadata only.  Never put prompts, generated prose, session
    # ids, project paths, or stderr in this lane: it is emitted in report JSON
    # and is intended to explain responsiveness rather than retain content.
    trace: list[dict[str, object]] = field(default_factory=list)
    _active_lane: str = field(default="unspecified", init=False, repr=False)
    _call_lane: str = field(default="unspecified", init=False, repr=False)
    _call_deadline_sec: float = field(default=0.0, init=False, repr=False)

    @staticmethod
    def _lane_name(lane: str | None) -> str:
        """Return a compact, non-content label suitable for public metadata."""
        if not isinstance(lane, str):
            return "unspecified"
        cleaned = "_".join(lane.strip().lower().split())
        return cleaned[:64] or "unspecified"

    @contextmanager
    def lane(self, lane: str):
        """Attribute a caller's next narrator work to a stable lane name.

        The context form preserves the long-standing ``run_llm_p(prompt,
        timeout, budget=...)`` seam used by library callers and test doubles.
        New callers may instead pass ``lane=`` directly to ``begin_call`` or
        ``run_llm_p``.
        """
        previous = self._active_lane
        self._active_lane = self._lane_name(lane)
        try:
            yield self
        finally:
            self._active_lane = previous

    def _record(self, event: str, **metadata: object) -> None:
        self.trace.append({"event": event, **metadata})

    def remaining_sec(self) -> float:
        if self.started_at is None:
            return self.total_sec
        return max(0.0, self.total_sec - (time.monotonic() - self.started_at))

    def begin_call(
        self, requested_timeout: float, *, lane: str | None = None,
    ) -> tuple[float, float] | None:
        """Reserve one call under the shared budget and its per-call deadline.

        The two-value return remains compatible with older callers.  ``lane``
        is optional metadata, not an additional scheduling policy, so adding
        it cannot change their timeout behavior.
        """
        if self.started_at is None:
            self.started_at = time.monotonic()
        remaining = self.remaining_sec()
        call_lane = self._lane_name(lane) if lane is not None else self._active_lane
        if remaining <= 0:
            self.stopped_reason = "budget_exhausted"
            self._record(
                "call", lane=call_lane, outcome="budget_exhausted",
                elapsed_sec=0.0, requested_timeout_sec=round(float(requested_timeout), 3),
            )
            return None
        allowed = min(float(requested_timeout), self.batch_deadline_sec, remaining)
        self._call_lane = call_lane
        self._call_deadline_sec = max(0.01, allowed)
        return time.monotonic(), self._call_deadline_sec

    def record_provider_attempt(
        self,
        provider: str,
        model: str,
        *,
        outcome: str,
        fallback: bool,
        elapsed_sec: float,
    ) -> None:
        """Record one provider attempt without retaining request/response data."""
        self._record(
            "provider_attempt", lane=self._call_lane, provider=provider,
            model=model, outcome=outcome, fallback=fallback,
            elapsed_sec=round(max(0.0, elapsed_sec), 3),
        )

    def record_batch(
        self, lane: str, completed: int, total: int, item_count: int, *,
        outcome: str = "completed",
    ) -> None:
        """Capture coarse batch progress, never individual work-item identity."""
        self._record(
            "batch", lane=self._lane_name(lane), completed=completed,
            total=total, item_count=item_count, outcome=outcome,
        )

    def finish_call(
        self, started: float, *, timed_out: bool = False, success: bool = False,
    ) -> None:
        elapsed = time.monotonic() - started
        self.batch_durations.append(elapsed)
        self.completed_calls += 1
        if timed_out:
            self.timed_out_calls += 1
        self._record(
            "call", lane=self._call_lane,
            outcome="success" if success else ("timed_out" if timed_out else "failed"),
            elapsed_sec=round(max(0.0, elapsed), 3),
            deadline_sec=round(self._call_deadline_sec, 3),
        )
        if self.remaining_sec() <= 0:
            self.stopped_reason = "budget_exhausted"

    def status(self) -> dict[str, object]:
        spent = 0.0 if self.started_at is None else self.total_sec - self.remaining_sec()
        lanes: dict[str, dict[str, object]] = {}
        for event in self.trace:
            lane = event.get("lane")
            if not isinstance(lane, str):
                continue
            summary = lanes.setdefault(
                lane,
                {"calls": 0, "successful_calls": 0, "failed_calls": 0,
                 "timed_out_calls": 0, "fallback_successes": 0, "batches": 0},
            )
            if event["event"] == "call":
                outcome = event.get("outcome")
                if outcome != "budget_exhausted":
                    summary["calls"] = int(summary["calls"]) + 1
                if outcome == "success":
                    summary["successful_calls"] = int(summary["successful_calls"]) + 1
                elif outcome == "timed_out":
                    summary["timed_out_calls"] = int(summary["timed_out_calls"]) + 1
                elif outcome not in ("budget_exhausted",):
                    summary["failed_calls"] = int(summary["failed_calls"]) + 1
            elif event["event"] == "provider_attempt":
                if event.get("outcome") == "success" and event.get("fallback"):
                    summary["fallback_successes"] = int(summary["fallback_successes"]) + 1
            elif event["event"] == "batch":
                summary["batches"] = int(summary["batches"]) + 1
        return {
            "total_sec": round(self.total_sec, 1),
            "spent_sec": round(max(0.0, spent), 1),
            "remaining_sec": round(self.remaining_sec(), 1),
            "completed_calls": self.completed_calls,
            "timed_out_calls": self.timed_out_calls,
            "stopped_reason": self.stopped_reason,
            "partial": bool(self.stopped_reason or self.timed_out_calls),
            "lanes": lanes,
            "trace": list(self.trace),
        }


_DEFAULT_NARRATIVE_BACKENDS: tuple[NarrativeBackend, ...] = (
    NarrativeBackend("claude", "sonnet"),
    NarrativeBackend("codex", "gpt-5.6-terra"),
    NarrativeBackend("antigravity", "gemini-3.6-flash-low", "low"),
)
_NARRATIVE_PROVIDER_NAMES = frozenset(b.provider for b in _DEFAULT_NARRATIVE_BACKENDS)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the columns SQLite currently exposes for ``table``."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _migration_1_baseline(conn: sqlite3.Connection) -> None:
    """Create/adopt the pre-fingerprint schema (#101).

    Databases shipped before ``PRAGMA user_version`` may already contain any
    subset of these tables.  ``CREATE IF NOT EXISTS`` plus the guarded column
    add makes adopting them deterministic without dropping cached rows.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_summaries (
            session_id     TEXT PRIMARY KEY,
            summary        TEXT NOT NULL,
            source         TEXT NOT NULL,
            project        TEXT,
            created_at     REAL NOT NULL,
            prompt_version INTEGER
        )
        """
    )
    if "prompt_version" not in _table_columns(conn, "session_summaries"):
        conn.execute(
            "ALTER TABLE session_summaries ADD COLUMN prompt_version INTEGER"
        )
    # Preserve the old adoption contract: existing summaries are stamped as
    # current rather than unexpectedly re-burning per-session Claude calls.
    conn.execute(
        "UPDATE session_summaries SET prompt_version = ? "
        "WHERE prompt_version IS NULL",
        (PROMPT_VERSION,),
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS period_aggregates (
            period_key   TEXT NOT NULL,
            category     TEXT NOT NULL,
            summary      TEXT NOT NULL,
            session_ids  TEXT NOT NULL,
            created_at   REAL NOT NULL,
            PRIMARY KEY (period_key, category)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS comparison_narratives (
            current_key  TEXT NOT NULL,
            previous_key TEXT NOT NULL,
            signature    TEXT NOT NULL,
            narrative    TEXT NOT NULL,
            created_at   REAL NOT NULL,
            PRIMARY KEY (current_key, previous_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_content_buckets (
            session_id TEXT PRIMARY KEY,
            bucket     TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    """Idempotently add one migration-owned column."""
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _migration_2_cache_fingerprints(conn: sqlite3.Connection) -> None:
    """Add per-family input fingerprints used by #102/#65."""
    # Reassert the baseline so a manually edited/incomplete user_version=1 DB
    # still gets a coherent schema rather than a cryptic ALTER failure.
    _migration_1_baseline(conn)
    for table in (
        "period_aggregates",
        "comparison_narratives",
        "session_content_buckets",
    ):
        _add_column_if_missing(
            conn, table, "input_fingerprint", "TEXT NOT NULL DEFAULT ''",
        )


def _migration_3_adopt_legacy_classifications(conn: sqlite3.Connection) -> None:
    """Stamp pre-fingerprint classification rows with the current fingerprint.

    Migration 2 backfilled ``input_fingerprint = ''`` — a value no read path
    ever matches — so every pre-existing classification silently stopped
    resolving (#118). The cache-only readers (trends / compare) never fire
    fresh LLM calls, so for them the orphaned rows could never lazily heal
    either: old windows degraded to fallback buckets permanently. Adopting
    the rows under the *current* config mirrors what migration 1 does for
    ``prompt_version``: zero re-burn for cache that is still meaningful.

    ``period_aggregates`` / ``comparison_narratives`` are deliberately left
    unstamped: their prompts changed after v0.5.1, and re-synthesis costs a
    few calls per window rather than one per session.
    """
    # Same manually-edited-DB guard migration 2 applies to a v1 DB: reassert
    # the full v2 shape so the UPDATE below cannot hit a missing column.
    _migration_2_cache_fingerprints(conn)
    conn.execute(
        "UPDATE session_content_buckets SET input_fingerprint = ? "
        "WHERE input_fingerprint = ''",
        (_content_classification_fingerprint(),),
    )


def _migration_4_narrator_provenance(conn: sqlite3.Connection) -> None:
    """Track the configured narrator behind every generated prose cache row.

    Existing auto summaries intentionally keep empty provenance.  They become
    stale on the next ``--llm-narrative`` run, which prevents a legacy Claude
    cache entry from being presented as if it had used the new explicit model
    policy.
    """
    _migration_3_adopt_legacy_classifications(conn)
    for table in (
        "session_summaries",
        "period_aggregates",
        "comparison_narratives",
    ):
        _add_column_if_missing(conn, table, "narrator_provider", "TEXT")
        _add_column_if_missing(conn, table, "narrator_model", "TEXT")
    _add_column_if_missing(
        conn, "session_summaries", "narrator_fingerprint",
        "TEXT NOT NULL DEFAULT ''",
    )


_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migration_1_baseline,
    _migration_2_cache_fingerprints,
    _migration_3_adopt_legacy_classifications,
    _migration_4_narrator_provenance,
)
assert len(_MIGRATIONS) == CACHE_SCHEMA_VERSION


class _CacheSchemaTooNew(sqlite3.DatabaseError):
    """Raised when an older ccstory binary opens a newer cache schema."""


class CacheUnavailable(RuntimeError):
    """``~/.ccstory/cache.db`` cannot be opened — corrupt, locked, or
    written by a newer ccstory.

    Raised instead of ``SystemExit`` because ccstory also runs in-process
    inside other tools (``build_recap()`` library consumers, the MCP
    server), and ``SystemExit`` subclasses ``BaseException`` — a host's
    ``except Exception`` cannot catch it, so one bad cache file would kill
    the whole host process (#119). The CLI catches this at its entry point
    and preserves the old behavior exactly: message to stderr, exit 1.

    ``str(exc)`` is the complete user-facing message, ``ccstory:``-prefixed
    lines included.
    """


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply missing cache migrations in ordered transactions."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > CACHE_SCHEMA_VERSION:
        raise _CacheSchemaTooNew(
            f"cache schema {current} is newer than supported "
            f"version {CACHE_SCHEMA_VERSION}"
        )
    for target in range(current + 1, CACHE_SCHEMA_VERSION + 1):
        try:
            conn.execute("BEGIN")
            _MIGRATIONS[target - 1](conn)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _corrupt_cache_message(e: Exception) -> str:
    """The recovery hint for a cache SQLite refuses to parse."""
    return (
        f"ccstory: error: cache at {DB_PATH} is corrupted ({e}).\n"
        f"ccstory: to reset, delete the file and re-run:\n"
        f"    rm {DB_PATH}\n"
        f"You'll lose cached per-session narratives + bucket assignments; "
        f"sessions get re-summarized on the next run."
    )


_verified_db_stamps: set[tuple[Path, int, int]] = set()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        stat = DB_PATH.stat()
        stamp = (DB_PATH, stat.st_mtime_ns, stat.st_size)
        if stamp not in _verified_db_stamps:
            # PRAGMA schema_version forces SQLite to parse the file early, so a
            # corrupt cache gets the existing actionable recovery hint.
            conn.execute("PRAGMA schema_version").fetchone()
            _run_migrations(conn)
            stat = DB_PATH.stat()
            _verified_db_stamps.add((DB_PATH, stat.st_mtime_ns, stat.st_size))
        return conn
    except _CacheSchemaTooNew as e:
        if conn is not None:
            conn.close()
        raise CacheUnavailable(
            f"ccstory: error: cache at {DB_PATH} was written by a newer "
            f"ccstory ({e}).\n"
            "ccstory: upgrade ccstory before opening this cache; the file "
            "was left untouched."
        ) from e
    except sqlite3.OperationalError as e:
        if conn is not None:
            conn.close()
        if "locked" in str(e).lower():
            # Transient concurrency (another ccstory process holds the DB,
            # e.g. two runs racing the one-shot migration) — advising `rm`
            # here would destroy a healthy cache over a retryable condition.
            raise CacheUnavailable(
                f"ccstory: error: cache at {DB_PATH} is locked by another "
                f"process ({e}).\n"
                "ccstory: another ccstory run (or a tool embedding it) is "
                "using the cache — retry once it finishes."
            ) from e
        raise CacheUnavailable(_corrupt_cache_message(e)) from e
    except sqlite3.DatabaseError as e:
        if conn is not None:
            conn.close()
        raise CacheUnavailable(_corrupt_cache_message(e)) from e
    except Exception:
        if conn is not None:
            conn.close()
        raise
    assert conn is not None
    return conn


def _cache_fingerprint(family: str, *parts: str) -> str:
    """Stable SHA-256 over the exact inputs that produced one cache family."""
    payload = json.dumps(
        [family, *parts], ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def upsert(
    session_id: str,
    summary: str,
    source: str,
    project: str | None = None,
    prompt_version: int = 0,
    narrator_provider: str | None = None,
    narrator_model: str | None = None,
    narrator_fingerprint: str = "",
) -> None:
    if not session_id or not summary:
        return
    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO session_summaries
               (session_id, summary, source, project, created_at, prompt_version,
                narrator_provider, narrator_model, narrator_fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, summary.strip(), source, project, time.time(),
             prompt_version, narrator_provider, narrator_model, narrator_fingerprint),
        )
        conn.commit()
    finally:
        conn.close()


def get(session_id: str) -> SessionSummary | None:
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT session_id, summary, source, project, created_at,
                      prompt_version, narrator_provider, narrator_model,
                      narrator_fingerprint
               FROM session_summaries WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
        return SessionSummary(*row) if row else None
    finally:
        conn.close()


def get_many(session_ids: list[str]) -> dict[str, SessionSummary]:
    if not session_ids:
        return {}
    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in session_ids)
        rows = conn.execute(
            f"""SELECT session_id, summary, source, project, created_at,
                       prompt_version, narrator_provider, narrator_model,
                       narrator_fingerprint
                FROM session_summaries WHERE session_id IN ({placeholders})""",
            session_ids,
        ).fetchall()
        return {r[0]: SessionSummary(*r) for r in rows}
    finally:
        conn.close()


def recent_auto_timestamps(limit: int = 60) -> list[float]:
    """`created_at` of the most recent `auto` summaries, oldest first.

    Retained as a cache-inspection helper for callers; batch writes mean these
    row timestamps are not a reliable measure of narrator-call latency.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT created_at FROM session_summaries
               WHERE source = 'auto'
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return sorted(r[0] for r in rows)


def import_from_claude_recap() -> int:
    """Pull cached summaries from ~/.claude/session_summaries.db (written by
    the personal_os /recap skill) into ccstory's cache.

    Idempotent — uses INSERT OR IGNORE so existing ccstory entries are
    preserved. Drops the recap-only `task_slug` column since ccstory's
    schema doesn't carry it. Silently returns 0 if the recap DB is absent
    (fresh users won't have it).
    """
    if not RECAP_DB_PATH.exists():
        return 0
    conn = _connect()
    try:
        try:
            # ATTACH doesn't support parameter binding for filenames, so
            # double single quotes per SQLite's literal-escape rule. Without
            # this, $HOME containing a "'" throws OperationalError.
            attach_path = str(RECAP_DB_PATH).replace("'", "''")
            conn.execute(f"ATTACH DATABASE '{attach_path}' AS recap")
        except sqlite3.OperationalError as e:
            LOG.warning("attach recap DB failed: %s", e)
            return 0
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO session_summaries
                   (session_id, summary, source, project, created_at,
                    prompt_version)
                   SELECT session_id, summary, source, project, created_at, ?
                   FROM recap.session_summaries
                   WHERE summary IS NOT NULL AND summary <> ''""",
                (PROMPT_VERSION,),
            )
            n = cur.rowcount or 0
            conn.commit()
            return n
        finally:
            conn.execute("DETACH DATABASE recap")
    except sqlite3.Error as e:
        LOG.warning("recap import failed: %s", e)
        return 0
    finally:
        conn.close()


def missing_ids(session_ids: list[str]) -> list[str]:
    if not session_ids:
        return []
    have = set(get_many(session_ids).keys())
    return [sid for sid in session_ids if sid not in have]


def resolve_transcript_path(sess) -> Path | None:
    """Locate the transcript backing a ``SessionStat``, whatever agent wrote it.

    Delegates to the session's provider, so this stays agent-agnostic — the
    on-disk layout of each agent lives in ``ccstory/providers/`` and nowhere
    else. Returns None when the transcript is gone (deleted, pruned worktree),
    which callers must treat as "skip", never as "empty conversation".

    One-shot convenience wrapper. Loops over many sessions should hold a
    ``providers.TranscriptResolver`` instead so provider indexes are built once.
    """
    from .providers import TranscriptResolver

    return TranscriptResolver().path_for(sess)


def _extract_excerpt(jsonl_path: Path, provider=None) -> tuple[str, str]:
    """Extract user-facing text excerpt for summarization. Returns (project, excerpt).

    Normal recap paths pass the registered provider, which owns its transcript
    format. The path-only fallback preserves older callers and recognizes the
    two formats shipped before the provider contract existed.
    """
    if provider is None:
        from .providers.claude import ClaudeCodeProvider
        from .providers.codex import CodexProvider

        provider = ClaudeCodeProvider(projects_dir=PROJECTS_DIR)
        try:
            with jsonl_path.open(encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record.get("payload"), dict):
                        provider = CodexProvider()
                    break
        except OSError:
            pass

    try:
        return provider.extract_excerpt(jsonl_path)
    except OSError:
        return jsonl_path.parent.name, ""


_PROMPT_TEMPLATE = """{language_directive}

Below is an excerpt of an AI coding-agent conversation (first/last user + assistant messages).

Write ONE sentence (max 18 words) summarizing what this session ACTUALLY DID — focus on outcomes, not process.

Good examples (English style; mirror this in whichever language you respond in):
- Refactored auth middleware to extract token validation into shared util
- Investigated TLS handshake failure on staging, traced to expired intermediate cert
- Drafted PR description for the v2 migration epic

Bad examples (don't do this):
- User asked X, the agent answered Y  (process, not outcome)
- A conversation about coding  (too vague)

Excerpt:
{excerpt}

Output only the one-sentence summary, no quotes, no prefix."""


SUMMARY_BATCH_SIZE = 40
SUMMARY_PROBE_BATCH_SIZE = 10
SUMMARY_MIN_BATCH_SIZE = 10
SUMMARY_BATCH_SLOW_SEC = 30
_SUMMARY_BATCH_EXCERPT_CHARS = 700
SUMMARY_OMISSION_RETRY_SIZE = 10


def _compact_batch_excerpt(excerpt: str, max_chars: int = _SUMMARY_BATCH_EXCERPT_CHARS) -> str:
    """Keep the semantic endpoints of a provider-formatted excerpt.

    Providers deliberately put the latest assistant response at the end of
    the excerpt.  A raw ``excerpt[:max_chars]`` therefore drops the outcome
    from long sessions and leaves the narrator with only the opening request.
    Keep the first user request, the latest user request, and the final
    assistant response within the batch budget.  The head/tail trim for each
    block preserves both intent and likely outcome evidence without changing
    the provider excerpt contract.
    """
    if max_chars <= 0 or not excerpt:
        return ""

    marker = re.compile(r"(?m)^\[(USER \d+|USER LATE|ASSISTANT END)\]\n?")
    matches = list(marker.finditer(excerpt))
    if not matches:
        return excerpt[:max_chars]

    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(excerpt)
        text = " ".join(excerpt[match.end():end].split()).strip()
        if text:
            blocks.append((match.group(1), text))

    user_blocks = [(label, text) for label, text in blocks if label.startswith("USER")]
    assistant_blocks = [(label, text) for label, text in blocks if label == "ASSISTANT END"]
    selected: list[tuple[str, str, int]] = []
    if user_blocks:
        selected.append((*user_blocks[0], 180))
        if len(user_blocks) > 1:
            selected.append((*user_blocks[-1], 160))
    if assistant_blocks:
        selected.append((*assistant_blocks[-1], 280))
    if not selected:
        return excerpt[:max_chars]

    def trim(text: str, budget: int) -> str:
        if len(text) <= budget:
            return text
        if budget <= 3:
            return text[:budget]
        head = max(1, (budget - 1) // 2)
        tail = budget - head - 1
        return f"{text[:head]}…{text[-tail:]}"

    rendered = [f"[{label}]\n{trim(text, budget)}" for label, text, budget in selected]
    compacted = "\n\n".join(rendered)
    # Marker overhead is small and fixed, but keep the public limit exact if
    # a future label or separator changes its size.
    return compacted[:max_chars]


_SUMMARY_BATCH_PROMPT = """{language_directive}

Below are excerpts from several AI coding-agent conversations. For EACH input
session, write one outcome-focused summary of what the session actually did.

Return exactly one JSON object per line, in the same order as the inputs:
{{"session_id":"the supplied id","summary":"one sentence, maximum 18 words"}}

Rules:
- Preserve every supplied session_id exactly. Never invent or omit an id.
- Describe outcomes, not the conversation process or a user request.
- Keep each summary between 4 and 200 characters.
- Output JSON lines only: no Markdown fence, array, explanation, or prefix.

Sessions:
{rows}"""


def claude_bin_available() -> bool:
    return shutil.which(CLAUDE_BIN) is not None


_flag_confirmed_broken = False


def run_claude_p(
    prompt: str,
    timeout: float,
    model: str = "sonnet",
    deadline: float | None = None,
) -> subprocess.CompletedProcess:
    """Run `claude -p --model <model> --output-format text <prompt>`, preferring
    `--no-session-persistence` so one-off summarization calls don't clutter
    the user's `claude --resume` list.

    Some Claude Code CLI versions silently no-op with that flag — exit 0,
    empty stdout (ccstory#52) — so on that exact signature we retry once
    without it, and remember it process-wide: a single `ccstory` run can
    call this dozens of times (once per session, per classification chunk,
    ...), and re-discovering a known-broken flag on every one of those
    calls would burn a wasted subprocess spawn each time. A real failure
    (non-zero exit) is returned as-is and does NOT mark the flag broken —
    callers keep their existing error handling.
    """
    global _flag_confirmed_broken
    base = [CLAUDE_BIN, "-p", "--model", model, "--output-format", "text"]

    def _remaining_timeout() -> float:
        if deadline is None:
            return timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(str(CLAUDE_BIN), timeout)
        return remaining

    if not _flag_confirmed_broken:
        r = subprocess.run(
            [*base, "--no-session-persistence", prompt],
            capture_output=True, text=True,
            timeout=_remaining_timeout(), check=False,
        )
        if r.returncode != 0 or r.stdout.strip():
            return r
        _flag_confirmed_broken = True
    return subprocess.run(
        [*base, prompt],
        capture_output=True, text=True,
        timeout=_remaining_timeout(), check=False,
    )


def codex_bin_available() -> bool:
    return shutil.which(CODEX_BIN) is not None


def antigravity_bin_available() -> bool:
    return ANTIGRAVITY_BIN.is_file() and os.access(ANTIGRAVITY_BIN, os.X_OK)


def narrative_backend_available(backend: NarrativeBackend) -> bool:
    if backend.provider == "claude":
        return claude_bin_available()
    if backend.provider == "codex":
        return codex_bin_available()
    if backend.provider == "antigravity":
        return antigravity_bin_available()
    return False


def llm_available() -> bool:
    """Whether at least one configured narrator CLI is locally available."""
    return any(narrative_backend_available(backend) for backend in narrative_backends())


def _run_codex_p(
    prompt: str, timeout: int, model: str,
) -> subprocess.CompletedProcess:
    """Run Codex without persisting a source session or granting write access."""
    return subprocess.run(
        [
            CODEX_BIN, "exec", "--ephemeral", "--sandbox", "read-only",
            "--model", model, prompt,
        ],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _run_antigravity_p(
    prompt: str, timeout: float, model: str, effort: str,
    deadline: float | None = None,
) -> subprocess.CompletedProcess | None:
    """Run Antigravity prose generation with retries bounded by one deadline."""
    for attempt in range(3):
        attempt_timeout = max(timeout, 180)
        if deadline is not None:
            attempt_timeout = min(attempt_timeout, deadline - time.monotonic())
            if attempt_timeout <= 0:
                raise subprocess.TimeoutExpired(str(ANTIGRAVITY_BIN), timeout)
        result = subprocess.run(
            [str(ANTIGRAVITY_BIN), "-p", prompt, "--model", model, "--effort", effort],
            capture_output=True, text=True, timeout=attempt_timeout, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result
        if attempt == 2:
            return result
        LOG.warning("antigravity narrative call failed; retrying (%d/3)", attempt + 1)
    return None


def run_llm_p(
    prompt: str,
    timeout: int,
    budget: NarrativeBudget | None = None,
    *,
    lane: str | None = None,
) -> NarrativeCall | None:
    """Run the configured local narrators in order until one returns prose.

    This dispatcher is shared by summaries, classification, init, aggregate,
    and comparison prose.  It never guesses a model: every configured backend
    carries one explicit model id, and a failed/unavailable provider simply
    advances to the next configured provider.
    """
    reservation = (
        budget.begin_call(timeout, lane=lane) if budget is not None else None
    )
    if budget is not None and reservation is None:
        return None
    started, allowed_timeout = reservation if reservation is not None else (
        time.monotonic(), float(timeout),
    )
    deadline = started + allowed_timeout if budget is not None else None
    timed_out = False
    for backend_index, backend in enumerate(narrative_backends()):
        if not narrative_backend_available(backend):
            continue
        remaining = allowed_timeout
        if deadline is not None:
            # Floating-point subtraction can produce a value fractionally
            # greater than the reservation (for example 45.00000000000003).
            # Never pass that excess to a subprocess: the batch deadline is a
            # hard responsiveness limit, not an approximate target.
            remaining = min(allowed_timeout, deadline - time.monotonic())
            if remaining <= 0:
                timed_out = True
                break
        try:
            provider_started = time.monotonic()
            if backend.provider == "claude":
                if deadline is None:
                    result = run_claude_p(prompt, remaining, backend.model)
                else:
                    result = run_claude_p(
                        prompt, remaining, backend.model, deadline=deadline,
                    )
            elif backend.provider == "codex":
                result = _run_codex_p(prompt, remaining, backend.model)
            else:
                result = _run_antigravity_p(
                    prompt, remaining, backend.model, backend.effort or "low",
                    deadline=deadline,
                )
        except subprocess.TimeoutExpired:
            timed_out = True
            if budget is not None:
                budget.record_provider_attempt(
                    backend.provider, backend.model, outcome="timed_out",
                    fallback=backend_index > 0,
                    elapsed_sec=time.monotonic() - provider_started,
                )
            LOG.warning("%s narrative call reached its deadline", backend.provider)
            break
        except (subprocess.SubprocessError, OSError) as exc:
            if budget is not None:
                budget.record_provider_attempt(
                    backend.provider, backend.model, outcome="error",
                    fallback=backend_index > 0,
                    elapsed_sec=time.monotonic() - provider_started,
                )
            LOG.warning("%s narrative call errored: %s", backend.provider, exc)
            continue
        if result is not None and result.returncode == 0 and result.stdout.strip():
            if budget is not None:
                budget.record_provider_attempt(
                    backend.provider, backend.model, outcome="success",
                    fallback=backend_index > 0,
                    elapsed_sec=time.monotonic() - provider_started,
                )
                budget.finish_call(started, timed_out=timed_out, success=True)
            return NarrativeCall(result.stdout, backend.provider, backend.model)
        if budget is not None:
            budget.record_provider_attempt(
                backend.provider, backend.model, outcome="failed",
                fallback=backend_index > 0,
                elapsed_sec=time.monotonic() - provider_started,
            )
        stderr = result.stderr.strip()[:200] if result is not None else "no result"
        LOG.warning("%s narrative call failed: %s", backend.provider, stderr)
    if budget is not None:
        budget.finish_call(started, timed_out=timed_out)
    return None


def _run_budgeted_llm_p(
    prompt: str, timeout: int, budget: NarrativeBudget | None, *, lane: str,
) -> NarrativeCall | None:
    """Run one caller-labelled lane without breaking the old monkeypatch seam."""
    if budget is None:
        return run_llm_p(prompt, timeout)
    # Keep ``lane`` on the budget context rather than forwarding a new keyword
    # into every old wrapper/test double of ``run_llm_p``.
    with budget.lane(lane):
        return run_llm_p(prompt, timeout, budget=budget)


def summarize_via_claude_p(excerpt: str, timeout: int = 60) -> NarrativeCall | None:
    """Call the selected local narrator to summarize. Returns None on failure.

    Uses subprocess so we draw on the selected CLI's signed-in plan quota.
    No API key, no cost to ccstory.
    """
    if not excerpt.strip():
        return None
    if not llm_available():
        return None
    prompt = _PROMPT_TEMPLATE.format(
        excerpt=excerpt[:8000], language_directive=language_directive(),
    )
    try:
        call = run_llm_p(prompt, timeout)
        if call is None:
            return None
        out = call.stdout.strip().split("\n", 1)[0].strip().strip('"').strip("'")
        if len(out) < 4 or len(out) > 200:
            return None
        return NarrativeCall(out, call.provider, call.model)
    except (subprocess.SubprocessError, OSError) as e:
        LOG.warning("narrative backend errored: %s", e)
        return None


def _parse_batch_summaries(text: str, requested_ids: set[str]) -> dict[str, str]:
    """Accept only valid JSONL summaries for requested session ids.

    A batch narrator has more opportunity to omit or hallucinate ids than the
    old one-session request. Keep the cache fail-closed: malformed rows and
    unknown ids are ignored, so callers can fall back only those sessions.
    """
    parsed: dict[str, str] = {}
    cleaned = text.strip()
    for fence in ("```json", "```JSON", "```"):
        cleaned = cleaned.replace(fence, "")
    for line in cleaned.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        session_id = row.get("session_id")
        summary = row.get("summary")
        if not isinstance(session_id, str) or session_id not in requested_ids:
            continue
        if not isinstance(summary, str):
            continue
        summary = " ".join(summary.split()).strip().strip('"').strip("'")
        if 4 <= len(summary) <= 200:
            parsed[session_id] = summary
    return parsed


def summarize_sessions_via_llm_batch(
    items: list[tuple[str, str, str]],
    *,
    timeout: int = 120,
    batch_size: int = SUMMARY_BATCH_SIZE,
    budget: NarrativeBudget | None = None,
    on_chunk_complete: Callable[[int, int], None] | None = None,
) -> dict[str, NarrativeCall]:
    """Generate per-session prose in bounded batches.

    ``items`` is ``[(session_id, project, excerpt)]``. The returned mapping is
    intentionally sparse: a model omission, malformed JSON, or failed batch
    does not manufacture a cache entry. ``backfill_for_sessions`` owns the
    existing per-session fallback policy for those cases.
    """
    if not items or not llm_available():
        return {}
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    out: dict[str, NarrativeCall] = {}
    chunk_index = 0
    start = 0
    current_batch_size = min(SUMMARY_PROBE_BATCH_SIZE, batch_size)
    while start < len(items):
        if budget is not None and budget.remaining_sec() <= 0:
            budget.stopped_reason = "budget_exhausted"
            budget.record_batch(
                "session_summaries", chunk_index + 1, chunk_index + 1, 0,
                outcome="budget_exhausted",
            )
            break
        chunk_index += 1
        chunk = items[start:start + current_batch_size]
        def make_prompt(prompt_chunk: list[tuple[str, str, str]]) -> str:
            rows = "\n\n".join(
                f"[session_id={session_id}; project={project}]\n"
                f"{_compact_batch_excerpt(excerpt)}"
                for session_id, project, excerpt in prompt_chunk
            )
            return _SUMMARY_BATCH_PROMPT.format(
                language_directive=language_directive(), rows=rows,
            )

        prompt = make_prompt(chunk)
        started = time.monotonic()
        try:
            call = _run_budgeted_llm_p(
                prompt, timeout, budget, lane="session_summaries",
            )
        except (subprocess.SubprocessError, OSError) as exc:
            LOG.warning("batch session summarization errored: %s", exc)
            call = None
        if call is not None:
            requested_ids = {session_id for session_id, _, _ in chunk}
            parsed = _parse_batch_summaries(call.stdout, requested_ids)
            for session_id, summary in parsed.items():
                out[session_id] = NarrativeCall(
                    summary, call.provider, call.model,
                )
            missing = [item for item in chunk if item[0] not in parsed]
            if missing:
                retry_chunk = missing[:SUMMARY_OMISSION_RETRY_SIZE]
                if budget is None or budget.remaining_sec() > 0:
                    retry_call = None
                    try:
                        retry_call = _run_budgeted_llm_p(
                            make_prompt(retry_chunk), timeout, budget,
                            lane="session_summaries_retry",
                        )
                    except (subprocess.SubprocessError, OSError) as exc:
                        LOG.warning("batch omission retry errored: %s", exc)
                    if retry_call is not None:
                        retry_ids = {session_id for session_id, _, _ in retry_chunk}
                        for session_id, summary in _parse_batch_summaries(
                            retry_call.stdout, retry_ids,
                        ).items():
                            out[session_id] = NarrativeCall(
                                summary, retry_call.provider, retry_call.model,
                            )
                    if budget is not None:
                        budget.record_batch(
                            "session_summaries_retry", chunk_index, chunk_index,
                            len(retry_chunk),
                            outcome="completed" if retry_call is not None else "failed",
                        )
        elapsed = time.monotonic() - started
        # Probe first, then react to current-model speed without assuming
        # linear token latency. Failed or slow chunks shrink the next request;
        # a quick probe grows toward the normal 40-session ceiling.
        if call is None or elapsed >= SUMMARY_BATCH_SLOW_SEC:
            current_batch_size = max(SUMMARY_MIN_BATCH_SIZE, current_batch_size // 2)
        elif current_batch_size < batch_size:
            current_batch_size = min(batch_size, current_batch_size * 2)
        start += len(chunk)
        if budget is not None:
            remaining = len(items) - start
            projected_total = chunk_index + (
                (remaining + current_batch_size - 1) // current_batch_size
            )
            budget.record_batch(
                "session_summaries", chunk_index, projected_total, len(chunk),
            )
        if on_chunk_complete is not None:
            remaining = len(items) - start
            projected_total = chunk_index + (
                (remaining + current_batch_size - 1) // current_batch_size
            )
            on_chunk_complete(chunk_index, projected_total)
    return out


def _fallback_narrative(excerpt: str) -> str:
    """Zero-cost first → last user-message narrative.

    A single-message session keeps the old 120-character fallback.  With
    multiple messages, showing both endpoints gives the reader a cheap hint
    of the session's arc without spending a local narrator call (#70).
    ``_extract_excerpt`` inserts bracketed role markers (plus an optional
    ``...`` sentinel), so parse those markers rather than splitting on blank
    lines that may legitimately occur inside a message.
    """
    if not excerpt:
        return ""

    marker = re.compile(
        r"(?m)^(?:\[(USER(?: \d+| LATE)|ASSISTANT END)\]|\.\.\.)\n?"
    )
    matches = list(marker.finditer(excerpt))
    user_msgs: list[str] = []
    for idx, match in enumerate(matches):
        role = match.group(1)
        if not role or not role.startswith("USER"):
            continue
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(excerpt)
        text = " ".join(excerpt[match.end():end].split())
        if text:
            user_msgs.append(text)

    # Be defensive for direct callers that pass an unmarked string.
    if not user_msgs:
        return " ".join(excerpt.split())[:120]
    if len(user_msgs) == 1:
        return user_msgs[0][:120]
    return f"{user_msgs[0][:60]} → {user_msgs[-1][:60]}"


def _needs_llm(existing: SessionSummary | None, force: bool = False) -> bool:
    """Whether a session should be (re)sent to the configured narrator.

    - missing            → yes (never summarized)
    - source == skipped  → no  (no usable content; retrying only wastes calls)
    - source == fallback → yes (Layer 0: upgrade the instant fallback to auto)
    - source == auto     → only if `force`, or its prompt_version is behind
                           PROMPT_VERSION (Layer 1: refresh when the prompt —
                           or the model it's tuned for — has improved)
    """
    if existing is None:
        return True
    if existing.source == "skipped":
        return False
    if existing.source == "fallback":
        return True
    if force:  # source == "auto"
        return True
    return (
        (existing.prompt_version or 0) < PROMPT_VERSION
        or existing.narrator_fingerprint != narrative_config_fingerprint()
    )


def summarize_session(
    session_id: str,
    jsonl_path: Path,
    use_llm: bool = False,
    force: bool = False,
    provider=None,
) -> SessionSummary | None:
    """Idempotent: returns the cached entry unless it needs (re)generation.

    Default (`use_llm=False`) never calls the LLM — it returns any cached
    entry untouched, else writes an instant first/last-user-message fallback
    (`source=fallback`).

    With `use_llm=True` the cache becomes *upgradable*: a `fallback` row is
    promoted to `auto`, and a stale `auto` row (older `prompt_version`, or
    any when `force=True`) is regenerated. An up-to-date `auto` row is
    returned as-is so we never re-burn `claude -p`. Regeneration is
    non-destructive — if `claude -p` fails, the existing summary is kept
    rather than downgraded to a fallback.
    """
    existing = get(session_id)
    if not use_llm:
        if existing:
            return existing
    elif existing and not _needs_llm(existing, force):
        return existing

    project, excerpt = _extract_excerpt(jsonl_path, provider=provider)
    if not excerpt:
        # Nothing usable to summarize now — don't clobber a prior summary.
        if existing and existing.source in ("auto", "fallback"):
            return existing
        upsert(session_id, "(no meaningful conversation)", "skipped", project=project)
        return get(session_id)
    if use_llm:
        call = summarize_via_claude_p(excerpt)
        # Compatibility with callers that monkeypatch the old helper to a
        # plain string; production always returns NarrativeCall.
        if isinstance(call, str):
            call = NarrativeCall(call, "claude", "sonnet")
        if call:
            upsert(
                session_id, call.stdout, "auto", project=project,
                prompt_version=PROMPT_VERSION,
                narrator_provider=call.provider,
                narrator_model=call.model,
                narrator_fingerprint=narrative_config_fingerprint(),
            )
            return get(session_id)
        # claude -p failed: keep a good existing summary instead of
        # downgrading it to a fallback on a transient failure.
        if existing and existing.source == "auto":
            return existing
    upsert(session_id, _fallback_narrative(excerpt), "fallback", project=project)
    return get(session_id)


_OVERALL_PROMPT = """{language_directive}

Below is a breakdown of every AI coding-agent session in a single time window, grouped by category. Each line under a category is one session's one-line summary.

Reframe the period around the user's GOAL THREADS, NOT a category-by-category log. Merge the categories into 2-4 mission threads reflecting what the user is building toward (fold tool-building categories into one "build" thread; keep investing research its own thread). Lead with the thread that took the most time.

For EACH thread write a block with TWO parts:
1) A SHORT bold header (ONE line): the CONCRETE thing achieved this period AND its payoff (what it now enables). Specific enough to identify THIS week alone, NEVER a generic progress arc reusable for any week. BANNED header styles (generic arcs reusable for ANY week): "laid the foundation", "now live", "from firefighting to foundation", "single-fix to platform", "X 從單點修復邁向地基層", "走向平台化", "全面收斂", "全面補強", "紀律收斂", "可信度全面補強", "維運全面收斂". Abstract progress-verbs used AS the payoff — 「收斂」「全面補強」「全面強化」「補強」「(全面)提升」— are BANNED: the payoff must be a concrete capability or result you can point to, never a process word. If a real version shipped (e.g. "ccstory v0.4.2"), name it. NO issue/PR numbers.
2) Then 1-3 bullet points (each line starts with "- "): one concrete thing done, phrased as an outcome, not a pile of technical nouns. Use the minimum that covers the thread — most threads need 1-2 bullets; write a 3rd only when there's a genuinely distinct third outcome, never by splitting one outcome into parts to hit the cap.

Concrete header examples (mirror this specificity in the response language):
- Good: "fomo-kernel 現金流與多幣別帳本落地，portfolio 首次能算真實成本基礎"
- Good: "ccstory v0.4.2 發版，週報改吃 record-claude 敘事管線"
- Bad (abstract, reusable any week): "投資決策引擎從單點修復邁向地基層落地"
- Bad (abstract progress-verb as payoff): "個人系統維運全面收斂，工具鏈可信度全面補強"

Rules:
- 2-4 threads total. Header = the key concrete win + its payoff, specific to THIS week. Bullets carry the supporting detail.
- 1-3 bullets per thread — don't pad to 3 by default; a thread with one clear outcome gets one bullet. NO issue/PR/commit numbers anywhere (no "#162", no "closes #31"); those live in a separate table. Name qualitative outcomes.
- Ground every claim in the summaries. Never invent a version or outcome. If a thread has no clear "before", just state the concrete win and its payoff; do not fabricate a before/after.

Period: {period}
Categories (hours): {category_summary}

Session breakdown by category:
{breakdown}

Output only the thread blocks (bold header + bullet lines) — no preamble, no quotes, no fences."""

OVERALL_KEY = "__overall__"


def synthesize_overall_for_period(
    period_key: str,
    category_hours: list[tuple[str, float]],
    sessions_by_category: dict[str, list[tuple[str, str]]],
    force_refresh: bool = False,
    timeout: int = 90,
    budget: NarrativeBudget | None = None,
) -> str | None:
    """Synthesize the overall goal-thread narrative for the whole period.

    `category_hours` is `[(category, hours), ...]` already sorted from
    largest to smallest. `sessions_by_category` is `{category:
    [(session_id, summary), ...]}` — only sessions with a real summary
    should be passed in.

    Cache key: (period_key, "__overall__"). Reuses the period_aggregates
    table; invalidates when the union of session ids differs.
    Returns None on LLM failure or empty input.
    """
    all_ids: list[str] = sorted(
        sid for items in sessions_by_category.values() for sid, _ in items
    )
    if not all_ids:
        return None

    cat_hours_line = ", ".join(
        f"{cat} {hrs:.1f}h" for cat, hrs in category_hours if hrs > 0
    ) or "(none)"
    breakdown_blocks: list[str] = []
    for cat, _ in category_hours:
        items = sessions_by_category.get(cat) or []
        if not items:
            continue
        bullets = "\n".join(f"  - {summary}" for _, summary in items)
        breakdown_blocks.append(f"{cat}:\n{bullets}")
    breakdown = "\n\n".join(breakdown_blocks)[:6000]
    prompt = _OVERALL_PROMPT.format(
        language_directive=language_directive(),
        period=period_key,
        category_summary=cat_hours_line,
        breakdown=breakdown,
    )
    # The fingerprint hashes a variant with hours coarsened to whole hours
    # (#121): the primary flow runs ccstory from inside a live Claude Code
    # session, so the active window's hours creep between any two reruns —
    # at 0.1h precision the overall cache effectively never hit for the
    # current week/month, re-burning a ~90s claude -p call per rerun. Sub-
    # hour drift now stays a hit; the prompt the LLM sees keeps 0.1h.
    fp_hours_line = ", ".join(
        f"{cat} {int(round(hrs))}h" for cat, hrs in category_hours if hrs > 0
    ) or "(none)"
    input_fingerprint = _cache_fingerprint(
        "period-overall",
        _OVERALL_PROMPT.format(
            language_directive=language_directive(),
            period=period_key,
            category_summary=fp_hours_line,
            breakdown=breakdown,
        ),
        narrative_config_fingerprint(),
    )

    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT summary, session_ids, input_fingerprint "
            "FROM period_aggregates "
            "WHERE period_key = ? AND category = ?",
            (period_key, OVERALL_KEY),
        ).fetchone()
        cached_ids = cur[1].split(",") if cur else []
        if (
            cur
            and not force_refresh
            and cached_ids == all_ids
            and cur[2] == input_fingerprint
        ):
            return cur[0]
    finally:
        conn.close()

    if not llm_available():
        return None

    call = _run_budgeted_llm_p(prompt, timeout, budget, lane="overall")
    if call is None:
        return None
    narrative = call.stdout.strip().strip('"').strip("'")
    if len(narrative) < 10:
        return None

    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO period_aggregates
               (period_key, category, summary, session_ids, created_at,
                input_fingerprint, narrator_provider, narrator_model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (period_key, OVERALL_KEY, narrative,
             ",".join(all_ids), time.time(), input_fingerprint,
             call.provider, call.model),
        )
        conn.commit()
    finally:
        conn.close()
    return narrative


_CATEGORY_PROMPT = """{language_directive}

Below are one-line summaries of every AI coding-agent session in the "{category}" category for one time window ({count} sessions).

Write a short synthesis of what the user did in THIS category over the period, in two parts:
1) ONE header line (max 20 words): the main thread, phrased as a narrative hook — not a flat category label.
2) Then 2-4 bullet points (each line starts with "- ", max 20 words each): concrete outcomes or decisions, in order of importance. Include incidents and security issues even when they are not the dominant theme.

Style:
- Concrete nouns (tickers, file names, tools) beat generic verbs.
- Synthesize; do NOT enumerate every session.
- No markdown bold, no fences, no quotes on the header line.
- One blank line between the header and the bullets; no blank lines between bullets.

Sessions:
{bullets}

Output the header line + bullets only — no preamble, no prefix, no fences."""


def synthesize_category_for_period(
    period_key: str,
    category: str,
    session_ids: list[str],
    summaries: list[str],
    force_refresh: bool = False,
    timeout: int = 90,
    budget: NarrativeBudget | None = None,
) -> str | None:
    """Synthesize a header + 2-4 bullets narrative for ONE category in a period (#57).

    Cache key: (period_key, category) in the same period_aggregates table
    the overall narrative uses — OVERALL_KEY ("__overall__") is reserved,
    so a user bucket by that name is skipped rather than colliding.
    Invalidates when the category's session-id set differs (sessions
    unchanged → no recompute; same dedup contract as the overall row).
    Returns None on LLM failure or empty input — never blocks the report.
    """
    if not session_ids or not summaries or category == OVERALL_KEY:
        return None
    ids_sorted = sorted(session_ids)

    bullets = "\n".join(f"- {s}" for s in summaries)[:6000]
    prompt = _CATEGORY_PROMPT.format(
        language_directive=language_directive(),
        category=category,
        count=len(summaries),
        bullets=bullets,
    )
    input_fingerprint = _cache_fingerprint(
        "period-category", prompt, narrative_config_fingerprint(),
    )

    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT summary, session_ids, input_fingerprint "
            "FROM period_aggregates "
            "WHERE period_key = ? AND category = ?",
            (period_key, category),
        ).fetchone()
        cached_ids = cur[1].split(",") if cur else []
        if (
            cur
            and not force_refresh
            and cached_ids == ids_sorted
            and cur[2] == input_fingerprint
        ):
            return cur[0]
    finally:
        conn.close()

    if not llm_available():
        return None

    call = _run_budgeted_llm_p(prompt, timeout, budget, lane="category")
    if call is None:
        return None
    narrative = call.stdout.strip().strip('"').strip("'")
    if len(narrative) < 10:
        return None

    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO period_aggregates
               (period_key, category, summary, session_ids, created_at,
                input_fingerprint, narrator_provider, narrator_model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (period_key, category, narrative,
             ",".join(ids_sorted), time.time(), input_fingerprint,
             call.provider, call.model),
        )
        conn.commit()
    finally:
        conn.close()
    return narrative


def get_overall_narrative(period_key: str) -> str | None:
    """Return cached overall narrative for a period, if any."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT summary FROM period_aggregates "
            "WHERE period_key = ? AND category = ?",
            (period_key, OVERALL_KEY),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_period_narrative_provenance(
    period_key: str, category: str = OVERALL_KEY,
) -> dict[str, str] | None:
    """Return recorded provider/model for one cached period narrative."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT narrator_provider, narrator_model FROM period_aggregates "
            "WHERE period_key = ? AND category = ?",
            (period_key, category),
        ).fetchone()
        if not row or not row[0] or not row[1]:
            return None
        return {"provider": row[0], "model": row[1]}
    finally:
        conn.close()


def get_comparison_narrative_provenance(
    current_key: str, previous_key: str,
) -> dict[str, str] | None:
    """Return recorded provider/model for one cached comparison narrative."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT narrator_provider, narrator_model FROM comparison_narratives "
            "WHERE current_key = ? AND previous_key = ?",
            (current_key, previous_key),
        ).fetchone()
        if not row or not row[0] or not row[1]:
            return None
        return {"provider": row[0], "model": row[1]}
    finally:
        conn.close()


def invalidate_period_aggregates(period_key: str | None = None) -> int:
    """Wipe per-period aggregate narratives so they regenerate next run.

    Pass `period_key` to clear one window only; `None` clears all. Returns
    the number of rows deleted. Callers: rule changes (any bucket assignment
    shift invalidates the prior aggregate prose), `--refresh`.
    """
    conn = _connect()
    try:
        if period_key is None:
            cur = conn.execute("DELETE FROM period_aggregates")
        else:
            cur = conn.execute(
                "DELETE FROM period_aggregates WHERE period_key = ?",
                (period_key,),
            )
        deleted = cur.rowcount or 0
        conn.commit()
        return deleted
    finally:
        conn.close()


def invalidate_content_buckets(session_ids: list[str] | None = None) -> int:
    """Wipe content-classification cache so sessions get re-classified.

    Pass a session_id list to scope the invalidation; `None` clears all rows.
    Returns the number of rows deleted. Callers: `--refresh` (scoped to the
    sessions in the current window) and `--refresh-all`.
    """
    conn = _connect()
    try:
        if session_ids is None:
            cur = conn.execute("DELETE FROM session_content_buckets")
        elif not session_ids:
            return 0
        else:
            placeholders = ",".join("?" for _ in session_ids)
            cur = conn.execute(
                f"DELETE FROM session_content_buckets "
                f"WHERE session_id IN ({placeholders})",
                session_ids,
            )
        deleted = cur.rowcount or 0
        conn.commit()
        return deleted
    finally:
        conn.close()


def invalidate_comparison_narratives() -> int:
    """Wipe all cross-period comparison narratives. Any cached prose was
    written against the old bucket assignment, so a rule change makes it
    stale even if the underlying session ids didn't move.
    """
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM comparison_narratives")
        deleted = cur.rowcount or 0
        conn.commit()
        return deleted
    finally:
        conn.close()


_COMPARISON_PROMPT = """{language_directive}

Below are session one-line summaries from two consecutive time windows for one user's AI coding-agent work, plus the per-bucket time deltas.

Write ONE OR TWO sentences (max 50 words) describing how the user's focus SHIFTED between the previous window and the current one. Focus on:
- What dominated each window
- The biggest shift (which bucket grew or shrank most, and what replaced it)
- Concrete content where possible (don't just say "more coding"; say what kind of coding)

Good example (English style; mirror this in whichever language you respond in):
- Investment work dropped 40% as Cambricon coverage wrapped up; ccstory plugin packaging took its place, explaining the coding swing.

Avoid:
- Listing numbers (the table above already has them)
- "More X, less Y" without context
- More than 2 sentences

PER-BUCKET TIME DELTA (current − previous, in hours):
{deltas}

PREVIOUS window ({previous_label}):
{previous_summaries}

CURRENT window ({current_label}):
{current_summaries}

Output the synthesized prose only — no quotes, no prefix, no fences."""


def _comparison_signature(
    current_summaries: list[tuple[str, str]],
    previous_summaries: list[tuple[str, str]],
    deltas: list[tuple[str, float, float]] | None = None,
) -> str:
    """Stable hash of both windows' (id, summary) pairs and the delta block.

    Including the summary content prevents the cache from returning a stale
    narrative when a session's summary is refreshed (e.g. via --force-refresh
    on session_summarizer) without its id changing.
    """
    cur = sorted(current_summaries)
    prev = sorted(previous_summaries)
    delta_part = sorted(deltas or [])
    h = hashlib.sha256()
    h.update(repr(cur).encode())
    h.update(b"|")
    h.update(repr(prev).encode())
    h.update(b"|")
    h.update(repr(delta_part).encode())
    return h.hexdigest()[:16]


def synthesize_comparison(
    current_key: str,
    previous_key: str,
    current_summaries: list[tuple[str, str]],
    previous_summaries: list[tuple[str, str]],
    deltas: list[tuple[str, float, float]] | None = None,
    force_refresh: bool = False,
    timeout: int = 90,
    budget: NarrativeBudget | None = None,
) -> str | None:
    """Cross-period prose synthesis. ~50-word delta narrative.

    `current_summaries` / `previous_summaries` are `[(session_id, summary), ...]`
    lists. `deltas` is an optional `[(category, current_min, previous_min), ...]`
    list passed into the prompt so the model can ground the "biggest shift"
    claim on real numbers rather than inferring from summary text.

    The cache key is `(current_key, previous_key)`; the cached row is
    invalidated when the signature (hash of both id+summary sets and the
    delta block) changes — so adding new sessions, refreshing summaries,
    or shifting bucket allocations all trigger regeneration.

    Returns the narrative string, or None on claude -p failure / absent
    summaries.
    """
    if not current_summaries or not previous_summaries:
        return None

    def _fmt(items: list[tuple[str, str]]) -> str:
        return "\n".join(f"- {s}" for _, s in items)

    def _fmt_deltas(items: list[tuple[str, float, float]] | None) -> str:
        if not items:
            return "(no per-bucket breakdown provided)"
        lines = []
        for cat, cur_min, prev_min in items:
            delta_h = (cur_min - prev_min) / 60.0
            lines.append(
                f"- {cat}: {prev_min/60:.1f}h → {cur_min/60:.1f}h "
                f"({delta_h:+.1f}h)"
            )
        return "\n".join(lines)

    prompt = _COMPARISON_PROMPT.format(
        language_directive=language_directive(),
        previous_label=previous_key,
        current_label=current_key,
        deltas=_fmt_deltas(deltas),
        previous_summaries=_fmt(previous_summaries)[:3000],
        current_summaries=_fmt(current_summaries)[:3000],
    )
    sig = _comparison_signature(current_summaries, previous_summaries, deltas)
    input_fingerprint = _cache_fingerprint(
        "period-comparison", prompt, narrative_config_fingerprint(),
    )

    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT narrative, signature, input_fingerprint "
            "FROM comparison_narratives "
            "WHERE current_key = ? AND previous_key = ?",
            (current_key, previous_key),
        ).fetchone()
        if (
            cur
            and not force_refresh
            and cur[1] == sig
            and cur[2] == input_fingerprint
        ):
            return cur[0]
    finally:
        conn.close()

    if not llm_available():
        return None

    call = _run_budgeted_llm_p(prompt, timeout, budget, lane="comparison")
    if call is None:
        return None
    narrative = call.stdout.strip().strip('"').strip("'")
    if len(narrative) < 10:
        return None

    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO comparison_narratives
               (current_key, previous_key, signature, narrative, created_at,
                input_fingerprint, narrator_provider, narrator_model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (current_key, previous_key, sig, narrative, time.time(),
             input_fingerprint, call.provider, call.model),
        )
        conn.commit()
    finally:
        conn.close()
    return narrative


_CONTENT_CLASSIFY_PROMPT = """You are categorizing AI coding-agent sessions by their CONTENT (not just folder name).

Preferred bucket vocabulary (from this user's config — prefer these names so the report stays aligned with the user's mental model):
{category_context}

Buckets already assigned earlier in this run or recovered from the compatible cache:
{already_used_buckets}

Pick from the preferred vocabulary when a session reasonably fits. Only introduce a NEW bucket name if multiple sessions clearly share a theme that none of the preferred buckets cover. Keep spelling exactly consistent across rows. Keep the total bucket vocabulary small (≤ 6).

For each session below, output ONE JSON object per line, with keys:
- session_id (the EXACT id string shown)
- bucket (a category name)

Sessions:
{rows}

Output ONLY the JSON lines, no prose, no fences, one object per line."""


# Default vocabulary handed to the LLM when the user has no `[categories]`.
# Mirrors the 4 named defaults documented in `init_categories._INIT_PROMPT`
# so the init path and content path advertise the same source-of-truth.
_DEFAULT_VOCAB_BLOCK = (
    "- coding: software projects (apps, CLIs, libraries, infra, dashboards)\n"
    "- investment: stock / portfolio / trading / equity research\n"
    "- writing: blogs, newsletters, posts, drafts, essays, docs\n"
    "- other: scratch, sandbox, experiments, anything that doesn't fit above"
)
_DEFAULT_BUCKET_NAMES = frozenset({"coding", "investment", "writing", "other"})
MAX_CONTENT_BUCKETS = 6
# Maximum number of new proposed content bucket names accepted per run.
# Solves issue #120 leak 2: when a user already has >= 6 configured categories,
# per-run headroom still allows inventing new names with sufficient evidence.
# MAX_CONTENT_BUCKETS remains the floor shared by all users; headroom is growth
# space added above it.
NEW_BUCKET_HEADROOM = 2
CONTENT_CLASSIFIER_POLICY_VERSION = 4


def _normalize_bucket_name(raw: object) -> str | None:
    """Normalize a model-proposed bucket while rejecting unsafe cache values."""
    if not isinstance(raw, str):
        return None
    normalized = " ".join(raw.strip().lower().split())
    if not normalized or len(normalized) > 60:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        return None
    return normalized


def _build_category_vocabulary() -> tuple[str, set[str]]:
    """Return the rendered prompt context and normalized preferred names."""
    # Local import avoids a module-load dependency and keeps monkeypatching
    # CONFIG_PATH effective in tests.
    from .categorizer import list_user_categories
    cats = list_user_categories()
    if not cats:
        return _DEFAULT_VOCAB_BLOCK, set(_DEFAULT_BUCKET_NAMES)
    lines: list[str] = []
    names: set[str] = set()
    for bucket in sorted(cats):
        kws = cats[bucket]
        normalized = _normalize_bucket_name(bucket)
        if not kws or normalized is None:
            continue
        lines.append(f"- {bucket}: project leaves {', '.join(kws)}")
        names.add(normalized)
    if not lines:
        return _DEFAULT_VOCAB_BLOCK, set(_DEFAULT_BUCKET_NAMES)
    return "\n".join(lines), names


def _build_category_context() -> str:
    """Render the user's `[categories]` (or the default 4-bucket vocab) as a
    prompt block for `_CONTENT_CLASSIFY_PROMPT`.

    Read at format-time so config edits take effect on the next run without
    a process restart. Falls back to `_DEFAULT_VOCAB_BLOCK` when the user
    has no `[categories]` table — same vocabulary `init` advertises, so the
    two LLM paths never publish parallel bucket names (issue #62).
    """
    return _build_category_vocabulary()[0]


def _content_classification_fingerprint(category_context: str | None = None) -> str:
    """Fingerprint prompt policy plus the user's effective category config."""
    context = (
        category_context
        if category_context is not None
        else _build_category_context()
    )
    return _cache_fingerprint(
        "content-classification",
        str(CONTENT_CLASSIFIER_POLICY_VERSION),
        str(MAX_CONTENT_BUCKETS),
        _CONTENT_CLASSIFY_PROMPT,
        context,
        narrative_config_fingerprint(),
    )


def _classify_cache_get_many(
    session_ids: list[str],
    input_fingerprint: str | None = None,
) -> dict[str, str]:
    if not session_ids:
        return {}
    fingerprint = input_fingerprint or _content_classification_fingerprint()
    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in session_ids)
        rows = conn.execute(
            f"SELECT session_id, bucket FROM session_content_buckets "
            f"WHERE session_id IN ({placeholders}) "
            f"AND input_fingerprint = ?",
            [*session_ids, fingerprint],
        ).fetchall()
        return {sid: bucket for sid, bucket in rows}
    finally:
        conn.close()


def _classify_cache_upsert_many(
    items: dict[str, str],
    input_fingerprint: str | None = None,
) -> None:
    if not items:
        return
    fingerprint = input_fingerprint or _content_classification_fingerprint()
    conn = _connect()
    try:
        now = time.time()
        conn.executemany(
            """INSERT OR REPLACE INTO session_content_buckets
               (session_id, bucket, created_at, input_fingerprint)
               VALUES (?, ?, ?, ?)""",
            [(sid, b, now, fingerprint) for sid, b in items.items()],
        )
        conn.commit()
    finally:
        conn.close()


def _parse_classification_lines(text: str) -> dict[str, str]:
    """Parse claude's JSON-lines output. Tolerate fences, blank lines."""
    out: dict[str, str] = {}
    cleaned = text.strip()
    for fence in ("```json", "```JSON", "```"):
        cleaned = cleaned.replace(fence, "")
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = obj.get("session_id")
        bucket = _normalize_bucket_name(obj.get("bucket"))
        if isinstance(sid, str) and bucket is not None:
            out[sid] = bucket
    return out


def _validated_chunk_buckets(
    parsed: dict[str, str],
    chunk_ids: list[str],
    accepted_buckets: set[str],
    bucket_limit: int,
    proposed_bucket_counts: dict[str, int] | None = None,
) -> dict[str, str]:
    """Accept only requested rows and enforce the run-wide vocabulary cap."""
    requested = set(chunk_ids)
    if proposed_bucket_counts is None:
        proposed_bucket_counts = {}

    for sid, bucket in parsed.items():
        if sid in requested and bucket not in accepted_buckets:
            proposed_bucket_counts[bucket] = proposed_bucket_counts.get(bucket, 0) + 1

    fresh: dict[str, str] = {}
    for sid in chunk_ids:
        bucket = parsed.get(sid)
        if bucket is None:
            continue
        if bucket not in accepted_buckets:
            # Issue #120 Leak 3: proposed_bucket_counts is accumulated across
            # chunks by the caller (classify_sessions_by_content). A bucket
            # proposed across multiple chunks eventually reaches threshold 2
            # in later chunks. Note: we deliberately do not perform a second
            # pass to retroactively rescue rows dropped in earlier chunks.
            if proposed_bucket_counts.get(bucket, 0) < 2:
                LOG.warning(
                    "dropping one-off content bucket %r for %s", bucket, sid,
                )
                continue
            if len(accepted_buckets) >= bucket_limit:
                LOG.warning(
                    "dropping content bucket %r for %s: vocabulary limit %d",
                    bucket, sid, bucket_limit,
                )
                continue
            accepted_buckets.add(bucket)
        fresh[sid] = bucket
    return fresh


def classify_sessions_by_content(
    items: list[tuple[str, str, str]],
    force_refresh: bool = False,
    timeout: int = 120,
    batch_size: int = 80,
    budget: NarrativeBudget | None = None,
    on_chunk_complete: Callable[[int, int], None] | None = None,
) -> dict[str, str]:
    """Batch-classify sessions by content.

    `items` is `[(session_id, project_leaf, summary), ...]`. Returns
    `{session_id: bucket}` for everything successfully classified (cached
    or freshly inferred). Sessions absent from the result fell through
    every path (cache miss + claude unavailable / parse failure).

    Caches each successful classification in `session_content_buckets`
    so subsequent runs hit zero claude -p calls for already-seen sessions.

    Sends pending sessions to `claude -p` in chunks of `batch_size` so a
    period with more than `batch_size` uncached sessions does not silently
    leave the tail folder-classified.

    ``on_chunk_complete`` (optional) is called after each LLM chunk
    finishes (success or failure) as ``cb(chunks_done, total_chunks)``.
    Callers wire this into a ``console.status`` so the user sees
    something like ``Running batch LLM… (2/3)`` for 200-session windows
    instead of an opaque spinner (issue #75).
    """
    if not items:
        return {}

    sids = [sid for sid, _, _ in items]
    category_context, preferred_buckets = _build_category_vocabulary()
    input_fingerprint = _content_classification_fingerprint(category_context)
    cached = (
        _classify_cache_get_many(sids, input_fingerprint)
        if not force_refresh
        else {}
    )

    pending = [(sid, leaf, summ) for sid, leaf, summ in items if sid not in cached]
    if not pending or not llm_available():
        return cached

    combined = dict(cached)
    total_chunks = (len(pending) + batch_size - 1) // batch_size
    # The preferred/configured names count toward the global cap even before
    # the model uses them. Compatible cached values seed the carry-forward so
    # a partial-cache run keeps the same vocabulary as its earlier sessions.
    accepted_buckets = set(preferred_buckets) | set(cached.values())
    used_buckets = set(cached.values())
    bucket_limit = max(MAX_CONTENT_BUCKETS, len(accepted_buckets) + NEW_BUCKET_HEADROOM)
    proposed_bucket_counts: dict[str, int] = {}
    # Local import, same reason as _build_category_vocabulary's: avoids a
    # module-load cycle and keeps CONFIG_PATH monkeypatching effective.
    from .categorizer import load_settings
    drop_fallback = load_settings().get("default_bucket", "coding")

    for chunk_idx, chunk_start in enumerate(
        range(0, len(pending), batch_size), start=1,
    ):
        if budget is not None and budget.remaining_sec() <= 0:
            budget.stopped_reason = "budget_exhausted"
            budget.record_batch(
                "content_classification", chunk_idx, total_chunks, 0,
                outcome="budget_exhausted",
            )
            break
        chunk = pending[chunk_start:chunk_start + batch_size]
        rows = "\n".join(
            f"[{sid}] folder: {leaf} | summary: {summ[:200]}"
            for sid, leaf, summ in chunk
        )
        prompt = _CONTENT_CLASSIFY_PROMPT.format(
            rows=rows,
            category_context=category_context,
            already_used_buckets=(
                ", ".join(sorted(used_buckets)) if used_buckets else "(none yet)"
            ),
        )
        fresh: dict[str, str] = {}
        try:
            call = _run_budgeted_llm_p(
                prompt, timeout, budget, lane="content_classification",
            )
            if call is not None:
                parsed = _parse_classification_lines(call.stdout)
                # Validate ids and vocabulary before anything reaches SQLite.
                chunk_ids = [sid for sid, _, _ in chunk]
                fresh = _validated_chunk_buckets(
                    parsed, chunk_ids, accepted_buckets, bucket_limit, proposed_bucket_counts,
                )
                # Negative-cache validation drops (#120): these sids DID get
                # an answer from the model, but the bucket was rejected
                # (one-off name, or the vocabulary cap). Without a cache row
                # they re-enter `pending` and re-burn a claude -p chunk on
                # every future run, forever. Caching them at the fallback
                # bucket bounds the cost; the row carries the current
                # input_fingerprint, so any config/vocab change rotates the
                # fingerprint and re-opens their shot at a real bucket.
                # Model omissions and parse failures stay uncached on
                # purpose — those are transient, and retrying them is
                # correct.
                dropped = [
                    sid for sid in chunk_ids
                    if parsed.get(sid) is not None and sid not in fresh
                ]
                for sid in dropped:
                    fresh[sid] = drop_fallback
            else:
                LOG.warning(
                    "content-classify failed (chunk %d-%d): no backend returned prose",
                    chunk_start, chunk_start + len(chunk),
                )
        except (subprocess.SubprocessError, OSError) as e:
            LOG.warning("content-classify errored: %s", e)

        # Upsert + accumulate (no-op on empty fresh); always fire the
        # progress callback so a failed chunk still advances the counter.
        _classify_cache_upsert_many(fresh, input_fingerprint)
        combined.update(fresh)
        used_buckets.update(fresh.values())
        if budget is not None:
            budget.record_batch(
                "content_classification", chunk_idx, total_chunks, len(chunk),
            )
        if on_chunk_complete is not None:
            on_chunk_complete(chunk_idx, total_chunks)

    return combined


def backfill_for_sessions(
    sessions: list,
    on_progress=None,
    use_llm: bool = False,
    force: bool = False,
    batch_size: int = SUMMARY_BATCH_SIZE,
    budget: NarrativeBudget | None = None,
    on_chunk_complete: Callable[[int, int], None] | None = None,
) -> dict:
    """Summarize sessions that are missing or, under `use_llm`, stale.

    `sessions` is a list of objects with `.session_id` and `.project` attrs.
    `use_llm=False` (default) only summarizes never-seen sessions with the
    instant first/last-user-message fallback. `use_llm=True` additionally upgrades
    `fallback` rows to `auto` and regenerates stale `auto` rows (older
    prompt_version, or every in-window `auto` when `force=True`) in bounded
    batch narrator calls. ``on_chunk_complete`` receives completed/total LLM
    batches; ``on_progress`` remains a per-session completion callback.
    Returns {"summarized": N, "fallback": F, "skipped": M, "already": K,
             "regenerated": R}.
    """
    by_id = {s.session_id: s for s in sessions if getattr(s, "session_id", None)}
    ids = list(by_id.keys())
    existing = get_many(ids)
    if use_llm:
        todo = [sid for sid in ids if _needs_llm(existing.get(sid), force)]
    else:
        todo = [sid for sid in ids if existing.get(sid) is None]
    regenerated = sum(1 for sid in todo if existing.get(sid) is not None)
    summarized = fallback = skipped = 0
    from .providers import TranscriptResolver

    resolver = TranscriptResolver()
    prepared: list[tuple[str, str, str]] = []
    projects: dict[str, str] = {}

    for sid in todo:
        sess = by_id[sid]
        jsonl_path = resolver.path_for(sess)
        if jsonl_path is None:
            # Only record a "not found" skip when nothing is cached;
            # otherwise keep the existing summary rather than clobber it.
            if existing.get(sid) is None:
                upsert(sid, "(jsonl not found)", "skipped", project=sess.project)
            skipped += 1
            continue
        if not use_llm:
            result = summarize_session(
                sid, jsonl_path, use_llm=False, force=force,
                provider=resolver.provider_for(sess),
            )
            if result and result.source == "fallback":
                fallback += 1
            else:
                skipped += 1
            continue

        project, excerpt = _extract_excerpt(
            jsonl_path, provider=resolver.provider_for(sess),
        )
        if not excerpt:
            # Retain a useful existing auto/fallback result rather than
            # replacing it with an empty-excerpt marker.
            if existing.get(sid) is None:
                upsert(sid, "(no meaningful conversation)", "skipped", project=project)
            skipped += 1
            continue
        projects[sid] = project
        prepared.append((sid, project, excerpt))

    generated = (
        summarize_sessions_via_llm_batch(
            prepared, batch_size=batch_size, budget=budget,
            on_chunk_complete=on_chunk_complete,
        )
        if use_llm else {}
    )
    for sid, _project, excerpt in prepared:
        call = generated.get(sid)
        if call is not None:
            upsert(
                sid, call.stdout, "auto", project=projects[sid],
                prompt_version=PROMPT_VERSION,
                narrator_provider=call.provider,
                narrator_model=call.model,
                narrator_fingerprint=narrative_config_fingerprint(),
            )
            summarized += 1
            continue
        # Exactly preserve the old non-destructive contract: a failed refresh
        # retains an existing auto summary; only a missing/fallback row falls
        # back to the extractive narrative.
        if existing.get(sid) and existing[sid].source == "auto":
            continue
        upsert(sid, _fallback_narrative(excerpt), "fallback", project=projects[sid])
        fallback += 1

    if on_progress:
        results = get_many(todo)
        for i, sid in enumerate(todo, start=1):
            result = results.get(sid)
            on_progress(i, len(todo), sid, result.source if result else "fail")
    return {
        "summarized": summarized,
        "fallback": fallback,
        "skipped": skipped,
        "already": len(ids) - len(todo),
        "regenerated": regenerated,
    }

