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


def summarize_session(session_id: str, jsonl_path: Path) -> SessionSummary | None:
    """Idempotent: skip if already cached."""
    existing = get(session_id)
    if existing:
        return existing
    project, excerpt = _extract_excerpt(jsonl_path)
    if not excerpt:
        upsert(session_id, "(no meaningful conversation)", "skipped", project=project)
        return get(session_id)
    summary = summarize_via_claude_p(excerpt)
    if not summary:
        first_line = excerpt.split("\n", 2)[1] if "\n" in excerpt else excerpt[:80]
        upsert(session_id, first_line[:120], "fallback", project=project)
        return get(session_id)
    upsert(session_id, summary, "auto", project=project)
    return get(session_id)


def backfill_for_sessions(sessions: list, on_progress=None) -> dict:
    """Summarize any sessions not yet in DB.

    `sessions` is a list of objects with `.session_id` and `.project` attributes.
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
        result = summarize_session(sid, jsonl_path)
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
