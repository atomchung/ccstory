"""Tests for ccstory.session_summarizer.

Focuses on what can be tested without invoking `claude -p`:
  - sqlite roundtrip (upsert/get/get_many/missing_ids)
  - first-user-message excerpt extraction + filtering
  - fallback narrative path (use_llm=False)
  - recap-DB import idempotency
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ccstory import session_summarizer as ss
from ccstory.session_summarizer import (
    _extract_excerpt,
    _fallback_narrative,
    get,
    get_many,
    import_from_claude_recap,
    missing_ids,
    summarize_session,
    upsert,
)

from tests.conftest import _ts, make_assistant_msg, make_user_msg, write_jsonl


class TestSqliteRoundtrip:
    def test_upsert_then_get(self, tmp_home: Path):
        upsert("sess1", "did a thing", "auto", project="myproj")
        s = get("sess1")
        assert s is not None
        assert s.session_id == "sess1"
        assert s.summary == "did a thing"
        assert s.source == "auto"
        assert s.project == "myproj"

    def test_upsert_replaces_existing(self, tmp_home: Path):
        upsert("sess1", "first", "fallback")
        upsert("sess1", "second", "auto")
        s = get("sess1")
        assert s.summary == "second"
        assert s.source == "auto"

    def test_upsert_empty_summary_is_noop(self, tmp_home: Path):
        upsert("sess1", "", "auto")
        assert get("sess1") is None

    def test_upsert_empty_id_is_noop(self, tmp_home: Path):
        upsert("", "x", "auto")

    def test_get_missing_returns_none(self, tmp_home: Path):
        assert get("nonexistent") is None

    def test_get_many(self, tmp_home: Path):
        upsert("a", "a-summary", "auto")
        upsert("b", "b-summary", "fallback")
        result = get_many(["a", "b", "missing"])
        assert set(result.keys()) == {"a", "b"}
        assert result["a"].summary == "a-summary"

    def test_get_many_empty_input(self, tmp_home: Path):
        assert get_many([]) == {}

    def test_missing_ids(self, tmp_home: Path):
        upsert("present", "x", "auto")
        miss = missing_ids(["present", "absent1", "absent2"])
        assert set(miss) == {"absent1", "absent2"}


class TestExtractExcerpt:
    def test_basic_excerpt(self, tmp_home: Path, jsonl_factory):
        records = [
            make_user_msg("First request", _ts(2026, 5, 10, 10, 0, 0)),
            make_assistant_msg("Answer one", _ts(2026, 5, 10, 10, 0, 5), "msg_1"),
            make_user_msg("Second request", _ts(2026, 5, 10, 10, 1, 0)),
            make_assistant_msg("Answer two", _ts(2026, 5, 10, 10, 1, 5), "msg_2"),
        ]
        path = jsonl_factory("-Users-alice-code-myapp", "sess", records)
        project, excerpt = _extract_excerpt(path)
        assert project == "-Users-alice-code-myapp"
        assert "First request" in excerpt
        assert "Second request" in excerpt
        assert "[USER 1]" in excerpt
        assert "[ASSISTANT END]" in excerpt

    def test_scheduled_task_filtered_out(self, tmp_home: Path, jsonl_factory):
        records = [
            make_user_msg(
                "<scheduled-task>run thing</scheduled-task>",
                _ts(2026, 5, 10, 10, 0, 0),
            ),
            make_user_msg("real text", _ts(2026, 5, 10, 10, 0, 30)),
            make_assistant_msg("ok", _ts(2026, 5, 10, 10, 1, 0), "msg_1"),
        ]
        path = jsonl_factory("-Users-alice-code-x", "sess", records)
        _, excerpt = _extract_excerpt(path)
        assert "scheduled-task" not in excerpt
        assert "real text" in excerpt

    def test_system_reminder_filtered_out(self, tmp_home: Path, jsonl_factory):
        records = [
            make_user_msg(
                "<system-reminder>internal</system-reminder>",
                _ts(2026, 5, 10, 10, 0, 0),
            ),
            make_user_msg("user content", _ts(2026, 5, 10, 10, 0, 30)),
        ]
        path = jsonl_factory("-Users-alice-code-x", "sess", records)
        _, excerpt = _extract_excerpt(path)
        assert "system-reminder" not in excerpt
        assert "user content" in excerpt

    def test_tool_result_filtered_out(self, tmp_home: Path, jsonl_factory):
        records = [
            make_user_msg(
                '{"tool_use_id": "abc", "type": "tool_result"}',
                _ts(2026, 5, 10, 10, 0, 0),
            ),
            make_user_msg("actual user", _ts(2026, 5, 10, 10, 0, 30)),
        ]
        path = jsonl_factory("-Users-alice-code-x", "sess", records)
        _, excerpt = _extract_excerpt(path)
        assert "tool_use_id" not in excerpt
        assert "actual user" in excerpt


class TestFallbackNarrative:
    def test_extracts_first_user_body(self):
        excerpt = "[USER 1]\nRefactor the auth flow\n\n[USER 2]\nMore stuff"
        assert _fallback_narrative(excerpt) == "Refactor the auth flow"

    def test_caps_at_120_chars(self):
        long_text = "x" * 200
        excerpt = f"[USER 1]\n{long_text}"
        assert len(_fallback_narrative(excerpt)) == 120

    def test_empty_input(self):
        assert _fallback_narrative("") == ""


class TestSummarizeSession:
    def test_use_llm_false_writes_fallback(self, tmp_home: Path, jsonl_factory):
        records = [
            make_user_msg("Build a CLI subcommand", _ts(2026, 5, 10, 10, 0, 0)),
            make_assistant_msg("ok", _ts(2026, 5, 10, 10, 0, 5), "msg_1"),
        ]
        path = jsonl_factory("-Users-alice-code-myapp", "sess-fallback", records)
        result = summarize_session("sess-fallback", path, use_llm=False)
        assert result is not None
        assert result.source == "fallback"
        assert "Build a CLI subcommand" in result.summary

    def test_cached_result_returned_immediately(self, tmp_home: Path, jsonl_factory):
        path = jsonl_factory(
            "-Users-alice-code-myapp",
            "sess-cached",
            [make_user_msg("X", _ts(2026, 5, 10, 10, 0, 0))],
        )
        upsert("sess-cached", "pre-existing summary", "auto", project="myproj")
        result = summarize_session("sess-cached", path, use_llm=False)
        assert result is not None
        assert result.summary == "pre-existing summary"
        assert result.source == "auto"  # cached entry untouched

    def test_empty_session_marks_skipped(self, tmp_home: Path, jsonl_factory):
        # File with no meaningful user content
        path = jsonl_factory("-Users-alice-code-x", "sess-empty", [])
        path.write_text("", encoding="utf-8")
        result = summarize_session("sess-empty", path, use_llm=False)
        assert result is not None
        assert result.source == "skipped"


class TestImportFromClaudeRecap:
    def test_missing_recap_db_returns_zero(self, tmp_home: Path):
        # RECAP_DB_PATH points to a non-existent file under tmp_home
        assert import_from_claude_recap() == 0

    def test_imports_rows_idempotently(self, tmp_home: Path):
        # Build a minimal recap DB at the expected path
        recap_path = tmp_home / ".claude" / "session_summaries.db"
        recap_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(recap_path))
        try:
            conn.execute(
                """CREATE TABLE session_summaries (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    source TEXT NOT NULL,
                    project TEXT,
                    created_at REAL NOT NULL,
                    task_slug TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO session_summaries VALUES (?, ?, ?, ?, ?, ?)",
                ("imported-1", "hello world", "auto", "proj", 1.0, "slug"),
            )
            conn.commit()
        finally:
            conn.close()

        first = import_from_claude_recap()
        assert first == 1
        assert get("imported-1").summary == "hello world"

        # Idempotent: a second run inserts nothing new
        second = import_from_claude_recap()
        assert second == 0
