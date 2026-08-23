"""OpenAI Codex session provider (``~/.codex/{sessions,archived_sessions}``).

Codex rollout transcripts are JSONL like Claude Code's, but nothing else
lines up:

  * every record is ``{"timestamp": ..., "type": ..., "payload": {...}}`` —
    the interesting bits live one level down under ``payload``;
  * the genuine user turn is an ``event_msg`` of payload type
    ``user_message`` with the text under ``message`` (NOT ``content``).
    The ``response_item`` user records carry the same text *plus* everything
    the harness injects (``<recommended_plugins>``, ``<environment_context>``,
    ``<skill>`` bodies, AGENTS.md), so reading those instead is how you end
    up summarizing a plugin list;
  * there is no project folder in the path — attribution comes from ``cwd``.
"""

from __future__ import annotations

import glob
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from ..time_tracking import (
    GAP_CAP_SEC,
    SessionStat,
    WindowEvidence,
    _parse_ts,
    active_intervals_for_timestamps,
    clip_active_intervals,
    make_window_evidence,
)
from .base import BaseAgentProvider, _usage_windows_utc
from .excerpts import (
    ASSISTANT_EVIDENCE_CHARS,
    build_excerpt,
    compact_evidence_text,
    include_message,
)
from .projects import encode_project_dir, worktree_origin

# Payload types whose timestamps count as "the agent was working". Bookkeeping
# events (`token_count`, `task_started`, ...) are skipped so the gap-sum stays
# methodologically comparable with the Claude provider, which only samples
# user/assistant records.
_ACTIVITY_EVENT_TYPES = frozenset({"user_message", "agent_message"})

# Trailing session id in a `rollout-<iso-ts>-<session-id>.jsonl` file name.
_ROLLOUT_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def is_subagent_meta(meta: dict) -> bool:
    """Is this rollout a subagent thread spawned by another session?

    Codex writes a spawned subagent its own rollout file, carrying a
    ``parent_thread_id`` and ``source: {"subagent": …}``. Its turns are already
    part of the parent session's wall clock, so counting it as a session of its
    own double-counts the work — the same reason the Claude provider skips
    ``subagents/`` paths. Measured on 532 real rollouts: 103 are subagent
    threads and 91 of those record a user turn, so the engagement filter does
    not catch them.
    """
    return bool(meta.get("parent_thread_id")) or "subagent" in str(meta.get("source"))


