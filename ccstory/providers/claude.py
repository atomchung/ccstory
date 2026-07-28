"""Claude Code session provider (``~/.claude/projects/**/*.jsonl``).

Lifted verbatim out of `time_tracking.parse_session` / `collect_sessions` when
the multi-agent split landed — the parsing rules and their comments are
unchanged, only their home is.
"""

from __future__ import annotations

import glob
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..time_tracking import (
    GAP_CAP_SEC,
    SessionStat,
    _extract_first_user_text,
    _is_subagent_path,
    _parse_ts,
)
from ..token_usage import ModelUsage
from .base import (
    BaseAgentProvider,
    ProviderRecord,
    SnapshotMetrics,
    _usage_windows_utc,
)
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


@dataclass
class _SessionAccumulator:
    """Build one top-level Claude session from already-decoded records."""

    timestamps: list[datetime] = field(default_factory=list)
    msg_count: int = 0
    user_msg_count: int = 0
    first_user_text: str = ""
    is_scheduled: bool = False
    first_raw_user_seen: bool = False
    cwd: str = ""
    records_complete: bool = True

    def add(self, record: dict) -> None:
        role = record.get("type")
        if role not in ("user", "assistant"):
            return

        if not self.cwd and isinstance(record.get("cwd"), str):
            self.cwd = record["cwd"]
        self.msg_count += 1

        raw_timestamp = record.get("timestamp")
        timestamp = _parse_ts(
            raw_timestamp if isinstance(raw_timestamp, str) else None
        )
        if timestamp is not None:
            self.timestamps.append(timestamp)
        else:
            self.records_complete = False

        if role != "user":
            return

        message = record.get("message")
        if not isinstance(message, dict):
            self.records_complete = False
            return
        text = _extract_first_user_text(message.get("content", "")).strip()
        if not self.first_raw_user_seen and text:
            self.first_raw_user_seen = True
            if text.startswith("<scheduled-task"):
                self.is_scheduled = True
        is_real_user = (
            text
            and not text.startswith("<")
            and "tool_use_id" not in text
        )
        if is_real_user:
            self.user_msg_count += 1
            if not self.first_user_text:
                self.first_user_text = text[:200]

    def finish(
        self,
        jsonl_path: Path,
        *,
        projects_dir: Path,
        agent_name: str,
    ) -> SessionStat | None:
        if not self.timestamps:
            return None

        timestamps = sorted(self.timestamps)
        active_sec = 0
        for previous, current in zip(timestamps, timestamps[1:]):
            gap = (current - previous).total_seconds()
            active_sec += min(gap, GAP_CAP_SEC)

        try:
            project = jsonl_path.relative_to(projects_dir).parts[0]
        except ValueError:
            project = jsonl_path.parent.name

        return SessionStat(
            project=project,
            # categorizer.resolve_session_bucket() remains the single shared
            # classification point for every provider and report window.
            category="",
            session_id=jsonl_path.stem,
            start=timestamps[0],
            end=timestamps[-1],
            active_sec=int(active_sec),
            msg_count=self.msg_count,
            user_msg_count=self.user_msg_count,
            first_user_text=self.first_user_text,
            is_scheduled=self.is_scheduled,
            cwd=self.cwd,
            timestamps=[timestamp.timestamp() for timestamp in timestamps],
            agent=agent_name,
            path=jsonl_path,
        )


