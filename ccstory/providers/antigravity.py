"""Google Antigravity session provider."""

from __future__ import annotations

import glob
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path, PurePath

from ..time_tracking import GAP_CAP_SEC, SessionStat, _parse_ts
from .base import BaseAgentProvider

LOG = logging.getLogger("ccstory.providers.antigravity")
ANTIGRAVITY_BRAIN = Path.home() / ".gemini" / "antigravity" / "brain"


def extract_clean_user_text(content: str) -> str:
    """Extract prompt text inside <USER_REQUEST> tags or fallback to whole content."""
    if not isinstance(content, str):
        return ""
    match = re.search(r"<USER_REQUEST>\s*(.*?)\s*(?:</USER_REQUEST>|$)", content, re.DOTALL)
    text = match.group(1).strip() if match else content.strip()
    return text


def extract_workspace_cwd(content: str) -> str:
    """Extract working directory path from system context in user input content."""
    if not isinstance(content, str):
        return ""
    m1 = re.search(r"The user has \d+ active workspaces.*?\n\s*(/.+?)\s*->", content)
    if m1:
        return m1.group(1).strip()
    m2 = re.search(r"Code relating to the user.*?written in the locations listed above:?\s*(/.+)", content)
    if m2:
        return m2.group(1).strip()
    return ""


class AntigravityProvider(BaseAgentProvider):
    """Session provider for Google Antigravity (~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl)."""

    def __init__(self, brain_dir: Path | None = None) -> None:
        self._brain_dir = brain_dir

    @property
    def brain_dir(self) -> Path:
        if self._brain_dir is not None:
            return self._brain_dir
        return Path.home() / ".gemini" / "antigravity" / "brain"

    @property
    def agent_name(self) -> str:
        return "antigravity"

    def parse_session(self, jsonl_path: Path) -> SessionStat | None:
        """Parse one Antigravity transcript.jsonl into a SessionStat."""
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

                    ts_str = d.get("created_at")
                    if ts_str:
                        ts = _parse_ts(ts_str)
                        if ts:
                            timestamps.append(ts)

                    step_type = d.get("type")
                    if step_type in ("USER_INPUT", "PLANNER_RESPONSE"):
                        msg_count += 1

                    if step_type == "USER_INPUT":
                        content = d.get("content", "")
                        if not cwd:
                            cwd = extract_workspace_cwd(content)

                        text = extract_clean_user_text(content)
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

        # Infer session ID & project name
        # Path structure: brain/<session_id>/.system_generated/logs/transcript.jsonl
        session_id = jsonl_path.parent.parent.parent.name
        if cwd and Path(cwd).name:
            project = Path(cwd).name
        else:
            project = "antigravity"

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
        """All Antigravity sessions overlapping [since, until)."""
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)

        stats: list[SessionStat] = []
        since_ts = since.timestamp()

        # Matchtranscript files: brain/*/.system_generated/logs/transcript.jsonl
        pattern = str(self.brain_dir / "*" / ".system_generated" / "logs" / "transcript.jsonl")
        for path_str in glob.glob(pattern):
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
