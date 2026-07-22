"""Claude Code session provider."""

from __future__ import annotations

import glob
from datetime import datetime, timezone
from pathlib import Path, PurePath

from ..time_tracking import GAP_CAP_SEC, SessionStat, _extract_first_user_text, _parse_ts
from .base import BaseAgentProvider

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def _is_subagent_path(path: PurePath) -> bool:
    """Detect the exact ``subagents`` component on any pathlib flavor."""
    return "subagents" in path.parts


class ClaudeCodeProvider(BaseAgentProvider):
    """Session provider for Claude Code (~/.claude/projects/**/*.jsonl)."""

    def __init__(self, projects_dir: Path | None = None) -> None:
        self._projects_dir = projects_dir

    @property
    def projects_dir(self) -> Path:
        if self._projects_dir is not None:
            return self._projects_dir
        return Path.home() / ".claude" / "projects"

    @property
    def agent_name(self) -> str:
        return "claude"

    def parse_session(self, jsonl_path: Path) -> SessionStat | None:
        """Compute active time + metadata for one Claude Code session file."""
        timestamps: list[datetime] = []
        msg_count = 0
        user_msg_count = 0
        first_user_text = ""
        is_scheduled = False
        first_raw_user_seen = False
        cwd = ""

        try:
            with jsonl_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        import json
                        d = json.loads(line)
                    except Exception:
                        continue
                    role = d.get("type")
                    if role not in ("user", "assistant"):
                        continue
                    if not cwd and isinstance(d.get("cwd"), str):
                        cwd = d["cwd"]
                    msg_count += 1
                    ts = _parse_ts(d.get("timestamp"))
                    if ts:
                        timestamps.append(ts)
                    if role == "user":
                        content = d.get("message", {}).get("content", "")
                        text = _extract_first_user_text(content).strip()
                        if not first_raw_user_seen and text:
                            first_raw_user_seen = True
                            if text.startswith("<scheduled-task"):
                                is_scheduled = True
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

        try:
            proj_dir = jsonl_path.relative_to(self.projects_dir).parts[0]
        except ValueError:
            proj_dir = jsonl_path.parent.name

        return SessionStat(
            project=proj_dir,
            category="",
            session_id=jsonl_path.stem,
            start=timestamps[0],
            end=timestamps[-1],
            active_sec=int(active_sec),
            msg_count=msg_count,
            user_msg_count=user_msg_count,
            first_user_text=first_user_text,
            is_scheduled=is_scheduled,
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
        """All Claude Code sessions overlapping [since, until)."""
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)

        stats: list[SessionStat] = []
        since_ts = since.timestamp()

        for path_str in glob.glob(str(self.projects_dir / "**" / "*.jsonl"), recursive=True):
            path = Path(path_str)
            if _is_subagent_path(path):
                continue
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
