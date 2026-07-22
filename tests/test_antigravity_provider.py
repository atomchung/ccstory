"""Tests for Google Antigravity session provider."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccstory.providers.antigravity import AntigravityProvider, extract_clean_user_text, extract_workspace_cwd
from ccstory.time_tracking import SessionStat, collect_sessions


def test_antigravity_text_and_cwd_extraction():
    content = (
        "<USER_REQUEST>\n"
        "Build multi-agent session collector for ccstory\n"
        "</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\n"
        "The current local time is: 2026-07-22T17:00:00+08:00.\n"
        "</ADDITIONAL_METADATA>\n"
        "<user_information>\n"
        "The user has 1 active workspaces, each defined by a URI:\n"
        "/Users/atomo/Side_project/ccstory -> atomchung/ccstory\n"
        "</user_information>"
    )

    clean_text = extract_clean_user_text(content)
    assert clean_text == "Build multi-agent session collector for ccstory"

    cwd = extract_workspace_cwd(content)
    assert cwd == "/Users/atomo/Side_project/ccstory"


def test_antigravity_provider_parse_session(tmp_path: Path):
    session_id = "test-session-1234"
    log_dir = tmp_path / session_id / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)
    transcript_file = log_dir / "transcript.jsonl"

    lines = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": "2026-07-22T10:00:00Z",
            "content": (
                "<USER_REQUEST>\nWrite unit tests for ccstory\n</USER_REQUEST>\n"
                "<user_information>\nThe user has 1 active workspaces:\n/Users/alice/projects/demo -> alice/demo\n</user_information>"
            ),
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-07-22T10:05:00Z",
            "content": "I will create test files.",
        },
    ]

    with transcript_file.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")

    provider = AntigravityProvider(brain_dir=tmp_path)
    assert provider.agent_name == "antigravity"

    stat = provider.parse_session(transcript_file)
    assert stat is not None
    assert stat.session_id == session_id
    assert stat.agent == "antigravity"
    assert stat.project == "demo"
    assert stat.cwd == "/Users/alice/projects/demo"
    assert stat.user_msg_count == 1
    assert stat.msg_count == 2
    assert "Write unit tests for ccstory" in stat.first_user_text
    assert stat.active_sec == 300  # 5 minutes gap


def test_collect_sessions_multi_agent(tmp_home: Path):
    # Setup mock Antigravity session
    brain_dir = tmp_home / ".gemini" / "antigravity" / "brain"
    session_id = "ag-session-5678"
    log_dir = brain_dir / session_id / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)
    transcript_file = log_dir / "transcript.jsonl"

    lines = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": "2026-07-22T12:00:00Z",
            "content": "<USER_REQUEST>\nRefactor multi-agent provider\n</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-07-22T12:02:00Z",
            "content": "Done.",
        },
    ]
    with transcript_file.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")

    since = datetime(2026, 7, 22, 0, 0, 0, tzinfo=timezone.utc)
    until = datetime(2026, 7, 23, 0, 0, 0, tzinfo=timezone.utc)

    # Filter only antigravity
    ag_sessions = collect_sessions(since, until, agent="antigravity")
    assert len(ag_sessions) == 1
    assert ag_sessions[0].agent == "antigravity"
    assert ag_sessions[0].session_id == session_id

    # Filter all
    all_sessions = collect_sessions(since, until, agent="all")
    assert len(all_sessions) >= 1
    ag_found = [s for s in all_sessions if s.agent == "antigravity"]
    assert len(ag_found) == 1
