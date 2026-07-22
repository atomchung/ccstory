"""OpenAI Codex session provider."""

from __future__ import annotations

import glob
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..time_tracking import GAP_CAP_SEC, SessionStat, _parse_ts
from .base import BaseAgentProvider

LOG = logging.getLogger("ccstory.providers.codex")
CODEX_DIR = Path.home() / ".codex"


def _extract_codex_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("text"):
                text_parts.append(part["text"])
        return "\n".join(text_parts)
    return ""


class CodexProvider(BaseAgentProvider):
    """Session provider for OpenAI Codex (~/.codex/sessions/**/*.jsonl & archived_sessions)."""

    def __init__(self, codex_dir: Path | None = None) -> None:
        self._codex_dir = codex_dir

    @property
    def codex_dir(self) -> Path:
        if self._codex_dir is not None:
            return self._codex_dir
        return Path.home() / ".codex"

    @property
    def agent_name(self) -> str:
        return "codex"

    def parse_session(self, jsonl_path: Path) -> SessionStat | None:
        """Parse one Codex transcript.jsonl into a SessionStat."""
        timestamps: list[datetime] = []
        msg_count = 0
        user_msg_count = 0
        first_user_text = ""
        cwd = ""

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

                    ts_str = d.get("timestamp")
                    if ts_str:
                        ts = _parse_ts(ts_str)
                        if ts:
                            timestamps.append(ts)

                    payload = d.get("payload", {})
                    if not isinstance(payload, dict):
                        continue

                    if not cwd and payload.get("cwd"):
                        cwd = payload["cwd"]

                    role = payload.get("role") or payload.get("type")
                    if role in ("user", "assistant", "user_message", "response_item"):
                        msg_count += 1

                    if role in ("user", "user_message"):
                        text = _extract_codex_text(payload.get("content", "")).strip()
                        is_real_user = (
                            text
                            and not text.startswith("<")
                            and "tool_use_id" not in text
                        )
                        if is_real_user:
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

        session_id = jsonl_path.stem
        if cwd and Path(cwd).name:
            project = Path(cwd).name
        else:
            project = "codex"

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

    def collect_sessions(
        self,
        since: datetime,
        until: datetime | None = None,
        engaged_only: bool = True,
    ) -> list[SessionStat]:
        """All Codex sessions overlapping [since, until)."""
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)

        stats: list[SessionStat] = []
        since_ts = since.timestamp()

        # Match active & archived session jsonl files
        patterns = [
            str(self.codex_dir / "sessions" / "**" / "*.jsonl"),
            str(self.codex_dir / "archived_sessions" / "**" / "*.jsonl"),
        ]

        for pattern in patterns:
            for path_str in glob.glob(pattern, recursive=True):
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
