"""Shared pytest fixtures.

Every module in ccstory captures `Path.home() / ...` at import time, so test
isolation requires patching the resulting constants on each module. The
`tmp_home` fixture wires those up under a `tmp_path`-derived directory tree.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccstory import categorizer, session_summarizer, time_tracking, token_usage


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated fake home with redirected paths on every ccstory module."""
    home = tmp_path / "home"
    projects = home / ".claude" / "projects"
    projects.mkdir(parents=True)
    ccstory_dir = home / ".ccstory"
    ccstory_dir.mkdir()

    monkeypatch.setattr(time_tracking, "CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(token_usage, "PROJECTS_DIR", projects)
    monkeypatch.setattr(session_summarizer, "PROJECTS_DIR", projects)
    monkeypatch.setattr(session_summarizer, "DB_PATH", ccstory_dir / "cache.db")
    monkeypatch.setattr(
        session_summarizer,
        "RECAP_DB_PATH",
        home / ".claude" / "session_summaries.db",
    )
    monkeypatch.setattr(
        session_summarizer,
        "CLAUDE_MD_PATH",
        home / ".claude" / "CLAUDE.md",
    )
    # language_directive() is @lru_cache'd; flush so per-test CLAUDE.md edits take effect.
    session_summarizer.language_directive.cache_clear()
    monkeypatch.setattr(categorizer, "CONFIG_PATH", ccstory_dir / "config.toml")
    return home


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def _ts(year: int, month: int, day: int, hour: int = 12, minute: int = 0,
        second: int = 0) -> str:
    """Build a UTC ISO timestamp string in the format Claude Code uses."""
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def make_user_msg(text: str, ts: str) -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def make_assistant_msg(
    text: str,
    ts: str,
    msg_id: str,
    model: str = "claude-opus-4-7",
    *,
    input_tokens: int = 100,
    cache_creation: int = 0,
    cache_read: int = 0,
    output_tokens: int = 50,
) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "id": msg_id,
            "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output_tokens,
            },
        },
    }


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


@pytest.fixture
def jsonl_factory(tmp_home: Path):
    """Build a session jsonl under the fake CLAUDE_PROJECTS tree.

    Returns a callable `(project_dir, session_id, records) -> Path`.
    """
    projects = tmp_home / ".claude" / "projects"

    def _make(project_dir: str, session_id: str, records: list[dict]) -> Path:
        path = projects / project_dir / f"{session_id}.jsonl"
        return write_jsonl(path, records)

    return _make


__all__ = [
    "tmp_home",
    "fixtures_dir",
    "jsonl_factory",
    "make_user_msg",
    "make_assistant_msg",
    "write_jsonl",
    "_ts",
]
