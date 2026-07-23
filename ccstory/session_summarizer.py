"""Session-level summaries cached to SQLite, generated via local `claude -p`.

This is the differentiator: ccusage gives numbers, ccstory gives a one-line
narrative per session. We invoke the user's *local* Claude Code CLI through
subprocess — no API key, no cost to us, no privacy concerns.

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
from dataclasses import dataclass
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

N_USER_HEAD = 3
N_USER_TAIL = 2
N_ASSISTANT_TAIL = 1

# Bump whenever the per-session narrative prompt (_PROMPT_TEMPLATE) is changed
# materially, or when retuning it for a newer/better default `claude` model.
# Cached "auto" summaries carrying a lower prompt_version are treated as stale
# and regenerated on the next `--llm-narrative` run. Keep this an int so the
# comparison `stored < PROMPT_VERSION` is monotonic.
PROMPT_VERSION = 1
CACHE_SCHEMA_VERSION = 3


_CLAUDE_MD_MAX_CHARS = 500


def _build_language_line(language: str) -> str:
    """Render the single-line `Respond in <X>.` directive used for explicit
    language picks (CLI flag, env, ccstory config, settings.json, locale)."""
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


_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migration_1_baseline,
    _migration_2_cache_fingerprints,
    _migration_3_adopt_legacy_classifications,
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


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        # PRAGMA schema_version forces SQLite to parse the file early, so a
        # corrupt cache gets the existing actionable recovery hint.
        conn.execute("PRAGMA schema_version").fetchone()
        _run_migrations(conn)
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
) -> None:
    if not session_id or not summary:
        return
    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO session_summaries
               (session_id, summary, source, project, created_at, prompt_version)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, summary.strip(), source, project, time.time(),
             prompt_version),
        )
        conn.commit()
    finally:
        conn.close()


def get(session_id: str) -> SessionSummary | None:
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT session_id, summary, source, project, created_at,
                      prompt_version
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
                       prompt_version
                FROM session_summaries WHERE session_id IN ({placeholders})""",
            session_ids,
        ).fetchall()
        return {r[0]: SessionSummary(*r) for r in rows}
    finally:
        conn.close()


def recent_auto_timestamps(limit: int = 60) -> list[float]:
    """`created_at` of the most recent `auto` summaries, oldest first.

    A backfill writes one row per `claude -p` call, so the gaps between
    consecutive rows are how long the calls actually took on this machine.
    That lets the ETA measure itself instead of trusting a constant (#113).
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


def _claude_record_text(d: dict) -> tuple[str | None, str]:
    """(role, text) for one Claude Code transcript record."""
    role = d.get("type")
    if role not in ("user", "assistant"):
        return None, ""
    msg = d.get("message")
    content = msg.get("content", "") if isinstance(msg, dict) else ""
    if isinstance(content, str):
        return role, content
    if isinstance(content, list):
        return role, "\n".join(
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return role, ""


def _codex_record_text(payload: dict, kind: str | None) -> tuple[str | None, str]:
    """(role, text) for one Codex rollout record.

    User text is read from the ``event_msg`` / ``user_message`` record only:
    the parallel ``response_item`` user records repeat the same turn wrapped in
    everything the harness injected (plugin lists, environment context, skill
    bodies), so preferring them means summarizing the harness, not the work.
    Assistant text is the mirror image — ``response_item`` carries the final
    message, ``event_msg`` / ``agent_message`` merely duplicates it.
    """
    from .providers.codex import strip_task_wrapper

    ptype = payload.get("type")
    if kind == "event_msg" and ptype == "user_message":
        return "user", strip_task_wrapper(_codex_content_text(payload.get("message", "")))
    if (
        kind == "response_item"
        and ptype == "message"
        and payload.get("role") == "assistant"
    ):
        return "assistant", _codex_content_text(payload.get("content", ""))
    return None, ""


def _codex_content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _extract_excerpt(jsonl_path: Path) -> tuple[str, str]:
    """Extract user-facing text excerpt for summarization. Returns (project, excerpt).

    Handles every agent's transcript format (#133): a record carrying a dict
    ``payload`` is Codex, anything else is Claude Code. Format sniffing per
    record rather than per file keeps this working for a caller that only has
    a path — `summarize_session` is reached from cache-repair paths that have
    no `SessionStat` to ask.
    """
    user_msgs: list[str] = []
    assistant_msgs: list[str] = []
    detected_cwd = ""
    try:
        project = jsonl_path.relative_to(PROJECTS_DIR).parts[0]
    except ValueError:
        project = jsonl_path.parent.name

    try:
        with jsonl_path.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = d.get("payload")
                if isinstance(payload, dict):
                    if not detected_cwd and isinstance(payload.get("cwd"), str):
                        detected_cwd = payload["cwd"]
                    role, text = _codex_record_text(payload, d.get("type"))
                else:
                    role, text = _claude_record_text(d)

                if role not in ("user", "assistant"):
                    continue
                text = text.strip()
                if (
                    not text
                    or text.startswith("<scheduled-task")
                    or text.startswith("<system-reminder>")
                    or "tool_use_id" in text
                ):
                    continue
                if role == "user":
                    user_msgs.append(text[:500])
                else:
                    assistant_msgs.append(text[:500])
    except OSError:
        return project, ""

    if detected_cwd:
        # Codex transcripts live in a date tree, not a project folder, so the
        # cache row's project has to come from the recorded cwd — encoded the
        # way the Claude provider names project dirs so both agents' rows land
        # under the same project.
        from .providers.codex import _encode_project_dir, _worktree_origin

        project = _encode_project_dir(_worktree_origin(detected_cwd)) or project

    parts: list[str] = []
    head_set = set(user_msgs[:N_USER_HEAD])
    for i, m in enumerate(user_msgs[:N_USER_HEAD]):
        parts.append(f"[USER {i+1}]\n{m}")
    if len(user_msgs) > N_USER_HEAD + N_USER_TAIL:
        parts.append("...")
    for m in user_msgs[-N_USER_TAIL:]:
        if m not in head_set:
            parts.append(f"[USER LATE]\n{m}")
    for m in assistant_msgs[-N_ASSISTANT_TAIL:]:
        parts.append(f"[ASSISTANT END]\n{m[:300]}")

    return project, "\n\n".join(parts)


_PROMPT_TEMPLATE = """{language_directive}