def _token_count(value: object) -> int | None:
    """Return an authoritative token count, rejecting malformed values."""

    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _accumulate_usage_record(
    record: dict,
    windows: Mapping[str, tuple[datetime, datetime]],
    by_model_by_window: Mapping[str, dict[str, ModelUsage]],
    seen_ids_by_window: Mapping[str, set[str]],
    assistant_turns_by_window: dict[str, int],
) -> bool:
    """Attribute one decoded Claude usage record to every matching window.

    The boolean says whether a record that advertised exact assistant usage
    was structurally complete. Non-usage records are valid no-ops.
    """

    message = record.get("message")
    raw_timestamp = record.get("timestamp")
    if not (
        isinstance(message, dict)
        and message.get("role") == "assistant"
        and "usage" in message
    ):
        return True

    if not isinstance(raw_timestamp, str) or not raw_timestamp:
        return False
    timestamp = _parse_ts(raw_timestamp)
    usage = message.get("usage")
    if timestamp is None or not isinstance(usage, dict):
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)

    model = message.get("model") or "unknown"
    message_id = message.get("id")
    if not isinstance(model, str):
        return False
    if message_id is not None and not isinstance(message_id, str):
        return False

    token_values: list[int] = []
    for key in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    ):
        count = _token_count(usage.get(key))
        if count is None:
            return False
        token_values.append(count)
    input_tokens, cache_creation, cache_read, output_tokens = token_values

    for key, (since, until) in windows.items():
        if timestamp < since or timestamp > until:
            continue
        if message_id:
            if message_id in seen_ids_by_window[key]:
                continue
            seen_ids_by_window[key].add(message_id)
        model_usage = by_model_by_window[key].setdefault(
            model,
            ModelUsage(model=model),
        )
        model_usage.turns += 1
        model_usage.input_tokens += input_tokens
        model_usage.cache_creation += cache_creation
        model_usage.cache_read += cache_read
        model_usage.output_tokens += output_tokens
        assistant_turns_by_window[key] += 1
    return True


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
        accumulator = _SessionAccumulator()

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
                    if not isinstance(d, dict):
                        continue
                    accumulator.add(d)
        except OSError:
            return None

        return accumulator.finish(
            jsonl_path,
            projects_dir=self.projects_dir,
            agent_name=self.agent_name,
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
        cost expenditures. Bounds are normalized to UTC here so direct provider
        callers retain the public rule that naive datetimes mean UTC.
        """
        normalized_windows = _usage_windows_utc(windows)
        assistant_turns = {key: 0 for key in normalized_windows}
        seen_ids = {key: set() for key in normalized_windows}

        # Target top-level project jsonls plus the two observed, fixed-depth
        # subagent layouts.  Claude Code's current layout nests subagents
        # below a parent-session directory; the shorter form remains for
        # older logs and fixtures.
        search_patterns = [
            str(self.projects_dir / "*" / "*.jsonl"),
            str(self.projects_dir / "*" / "subagents" / "*.jsonl"),
            str(self.projects_dir / "*" / "*" / "subagents" / "*.jsonl"),
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
                        if not isinstance(d, dict):
                            continue
                        _accumulate_usage_record(
                            d,
                            normalized_windows,
                            by_model_by_window,
                            seen_ids,
                            assistant_turns,
                        )
            except OSError:
                continue

        return assistant_turns

    def collect_snapshot(
        self,
        windows: Mapping[str, tuple[datetime, datetime]],
        *,
        engaged_only: bool = True,
    ) -> ProviderRecord:
        """Derive session and exact-usage facts from one Claude source pass.

        Metrics count unique matched paths, open attempts, successfully
        decoded dictionary records, and zero companion reads. Inventory is
        complete only when enumeration and every source read succeed, every
        non-empty row decodes to a dictionary, and session/usage candidate
        records contain the schema needed for their facts. Provider-irrelevant
        dictionary records remain valid no-ops.
        """

        if not windows:
            return ProviderRecord(
                agent=self.agent_name,
                sessions_by_window={},
                by_model_by_window={},
                assistant_turns_by_window={},
                metrics=SnapshotMetrics(
                    sources_enumerated=0,
                    source_opens=0,
                    records_parsed=0,
                    companion_reads=0,
                    record_inventory_complete=False,
                ),
            )

        normalized_windows = _usage_windows_utc(windows)
        by_model_by_window: dict[str, dict[str, ModelUsage]] = {
            key: {} for key in normalized_windows
        }
        assistant_turns_by_window = {
            key: 0 for key in normalized_windows
        }
        seen_ids_by_window: dict[str, set[str]] = {
            key: set() for key in normalized_windows
        }

        search_patterns = [
            str(self.projects_dir / "*" / "*.jsonl"),
            str(self.projects_dir / "*" / "subagents" / "*.jsonl"),
            str(self.projects_dir / "*" / "*" / "subagents" / "*.jsonl"),
        ]
        source_paths: list[Path] = []
        seen_paths: set[Path] = set()
        inventory_complete = True
        for pattern in search_patterns:
            try:
                matching_paths = glob.glob(pattern)
            except OSError:
                inventory_complete = False
                continue
            for path_str in matching_paths:
                path = Path(path_str)
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                source_paths.append(path)

        sessions: list[SessionStat] = []
        source_opens = 0
        records_parsed = 0
        for path in source_paths:
            session_accumulator = (
                None if _is_subagent_path(path) else _SessionAccumulator()
            )
            source_opens += 1
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            inventory_complete = False
                            continue
                        if not isinstance(record, dict):
                            inventory_complete = False
                            continue
                        records_parsed += 1
                        if session_accumulator is not None:
                            session_accumulator.add(record)
                        if not _accumulate_usage_record(
                            record,
                            normalized_windows,
                            by_model_by_window,
                            seen_ids_by_window,
                            assistant_turns_by_window,
                        ):
                            inventory_complete = False
            except (OSError, UnicodeError):
                inventory_complete = False
                continue

            if session_accumulator is None:
                continue
            if not session_accumulator.records_complete:
                inventory_complete = False
            session = session_accumulator.finish(
                path,
                projects_dir=self.projects_dir,
                agent_name=self.agent_name,
            )
            if session is None or (engaged_only and not session.engaged):
                continue
            sessions.append(session)

        sessions_by_window = {
            key: [
                session
                for session in sessions
                if session.end.astimezone(timezone.utc) >= since
                and session.start.astimezone(timezone.utc) < until
            ]
            for key, (since, until) in normalized_windows.items()
        }
        return ProviderRecord(
            agent=self.agent_name,
            sessions_by_window=sessions_by_window,
            by_model_by_window=by_model_by_window,
            assistant_turns_by_window=assistant_turns_by_window,
            metrics=SnapshotMetrics(
                sources_enumerated=len(source_paths),
                source_opens=source_opens,
                records_parsed=records_parsed,
                companion_reads=0,
                record_inventory_complete=inventory_complete,
            ),
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
