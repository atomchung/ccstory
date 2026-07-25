"""Antigravity session provider tests: parsing, SQLite attribution, token usage, excerpts, and registry integration."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccstory import cli
from ccstory.providers import (
    TranscriptResolver,
    agent_label,
    collect_multi_agent_sessions,
    create_providers,
    list_providers,
)
from ccstory.providers.antigravity import (
    AntigravityProvider,
    extract_cwd_from_db,
    extract_user_request_text,
)
from ccstory.recap import _agent_data_roots
from ccstory.report import _agent_title
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
    def test_reads_user_turns_and_unwraps_user_request(self, antigravity_factory, tmp_home):
        path = antigravity_factory(
            SID,
            [
                _user("Add unit tests for Antigravity provider", 1),
                _planner("I will write the test file.", 2),
                _user("Run pytest to verify", 6),
            ],
            cwd="/Users/x/Side_project/ccstory",
        )
        provider = AntigravityProvider(antigravity_dir=tmp_home / ".gemini" / "antigravity")
        stat = provider.parse_session(path)

        assert stat is not None
        assert stat.agent == "antigravity"
        assert stat.user_msg_count == 2
        assert stat.first_user_text == "Add unit tests for Antigravity provider"
        assert stat.project == "-Users-x-Side-project-ccstory"
        assert stat.path == path

    def test_unwraps_user_request_helper(self):
        raw = "<USER_REQUEST>\nFix bug in parser\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\ntime: 12:00\n</ADDITIONAL_METADATA>"
        assert extract_user_request_text(raw) == "Fix bug in parser"

    def test_default_project_when_no_cwd(self, antigravity_factory, tmp_home):
        path = antigravity_factory(
            SID,
            [_user("Hello", 1), _planner("Hi there", 2)],
        )
        provider = AntigravityProvider(antigravity_dir=tmp_home / ".gemini" / "antigravity")
        stat = provider.parse_session(path)
        assert stat is not None
        assert stat.project == "antigravity"

    def test_gap_capping_for_active_time(self, antigravity_factory, tmp_home):
        # Turns at 0m, 10m (gap 10m -> capped to 5m=300s)
        path = antigravity_factory(
            SID,
            [_user("start", 0), _planner("working", 10)],
            cwd="/Users/x/demo",
        )
        provider = AntigravityProvider(antigravity_dir=tmp_home / ".gemini" / "antigravity")
        stat = provider.parse_session(path)
        assert stat is not None
        assert stat.active_sec == 300

    def test_sqlite_cwd_extraction(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB);")
        c.execute(
            "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
            (b"foo file:///Users/test/project bar",),
        )
        conn.commit()
        conn.close()

        extracted = extract_cwd_from_db(db_path)
        assert extracted == "/Users/test/project"


class TestAntigravityUsageCollection:
    def test_returns_zero_when_no_explicit_tokens(self, antigravity_factory, tmp_home):
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

        provider = AntigravityProvider(antigravity_dir=tmp_home / ".gemini" / "antigravity")
        by_model: dict = {}
        turns = provider.collect_usage(since, until, by_model)
        assert turns == 0
        assert len(by_model) == 0

    def test_collects_tokens_when_explicit_usage_present(self, antigravity_factory, tmp_home):
        rec = _planner("Hello", 2)
        rec["usage"] = {
            "model": "gemini-3.6-flash",
            "input_tokens": 100,
            "output_tokens": 50,
        }
        antigravity_factory(SID, [_user("Hi", 1), rec], cwd="/Users/x/demo")

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 7, 30, tzinfo=timezone.utc)

        provider = AntigravityProvider(antigravity_dir=tmp_home / ".gemini" / "antigravity")
        by_model: dict = {}
        turns = provider.collect_usage(since, until, by_model)
        assert turns == 1
        assert "gemini-3.6-flash" in by_model
        assert by_model["gemini-3.6-flash"].input_tokens == 100
        assert by_model["gemini-3.6-flash"].output_tokens == 50


class TestAntigravityMultiAgentCollection:
    def test_collects_antigravity_and_multi_agent(self, antigravity_factory, tmp_home):
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
    def test_resolves_transcript_path(self, antigravity_factory, tmp_home):
        path = antigravity_factory(
            SID,
            [_user("query", 1), _planner("ans", 2)],
            cwd="/Users/x/demo",
        )
        provider = AntigravityProvider(antigravity_dir=tmp_home / ".gemini" / "antigravity")
        stat = provider.parse_session(path)
        assert stat is not None
        resolver = TranscriptResolver()
        assert resolver.path_for(stat) == path


class TestAntigravityExcerptExtraction:
    def test_extract_excerpt(self, antigravity_factory, tmp_home):
        path = antigravity_factory(
            SID,
            [
                _user("First user request", 1),
                _planner("First planner response", 2),
            ],
            cwd="/Users/x/Side_project/ccstory",
        )
        provider = AntigravityProvider(antigravity_dir=tmp_home / ".gemini" / "antigravity")
        project, excerpt = provider.extract_excerpt(path)
        assert project == "-Users-x-Side-project-ccstory"
        assert "[USER 1]\nFirst user request" in excerpt
        assert "[ASSISTANT END]\nFirst planner response" in excerpt


class TestAntigravityRegistryContracts:
    def test_registry_integration(self, tmp_home, capsys):
        assert "antigravity" in list_providers()
        assert agent_label("antigravity") == "Google Antigravity"
        assert create_providers("antigravity")[0].agent_name == "antigravity"
        assert _agent_title("antigravity", "Recap") == "Google Antigravity Recap"

        data_roots = _agent_data_roots("antigravity")
        assert any(name == "antigravity" for name, _ in data_roots)

        with pytest.raises(SystemExit) as excinfo:
            cli.main(["week", "--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "antigravity" in out
