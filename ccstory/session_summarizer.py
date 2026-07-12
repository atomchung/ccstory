"""Session-level summaries cached to SQLite, generated via local `claude -p`.

This is the differentiator: ccusage gives numbers, ccstory gives a one-line
narrative per session. We invoke the user's *local* Claude Code CLI through
subprocess — no API key, no cost to us, no privacy concerns.

Extracted from ting/personal_os/core/session_summarizer.py. Simplified for v1:
  - Single source ("auto") — dropped the personal_os curated "record" source
  - DB lives at ~/.ccstory/cache.db (not polluting Claude Code's own dir)
"""

from __future__ import annotations

import json
import logging
import os
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


_CLAUDE_MD_MAX_CHARS = 500


def _build_language_line(language: str) -> str:
    """Render the single-line `Respond in <X>.` directive used for explicit
    language picks (CLI flag, env, ccstory config, settings.json, locale)."""
    return (
        f"Respond in {language}. "
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


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(DB_PATH))
        # PRAGMA integrity_check returns "ok" on a healthy db. A corrupt
        # file errors here instead of much later inside a query, so we
        # can give the user a clear recovery hint at startup time.
        conn.execute("PRAGMA schema_version").fetchone()
    except sqlite3.DatabaseError as e:
        import sys as _sys
        print(
            f"ccstory: error: cache at {DB_PATH} is corrupted ({e}).\n"
            f"ccstory: to reset, delete the file and re-run:\n"
            f"    rm {DB_PATH}\n"
            f"You'll lose cached per-session narratives + bucket assignments; "
            f"sessions get re-summarized on the next run.",
            file=_sys.stderr,
        )
        raise SystemExit(1) from e
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_summaries (
            session_id TEXT PRIMARY KEY,
            summary    TEXT NOT NULL,
            source     TEXT NOT NULL,
            project    TEXT,
            created_at REAL NOT NULL
        )
        """
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
    conn.commit()
    return conn


def upsert(
    session_id: str,
    summary: str,
    source: str,
    project: str | None = None,
) -> None:
    if not session_id or not summary:
        return
    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO session_summaries
               (session_id, summary, source, project, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, summary.strip(), source, project, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def get(session_id: str) -> SessionSummary | None:
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT session_id, summary, source, project, created_at
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
            f"""SELECT session_id, summary, source, project, created_at
                FROM session_summaries WHERE session_id IN ({placeholders})""",
            session_ids,
        ).fetchall()
        return {r[0]: SessionSummary(*r) for r in rows}
    finally:
        conn.close()


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
                   (session_id, summary, source, project, created_at)
                   SELECT session_id, summary, source, project, created_at
                   FROM recap.session_summaries
                   WHERE summary IS NOT NULL AND summary <> ''"""
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


