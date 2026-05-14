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


def summarize_session(
    session_id: str,
    jsonl_path: Path,
) -> SessionSummary | None:
    """Idempotent — returns cached entry if present.

    Tier 1: built-in /recap output (`system/away_summary`) if present.
    Tier 2: first user message as fallback.

    Both tiers read jsonl directly. No subprocess, no LLM call, no cost.
    """
    existing = get(session_id)
    if existing:
        return existing

    project = _project_of(jsonl_path)

    recap = extract_away_summary(jsonl_path)
    if recap:
        upsert(session_id, recap, SRC_AWAY_SUMMARY, project=project)
        return get(session_id)

    excerpt = _extract_excerpt(jsonl_path)
    if not excerpt:
        upsert(session_id, "(no meaningful conversation)", SRC_SKIPPED, project=project)
        return get(session_id)
    upsert(session_id, _first_user_narrative(excerpt), SRC_FIRST_USER, project=project)
    return get(session_id)


def backfill_for_sessions(
    sessions: list,
    on_progress=None,
) -> dict:
    """Resolve narratives for sessions not yet in cache.

    `sessions` is a list of objects with `.session_id` and `.project` attrs.
    Pure jsonl reads — no LLM, no subprocess, no cost.
    Returns {"away_summary": N, "first_user_msg": F, "skipped": M, "already": K}.
    """
    by_id = {s.session_id: s for s in sessions if getattr(s, "session_id", None)}
    miss = missing_ids(list(by_id.keys()))
    away = first_user = skipped = 0
    for i, sid in enumerate(miss):
        sess = by_id[sid]
        jsonl_path = PROJECTS_DIR / sess.project / f"{sid}.jsonl"
        if not jsonl_path.exists():
            matches = list(PROJECTS_DIR.rglob(f"{sid}.jsonl"))
            if not matches:
                upsert(sid, "(jsonl not found)", SRC_SKIPPED, project=sess.project)
                skipped += 1
                if on_progress:
                    on_progress(i + 1, len(miss), sid, SRC_SKIPPED)
                continue
            jsonl_path = matches[0]
        result = summarize_session(sid, jsonl_path)
        if result and result.source == SRC_AWAY_SUMMARY:
            away += 1
        elif result and result.source == SRC_FIRST_USER:
            first_user += 1
        else:
            skipped += 1
        if on_progress:
            on_progress(i + 1, len(miss), sid, result.source if result else "fail")
    return {
        "away_summary": away,
        "first_user_msg": first_user,
        "skipped": skipped,
        "already": len(by_id) - len(miss),
    }
