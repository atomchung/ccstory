"""Claude Code session provider (``~/.claude/projects/**/*.jsonl``).

Lifted verbatim out of `time_tracking.parse_session` / `collect_sessions` when
the multi-agent split landed — the parsing rules and their comments are
unchanged, only their home is.
"""

from __future__ import annotations

import glob
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from ..time_tracking import (
    GAP_CAP_SEC,
    SessionStat,
    _extract_first_user_text,
    _is_subagent_path,
    _parse_ts,
)
from .base import BaseAgentProvider
from .excerpts import build_excerpt, include_message


def _conversation_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


class ClaudeCodeProvider(BaseAgentProvider):
    """Session provider for Claude Code."""

    def __init__(self, projects_dir: Path | None = None) -> None:
        self._projects_dir = projects_dir

    @property
    def projects_dir(self) -> Path:
        # Resolved at call time, never captured at import: tests monkeypatch
        # $HOME to redirect every path away from the real user's data
        # (tests/test_test_isolation.py guards exactly this).
        if self._projects_dir is not None:
            return self._projects_dir
        return Path.home() / ".claude" / "projects"

    @property
    def agent_name(self) -> str:
        return "claude"

    def data_roots(self) -> tuple[Path, ...]:
        return (self.projects_dir,)

    def extract_excerpt(self, jsonl_path: Path) -> tuple[str, str]:
        user_msgs: list[str] = []
        assistant_msgs: list[str] = []
        try:
            project = jsonl_path.relative_to(self.projects_dir).parts[0]
        except ValueError:
            project = jsonl_path.parent.name

        try:
            with jsonl_path.open(encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    role = record.get("type")
                    if role not in ("user", "assistant"):
                        continue
                    message = record.get("message")
                    content = (
                        message.get("content", "")
                        if isinstance(message, dict)
                        else ""
                    )
                    text = _conversation_text(content).strip()
                    if not include_message(text):
                        continue
                    if role == "user":
                        user_msgs.append(text[:500])
                    else:
                        assistant_msgs.append(text[:500])
        except OSError:
            return project, ""

        return project, build_excerpt(user_msgs, assistant_msgs)

    def parse_session(self, jsonl_path: Path) -> SessionStat | None:
        """Compute active time + metadata for one session file."""
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
                        d = json.loads(line)
                    except json.JSONDecodeError:
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
            # Left empty on purpose — categorizer.resolve_session_bucket() is
            # the single point where every classification path (current
            # window, prev window, trend, refresh) converges. Filling category
            # here would let callers that forget to run the resolver silently
            # use folder-only buckets, which is exactly the bug #61 root cause.
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

    def collect_usage(
        self,
        since: datetime,
        until: datetime,
        by_model: dict,
    ) -> int:
        return self.collect_usage_for_windows(
            {"window": (since, until)}, {"window": by_model},
        )["window"]

    def collect_usage_for_windows(
        self,
        windows: Mapping[str, tuple[datetime, datetime]],
        by_model_by_window: Mapping[str, dict],
    ) -> dict[str, int]:
        """Scan all Claude Code jsonl files and aggregate token usage in [since, until].

        Note: Claude Code logs usage per assistant message (non-cumulative). Subagent
        transcript files record token usage generated by the subagent independently,
        which is not logged in the parent session file. Therefore, collect_usage
        deliberately includes subagent files as they represent distinct, real API
        cost expenditures.
        """
        from ..token_usage import ModelUsage

        assistant_turns = {key: 0 for key in windows}
        seen_ids = {key: set() for key in windows}
        earliest_ts = min(since for since, _until in windows.values()).timestamp()

        # Target top-level project jsonls and nested subagent jsonls
        search_patterns = [
            str(self.projects_dir / "*" / "*.jsonl"),
            str(self.projects_dir / "*" / "subagents" / "*.jsonl"),
        ]
        matching_paths: list[str] = []
        for pattern in search_patterns:
            matching_paths.extend(glob.glob(pattern))

        for path_str in matching_paths:
            fp = Path(path_str)
            try:
                with fp.open() as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = d.get("message")
                        ts = d.get("timestamp")
                        if not (
                            isinstance(msg, dict)
                            and msg.get("role") == "assistant"
                            and "usage" in msg
                            and ts
                        ):
                            continue
                        try:
                            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        mid = msg.get("id")
                        u = msg["usage"]
                        model = msg.get("model") or "unknown"
                        for key, (since, until) in windows.items():
                            if t < since or t > until:
                                continue
                            if mid:
                                if mid in seen_ids[key]:
                                    continue
                                seen_ids[key].add(mid)
                            mu = by_model_by_window[key].setdefault(
                                model, ModelUsage(model=model),
                            )
                            mu.turns += 1
                            mu.input_tokens += u.get("input_tokens", 0) or 0
                            mu.cache_creation += (
                                u.get("cache_creation_input_tokens", 0) or 0
                            )
                            mu.cache_read += (
                                u.get("cache_read_input_tokens", 0) or 0
                            )
                            mu.output_tokens += u.get("output_tokens", 0) or 0
                            assistant_turns[key] += 1
            except OSError:
                continue

        return assistant_turns

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

        for path_str in glob.glob(str(self.projects_dir / "*" / "*.jsonl")):
            path = Path(path_str)
            # Skip nested subagent traces (double-count guard)
            if _is_subagent_path(path):
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
        """Session id → transcript, for stats rebuilt from cache (no `.path`)."""
        found = super().transcript_path(sess)
        if found is not None:
            return found
        direct = self.projects_dir / sess.project / f"{sess.session_id}.jsonl"
        if direct.exists():
            return direct
        # The project folder can differ from where the file actually sits
        # (renamed repo, moved worktree). Target project dirs directly.
        matches = glob.glob(
            str(self.projects_dir / "*" / f"{sess.session_id}.jsonl"),
        )
        return Path(matches[0]) if matches else None
