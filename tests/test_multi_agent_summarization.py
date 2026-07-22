"""Tests for multi-agent transcript excerpt extraction & narrative pipeline integration."""

from __future__ import annotations

import json
from pathlib import Path

from ccstory.session_summarizer import _extract_excerpt, _sanitize_antigravity_text


def test_extract_excerpt_antigravity_format(tmp_path: Path):
    log_dir = tmp_path / "antigravity" / "brain" / "sess-ag-1" / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)
    transcript_file = log_dir / "transcript.jsonl"

    lines = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": "2026-07-22T10:00:00Z",
            "content": (
                "<USER_REQUEST>\nImplement multi-agent recap synthesis\n</USER_REQUEST>\n"
                "<user_information>\nThe user has 1 active workspaces:\n/Users/alice/projects/demo -> alice/demo\n</user_information>"
            ),
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-07-22T10:05:00Z",
            "content": "Added multi-agent recap synthesis code.",
        },
    ]

    with transcript_file.open("w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item) + "\n")

    proj, excerpt = _extract_excerpt(transcript_file)
    assert proj == "demo"
    assert "[USER 1]\nImplement multi-agent recap synthesis" in excerpt
    assert "[ASSISTANT END]\nAdded multi-agent recap synthesis code." in excerpt


def test_extract_excerpt_codex_format(tmp_path: Path):
    sess_dir = tmp_path / "codex" / "sessions"
    sess_dir.mkdir(parents=True)
    transcript_file = sess_dir / "sess-codex-1.jsonl"

    lines = [
        {
            "timestamp": "2026-07-22T10:00:00Z",
            "payload": {
                "role": "user",
                "cwd": "/Users/alice/projects/my-app",
                "content": [{"type": "text", "text": "Refactor time tracking module for multi-agent support"}],
            },
        },
        {
            "timestamp": "2026-07-22T10:02:00Z",
            "payload": {
                "role": "assistant",
                "content": "Updated time_tracking.py with multi-agent providers.",
            },
        },
    ]

    with transcript_file.open("w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item) + "\n")

    proj, excerpt = _extract_excerpt(transcript_file)
    assert proj == "my-app"
    assert "[USER 1]\nRefactor time tracking module for multi-agent support" in excerpt
    assert "[ASSISTANT END]\nUpdated time_tracking.py with multi-agent providers." in excerpt


def test_sanitize_antigravity_text_without_user_request():
    raw = (
        "<user_information>Some secret prompt context</user_information>\n"
        "<SYSTEM_MESSAGE>Internal system message</SYSTEM_MESSAGE>\n"
        "Fix syntax error in categorizer.py"
    )
    cleaned = _sanitize_antigravity_text(raw)
    assert cleaned == "Fix syntax error in categorizer.py"
    assert "<user_information>" not in cleaned


def test_extract_excerpt_repeated_short_prompts(tmp_path: Path):
    sess_dir = tmp_path / "claude" / "projects" / "myrepo"
    sess_dir.mkdir(parents=True)
    transcript_file = sess_dir / "sess-repeat.jsonl"

    lines = [
        {"type": "user", "message": {"content": "run tests"}},
        {"type": "assistant", "message": {"content": "Running pytest..."}},
        {"type": "user", "message": {"content": "fix error"}},
        {"type": "assistant", "message": {"content": "Fixed bug in time_tracking.py"}},
        {"type": "user", "message": {"content": "run tests"}},
        {"type": "assistant", "message": {"content": "Tests pass!"}},
        {"type": "user", "message": {"content": "run tests"}},
        {"type": "assistant", "message": {"content": "Tests still pass!"}},
    ]

    with transcript_file.open("w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item) + "\n")

    proj, excerpt = _extract_excerpt(transcript_file)
    assert proj == "myrepo"
    assert "[USER 1]\nrun tests" in excerpt
    assert "[USER LATE]\nrun tests" in excerpt
