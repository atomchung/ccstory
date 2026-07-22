"""Tests for OpenAI Codex session provider."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccstory.providers.codex import CodexProvider
from ccstory.time_tracking import collect_sessions


def test_codex_provider_parse_session(tmp_path: Path):
    session_id = "rollout-2026-06-03T08-30-06-019e8ae3"
    sessions_dir = tmp_path / "sessions" / "2026" / "06" / "03"
    sessions_dir.mkdir(parents=True)
    transcript_file = sessions_dir / f"{session_id}.jsonl"

    lines = [
        {
            "timestamp": "2026-06-03T08:30:00Z",
            "type": "session_meta",
            "payload": {
                "cwd": "/Users/alice/Side_project/myrepo",
            },
        },
        {
            "timestamp": "2026-06-03T08:31:00Z",
            "type": "user_message",
            "payload": {
                "role": "user",
                "content": "Refactor database migrations for codex",
            },
        },
        {
            "timestamp": "2026-06-03T08:35:00Z",
            "type": "assistant_message",
            "payload": {
                "role": "assistant",
                "content": "Completed migration refactoring.",
            },
        },
    ]

    with transcript_file.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")

    provider = CodexProvider(codex_dir=tmp_path)
    assert provider.agent_name == "codex"

    stat = provider.parse_session(transcript_file)
    assert stat is not None
    assert stat.session_id == session_id
    assert stat.agent == "codex"
    assert stat.project == "myrepo"
    assert stat.cwd == "/Users/alice/Side_project/myrepo"
    assert stat.user_msg_count == 1
    assert stat.msg_count == 2
    assert "Refactor database migrations for codex" in stat.first_user_text
    assert stat.active_sec == 300  # 5 minutes gap


def test_collect_sessions_codex(tmp_home: Path):
    codex_dir = tmp_home / ".codex"
    session_id = "rollout-codex-test-999"
    sessions_dir = codex_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    transcript_file = sessions_dir / f"{session_id}.jsonl"

    lines = [
        {
            "timestamp": "2026-07-22T10:00:00Z",
            "type": "user_message",
            "payload": {
                "cwd": "/Users/alice/code/testproj",
                "role": "user",
                "content": "Build multi-agent runner",
            },
        },
        {
            "timestamp": "2026-07-22T10:05:00Z",
            "type": "assistant_message",
            "payload": {
                "role": "assistant",
                "content": "Done.",
            },
        },
    ]
    with transcript_file.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")

    since = datetime(2026, 7, 22, 0, 0, 0, tzinfo=timezone.utc)
    until = datetime(2026, 7, 23, 0, 0, 0, tzinfo=timezone.utc)

    sessions = collect_sessions(since, until, agent="codex")
    assert len(sessions) == 1
    assert sessions[0].agent == "codex"
    assert sessions[0].session_id == session_id
