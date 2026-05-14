"""Per-session narratives sourced ONLY from native Claude Code data.

Reads directly from `~/.claude/projects/*/`*.jsonl. No `claude -p`, no
subprocess, no external API call, no cost.

Two-tier fallback (both native, both free):

  Tier 1: Built-in `/recap` output
      Claude Code v2.1.114+ writes each generated recap into the session
      jsonl as `{"type":"system","subtype":"away_summary","content":"..."}`.
      Multi-sentence, includes progress + next step. Highest quality.

  Tier 2: First user message
      100% covered fallback when no recap record exists in the jsonl.

ccstory's own cache lives at ~/.ccstory/cache.db. We **never** write inside
~/.claude/* (Anthropic's namespace) and **never** silently read from
non-native databases under ~/.claude/ (those are written by other tools, not
Claude Code itself).

(`aiTitle` as a middle tier is tracked in a separate issue for testing.)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("ccstory.summarizer")
DB_PATH = Path.home() / ".ccstory" / "cache.db"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Source labels stored on each cached row. Stable strings — used by the CLI
# progress counters and by anyone querying the cache directly.
SRC_AWAY_SUMMARY = "away_summary"   # Tier 1: built-in /recap output
SRC_FIRST_USER = "first_user_msg"   # Tier 2: opening user prompt
SRC_SKIPPED = "skipped"             # no usable content found

N_USER_HEAD = 3
N_USER_TAIL = 2
N_ASSISTANT_TAIL = 1

# Trailing hint Claude Code appends to its built-in /recap output. We strip
# it so the narrative reads cleanly inside the recap card.
_RECAP_HINT_RE = re.compile(r"\s*\(disable recaps in /config\)\s*$")


@dataclass
class SessionSummary:
    session_id: str
    summary: str
    source: str
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


def _touch_cache(session_id: str) -> None:
    """Bump `created_at` to now without changing content.

    Used after a stale-cache re-check that found nothing new — marks "we
    revisited at time T" so subsequent runs only re-check when the jsonl
    has been written after T. Without this, every run would re-extract
    away_summary for the same non-promoting rows forever.
    """
    conn = _connect()
    try:
        conn.execute(
            "UPDATE session_summaries SET created_at = ? WHERE session_id = ?",
            (time.time(), session_id),
        )
        conn.commit()
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


def _project_of(jsonl_path: Path) -> str:
    try:
        return jsonl_path.relative_to(PROJECTS_DIR).parts[0]
    except ValueError:
        return jsonl_path.parent.name


def extract_away_summary(jsonl_path: Path) -> str | None:
    """Return the most recent built-in /recap output stored in this session's
    jsonl, or None if no `away_summary` record exists.

    Schema: Claude Code (v2.1.114+) writes each /recap (manual or auto-fired
    after 3+ min idle) into the session jsonl as
        {"type":"system","subtype":"away_summary","content":"<recap>", ...}

    Multiple records may appear in one session — we take the last one. The
    trailing "(disable recaps in /config)" hint is stripped.
    """
    latest: str | None = None
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
                if d.get("type") != "system":
                    continue
                if d.get("subtype") != "away_summary":
                    continue
                content = (d.get("content") or "").strip()
                content = _RECAP_HINT_RE.sub("", content).strip()
                if content:
                    latest = content
    except OSError:
        return None
    return latest


def _extract_excerpt(jsonl_path: Path) -> str:
    """Tier-2 source: first user message of the session.

    Also collects head/tail user + tail assistant messages for context, but
    only the first user message is used as narrative. The wider excerpt is
    retained as a single block in case future tiers want it.
    """
    user_msgs: list[str] = []
    assistant_msgs: list[str] = []
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
        return ""

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

    return "\n\n".join(parts)


def _first_user_narrative(excerpt: str) -> str:
    """First non-empty user message line as a poor-man's narrative.

    Format of excerpt is `[USER 1]\\n<text>\\n\\n[USER 2]\\n...`, so the
    second line after splitting is the first user message body.
    """
    parts = excerpt.split("\n", 2)
    line = parts[1] if len(parts) > 1 else excerpt
    return line[:120]


def _jsonl_mtime(jsonl_path: Path) -> float:
    try:
        return jsonl_path.stat().st_mtime if jsonl_path.exists() else 0.0
    except OSError:
        return 0.0


def summarize_session(
    session_id: str,
    jsonl_path: Path,
) -> SessionSummary | None:
    """Resolve a session's narrative, with automatic refresh on jsonl change.

    Tier 1: built-in /recap output (`system/away_summary`)
    Tier 2: first user message (fallback)

    Cache freshness is mtime-based: if the jsonl has been written after the
    cache entry, we re-extract Tier 1 and promote/replace when there's
    new content. `/recap` fires asynchronously (background after 3+ min
    idle, or manual `/recap`), so a session cached at T2 today often has
    a Tier 1 record added later — this refresh path picks it up.

    All paths read jsonl directly. No subprocess, no LLM call, no cost.
    """
    existing = get(session_id)
    if existing and _jsonl_mtime(jsonl_path) <= existing.created_at:
        return existing

    project = _project_of(jsonl_path)

    recap = extract_away_summary(jsonl_path)
    if recap:
        # T1 hit. Either fresh content or unchanged from before; upsert
        # either way so created_at advances and the row is at SRC_AWAY_SUMMARY.
        if not existing or recap != existing.summary or existing.source != SRC_AWAY_SUMMARY:
            upsert(session_id, recap, SRC_AWAY_SUMMARY, project=project)
        else:
            _touch_cache(session_id)
        return get(session_id)

    if existing:
        # Stale re-check found no Tier 1; keep existing row but mark
        # checked-now so subsequent runs short-circuit until jsonl changes again.
        _touch_cache(session_id)
        return get(session_id)

    excerpt = _extract_excerpt(jsonl_path)
    if not excerpt:
        upsert(session_id, "(no meaningful conversation)", SRC_SKIPPED, project=project)
        return get(session_id)
    upsert(session_id, _first_user_narrative(excerpt), SRC_FIRST_USER, project=project)
    return get(session_id)
