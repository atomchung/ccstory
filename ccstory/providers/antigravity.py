"""Google Antigravity session provider (``~/.gemini/antigravity/brain/**/transcript.jsonl``).

Antigravity sessions store context transcripts as JSONL step logs in brain
directories, with conversation metadata blobs stored in companion SQLite databases
under ``~/.gemini/antigravity/conversations/<session_id>.db``.
"""

from __future__ import annotations

import glob
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..time_tracking import GAP_CAP_SEC, SessionStat, _parse_ts
from .base import BaseAgentProvider
from .codex import _encode_project_dir, _worktree_origin


def extract_user_request_text(text: str) -> str:
    """Extract user prompt text, unwrapping ``<USER_REQUEST>`` envelopes when present."""
    stripped = text.strip()
    if "<USER_REQUEST>" in stripped:
        parts = stripped.split("<USER_REQUEST>", 1)[1]
        if "</USER_REQUEST>" in parts:
            stripped = parts.split("</USER_REQUEST>", 1)[0]
        else:
            stripped = parts
    return stripped.strip()


def extract_cwd_from_db(db_path: Path) -> str:
    """Extract launch CWD from an Antigravity conversation database if present."""
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        rows = c.execute("SELECT data FROM trajectory_metadata_blob;").fetchall()
        conn.close()
        for row in rows:
            data = row[0]
            if isinstance(data, bytes):
                matches = re.findall(rb"file://(/[^ \x00-\x1f\x7f-\xff\"]+)", data)
                if matches:
                    return matches[0].decode("utf-8", errors="ignore")
    except Exception:
        pass
    return ""


class AntigravityProvider(BaseAgentProvider):
    """Session provider for Google Antigravity."""

    def __init__(self, antigravity_dir: Path | None = None) -> None:
        self._antigravity_dir = antigravity_dir

    @property
    def antigravity_dir(self) -> Path:
        if self._antigravity_dir is not None:
            return self._antigravity_dir
        return Path.home() / ".gemini" / "antigravity"

    @property
    def agent_name(self) -> str:
        return "antigravity"

    def data_roots(self) -> tuple[Path, ...]:
        return (self.antigravity_dir / "brain",)

    def _transcript_glob(self) -> str:
        return str(
            self.antigravity_dir
            / "brain"
            / "*"
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )

    def parse_session(self, jsonl_path: Path) -> SessionStat | None:
        """Parse one Antigravity transcript.jsonl into a SessionStat."""
        timestamps: list[datetime] = []
        msg_count = 0
        user_msg_count = 0
        first_user_text = ""
        cwd = ""

        # Extract session_id from path structure
        session_id = ""
        parts = jsonl_path.parts
        if "brain" in parts:
            idx = parts.index("brain")
            if idx + 1 < len(parts):
                session_id = parts[idx + 1]
        if not session_id:
            session_id = jsonl_path.stem

        # Retrieve CWD from SQLite DB if available
        db_path = self.antigravity_dir / "conversations" / f"{session_id}.db"
        cwd = extract_cwd_from_db(db_path)

        try:
            with jsonl_path.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    stype = d.get("type")
                    source = d.get("source")
                    content = d.get("content", "") or ""

                    ts = _parse_ts(d.get("created_at"))
                    if ts:
                        timestamps.append(ts)

                    if stype in ("USER_INPUT", "PLANNER_RESPONSE") or source in (
                        "USER_EXPLICIT",
                        "MODEL",
                    ):
                        msg_count += 1

                    if stype == "USER_INPUT" or source == "USER_EXPLICIT":
                        text = extract_user_request_text(content)
                        if text and not text.startswith("<") and "tool_use_id" not in text:
                            user_msg_count += 1
                            if not first_user_text:
                                first_user_text = text[:200]
        except OSError:
            return None

        if not timestamps:
            return None

        timestamps.sort()
        active_sec = 0
        for prev, curr in zip(timestamps, timestamps[1:]):
            gap = (curr - prev).total_seconds()
            active_sec += min(gap, GAP_CAP_SEC)

        project = (
            _encode_project_dir(_worktree_origin(cwd)) if cwd else "antigravity"
        )

        return SessionStat(
            project=project,
            category="",
            session_id=session_id,
            start=timestamps[0],
            end=timestamps[-1],
            active_sec=int(active_sec),
            msg_count=msg_count,
            user_msg_count=user_msg_count,
            first_user_text=first_user_text,
            is_scheduled=False,
            cwd=cwd,
            timestamps=[t.timestamp() for t in timestamps],
            agent=self.agent_name,
            path=jsonl_path,
        )

    def collect_usage(
        self,
        since: datetime,
        until: datetime,
        by_model: dict,
    ) -> int:
        """Scan all Antigravity jsonl transcripts and aggregate token usage in [since, until]."""
        from ..token_usage import ModelUsage

        assistant_turns = 0

        for path_str in glob.glob(self._transcript_glob()):
            jsonl_path = Path(path_str)
            try:
                with jsonl_path.open("r", encoding="utf-8", errors="ignore") as f:
                    accumulated_inp = 0
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        stype = d.get("type")
                        source = d.get("source")
                        content = d.get("content", "") or ""
                        thinking = d.get("thinking", "") or ""
                        ts_raw = d.get("created_at")

                        if stype in (
                            "USER_INPUT",
                            "VIEW_FILE",
                            "RUN_COMMAND",
                            "LIST_DIRECTORY",
                            "GREP_SEARCH",
                            "READ_URL_CONTENT",
                            "SEARCH_WEB",
                        ) or source == "USER_EXPLICIT":
                            accumulated_inp += len(content)

                        if (
                            stype == "PLANNER_RESPONSE" or source == "MODEL"
                        ) and ts_raw:
                            ts = _parse_ts(ts_raw)
                            if not ts or ts < since or ts > until:
                                continue

                            model = "gemini-3.6-flash"
                            out_tokens = max(1, (len(content) + len(thinking)) // 4)
                            inp_tokens = max(1, accumulated_inp // 4)

                            mu = by_model.setdefault(
                                model, ModelUsage(model=model)
                            )
                            mu.turns += 1
                            mu.input_tokens += inp_tokens
                            mu.output_tokens += out_tokens
                            assistant_turns += 1

                            accumulated_inp += len(content) + len(thinking)
            except OSError:
                continue

        return assistant_turns

    def collect_sessions(
        self,
        since: datetime,
        until: datetime | None = None,
        engaged_only: bool = True,
    ) -> list[SessionStat]:
        """All Antigravity sessions overlapping [since, until)."""
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)

        stats: list[SessionStat] = []
        since_ts = since.timestamp()

        for path_str in glob.glob(self._transcript_glob()):
            path = Path(path_str)
            try:
                if path.stat().st_mtime < since_ts:
                    continue
            except OSError:
                continue

            s = self.parse_session(path)
            if not s:
                continue
            if s.end < since:
                continue
            if until is not None and s.start >= until:
                continue
            if engaged_only and not s.engaged:
                continue
            stats.append(s)
        return stats

    def transcript_path(self, sess: SessionStat) -> Path | None:
        """Session id → transcript.jsonl."""
        found = super().transcript_path(sess)
        if found is not None:
            return found
        direct = (
            self.antigravity_dir
            / "brain"
            / sess.session_id
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        if direct.exists():
            return direct
        return None
