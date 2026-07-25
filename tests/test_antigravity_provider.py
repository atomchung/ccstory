"""Antigravity session parsing: text extraction, project attribution, transcript lookup."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ccstory.providers import TranscriptResolver, collect_multi_agent_sessions
from ccstory.providers.antigravity import AntigravityProvider, extract_user_request_text
from ccstory.token_usage import collect_usage


def _ts(minute: int) -> str:
    return datetime(2026, 7, 22, 12, minute, tzinfo=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _user(text: str, minute: int) -> dict:
    return {
        "step_index": minute,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": _ts(minute),
        "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>",
    }


def _planner(text: str, minute: int, thinking: str = "") -> dict:
    return {
        "step_index": minute,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": _ts(minute),
        "content": text,
        "thinking": thinking,
    }


@pytest.fixture
def antigravity_factory(tmp_home: Path):
    """Write an Antigravity transcript and companion SQLite DB into fake home. Returns transcript path."""

    def _make(
        session_id: str, records: list[dict], cwd: str | None = None
    ) -> Path:
        brain_dir = (
            tmp_home
            / ".gemini"
            / "antigravity"
            / "brain"
            / session_id
            / ".system_generated"
            / "logs"
        )
        brain_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = brain_dir / "transcript.jsonl"
        with transcript_path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        if cwd:
            conv_dir = tmp_home / ".gemini" / "antigravity" / "conversations"
            conv_dir.mkdir(parents=True, exist_ok=True)
            db_path = conv_dir / f"{session_id}.db"
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute(
                "CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB);"
            )
            blob = f"\x0a(file://{cwd}".encode("utf-8")
            c.execute(
                "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (blob,)
            )
            conn.commit()
            conn.close()

        return transcript_path

    return _make


SID = "076447e4-3f6c-42e0-ada8-e1ade38b7706"


class TestAntigravityParsing:
    def test_reads_user_turns_and_unwraps_user_request(self, antigravity_factory):
        path = antigravity_factory(
            SID,
            [
                _user("Add unit tests for Antigravity provider", 1),
                _planner("I will write the test file.", 2),
                _user("Run pytest to verify", 6),
            ],
            cwd="/Users/x/Side_project/ccstory",
        )
        stat = AntigravityProvider().parse_session(path)

        assert stat is not None
        assert stat.agent == "antigravity"
        assert stat.user_msg_count == 2
        assert stat.first_user_text == "Add unit tests for Antigravity provider"
        assert stat.project == "-Users-x-Side-project-ccstory"
        assert stat.path == path

    def test_unwraps_user_request_helper(self):
        raw = "<USER_REQUEST>\nFix bug in parser\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\ntime: 12:00\n</ADDITIONAL_METADATA>"
        assert extract_user_request_text(raw) == "Fix bug in parser"

    def test_default_project_when_no_cwd(self, antigravity_factory):
        path = antigravity_factory(
            SID,
            [_user("Hello", 1), _planner("Hi there", 2)],
        )
        stat = AntigravityProvider().parse_session(path)
        assert stat is not None
        assert stat.project == "antigravity"

    def test_gap_capping_for_active_time(self, antigravity_factory):
        # Turns at 0m, 10m (gap 10m -> capped to 5m=300s)
        path = antigravity_factory(
            SID,
            [_user("start", 0), _planner("working", 10)],
            cwd="/Users/x/demo",
        )
        stat = AntigravityProvider().parse_session(path)
        assert stat is not None
        assert stat.active_sec == 300


class TestAntigravityUsageCollection:
    def test_collects_token_usage(self, antigravity_factory):
        antigravity_factory(
            SID,
            [
                _user("A" * 400, 1),
                _planner("B" * 200, 2, thinking="C" * 100),
            ],
            cwd="/Users/x/demo",
        )
        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 7, 30, tzinfo=timezone.utc)

        report = collect_usage(since, until, agent="antigravity")
        assert report.assistant_turns == 1
        assert "gemini-3.6-flash" in report.by_model
        model_usage = report.by_model["gemini-3.6-flash"]
        assert model_usage.turns == 1
        assert model_usage.input_tokens > 0
        assert model_usage.output_tokens > 0


class TestAntigravityMultiAgentCollection:
    def test_collects_antigravity_and_multi_agent(self, antigravity_factory):
        antigravity_factory(
            SID,
            [_user("test multi agent", 1), _planner("ok", 3)],
            cwd="/Users/x/demo",
        )

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        stats = collect_multi_agent_sessions(since, agent="antigravity")
        assert len(stats) == 1
        assert stats[0].agent == "antigravity"

        all_stats = collect_multi_agent_sessions(since, agent="all")
        assert any(s.agent == "antigravity" for s in all_stats)


class TestAntigravityTranscriptResolution:
    def test_resolves_transcript_path(self, antigravity_factory):
        path = antigravity_factory(
            SID,
            [_user("query", 1), _planner("ans", 2)],
            cwd="/Users/x/demo",
        )
        stat = AntigravityProvider().parse_session(path)
        resolver = TranscriptResolver()
        assert resolver.path_for(stat) == path
