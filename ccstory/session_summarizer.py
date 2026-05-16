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
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("ccstory.summarizer")
DB_PATH = Path.home() / ".ccstory" / "cache.db"
RECAP_DB_PATH = Path.home() / ".claude" / "session_summaries.db"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_BIN = "claude"

N_USER_HEAD = 3
N_USER_TAIL = 2
N_ASSISTANT_TAIL = 1


@dataclass
class SessionSummary:
    session_id: str
    summary: str
    source: str  # "auto" | "skipped" | "fallback"
    project: str | None = None
    created_at: float = 0.0


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
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
            conn.execute(f"ATTACH DATABASE '{RECAP_DB_PATH}' AS recap")
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


_PROMPT_TEMPLATE = """Below is an excerpt of a Claude Code conversation (first/last user + assistant messages).

Write ONE sentence (max 18 words, English) summarizing what this session ACTUALLY DID — focus on outcomes, not process.

Good examples:
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
    prompt = _PROMPT_TEMPLATE.format(excerpt=excerpt[:8000])
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


_AGG_PROMPT = """Below are the one-line summaries of every session in a single category for one time window. Each line is one session.

Write 2-3 sentences (max 60 words, English) synthesizing what the user was actually working on in this category for the period. Focus on:
- The main thread (most recurring theme)
- Any key decisions / outcomes
- If there are clear sub-themes, group them

Good example:
- Investment work focused on AI semiconductor chain deep-dives: Cambricon SELL initiation at RMB 650, AVGO Q1 wiki refresh against SemiAnalysis consensus, plus a Q1 thesis update across NVDA/AMD/MU/TSLA/ORCL via parallel agents. Also established a weekly portfolio-watch routine.

Avoid:
- Repeating individual session contents (synthesize, don't list)
- Bullet points (write prose)
- Going over 3 sentences / 200 chars

Category: {category}
Sessions in period: {count}

Session summaries:
{summaries}

Output the synthesized prose only — no quotes, no prefix, no fences."""


def aggregate_for_period(
    period_key: str,
    category: str,
    session_ids: list[str],
    summaries: list[str],
    force_refresh: bool = False,
    timeout: int = 90,
) -> str | None:
    """Synthesize a 2-3 sentence narrative for one category in a period.

    Cache key: (period_key, category). If the set of session ids differs
    from the cached entry, regenerate (sessions added/removed since last run).
    Returns None on LLM failure.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT summary, session_ids FROM period_aggregates "
            "WHERE period_key = ? AND category = ?",
            (period_key, category),
        ).fetchone()
        cached_ids = set(cur[1].split(",")) if cur else set()
        new_ids = set(session_ids)
        if cur and not force_refresh and cached_ids == new_ids:
            return cur[0]
    finally:
        conn.close()

    if not summaries:
        return None
    bullets = "\n".join(f"- {s}" for s in summaries)
    prompt = _AGG_PROMPT.format(
        category=category, count=len(summaries), summaries=bullets[:6000],
    )
    if not claude_bin_available():
        return None
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", "--output-format", "text",
             "--no-session-persistence", prompt],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if r.returncode != 0:
            LOG.warning("aggregate claude -p failed: %s", r.stderr.strip()[:200])
            return None
        narrative = r.stdout.strip().strip('"').strip("'")
        if len(narrative) < 10:
            return None
    except (subprocess.SubprocessError, OSError) as e:
        LOG.warning("aggregate errored: %s", e)
        return None

    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO period_aggregates
               (period_key, category, summary, session_ids, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (period_key, category, narrative,
             ",".join(sorted(session_ids)), time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return narrative


def get_period_aggregates(period_key: str) -> dict[str, str]:
    """Return {category: summary} for a period, only ones already in cache."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT category, summary FROM period_aggregates WHERE period_key = ?",
            (period_key,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


_COMPARISON_PROMPT = """Below are session one-line summaries from two consecutive time windows for one user's Claude Code work, plus the per-bucket time deltas.

Write ONE OR TWO sentences (max 50 words, English) describing how the user's focus SHIFTED between the previous window and the current one. Focus on:
- What dominated each window
- The biggest shift (which bucket grew or shrank most, and what replaced it)
- Concrete content where possible (don't just say "more coding"; say what kind of coding)

Good example:
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