def _extract_excerpt(jsonl_path: Path) -> tuple[str, str]:
    """Extract user-facing text excerpt for summarization. Returns (project, excerpt)."""
    user_msgs: list[str] = []
    assistant_msgs: list[str] = []
    try:
        project = jsonl_path.relative_to(PROJECTS_DIR).parts[0]
    except ValueError:
        project = jsonl_path.parent.name

    try:
        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = d.get("type")
                if role not in ("user", "assistant"):
                    continue
                content = d.get("message", {}).get("content", "")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            parts.append(c.get("text", ""))
                    text = "\n".join(parts)
                else:
                    text = ""
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
        r = subprocess.run(
            [
                CLAUDE_BIN, "-p",
                "--output-format", "text",
                "--no-session-persistence",
                prompt,
            ],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
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
    """First non-empty user message line as a poor-man's narrative.

    Format of excerpt is `[USER 1]\n<text>\n\n[USER 2]\n...`, so [1] after
    splitting on newline is the first user message body.
    """
    parts = excerpt.split("\n", 2)
    line = parts[1] if len(parts) > 1 else excerpt
    return line[:120]


def summarize_session(
    session_id: str,
    jsonl_path: Path,
    use_llm: bool = False,
) -> SessionSummary | None:
    """Idempotent: returns cached entry if present.

    Default (`use_llm=False`) is instant — generates a fallback narrative
    from the session's first user message and caches it as `source=fallback`.

    Set `use_llm=True` to attempt `claude -p` polish (slow, ~30-60s/session
    cold start). On `claude -p` failure, falls through to the same fallback
    narrative.
    """
    existing = get(session_id)
    if existing:
        return existing
    project, excerpt = _extract_excerpt(jsonl_path)
    if not excerpt:
        upsert(session_id, "(no meaningful conversation)", "skipped", project=project)
        return get(session_id)
    if use_llm:
        summary = summarize_via_claude_p(excerpt)
        if summary:
            upsert(session_id, summary, "auto", project=project)
            return get(session_id)
    upsert(session_id, _fallback_narrative(excerpt), "fallback", project=project)
    return get(session_id)


_OVERALL_PROMPT = """{language_directive}

Below is a breakdown of every Claude Code session in a single time window, grouped by category. Each line under a category is one session's one-line summary.

Write EXACTLY 3 sentences (max 90 words total) synthesizing what the user did across the WHOLE period. Cover:
1. The dominant focus (the biggest category and its main thread)
2. The other meaningful threads, grouped briefly
3. Any cross-cutting decision, outcome, or shift worth flagging

Style:
- Prose only — no bullets, no headers, no lists.
- Concrete nouns (tickers, file names, tools) beat generic verbs.
- Synthesize across categories; do NOT list every session.

Period: {period}
Categories (hours): {category_summary}

Session breakdown by category:
{breakdown}

Output the 3-sentence prose only — no quotes, no prefix, no fences."""

OVERALL_KEY = "__overall__"


def synthesize_overall_for_period(
    period_key: str,
    category_hours: list[tuple[str, float]],
    sessions_by_category: dict[str, list[tuple[str, str]]],
    force_refresh: bool = False,
    timeout: int = 90,
) -> str | None:
    """Synthesize ONE 3-sentence overall narrative for the whole period.

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

    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT summary, session_ids FROM period_aggregates "
            "WHERE period_key = ? AND category = ?",
            (period_key, OVERALL_KEY),
        ).fetchone()
        cached_ids = cur[1].split(",") if cur else []
        if cur and not force_refresh and cached_ids == all_ids:
            return cur[0]
    finally:
        conn.close()

    if not claude_bin_available():
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

    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", "--output-format", "text",
             "--no-session-persistence", prompt],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
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
               (period_key, category, summary, session_ids, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (period_key, OVERALL_KEY, narrative,
             ",".join(all_ids), time.time()),
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
    import hashlib
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
    sig = _comparison_signature(current_summaries, previous_summaries, deltas)

    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT narrative, signature FROM comparison_narratives "
            "WHERE current_key = ? AND previous_key = ?",
            (current_key, previous_key),
        ).fetchone()
        if cur and not force_refresh and cur[1] == sig:
            return cur[0]
    finally:
        conn.close()

    if not current_summaries or not previous_summaries:
        return None
    if not claude_bin_available():
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
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", "--output-format", "text",
             "--no-session-persistence", prompt],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
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
               (current_key, previous_key, signature, narrative, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (current_key, previous_key, sig, narrative, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return narrative


_CONTENT_CLASSIFY_PROMPT = """You are categorizing Claude Code sessions by their CONTENT (not just folder name).

Preferred bucket vocabulary (from this user's config — prefer these names so the report stays aligned with the user's mental model):
{category_context}

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


def _build_category_context() -> str:
    """Render the user's `[categories]` (or the default 4-bucket vocab) as a
    prompt block for `_CONTENT_CLASSIFY_PROMPT`.

    Read at format-time so config edits take effect on the next run without
    a process restart. Falls back to `_DEFAULT_VOCAB_BLOCK` when the user
    has no `[categories]` table — same vocabulary `init` advertises, so the
    two LLM paths never publish parallel bucket names (issue #62).
    """
    # Local import to avoid a circular dependency at module load
    # (`categorizer` doesn't import this module, but keeping the boundary
    # explicit makes test monkeypatching cleaner).
    from .categorizer import list_user_categories
    cats = list_user_categories()
    if not cats:
        return _DEFAULT_VOCAB_BLOCK
    lines = []
    for bucket in sorted(cats):
        kws = cats[bucket]
        if not kws:
            continue
        lines.append(f"- {bucket}: project leaves {', '.join(kws)}")
    return "\n".join(lines) if lines else _DEFAULT_VOCAB_BLOCK


def _classify_cache_get_many(session_ids: list[str]) -> dict[str, str]:
    if not session_ids:
        return {}
    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in session_ids)
        rows = conn.execute(
            f"SELECT session_id, bucket FROM session_content_buckets "
            f"WHERE session_id IN ({placeholders})",
            session_ids,
        ).fetchall()
        return {sid: bucket for sid, bucket in rows}
    finally:
        conn.close()


