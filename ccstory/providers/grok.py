"""Local Grok CLI session provider.

Grok stores one directory per session beneath $GROK_HOME/sessions (or
~/.grok/sessions). Session metadata lives in summary.json;
chat_history.jsonl contains conversation messages; and updates.jsonl records
native turn_completed usage receipts. Only those final receipts are used for
token accounting.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..time_tracking import (
    GAP_CAP_SEC,
    SessionStat,
    active_intervals_for_timestamps,
)
from ..token_usage import ModelUsage
from .base import (
    BaseAgentProvider,
    ProviderRecord,
    SnapshotMetrics,
    _usage_windows_utc,
)
from .excerpts import build_excerpt, include_message
from .projects import encode_project_dir, worktree_origin

LOG = logging.getLogger("ccstory.providers.grok")

_TOKEN_FIELDS = (
    "inputTokens",
    "outputTokens",
    "cachedReadTokens",
    "cacheCreationTokens",
)
_TOP_TOKEN_FIELDS = (*_TOKEN_FIELDS, "totalTokens")
_MAX_TOKEN_COUNT = 2**63 - 1


@dataclass(frozen=True)
class _UsageEvent:
    """One validated final usage receipt from Grok's native update stream."""

    identity: tuple[str, ...]
    signature: tuple[Any, ...]
    timestamp: datetime
    models: dict[str, tuple[int, int, int, int]]
    used_fallback_identity: bool = False


@dataclass
class _SessionRead:
    summary_path: Path
    summary: dict[str, Any] | None
    session: SessionStat | None
    usage_events: list[_UsageEvent] = field(default_factory=list)
    records_parsed: int = 0
    opens: int = 0
    companion_reads: int = 0
    complete: bool = True


def _timestamp(value: object) -> datetime | None:
    """Parse ISO session stamps or native Unix-second stamps."""

    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(
                raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            )
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _nonnegative_int(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_TOKEN_COUNT
    ):
        return None
    return value


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _project_name(cwd: object, workspace_key: str) -> tuple[str, str]:
    if not isinstance(cwd, str) or not cwd.strip():
        return workspace_key, ""
    origin = worktree_origin(cwd)
    return encode_project_dir(origin) or workspace_key, cwd


def _parse_usage_event(
    row: dict[str, Any],
    session_id: str,
) -> tuple[_UsageEvent | None, bool]:
    """Return a validated final usage event and whether its row is complete.

    The observed Grok log format reports cached reads as a subcategory of
    inputTokens. The shared ModelUsage shape stores cached reads separately, so
    fresh input is input minus cached reads. The current native logs report no
    cache-creation tokens; until nonzero cache-creation semantics are verified,
    such a row is rejected. Reasoning token counts are a subset of output and
    are intentionally not added a second time. Provider cost fields are never
    read.
    """

    params = row.get("params")
    update = params.get("update") if isinstance(params, dict) else None
    if not isinstance(params, dict) or not isinstance(update, dict):
        return None, False
    if row.get("method") != "_x.ai/session/update":
        return None, False
    if update.get("sessionUpdate") != "turn_completed":
        return None, True

    raw_usage = update.get("usage")
    if not isinstance(raw_usage, dict):
        return None, False

    timestamp = _timestamp(row.get("timestamp"))
    prompt_id = update.get("prompt_id")
    meta = params.get("_meta")
    event_id = meta.get("eventId") if isinstance(meta, dict) else None
    raw_session_id = params.get("sessionId")
    if (
        timestamp is None
        or not isinstance(prompt_id, str)
        or not prompt_id
        or not isinstance(raw_session_id, str)
        or raw_session_id != session_id
        or (event_id is not None and (not isinstance(event_id, str) or not event_id))
    ):
        return None, False

    top_values = {
        key: _nonnegative_int(raw_usage.get(key))
        for key in _TOP_TOKEN_FIELDS
    }
    if any(value is None for value in top_values.values()):
        return None, False
    if top_values["totalTokens"] != (
        top_values["inputTokens"] + top_values["outputTokens"]
    ):
        return None, False

    raw_models = raw_usage.get("modelUsage")
    if not isinstance(raw_models, dict) or not raw_models:
        return None, False

    models: dict[str, tuple[int, int, int, int]] = {}
    sums = Counter()
    signature_models: list[tuple[str, tuple[int, ...]]] = []
    for model, raw_model_usage in sorted(raw_models.items()):
        if (
            not isinstance(model, str)
            or not model.strip()
            or not isinstance(raw_model_usage, dict)
        ):
            return None, False
        values = [_nonnegative_int(raw_model_usage.get(key)) for key in _TOKEN_FIELDS]
        if any(value is None for value in values):
            return None, False
        input_tokens, output_tokens, cache_read, cache_creation = values
        if cache_creation != 0 or cache_read > input_tokens:
            return None, False
        raw_total = _nonnegative_int(raw_model_usage.get("totalTokens"))
        if raw_total != input_tokens + output_tokens:
            return None, False
        reasoning_tokens = raw_model_usage.get("reasoningTokens")
        if reasoning_tokens is not None:
            reasoning_tokens = _nonnegative_int(reasoning_tokens)
            if reasoning_tokens is None or reasoning_tokens > output_tokens:
                return None, False

        models[model] = (
            input_tokens - cache_read,
            output_tokens,
            cache_read,
            cache_creation,
        )
        for key, value in zip(_TOKEN_FIELDS, values):
            sums[key] += value
        signature_models.append((model, tuple(values) + (raw_total,)))

    if any(sums[key] != top_values[key] for key in _TOKEN_FIELDS):
        return None, False
    if sums["inputTokens"] + sums["outputTokens"] != top_values["totalTokens"]:
        return None, False

    used_fallback_identity = event_id is None
    identity = (
        ("event", event_id)
        if isinstance(event_id, str)
        else ("session-prompt", session_id, prompt_id)
    )
    signature = (prompt_id, tuple(signature_models))
    return (
        _UsageEvent(
            identity=identity,
            signature=signature,
            timestamp=timestamp,
            models=models,
            used_fallback_identity=used_fallback_identity,
        ),
        True,
    )