def _codex_text(content) -> str:
    """Flatten a Codex ``content`` value (str, or list of typed text parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def strip_task_wrapper(text: str) -> str:
    """Unwrap the ``<task>…</task>`` envelope the Codex CLI plugin sends.

    A user turn dispatched from Claude Code's `/codex` plugin arrives wrapped
    in that tag. It is a real user request, not harness noise, so it must not
    be dropped by the leading-``<`` filter — but the tag itself is not worth
    showing in a narrative.
    """
    stripped = text.strip()
    if stripped.startswith("<task>"):
        stripped = stripped[len("<task>"):]
        if stripped.rstrip().endswith("</task>"):
            stripped = stripped.rstrip()[: -len("</task>")]
    return stripped.strip()


class CodexProvider(BaseAgentProvider):
    """Session provider for OpenAI Codex."""

    def __init__(self, codex_dir: Path | None = None) -> None:
        self._codex_dir = codex_dir
        self._index: dict[str, Path] | None = None

    @property
    def codex_dir(self) -> Path:
        # Resolved at call time — see ClaudeCodeProvider.projects_dir.
        if self._codex_dir is not None:
            return self._codex_dir
        return Path.home() / ".codex"

    @property
    def agent_name(self) -> str:
        return "codex"

    def data_roots(self) -> tuple[Path, ...]:
        return (
            self.codex_dir / "sessions",
            self.codex_dir / "archived_sessions",
        )

    def extract_excerpt(self, jsonl_path: Path) -> tuple[str, str]:
        user_msgs: list[str] = []
        assistant_msgs: list[str] = []
        cwd = ""

        try:
            with jsonl_path.open(encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if not cwd and isinstance(payload.get("cwd"), str):
                        cwd = payload["cwd"]

                    kind = record.get("type")
                    ptype = payload.get("type")
                    role: str | None = None
                    text = ""
                    if kind == "event_msg" and ptype == "user_message":
                        role = "user"
                        text = strip_task_wrapper(
                            _codex_text(payload.get("message", ""))
                        )
                    elif (
                        kind == "response_item"
                        and ptype == "message"
                        and payload.get("role") == "assistant"
                    ):
                        role = "assistant"
                        text = _codex_text(payload.get("content", ""))

                    text = text.strip()
                    if role is None or not include_message(text):
                        continue
                    if role == "user":
                        user_msgs.append(text[:500])
                    else:
                        assistant_msgs.append(text[:500])
        except OSError:
            return jsonl_path.parent.name, ""

        project = jsonl_path.parent.name
        if cwd:
            project = encode_project_dir(worktree_origin(cwd)) or project
        return project, build_excerpt(user_msgs, assistant_msgs)

    def extract_window_evidence(
        self,
        session: SessionStat,
        since: datetime,
        until: datetime,
    ) -> WindowEvidence | None:
        """Return Codex transcript facts authored inside ``[since, until)``.

        The activity and message predicates mirror :meth:`parse_session`:
        event user messages are the sole real-user source, while response
        items supply assistant evidence and tool/message counts. A missing or
        unreadable transcript has no safe bounded substitute, so callers
        receive ``None`` rather than this provider's full-session excerpt from
        ``extract_excerpt`` (which is intentionally unbounded).

        ``session`` must be the **physical** ``SessionStat``: the clipped
        active intervals are derived from its complete timestamp list, which a
        window slice no longer carries.  Passing a slice would silently
        under-report activity, so ``session_slice_for_window`` re-validates the
        returned intervals against the physical session it was given.

        A record whose timestamp does not parse is skipped rather than
        counted: an event that cannot be placed in time cannot be proven to
        belong to this window.
        """

        path = self.transcript_path(session)
        if path is None:
            return None

        window_since, window_until = _usage_windows_utc(
            {"window": (since, until)}
        )["window"]
        timestamps: list[float] = []
        user_msgs: list[str] = []
        assistant_msgs: list[str] = []
        msg_count = 0
        user_msg_count = 0
        first_user_text = ""
        latest_user_text = ""

        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue

                    kind = record.get("type")
                    payload_type = payload.get("type")
                    counts_as_activity = kind == "response_item" or (
                        kind == "event_msg"
                        and payload_type in _ACTIVITY_EVENT_TYPES
                    )
                    if not counts_as_activity:
                        continue
                    raw_timestamp = record.get("timestamp")
                    timestamp = _parse_ts(
                        raw_timestamp if isinstance(raw_timestamp, str) else None
                    )
                    if timestamp is None:
                        continue
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    else:
                        timestamp = timestamp.astimezone(timezone.utc)
                    if not window_since <= timestamp < window_until:
                        continue

                    timestamps.append(timestamp.timestamp())
                    if kind == "response_item":
                        is_message = (
                            payload_type == "message"
                            and payload.get("role") in ("user", "assistant")
                        )
                        is_tool_record = isinstance(payload_type, str) and (
                            payload_type.endswith("_call")
                            or payload_type.endswith("_call_output")
                        )
                        if is_message or is_tool_record:
                            msg_count += 1
                        if (
                            payload_type == "message"
                            and payload.get("role") == "assistant"
                        ):
                            text = _codex_text(payload.get("content", "")).strip()
                            if include_message(text):
                                # build_excerpt owns bounded head+tail compaction.
                                assistant_msgs.append(text)
                        continue

                    if payload_type != "user_message":
                        continue
                    msg_count += 1
                    text = strip_task_wrapper(
                        _codex_text(payload.get("message", ""))
                    ).strip()
                    if text and "tool_use_id" not in text:
                        user_msg_count += 1
                        if not first_user_text:
                            first_user_text = text[:200]
                        latest_user_text = text[:200]
                    if include_message(text):
                        user_msgs.append(text)
        except (OSError, UnicodeError):
            return None

        intervals = clip_active_intervals(
            active_intervals_for_timestamps(session.timestamps),
            window_since,
            window_until,
        )
        final_assistant_text = (
            compact_evidence_text(assistant_msgs[-1], ASSISTANT_EVIDENCE_CHARS)
            if assistant_msgs
            else ""
        )
        return make_window_evidence(
            timestamps=timestamps,
            active_intervals=intervals,
            msg_count=msg_count,
            user_msg_count=user_msg_count,
            first_user_text=first_user_text,
            latest_user_text=latest_user_text,
            final_assistant_text=final_assistant_text,
            excerpt=build_excerpt(user_msgs, assistant_msgs),
        )

    def _transcript_globs(self) -> list[str]:
        return [str(root / "**" / "*.jsonl") for root in self.data_roots()]

    def parse_session(self, jsonl_path: Path) -> SessionStat | None:
        """Parse one Codex rollout transcript into a SessionStat."""
        timestamps: list[datetime] = []
        msg_count = 0
        user_msg_count = 0
        first_user_text = ""
        cwd = ""
        session_id = ""
        delegation_source = ""
        is_automation = False

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

                    payload = d.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    kind = d.get("type")
                    ptype = payload.get("type")

                    if kind == "session_meta":
                        if is_subagent_meta(payload):
                            return None
                        if payload.get("originator") == "Claude Code":
                            delegation_source = "claude_code"
                        if payload.get("thread_source") == "automation":
                            # SCHEDULED-mode provenance only
                            # (ccstory/provenance.py). Not is_scheduled:
                            # that field relaxes SessionStat.engaged's
                            # admission threshold and was measured to admit
                            # +41 sessions (+13.7%) when this marker was
                            # wired into it during #240 (see #136).
                            is_automation = True
                        # `id` is this rollout; `session_id` is the *thread*
                        # root and is shared by every resumed/forked rollout of
                        # it — 313 of 532 real files collide on it, which as a
                        # cache key means each one's summary overwrites the
                        # last. `id` was unique across all of them.
                        for key in ("id", "session_id"):
                            if not session_id and isinstance(payload.get(key), str):
                                session_id = payload[key]
                    # `session_meta` carries the launch cwd, `turn_context`
                    # the per-turn one; either is fine, first wins.
                    if not cwd and isinstance(payload.get("cwd"), str):
                        cwd = payload["cwd"]

                    counts_as_activity = kind == "response_item" or (
                        kind == "event_msg" and ptype in _ACTIVITY_EVENT_TYPES
                    )
                    if not counts_as_activity:
                        continue

                    ts = _parse_ts(d.get("timestamp"))
                    if ts:
                        timestamps.append(ts)

                    if kind == "response_item":
                        # Mirror the Claude provider's msg_count, which counts
                        # every user/assistant record including tool traffic.
                        # Reasoning items have no counterpart there.
                        if (
                            ptype == "message"
                            and payload.get("role") in ("user", "assistant")
                        ) or (
                            isinstance(ptype, str)
                            and (
                                ptype.endswith("_call")
                                or ptype.endswith("_call_output")
                            )
                        ):
                            msg_count += 1
                        continue

                    if ptype == "user_message":
                        raw_text = _codex_text(payload.get("message", ""))
                        stripped = raw_text.strip()
                        if stripped.startswith("<codex_delegation>"):
                            delegation_source = "codex_delegation"
                        elif (
                            stripped.startswith("<task>")
                            and not delegation_source
                        ):
                            delegation_source = "claude_code"
                        text = strip_task_wrapper(raw_text)
                        if text and "tool_use_id" not in text:
                            user_msg_count += 1
                            if not first_user_text:
                                first_user_text = text[:200]
                        msg_count += 1
        except OSError:
            return None

        if not timestamps:
            return None

        timestamps.sort()
        active_sec = 0
        for prev, curr in zip(timestamps, timestamps[1:]):
            gap = (curr - prev).total_seconds()
            active_sec += min(gap, GAP_CAP_SEC)

        return SessionStat(
            project=encode_project_dir(worktree_origin(cwd)) if cwd else "codex",
            # Left empty on purpose — see ClaudeCodeProvider.parse_session.
            category="",
            session_id=session_id or jsonl_path.stem,
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
            is_delegated=bool(delegation_source),
            delegation_source=delegation_source,
            is_automation=is_automation,
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
        """Scan all Codex jsonl files and aggregate token usage in [since, until].

        Each rollout id is a cumulative counter branch. Spawned subagents have
        their own branches and therefore their own real token cost, but a child
        can begin at a cumulative baseline copied from an ancestor. Interleaving
        all branches by their shared root ``session_id`` loses divergent child
        deltas; summing each branch's full final total counts that inherited
        baseline repeatedly.

        This collector reconstructs each branch independently, removes the
        longest leading snapshot sequence copied from an ancestor, then
        attributes positive adjacent deltas in the requested window. Subagent
        rollouts remain excluded from SessionStat/time collection, but their
        branch-local token usage is included exactly once here.
        """
        from ..token_usage import ModelUsage

        assistant_turns = {key: 0 for key in windows}
        fields = (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
        )
        branch_parents: dict[str, str | None] = {}
        snapshots_by_branch: dict[
            str, list[tuple[datetime, dict, str]]
        ] = {}

        for pattern in self._transcript_globs():
            for path_str in glob.glob(pattern, recursive=True):
                jsonl_path = Path(path_str)

                current_model = "unknown"
                branch_id = str(jsonl_path)
                parent_id: str | None = None
                identity_seen = False
                snapshots: list[tuple[datetime, dict, str]] = []

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

                            kind = d.get("type")
                            payload = (
                                d.get("payload")
                                if isinstance(d.get("payload"), dict)
                                else {}
                            )

                            if kind == "session_meta":
                                # Forked rollouts can carry copied ancestor
                                # session_meta records after their own. The
                                # first one is the identity of this file.
                                if not identity_seen:
                                    rollout_id = payload.get("id")
                                    if isinstance(rollout_id, str) and rollout_id:
                                        branch_id = rollout_id
                                    else:
                                        root_id = payload.get("session_id")
                                        if isinstance(root_id, str) and root_id:
                                            branch_id = root_id
                                    parent = payload.get("parent_thread_id")
                                    if isinstance(parent, str) and parent:
                                        parent_id = parent
                                    identity_seen = True
                            elif kind == "turn_context":
                                m = payload.get("model")
                                if isinstance(m, str) and m:
                                    current_model = m
                            elif (
                                kind == "event_msg"
                                and payload.get("type") == "token_count"
                            ):
                                ts_raw = d.get("timestamp")
                                info = (
                                    payload.get("info")
                                    if isinstance(payload.get("info"), dict)
                                    else {}
                                )
                                ttu = (
                                    info.get("total_token_usage")
                                    if isinstance(info, dict)
                                    else None
                                )
                                if ts_raw and isinstance(ttu, dict):
                                    ts = _parse_ts(ts_raw)
                                    if ts:
                                        snapshots.append((ts, ttu, current_model))
                except OSError:
                    continue

                if not snapshots:
                    continue
                snapshots_by_branch.setdefault(branch_id, []).extend(snapshots)
                if branch_id not in branch_parents or parent_id is not None:
                    branch_parents[branch_id] = parent_id

        ordered_by_branch: dict[
            str, list[tuple[datetime, dict, str]]
        ] = {}
        for branch_id, snapshots in snapshots_by_branch.items():
            # Resumed rollouts can copy the last snapshot from their parent.
            # Deduplicate the exact cumulative point, preferring a known model
            # when one copy appeared before its turn_context was restored.
            unique: dict[tuple, tuple[datetime, dict, str]] = {}
            for ts, totals, model in snapshots:
                key = (
                    ts,
                    tuple(int(totals.get(field, 0) or 0) for field in fields),
                )
                existing = unique.get(key)
                if existing is None or (
                    existing[2] == "unknown" and model != "unknown"
                ):
                    unique[key] = (ts, totals, model)
            ordered = sorted(
                unique.values(),
                key=lambda item: (
                    item[0],
                    tuple(int(item[1].get(field, 0) or 0) for field in fields),
                ),
            )
            ordered_by_branch[branch_id] = ordered

        def _totals_key(snapshot: tuple[datetime, dict, str]) -> tuple[int, ...]:
            return tuple(
                max(0, int(snapshot[1].get(field, 0) or 0))
                for field in fields
            )

        def _inherited_prefix_len(
            branch_id: str,
            snapshots: list[tuple[datetime, dict, str]],
        ) -> int:
            """Longest leading child sequence copied from an ancestor.

            Parent logs can retain repeated no-change snapshots that a copied
            child prefix omits, so equality must be order-preserving rather
            than strictly contiguous. The first child value not found later in
            the ancestor marks the start of branch-local usage.
            """
            if not snapshots:
                return 0
            child_values = [_totals_key(snapshot) for snapshot in snapshots]
            best = 0
            ancestor_id = branch_parents.get(branch_id)
            visited = {branch_id}
            while ancestor_id and ancestor_id not in visited:
                visited.add(ancestor_id)
                ancestor = ordered_by_branch.get(ancestor_id, [])
                ancestor_values = [
                    _totals_key(snapshot) for snapshot in ancestor
                ]
                cursor = 0
                matched = 0
                for child_value in child_values:
                    found = next(
                        (
                            index
                            for index in range(cursor, len(ancestor_values))
                            if ancestor_values[index] == child_value
                        ),
                        None,
                    )
                    if found is None:
                        break
                    matched += 1
                    cursor = found + 1
                best = max(best, matched)
                ancestor_id = branch_parents.get(ancestor_id)
            return best

        for branch_id, ordered in ordered_by_branch.items():
            inherited = _inherited_prefix_len(branch_id, ordered)
            previous = {field: 0 for field in fields}
            for index, (ts, totals, model) in enumerate(ordered):
                current = {
                    field: max(0, int(totals.get(field, 0) or 0))
                    for field in fields
                }
                delta = {
                    field: max(0, current[field] - previous[field])
                    for field in fields
                }
                previous = current

                if index < inherited:
                    continue

                # The snapshot timestamp owns the increment since the previous
                # cumulative snapshot. Keeping every baseline is what excludes
                # pre-window usage when this one scan attributes deltas to
                # several adjacent windows.
                if not any(delta.values()):
                    continue

                inp = delta["input_tokens"]
                cached_inp = delta["cached_input_tokens"]
                cw = delta["cache_write_input_tokens"]
                out = delta["output_tokens"]

                uncached_inp = max(0, inp - cached_inp)

                for key, (since, until) in windows.items():
                    if ts < since or ts > until:
                        continue
                    mu = by_model_by_window[key].setdefault(
                        model, ModelUsage(model=model),
                    )
                    mu.turns += 1
                    mu.input_tokens += uncached_inp
                    mu.cache_read += cached_inp
                    mu.cache_creation += cw
                    mu.output_tokens += out
                    assistant_turns[key] += 1

        return assistant_turns

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

        for pattern in self._transcript_globs():
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

    def transcript_path(self, sess: SessionStat) -> Path | None:
        """Session id → rollout file, via a tree index built at most once.

        Codex file names are ``rollout-<iso-ts>-<session-id>.jsonl``, so the id
        alone does not give the path. Scanning the tree per session cost ~270ms
        × N — 35s on a 130-session week; one index amortizes that to a single
        walk for the whole run.
        """
        found = super().transcript_path(sess)
        if found is not None:
            return found
        if self._index is None:
            index: dict[str, Path] = {}
            for pattern in self._transcript_globs():
                for path_str in glob.glob(pattern, recursive=True):
                    path = Path(path_str)
                    stem = path.stem
                    index.setdefault(stem, path)
                    # `rollout-2026-07-22T22-12-59-<uuid>` → also key by the
                    # uuid, which is what `session_meta.session_id` reports.
                    m = _ROLLOUT_ID_RE.search(stem)
                    if m:
                        index.setdefault(m.group(1), path)
            self._index = index
        return self._index.get(sess.session_id)