def _classify_cache_upsert_many(items: dict[str, str]) -> None:
    if not items:
        return
    conn = _connect()
    try:
        now = time.time()
        conn.executemany(
            """INSERT OR REPLACE INTO session_content_buckets
               (session_id, bucket, created_at) VALUES (?, ?, ?)""",
            [(sid, b, now) for sid, b in items.items()],
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
        bucket = obj.get("bucket")
        if isinstance(sid, str) and isinstance(bucket, str):
            normalized = bucket.strip().lower()
            if normalized:
                out[sid] = normalized
    return out


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
    cached = _classify_cache_get_many(sids) if not force_refresh else {}

    pending = [(sid, leaf, summ) for sid, leaf, summ in items if sid not in cached]
    if not pending or not claude_bin_available():
        return cached

    combined = dict(cached)
    total_chunks = (len(pending) + batch_size - 1) // batch_size
    # Resolve user [categories] once for the whole pass — every chunk shares
    # the same vocab block, and avoiding per-chunk file IO matters when a big
    # window splits into 3+ batches.
    category_context = _build_category_context()

    for chunk_idx, chunk_start in enumerate(
        range(0, len(pending), batch_size), start=1,
    ):
        chunk = pending[chunk_start:chunk_start + batch_size]
        rows = "\n".join(
            f"[{sid}] folder: {leaf} | summary: {summ[:200]}"
            for sid, leaf, summ in chunk
        )
        prompt = _CONTENT_CLASSIFY_PROMPT.format(
            rows=rows, category_context=category_context,
        )
        fresh: dict[str, str] = {}
        try:
            r = subprocess.run(
                [CLAUDE_BIN, "-p", "--output-format", "text",
                 "--no-session-persistence", prompt],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            if r.returncode == 0:
                parsed = _parse_classification_lines(r.stdout)
                # Only accept rows whose session id is in THIS chunk — guards
                # against the model hallucinating ids or returning rows we
                # never asked about.
                chunk_ids = {sid for sid, _, _ in chunk}
                fresh = {sid: b for sid, b in parsed.items() if sid in chunk_ids}
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
        _classify_cache_upsert_many(fresh)
        combined.update(fresh)
        if on_chunk_complete is not None:
            on_chunk_complete(chunk_idx, total_chunks)

    return combined


def backfill_for_sessions(
    sessions: list,
    on_progress=None,
    use_llm: bool = False,
) -> dict:
    """Summarize any sessions not yet in DB.

    `sessions` is a list of objects with `.session_id` and `.project` attributes.
    `use_llm=False` (default) uses the instant first-user-msg fallback;
    `use_llm=True` opts into `claude -p` polish per session.
    Returns {"summarized": N, "fallback": F, "skipped": M, "already": K}.
    """
    by_id = {s.session_id: s for s in sessions if getattr(s, "session_id", None)}
    miss = missing_ids(list(by_id.keys()))
    summarized = fallback = skipped = 0
    for i, sid in enumerate(miss):
        sess = by_id[sid]
        jsonl_path = PROJECTS_DIR / sess.project / f"{sid}.jsonl"
        if not jsonl_path.exists():
            matches = list(PROJECTS_DIR.rglob(f"{sid}.jsonl"))
            if not matches:
                upsert(sid, "(jsonl not found)", "skipped", project=sess.project)
                skipped += 1
                if on_progress:
                    on_progress(i + 1, len(miss), sid, "skipped")
                continue
            jsonl_path = matches[0]
        result = summarize_session(sid, jsonl_path, use_llm=use_llm)
        if result and result.source == "auto":
            summarized += 1
        elif result and result.source == "fallback":
            fallback += 1
        else:
            skipped += 1
        if on_progress:
            on_progress(i + 1, len(miss), sid, result.source if result else "fail")
    return {
        "summarized": summarized,
        "fallback": fallback,
        "skipped": skipped,
        "already": len(by_id) - len(miss),
    }
