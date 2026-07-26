"""Google Antigravity session provider (``~/.gemini/antigravity/brain/**/transcript.jsonl``).

Antigravity sessions store context transcripts as JSONL step logs in brain
directories, with conversation metadata blobs stored in companion SQLite databases
under ``~/.gemini/antigravity/conversations/<session_id>.db``.
"""

from __future__ import annotations

import contextlib
import glob
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from ..time_tracking import GAP_CAP_SEC, SessionStat, _parse_ts
from .base import BaseAgentProvider
from .excerpts import build_excerpt, include_message
from .projects import encode_project_dir, worktree_origin


def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a varint starting at pos. Returns (val, new_pos).
    Raises ValueError on unexpected EOF or malformed varint (varint > 10 bytes).
    """
    res = 0
    shift = 0
    start = pos
    n = len(data)
    while pos < n:
        b = data[pos]
        pos += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, pos
        shift += 7
        if shift >= 70 or (pos - start) > 10:
            raise ValueError("Malformed varint")
    raise ValueError("Truncated varint")


def iter_protobuf_fields(data: bytes):
    """Yield (field_number, wire_type, val_or_bytes) for fields in data.

    Stops iteration cleanly if data is truncated, malformed, or has invalid wire type.
    """
    pos = 0
    n = len(data)
    while pos < n:
        try:
            key, pos = decode_varint(data, pos)
        except ValueError:
            break
        field_num = key >> 3
        wire_type = key & 0x07
        if field_num == 0:
            break
        if wire_type == 0:  # Varint
            try:
                val, pos = decode_varint(data, pos)
            except ValueError:
                break
            yield field_num, wire_type, val
        elif wire_type == 1:  # 64-bit
            if pos + 8 > n:
                break
            val_bytes = data[pos : pos + 8]
            pos += 8
            yield field_num, wire_type, val_bytes
        elif wire_type == 2:  # Length-delimited
            try:
                length, pos = decode_varint(data, pos)
            except ValueError:
                break
            if length < 0 or pos + length > n:
                break
            val_bytes = data[pos : pos + length]
            pos += length
            yield field_num, wire_type, val_bytes
        elif wire_type == 5:  # 32-bit
            if pos + 4 > n:
                break
            val_bytes = data[pos : pos + 4]
            pos += 4
            yield field_num, wire_type, val_bytes
        else:
            # Wire type 3, 4, or unsupported
            break


def _read_title_map(pb_path: Path) -> dict[str, str]:
    """Parse agyhub_summaries_proto.pb to map session_id -> native_title."""
    if not pb_path.exists():
        return {}
    try:
        data = pb_path.read_bytes()
    except OSError:
        return {}

    title_map: dict[str, str] = {}
    for f_num, w_type, entry_bytes in iter_protobuf_fields(data):
        if f_num == 1 and w_type == 2 and isinstance(entry_bytes, bytes):
            sid = ""
            title = ""
            for ef_num, ew_type, eval_bytes in iter_protobuf_fields(entry_bytes):
                if ef_num == 1 and ew_type == 2 and isinstance(eval_bytes, bytes):
                    sid = eval_bytes.decode("utf-8", errors="ignore").strip()
                elif ef_num == 2 and ew_type == 2 and isinstance(eval_bytes, bytes):
                    for sf_num, sw_type, sval_bytes in iter_protobuf_fields(eval_bytes):
                        if sf_num == 1 and sw_type == 2 and isinstance(sval_bytes, bytes):
                            title = sval_bytes.decode("utf-8", errors="ignore").strip()
            if sid and title:
                title_map[sid] = title
    return title_map


def parse_gen_metadata_blob(data: bytes) -> tuple[str, int, int] | None:
    """Decode gen_metadata data blob into (response_model, input_tokens, output_tokens).

    Outer field 1 = generation message.
    Generation field 19 = response_model (string).
    Generation field 4 = usage message.
    Usage field 2 = input_tokens (varint).
    Usage field 3 = output_tokens (varint).
    Fail closed (return None) if corrupt, missing fields, empty model, or negative tokens.
    """
    if not isinstance(data, bytes) or not data:
        return None

    gen_bytes: bytes | None = None
    for f_num, w_type, val in iter_protobuf_fields(data):
        if f_num == 1 and w_type == 2 and isinstance(val, bytes):
            gen_bytes = val
            break

    if gen_bytes is None:
        return None

    model: str | None = None
    inp: int | None = None
    out: int | None = None

    for f_num, w_type, val in iter_protobuf_fields(gen_bytes):
        if f_num == 19 and w_type == 2 and isinstance(val, bytes):
            m_str = val.decode("utf-8", errors="ignore").strip()
            if m_str:
                model = m_str
        elif f_num == 4 and w_type == 2 and isinstance(val, bytes):
            for uf_num, uw_type, uval in iter_protobuf_fields(val):
                if uf_num == 2 and uw_type == 0 and isinstance(uval, int):
                    if uval >= 0:
                        inp = uval
                elif uf_num == 3 and uw_type == 0 and isinstance(uval, int):
                    if uval >= 0:
                        out = uval

    if not model or inp is None or out is None:
        return None

    return model, inp, out


def extract_user_request_text(text: str) -> str:
    """Extract user prompt text, unwrapping ``<USER_REQUEST>`` and cleaning system envelopes."""
    stripped = text.strip()
    if "<USER_REQUEST>" in stripped:
        parts = stripped.split("<USER_REQUEST>", 1)[1]
        if "</USER_REQUEST>" in parts:
            stripped = parts.split("</USER_REQUEST>", 1)[0]
        else:
            stripped = parts

    # Safely remove well-formed system envelope blocks
    stripped = re.sub(
        r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>[\s\S]*?</\1>",
        "",
        stripped,
    )
    return stripped.strip()


def _is_subagent_transcript(jsonl_path: Path, first_content: str = "") -> bool:
    """True if transcript belongs to a spawned subagent rather than a top-level session."""
    if "subagents" in jsonl_path.parts:
        return True
    if first_content:
        stripped = first_content.strip().lower()
        if stripped.startswith("<identity>") or stripped.startswith("<subagents>"):
            return True
    return False


def _content_text(value: object) -> str:
    """Text payload from the string or text-part shapes seen in step logs."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            part["text"]
            for part in value
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _exact_token_count(value: object) -> int | None:
    """A real non-negative JSON integer, never a heuristic conversion."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _is_user_step(stype: object, source: object) -> bool:
    """True only for explicit user-authored steps, never system-injected input."""
    return (stype == "USER_INPUT" and source in (None, "USER_EXPLICIT")) or (
        stype is None and source == "USER_EXPLICIT"
    )


def _is_assistant_step(stype: object, source: object) -> bool:
    """True only for narrative model responses, never tool/model events."""
    return (
        stype == "PLANNER_RESPONSE" and source in (None, "MODEL")
    ) or (stype is None and source == "MODEL")


def extract_cwd_from_db(db_path: Path) -> str:
    """Extract launch CWD from an Antigravity conversation database if present."""
    if not db_path.exists():
        return ""
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            c = conn.cursor()
            rows = c.execute("SELECT data FROM trajectory_metadata_blob;").fetchall()
            for row in rows:
                data = row[0]
                if isinstance(data, bytes):
                    matches = re.findall(rb'file://(/[^\x00-\x1f"]+)', data)
                    if matches:
                        encoded = matches[0].decode(
                            "utf-8", errors="ignore"
                        ).strip()
                        return unquote(encoded)
    except (sqlite3.Error, OSError):
        pass
    return ""


def _extract_session_id(jsonl_path: Path) -> str:
    parts = jsonl_path.parts
    if "brain" in parts:
        idx = parts.index("brain")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return jsonl_path.stem


class AntigravityProvider(BaseAgentProvider):
    """Session provider for Google Antigravity."""

    def __init__(self, antigravity_dir: Path | None = None) -> None:
        self._antigravity_dir = antigravity_dir
        self._title_map: dict[str, str] | None = None

    def _get_title_map(self) -> dict[str, str]:
        if self._title_map is None:
            pb_path = self.antigravity_dir / "agyhub_summaries_proto.pb"
            self._title_map = _read_title_map(pb_path)
        return self._title_map

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

    def extract_excerpt(self, path: Path) -> tuple[str, str]:
        """Return ``(project, bounded user-facing excerpt)`` for one transcript."""
        user_msgs: list[str] = []
        assistant_msgs: list[str] = []

        session_id = _extract_session_id(path)
        db_path = self.antigravity_dir / "conversations" / f"{session_id}.db"
        cwd = extract_cwd_from_db(db_path)
        project = (
            encode_project_dir(worktree_origin(cwd)) if cwd else "antigravity"
        )

        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
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
                    content = _content_text(d.get("content"))

                    is_user = _is_user_step(stype, source)
                    is_assistant = _is_assistant_step(stype, source)
                    if is_user:
                        text = extract_user_request_text(content)
                        if include_message(text):
                            user_msgs.append(text[:500])
                    elif is_assistant:
                        text = content.strip()
                        if include_message(text):
                            assistant_msgs.append(text[:500])
        except OSError:
            pass

        return project, build_excerpt(user_msgs, assistant_msgs)

    def _get_child_session_ids(self) -> set[str]:
        """Discover child subagent conversation IDs referenced by INVOKE_SUBAGENT records."""
        all_paths = glob.glob(self._transcript_glob())
        all_session_ids = {_extract_session_id(Path(p)) for p in all_paths}
        child_ids: set[str] = set()

        for path_str in all_paths:
            path = Path(path_str)
            curr_sid = _extract_session_id(path)
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if d.get("type") == "INVOKE_SUBAGENT":
                            record_str = json.dumps(d)
                            for sid in all_session_ids:
                                if sid != curr_sid and sid in record_str:
                                    child_ids.add(sid)
            except OSError:
                continue

        return child_ids

    def _is_child_session(
        self, jsonl_path: Path, child_ids: set[str] | None = None
    ) -> bool:
        if _is_subagent_transcript(jsonl_path):
            return True
        sid = _extract_session_id(jsonl_path)
        if child_ids is None:
            child_ids = self._get_child_session_ids()
        return sid in child_ids

    def parse_session(
        self, jsonl_path: Path, child_ids: set[str] | None = None
    ) -> SessionStat | None:
        """Parse one Antigravity transcript.jsonl into a SessionStat."""
        if self._is_child_session(jsonl_path, child_ids=child_ids):
            return None

        timestamps: list[datetime] = []
        msg_count = 0
        user_msg_count = 0
        first_user_text = ""

        session_id = _extract_session_id(jsonl_path)
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
                    content = _content_text(d.get("content"))

                    ts = _parse_ts(d.get("created_at"))
                    if ts:
                        timestamps.append(ts)

                    is_user = _is_user_step(stype, source)
                    is_assistant = _is_assistant_step(stype, source)
                    if is_user or is_assistant:
                        msg_count += 1

                    if is_user:
                        text = extract_user_request_text(content)
                        if include_message(text):
                            user_msg_count += 1
                            if not first_user_text:
                                first_user_text = text[:200]
                                if _is_subagent_transcript(jsonl_path, content):
                                    return None
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
            encode_project_dir(worktree_origin(cwd)) if cwd else "antigravity"
        )

        native_title = self._get_title_map().get(session_id, "")

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
            native_title=native_title,
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
        """Scan all Antigravity step logs and aggregate token usage in [since, until]."""
        from ..token_usage import ModelUsage

        assistant_turns = 0
        child_ids = self._get_child_session_ids()

        for path_str in glob.glob(self._transcript_glob()):
            jsonl_path = Path(path_str)
            if self._is_child_session(jsonl_path, child_ids):
                continue

            session_id = _extract_session_id(jsonl_path)
            step_timestamps: dict[int, datetime] = {}
            transcript_steps: list[dict] = []

            logs_dir = jsonl_path.parent
            full_path = logs_dir / "transcript_full.jsonl"
            paths_to_read = [full_path, jsonl_path] if full_path.exists() and full_path != jsonl_path else [jsonl_path]

            for p in paths_to_read:
                try:
                    with p.open("r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                d = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            step_idx = d.get("step_index")
                            ts_raw = d.get("created_at")
                            if isinstance(step_idx, int) and ts_raw:
                                ts = _parse_ts(ts_raw)
                                if ts and step_idx not in step_timestamps:
                                    step_timestamps[step_idx] = ts

                            if p == jsonl_path:
                                transcript_steps.append(d)
                except OSError:
                    continue

            db_counted_steps: set[int] = set()
            db_path = self.antigravity_dir / "conversations" / f"{session_id}.db"

            if db_path.exists():
                try:
                    uri = f"{db_path.resolve().as_uri()}?mode=ro"
                    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
                        c = conn.cursor()
                        rows = c.execute("SELECT idx, data FROM gen_metadata;").fetchall()
                        for row in rows:
                            idx, blob = row[0], row[1]
                            if not isinstance(idx, int) or idx not in step_timestamps:
                                continue

                            ts = step_timestamps[idx]
                            parsed = parse_gen_metadata_blob(blob)
                            if parsed is None:
                                continue

                            db_counted_steps.add(idx)
                            if since <= ts <= until:
                                model, inp, out = parsed
                                mu = by_model.setdefault(model, ModelUsage(model=model))
                                mu.turns += 1
                                mu.input_tokens += inp
                                mu.output_tokens += out
                                assistant_turns += 1
                except (sqlite3.Error, OSError):
                    pass

            for d in transcript_steps:
                stype = d.get("type")
                source = d.get("source")
                if not _is_assistant_step(stype, source):
                    continue

                ts_raw = d.get("created_at")
                if not ts_raw:
                    continue
                ts = _parse_ts(ts_raw)
                if not ts or not (since <= ts <= until):
                    continue

                step_idx = d.get("step_index")
                if isinstance(step_idx, int) and step_idx in db_counted_steps:
                    continue

                usage = d.get("usage")
                inp_raw = None
                out_raw = None
                cache_read_raw = None
                model_raw = d.get("model")

                if isinstance(usage, dict):
                    if "input_tokens" in usage:
                        inp_raw = usage["input_tokens"]
                    elif "prompt_tokens" in usage:
                        inp_raw = usage["prompt_tokens"]

                    if "output_tokens" in usage:
                        out_raw = usage["output_tokens"]
                    elif "completion_tokens" in usage:
                        out_raw = usage["completion_tokens"]

                    if "cache_read_input_tokens" in usage:
                        cache_read_raw = usage["cache_read_input_tokens"]
                    elif "cached_input_tokens" in usage:
                        cache_read_raw = usage["cached_input_tokens"]
                    elif "cached_content_token_count" in usage:
                        cache_read_raw = usage["cached_content_token_count"]

                    if isinstance(usage.get("model"), str) and usage["model"].strip():
                        model_raw = usage["model"]
                else:
                    if "input_tokens" in d:
                        inp_raw = d["input_tokens"]
                    elif "prompt_tokens" in d:
                        inp_raw = d["prompt_tokens"]

                    if "output_tokens" in d:
                        out_raw = d["output_tokens"]
                    elif "completion_tokens" in d:
                        out_raw = d["completion_tokens"]

                    if "cache_read_input_tokens" in d:
                        cache_read_raw = d["cache_read_input_tokens"]
                    elif "cached_input_tokens" in d:
                        cache_read_raw = d["cached_input_tokens"]
                    elif "cached_content_token_count" in d:
                        cache_read_raw = d["cached_content_token_count"]

                inp = _exact_token_count(inp_raw)
                out = _exact_token_count(out_raw)
                model = (
                    model_raw.strip()
                    if isinstance(model_raw, str) and model_raw.strip()
                    else None
                )

                if inp is None or out is None or model is None:
                    continue

                cache_read_val = _exact_token_count(cache_read_raw)
                cache_read = cache_read_val if cache_read_val is not None else 0

                mu = by_model.setdefault(model, ModelUsage(model=model))
                mu.turns += 1
                mu.input_tokens += inp
                mu.output_tokens += out
                mu.cache_read += cache_read
                assistant_turns += 1

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
        child_ids = self._get_child_session_ids()

        for path_str in glob.glob(self._transcript_glob()):
            path = Path(path_str)
            try:
                if path.stat().st_mtime < since_ts:
                    continue
            except OSError:
                continue

            s = self.parse_session(path, child_ids=child_ids)
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