Below is an excerpt of a Claude Code conversation (first/last user + assistant messages).

Write ONE sentence (max 18 words) summarizing what this session ACTUALLY DID — focus on outcomes, not process.

Good examples (English style; mirror this in whichever language you respond in):
- Refactored auth middleware to extract token validation into shared util
- Investigated TLS handshake failure on staging, traced to expired intermediate cert
- Drafted PR description for the v2 migration epic

Bad examples (don't do this):
- User asked X, Claude answered Y  (process, not outcome)
- A conversation about coding  (too vague)

Excerpt:
{excerpt}

Output only the one-sentence summary, no quotes, no prefix."""


def claude_bin_available() -> bool:
    return shutil.which(CLAUDE_BIN) is not None


_flag_confirmed_broken = False


def run_claude_p(prompt: str, timeout: int) -> subprocess.CompletedProcess:
    """Run `claude -p --output-format text <prompt>`, preferring
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
    base = [CLAUDE_BIN, "-p", "--output-format", "text"]
    if not _flag_confirmed_broken:
        r = subprocess.run(
            [*base, "--no-session-persistence", prompt],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if r.returncode != 0 or r.stdout.strip():
            return r
        _flag_confirmed_broken = True
    return subprocess.run(
        [*base, prompt],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def summarize_via_claude_p(excerpt: str, timeout: int = 60) -> str | None:
    """Call local `claude -p` to summarize. Returns None on failure.

    Uses subprocess so we draw on the user's own Claude Code session/quota.
    No API key, no cost to ccstory.
    """
    if not excerpt.strip():
        return None
    if not claude_bin_available():
        return None
    prompt = _PROMPT_TEMPLATE.format(
        excerpt=excerpt[:8000], language_directive=language_directive(),
    )
    try:
        r = run_claude_p(prompt, timeout)
        if r.returncode != 0:
            LOG.warning("claude -p failed (rc=%s): %s", r.returncode, r.stderr.strip()[:200])
            return None
        out = r.stdout.strip().split("\n", 1)[0].strip().strip('"').strip("'")
        if len(out) < 4 or len(out) > 200:
            return None
        return out
    except (subprocess.SubprocessError, OSError) as e:
        LOG.warning("claude -p errored: %s", e)
        return None


def _fallback_narrative(excerpt: str) -> str:
    """Zero-cost first → last user-message narrative.

    A single-message session keeps the old 120-character fallback.  With
    multiple messages, showing both endpoints gives the reader a cheap hint
    of the session's arc without spending a ``claude -p`` call (#70).
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
    """Whether a session should be (re)sent to `claude -p` under use_llm=True.

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
    return (existing.prompt_version or 0) < PROMPT_VERSION


def summarize_session(
    session_id: str,
    jsonl_path: Path,
    use_llm: bool = False,
    force: bool = False,
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

    project, excerpt = _extract_excerpt(jsonl_path)
    if not excerpt:
        # Nothing usable to summarize now — don't clobber a prior summary.
        if existing and existing.source in ("auto", "fallback"):
            return existing
        upsert(session_id, "(no meaningful conversation)", "skipped", project=project)
        return get(session_id)
    if use_llm:
        summary = summarize_via_claude_p(excerpt)
        if summary:
            upsert(session_id, summary, "auto", project=project,
                   prompt_version=PROMPT_VERSION)
            return get(session_id)
        # claude -p failed: keep a good existing summary instead of
        # downgrading it to a fallback on a transient failure.
        if existing and existing.source == "auto":
            return existing
    upsert(session_id, _fallback_narrative(excerpt), "fallback", project=project)
    return get(session_id)


_OVERALL_PROMPT = """{language_directive}

Below is a breakdown of every Claude Code session in a single time window, grouped by category. Each line under a category is one session's one-line summary.

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

    if not claude_bin_available():
        return None

    try:
        r = run_claude_p(prompt, timeout)
        if r.returncode != 0:
            LOG.warning("overall claude -p failed: %s", r.stderr.strip()[:200])
            return None
        narrative = r.stdout.strip().strip('"').strip("'")
        if len(narrative) < 10:
            return None
    except (subprocess.SubprocessError, OSError) as e:
        LOG.warning("overall errored: %s", e)
        return None

    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO period_aggregates
               (period_key, category, summary, session_ids, created_at,
                input_fingerprint)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (period_key, OVERALL_KEY, narrative,
             ",".join(all_ids), time.time(), input_fingerprint),
        )
        conn.commit()
    finally:
        conn.close()
    return narrative


_CATEGORY_PROMPT = """{language_directive}

Below are one-line summaries of every Claude Code session in the "{category}" category for one time window ({count} sessions).

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
    input_fingerprint = _cache_fingerprint("period-category", prompt)

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

    if not claude_bin_available():
        return None

    try:
        r = run_claude_p(prompt, timeout)
        if r.returncode != 0:
            LOG.warning("category %r claude -p failed: %s",
                        category, r.stderr.strip()[:200])
            return None
        narrative = r.stdout.strip().strip('"').strip("'")
        if len(narrative) < 10:
            return None
    except (subprocess.SubprocessError, OSError) as e:
        LOG.warning("category %r errored: %s", category, e)
        return None

    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO period_aggregates
               (period_key, category, summary, session_ids, created_at,
                input_fingerprint)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (period_key, category, narrative,
             ",".join(ids_sorted), time.time(), input_fingerprint),
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

Below are session one-line summaries from two consecutive time windows for one user's Claude Code work, plus the per-bucket time deltas.

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
    input_fingerprint = _cache_fingerprint("period-comparison", prompt)

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

    if not claude_bin_available():
        return None

    try:
        r = run_claude_p(prompt, timeout)
        if r.returncode != 0:
            LOG.warning("comparison claude -p failed: %s", r.stderr.strip()[:200])
            return None
        narrative = r.stdout.strip().strip('"').strip("'")
        if len(narrative) < 10:
            return None
    except (subprocess.SubprocessError, OSError) as e:
        LOG.warning("comparison errored: %s", e)
        return None

    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO comparison_narratives
               (current_key, previous_key, signature, narrative, created_at,
                input_fingerprint)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (current_key, previous_key, sig, narrative, time.time(),
             input_fingerprint),
        )
        conn.commit()
    finally:
        conn.close()
    return narrative


_CONTENT_CLASSIFY_PROMPT = """You are categorizing Claude Code sessions by their CONTENT (not just folder name).

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
CONTENT_CLASSIFIER_POLICY_VERSION = 3


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
    if not pending or not claude_bin_available():
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
            r = run_claude_p(prompt, timeout)
            if r.returncode == 0:
                parsed = _parse_classification_lines(r.stdout)
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
                    "content-classify claude -p failed (chunk %d-%d): %s",
                    chunk_start, chunk_start + len(chunk),
                    r.stderr.strip()[:200],
                )
        except (subprocess.SubprocessError, OSError) as e:
            LOG.warning("content-classify errored: %s", e)

        # Upsert + accumulate (no-op on empty fresh); always fire the
        # progress callback so a failed chunk still advances the counter.
        _classify_cache_upsert_many(fresh, input_fingerprint)
        combined.update(fresh)
        used_buckets.update(fresh.values())
        if on_chunk_complete is not None:
            on_chunk_complete(chunk_idx, total_chunks)

    return combined


def backfill_for_sessions(
    sessions: list,
    on_progress=None,
    use_llm: bool = False,
    force: bool = False,
) -> dict:
    """Summarize sessions that are missing or, under `use_llm`, stale.

    `sessions` is a list of objects with `.session_id` and `.project` attrs.
    `use_llm=False` (default) only summarizes never-seen sessions with the
    instant first/last-user-message fallback. `use_llm=True` additionally upgrades
    `fallback` rows to `auto` and regenerates stale `auto` rows (older
    prompt_version, or every in-window `auto` when `force=True`).
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
    for i, sid in enumerate(todo):
        sess = by_id[sid]
        jsonl_path = resolver.path_for(sess)
        if jsonl_path is None:
            # Only record a "not found" skip when nothing is cached;
            # otherwise keep the existing summary rather than clobber it.
            if existing.get(sid) is None:
                upsert(sid, "(jsonl not found)", "skipped", project=sess.project)
            skipped += 1
            if on_progress:
                on_progress(i + 1, len(todo), sid, "skipped")
            continue
        result = summarize_session(sid, jsonl_path, use_llm=use_llm, force=force)
        if result and result.source == "auto":
            summarized += 1
        elif result and result.source == "fallback":
            fallback += 1
        else:
            skipped += 1
        if on_progress:
            on_progress(i + 1, len(todo), sid, result.source if result else "fail")
    return {
        "summarized": summarized,
        "fallback": fallback,
        "skipped": skipped,
        "already": len(ids) - len(todo),
        "regenerated": regenerated,
    }
