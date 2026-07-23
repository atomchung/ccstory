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
from datetime import datetime, timezone
from pathlib import Path

from ..time_tracking import GAP_CAP_SEC, SessionStat, _parse_ts
from .base import BaseAgentProvider

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


def _encode_project_dir(cwd: str) -> str:
    """Render a cwd the way Claude Code names its project folders.

    ``/Users/a/Side_project/ccstory`` → ``-Users-a-Side-project-ccstory``.
    Codex has no project folder of its own, so we mint the identifier Claude
    Code *would* have used: every downstream consumer (categorizer buckets,
    ``[projects]`` aliases, the layer-2 rollup) already runs
    ``normalize_project_name`` over that shape, so the same repo lands in the
    same bucket no matter which agent worked in it.
    """
    return (
        str(cwd)
        .replace("\\", "-")  # Windows transcripts record backslash paths
        .replace("/", "-")
        .replace(".", "-")
        .replace("_", "-")
    )


def _worktree_origin(cwd: str) -> str:
    """Fold a git worktree checkout back onto the repo it was created from.

    Codex parks its own worktrees at ``~/.codex/worktrees/<hash>/<repo>``,
    entirely outside the repo — so unlike Claude Code's in-repo
    ``.claude/worktrees/<name>`` (which ``normalize_project_name`` strips by
    pattern) the parent path is not recoverable from the string alone. It *is*
    recoverable from git: a linked worktree's ``.git`` is a file pointing at
    ``<repo>/.git/worktrees/<name>``.

    Returns the origin repo path, or ``cwd`` unchanged when this is not a
    linked worktree / the checkout has since been pruned.
    """
    if not cwd:
        return cwd
    pointer = Path(cwd) / ".git"
    try:
        if not pointer.is_file():
            return cwd
        line = pointer.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return cwd
    if not line.startswith("gitdir:"):
        return cwd
    gitdir = Path(line[len("gitdir:"):].strip())
    parts = gitdir.parts
    try:
        # <repo>/.git/worktrees/<name>  →  <repo>
        idx = len(parts) - 1 - parts[::-1].index("worktrees")
    except ValueError:
        return cwd
    if idx < 2 or parts[idx - 1] != ".git":
        return cwd
    return str(Path(*parts[: idx - 1]))


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

    def _transcript_globs(self) -> list[str]:
        return [
            str(self.codex_dir / "sessions" / "**" / "*.jsonl"),
            str(self.codex_dir / "archived_sessions" / "**" / "*.jsonl"),
        ]

    def parse_session(self, jsonl_path: Path) -> SessionStat | None:
        """Parse one Codex rollout transcript into a SessionStat."""
        timestamps: list[datetime] = []
        msg_count = 0
        user_msg_count = 0
        first_user_text = ""
        cwd = ""
        session_id = ""

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
                        text = strip_task_wrapper(
                            _codex_text(payload.get("message", ""))
                        )
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
            project=_encode_project_dir(_worktree_origin(cwd)) if cwd else "codex",
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
        )

    def collect_usage(
        self,
        since: datetime,
        until: datetime,
        by_model: dict,
    ) -> int:
        """Scan all Codex jsonl files and aggregate token usage in [since, until].

        Codex total_token_usage in a rollout file is cumulative across the entire
        thread root. Multiple rollout files (such as subagent spawns, resumes, or forks)
        belong to the same thread root and each carries total cumulative token usage from
        the thread's beginning. Summing token usage across all rollout files causes
        massive inflation (e.g. 7.3x overcounting).

        Filtering out subagent rollouts (via is_subagent_meta, which checks for
        parent_thread_id) provides exact 1-to-1 deduplication for threads. This filter is
        a deduplication step rather than an exclusion of costs, as subagent usage is
        already accumulated inside the parent thread's rollout.
        """
        from ..token_usage import ModelUsage

        assistant_turns = 0
        since_ts = since.timestamp()

        for pattern in self._transcript_globs():
            for path_str in glob.glob(pattern, recursive=True):
                jsonl_path = Path(path_str)
                try:
                    if jsonl_path.stat().st_mtime < since_ts:
                        continue
                except OSError:
                    continue

                is_subagent = False
                current_model = "unknown"
                last_token_count_record: tuple[datetime, dict, str] | None = None
                tc_count = 0

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
                                if is_subagent_meta(payload):
                                    is_subagent = True
                                    break
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
                                        last_token_count_record = (
                                            ts,
                                            ttu,
                                            current_model,
                                        )
                                        tc_count += 1
                except OSError:
                    continue

                if is_subagent or last_token_count_record is None:
                    continue

                ts, ttu, model = last_token_count_record
                if ts < since or ts > until:
                    continue

                inp = ttu.get("input_tokens", 0) or 0
                cached_inp = ttu.get("cached_input_tokens", 0) or 0
                cw = ttu.get("cache_write_input_tokens", 0) or 0
                out = ttu.get("output_tokens", 0) or 0

                uncached_inp = max(0, inp - cached_inp)

                mu = by_model.setdefault(model, ModelUsage(model=model))
                mu.turns += tc_count
                mu.input_tokens += uncached_inp
                mu.cache_read += cached_inp
                mu.cache_creation += cw
                mu.output_tokens += out
                assistant_turns += tc_count

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