class GrokProvider(BaseAgentProvider):
    """Read Grok CLI conversation sessions and final token usage receipts."""

    def __init__(self, sessions_dir: Path | None = None) -> None:
        self._sessions_dir = sessions_dir

    @property
    def sessions_dir(self) -> Path:
        if self._sessions_dir is not None:
            return self._sessions_dir
        grok_home = os.environ.get("GROK_HOME", "").strip()
        base = Path(grok_home).expanduser() if grok_home else Path.home() / ".grok"
        return base / "sessions"

    @property
    def agent_name(self) -> str:
        return "grok"

    def data_roots(self) -> tuple[Path, ...]:
        return (self.sessions_dir,)

    def _summary_paths(self) -> tuple[list[Path], bool]:
        try:
            paths = sorted(
                Path(path)
                for path in glob.glob(
                    str(self.sessions_dir / "*" / "*" / "summary.json")
                )
            )
            return paths, True
        except OSError:
            LOG.warning("could not enumerate Grok sessions")
            return [], False

    def _read_session(self, summary_path: Path) -> _SessionRead:
        result = _SessionRead(summary_path=summary_path, summary=None, session=None)
        result.opens += 1
        try:
            with summary_path.open("r", encoding="utf-8") as handle:
                summary = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            result.complete = False
            return result
        result.companion_reads += 1
        if not isinstance(summary, dict):
            result.complete = False
            return result
        result.summary = summary

        session_id = summary_path.parent.name
        info = summary.get("info")
        if (
            not isinstance(info, dict)
            or info.get("id") != session_id
            or not isinstance(info.get("cwd"), str)
            or not info.get("cwd")
        ):
            result.complete = False

        start = _timestamp(summary.get("created_at"))
        end = _timestamp(summary.get("last_active_at"))
        if start is None or end is None or end < start:
            result.complete = False

        chat_path = summary_path.parent / "chat_history.jsonl"
        user_messages: list[str] = []
        assistant_messages: list[str] = []
        msg_count = 0
        user_msg_count = 0
        first_user_text = ""
        result.opens += 1
        try:
            with chat_path.open("r", encoding="utf-8") as handle:
                result.companion_reads += 1
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        result.complete = False
                        continue
                    result.records_parsed += 1
                    if not isinstance(record, dict):
                        result.complete = False
                        continue
                    role = record.get("type")
                    if role not in ("user", "assistant"):
                        continue
                    msg_count += 1
                    content = _content_text(record.get("content")).strip()
                    if not include_message(content):
                        continue
                    if role == "user":
                        user_msg_count += 1
                        user_messages.append(content)
                        if not first_user_text:
                            first_user_text = content[:200]
                    else:
                        assistant_messages.append(content)
        except (OSError, UnicodeError):
            result.complete = False

        update_path = summary_path.parent / "updates.jsonl"
        activity_timestamps: list[float] = []
        result.opens += 1
        try:
            with update_path.open("r", encoding="utf-8") as handle:
                result.companion_reads += 1
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        result.complete = False
                        continue
                    result.records_parsed += 1
                    if not isinstance(row, dict):
                        result.complete = False
                        continue

                    params = row.get("params")
                    update = params.get("update") if isinstance(params, dict) else None
                    raw_session_id = (
                        params.get("sessionId") if isinstance(params, dict) else None
                    )
                    row_timestamp = _timestamp(row.get("timestamp"))
                    if (
                        row_timestamp is not None
                        and start is not None
                        and row_timestamp >= start
                    ):
                        activity_timestamps.append(row_timestamp.timestamp())
                    elif row_timestamp is None:
                        if (
                            isinstance(update, dict)
                            and update.get("sessionUpdate") == "turn_completed"
                        ):
                            result.complete = False
                    if (
                        isinstance(update, dict)
                        and update.get("sessionUpdate") == "turn_completed"
                    ):
                        if raw_session_id != session_id:
                            result.complete = False
                            continue
                        event, valid = _parse_usage_event(row, session_id)
                        if not valid:
                            result.complete = False
                        elif event is not None:
                            result.usage_events.append(event)
        except (OSError, UnicodeError):
            result.complete = False

        session_kind = summary.get("session_kind")
        if (
            session_kind not in ("subagent", "subagent_resume")
            and start is not None
            and end is not None
            and end >= start
        ):
            all_times = sorted(
                set([start.timestamp(), end.timestamp(), *activity_timestamps])
            )
            active_sec = sum(
                min(interval.end - interval.start, GAP_CAP_SEC)
                for interval in active_intervals_for_timestamps(all_times)
            )
            cwd = info.get("cwd") if isinstance(info, dict) else ""
            project, cwd_value = _project_name(cwd, summary_path.parent.parent.name)
            native_title = summary.get("generated_title")
            result.session = SessionStat(
                project=project,
                category="",
                session_id=session_id,
                start=start,
                end=end,
                active_sec=int(active_sec),
                msg_count=msg_count,
                user_msg_count=user_msg_count,
                first_user_text=first_user_text,
                cwd=cwd_value,
                timestamps=all_times,
                agent=self.agent_name,
                path=summary_path,
                native_title=native_title if isinstance(native_title, str) else "",
            )
        return result

    def parse_session(self, path: Path) -> SessionStat | None:
        summary_path = path / "summary.json" if path.is_dir() else path
        if summary_path.name != "summary.json":
            summary_path = summary_path.parent / "summary.json"
        return self._read_session(summary_path).session

    def collect_sessions(
        self,
        since: datetime,
        until: datetime | None = None,
        engaged_only: bool = True,
    ) -> list[SessionStat]:
        end = until or datetime.now(timezone.utc)
        window_since, window_until = _usage_windows_utc(
            {"grok": (since, end)}
        )["grok"]
        paths, _complete = self._summary_paths()
        sessions: list[SessionStat] = []
        for summary_path in paths:
            session = self._read_session(summary_path).session
            if session is None or (engaged_only and not session.engaged):
                continue
            session_start = session.start.astimezone(timezone.utc)
            session_end = session.end.astimezone(timezone.utc)
            if session_end >= window_since and session_start < window_until:
                sessions.append(session)
        return sessions

    def extract_excerpt(self, path: Path) -> tuple[str, str]:
        summary_path = path / "summary.json" if path.is_dir() else path
        if summary_path.name != "summary.json":
            summary_path = summary_path.parent / "summary.json"
        try:
            with summary_path.open("r", encoding="utf-8") as handle:
                summary = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return summary_path.parent.parent.name, ""
        if not isinstance(summary, dict):
            return summary_path.parent.parent.name, ""
        info = summary.get("info")
        cwd = info.get("cwd") if isinstance(info, dict) else ""
        project, _cwd = _project_name(cwd, summary_path.parent.parent.name)
        users: list[str] = []
        assistants: list[str] = []
        try:
            with (summary_path.parent / "chat_history.jsonl").open(
                "r", encoding="utf-8"
            ) as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    role = record.get("type")
                    if role not in ("user", "assistant"):
                        continue
                    content = _content_text(record.get("content")).strip()
                    if not include_message(content):
                        continue
                    (users if role == "user" else assistants).append(content)
        except (OSError, UnicodeError):
            return project, ""
        return project, build_excerpt(users, assistants)

    def collect_usage(
        self,
        since: datetime,
        until: datetime,
        by_model: dict,
    ) -> int:
        return self.collect_usage_for_windows(
            {"grok": (since, until)},
            {"grok": by_model},
        ).get("grok", 0)

    def collect_usage_for_windows(
        self,
        windows: Mapping[str, tuple[datetime, datetime]],
        by_model_by_window: Mapping[str, dict],
    ) -> dict[str, int]:
        normalized = _usage_windows_utc(windows)
        turns = {key: 0 for key in normalized}
        event_groups: dict[tuple[str, ...], list[_UsageEvent]] = defaultdict(list)
        paths, _complete = self._summary_paths()
        for summary_path in paths:
            session_read = self._read_session(summary_path)
            for event in session_read.usage_events:
                event_groups[event.identity].append(event)

        for group in event_groups.values():
            signatures = {event.signature for event in group}
            if len(signatures) != 1:
                LOG.warning("conflicting duplicate Grok usage records were skipped")
                continue
            event = min(group, key=lambda item: item.timestamp)
            for key, (since, until) in normalized.items():
                if not since <= event.timestamp <= until:
                    continue
                destination = by_model_by_window[key]
                for model, (
                    input_tokens,
                    output_tokens,
                    cache_read,
                    cache_creation,
                ) in event.models.items():
                    usage = destination.get(model)
                    if usage is None:
                        usage = ModelUsage(model=model)
                        destination[model] = usage
                    usage.turns += 1
                    usage.input_tokens += input_tokens
                    usage.output_tokens += output_tokens
                    usage.cache_read += cache_read
                    usage.cache_creation += cache_creation
                    usage.max_request_prompt_tokens = max(
                        usage.max_request_prompt_tokens or 0,
                        input_tokens + cache_read + cache_creation,
                    )
                turns[key] += 1
        return turns

    def collect_snapshot(
        self,
        windows: Mapping[str, tuple[datetime, datetime]],
        *,
        engaged_only: bool = True,
    ) -> ProviderRecord:
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
        normalized = _usage_windows_utc(windows)
        by_model_by_window: dict[str, dict[str, ModelUsage]] = {
            key: {} for key in normalized
        }
        assistant_turns_by_window = {key: 0 for key in normalized}
        sessions: list[SessionStat] = []
        event_groups: dict[tuple[str, ...], list[_UsageEvent]] = defaultdict(list)
        paths, inventory_complete = self._summary_paths()
        source_opens = 0
        records_parsed = 0
        companion_reads = 0

        for summary_path in paths:
            read = self._read_session(summary_path)
            source_opens += read.opens
            records_parsed += read.records_parsed
            companion_reads += read.companion_reads
            inventory_complete = inventory_complete and read.complete
            if read.session is not None and (
                not engaged_only or read.session.engaged
            ):
                sessions.append(read.session)
            for event in read.usage_events:
                event_groups[event.identity].append(event)

        for group in event_groups.values():
            signatures = {event.signature for event in group}
            if len(signatures) != 1:
                inventory_complete = False
                LOG.warning("conflicting duplicate Grok usage records were skipped")
                continue
            event = min(group, key=lambda item: item.timestamp)
            if event.used_fallback_identity:
                inventory_complete = False
            for key, (since, until) in normalized.items():
                if not since <= event.timestamp <= until:
                    continue
                destination = by_model_by_window[key]
                for model, (
                    input_tokens,
                    output_tokens,
                    cache_read,
                    cache_creation,
                ) in event.models.items():
                    usage = destination.get(model)
                    if usage is None:
                        usage = ModelUsage(model=model)
                        destination[model] = usage
                    usage.turns += 1
                    usage.input_tokens += input_tokens
                    usage.output_tokens += output_tokens
                    usage.cache_read += cache_read
                    usage.cache_creation += cache_creation
                    usage.max_request_prompt_tokens = max(
                        usage.max_request_prompt_tokens or 0,
                        input_tokens + cache_read + cache_creation,
                    )
                assistant_turns_by_window[key] += 1

        sessions_by_window: dict[str, list[SessionStat]] = {}
        for key, (since, until) in normalized.items():
            sessions_by_window[key] = [
                session
                for session in sessions
                if session.end.astimezone(timezone.utc) >= since
                and session.start.astimezone(timezone.utc) < until
            ]
        return ProviderRecord(
            agent=self.agent_name,
            sessions_by_window=sessions_by_window,
            by_model_by_window=by_model_by_window,
            assistant_turns_by_window=assistant_turns_by_window,
            metrics=SnapshotMetrics(
                sources_enumerated=len(paths),
                source_opens=source_opens,
                records_parsed=records_parsed,
                companion_reads=companion_reads,
                record_inventory_complete=inventory_complete,
            ),
        )
