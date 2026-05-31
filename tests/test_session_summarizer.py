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
    OVERALL_KEY,
    get,
    get_many,
    get_overall_narrative,
    import_from_claude_recap,
    language_directive,
    missing_ids,
    summarize_session,
    synthesize_overall_for_period,
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


class TestRetroactiveRefresh:
    """Retroactive upgrade/refresh of cached narratives (the freeze fix).

    `--llm-narrative` must be able to upgrade a cached `fallback` to `auto`
    and refresh a stale `auto`, while never re-burning an up-to-date one and
    never downgrading a good summary on a transient `claude -p` failure.
    """

    def _jsonl(self, jsonl_factory):
        return jsonl_factory(
            "-Users-alice-code-myapp", "sess-r",
            [make_user_msg("Refactor the auth flow", _ts(2026, 5, 10, 10, 0, 0))],
        )

    def test_use_llm_upgrades_fallback_to_auto(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        path = self._jsonl(jsonl_factory)
        upsert("sess-r", "stale fallback line", "fallback", project="myapp")
        monkeypatch.setattr(ss, "summarize_via_claude_p",
                            lambda *a, **k: "polished outcome")
        result = summarize_session("sess-r", path, use_llm=True)
        assert result.source == "auto"
        assert result.summary == "polished outcome"
        assert result.prompt_version == ss.PROMPT_VERSION

    def test_current_auto_not_reburned(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        path = self._jsonl(jsonl_factory)
        upsert("sess-r", "good summary", "auto", project="myapp",
               prompt_version=ss.PROMPT_VERSION)

        def _boom(*a, **k):
            raise AssertionError("claude -p must not run for an up-to-date auto row")

        monkeypatch.setattr(ss, "summarize_via_claude_p", _boom)
        result = summarize_session("sess-r", path, use_llm=True)
        assert result.source == "auto"
        assert result.summary == "good summary"

    def test_stale_auto_refreshed(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        path = self._jsonl(jsonl_factory)
        upsert("sess-r", "old-model summary", "auto", project="myapp",
               prompt_version=ss.PROMPT_VERSION - 1)
        monkeypatch.setattr(ss, "summarize_via_claude_p",
                            lambda *a, **k: "new-model summary")
        result = summarize_session("sess-r", path, use_llm=True)
        assert result.summary == "new-model summary"
        assert result.prompt_version == ss.PROMPT_VERSION

    def test_force_regenerates_current_auto(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        path = self._jsonl(jsonl_factory)
        upsert("sess-r", "good summary", "auto", project="myapp",
               prompt_version=ss.PROMPT_VERSION)
        monkeypatch.setattr(ss, "summarize_via_claude_p",
                            lambda *a, **k: "forced refresh")
        result = summarize_session("sess-r", path, use_llm=True, force=True)
        assert result.summary == "forced refresh"

    def test_failed_refresh_keeps_existing_auto(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        # Non-destructive: a claude -p failure must not downgrade a good
        # auto summary to a fallback.
        path = self._jsonl(jsonl_factory)
        upsert("sess-r", "good summary", "auto", project="myapp",
               prompt_version=ss.PROMPT_VERSION - 1)
        monkeypatch.setattr(ss, "summarize_via_claude_p", lambda *a, **k: None)
        result = summarize_session("sess-r", path, use_llm=True)
        assert result.source == "auto"
        assert result.summary == "good summary"

    def test_skipped_not_retried(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        path = self._jsonl(jsonl_factory)
        upsert("sess-r", "(no meaningful conversation)", "skipped", project="myapp")

        def _boom(*a, **k):
            raise AssertionError("claude -p must not run for a skipped row")

        monkeypatch.setattr(ss, "summarize_via_claude_p", _boom)
        result = summarize_session("sess-r", path, use_llm=True)
        assert result.source == "skipped"

    def test_use_llm_false_never_upgrades(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        path = self._jsonl(jsonl_factory)
        upsert("sess-r", "fallback line", "fallback", project="myapp")

        def _boom(*a, **k):
            raise AssertionError("claude -p must not run without use_llm")

        monkeypatch.setattr(ss, "summarize_via_claude_p", _boom)
        result = summarize_session("sess-r", path, use_llm=False)
        assert result.source == "fallback"


class TestNeedsLlm:
    def test_matrix(self):
        SS = ss.SessionSummary
        assert ss._needs_llm(None) is True
        assert ss._needs_llm(SS("i", "s", "skipped")) is False
        assert ss._needs_llm(SS("i", "s", "fallback")) is True
        cur = SS("i", "s", "auto", prompt_version=ss.PROMPT_VERSION)
        assert ss._needs_llm(cur) is False
        assert ss._needs_llm(cur, force=True) is True
        stale = SS("i", "s", "auto", prompt_version=ss.PROMPT_VERSION - 1)
        assert ss._needs_llm(stale) is True
        # legacy NULL prompt_version coerces to 0 → stale
        assert ss._needs_llm(SS("i", "s", "auto", prompt_version=None)) is True


class TestPromptVersionMigration:
    def test_legacy_rows_stamped_current(self, tmp_home: Path):
        # Simulate a pre-feature DB: session_summaries without prompt_version.
        ss.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(str(ss.DB_PATH))
        raw.execute(
            """CREATE TABLE session_summaries (
                   session_id TEXT PRIMARY KEY, summary TEXT NOT NULL,
                   source TEXT NOT NULL, project TEXT, created_at REAL NOT NULL)"""
        )
        raw.execute(
            "INSERT INTO session_summaries VALUES (?, ?, ?, ?, ?)",
            ("legacy", "old summary", "auto", "proj", 1.0),
        )
        raw.commit()
        raw.close()
        # First ccstory connect must add the column and stamp the legacy row
        # as *current* (not 0), so adopting the feature doesn't silently
        # re-burn the existing cache.
        row = get("legacy")
        assert row is not None
        assert row.prompt_version == ss.PROMPT_VERSION
        assert ss._needs_llm(row) is False


class TestLanguageDirective:
    def test_missing_claude_md_falls_back_to_english(self, tmp_home: Path):
        # No CLAUDE.md written → expect the English fallback line.
        ss.language_directive.cache_clear()
        assert language_directive() == "Respond in English."

    def test_pastes_claude_md_excerpt(self, tmp_home: Path):
        md_path = tmp_home / ".claude" / "CLAUDE.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(
            "# 個人偏好\nAlways respond in Traditional Chinese.\n",
            encoding="utf-8",
        )
        ss.language_directive.cache_clear()
        directive = language_directive()
        assert "--- CLAUDE.md ---" in directive
        assert "Traditional Chinese" in directive
        assert "個人偏好" in directive

    def test_truncates_long_claude_md(self, tmp_home: Path):
        md_path = tmp_home / ".claude" / "CLAUDE.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("x" * 5000, encoding="utf-8")
        ss.language_directive.cache_clear()
        directive = language_directive()
        # Body between the markers should be capped at _CLAUDE_MD_MAX_CHARS.
        body = directive.split("--- CLAUDE.md ---\n", 1)[1].split("\n--- end ---", 1)[0]
        assert len(body) <= ss._CLAUDE_MD_MAX_CHARS

    def test_settings_json_language_used_when_no_claude_md(
        self, tmp_home: Path,
    ):
        """Issue #55: users who set language via Claude Code's /config UI
        (which writes settings.json `language`) should get that language
        respected even without a global CLAUDE.md."""
        settings = tmp_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            '{"language": "Traditional Chinese", "theme": "dark"}',
            encoding="utf-8",
        )
        ss.language_directive.cache_clear()
        directive = language_directive()
        assert directive == (
            "Respond in Traditional Chinese. "
            "Keep the same length / format limits regardless of language."
        )

    def test_claude_md_wins_over_settings_json(self, tmp_home: Path):
        """When both exist, CLAUDE.md is canonical (it may contain more
        than just a language hint, so don't downgrade to a single line)."""
        md_path = tmp_home / ".claude" / "CLAUDE.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("Respond in Japanese.\n", encoding="utf-8")
        settings = tmp_home / ".claude" / "settings.json"
        settings.write_text('{"language": "Spanish"}', encoding="utf-8")
        ss.language_directive.cache_clear()
        directive = language_directive()
        assert "--- CLAUDE.md ---" in directive
        assert "Japanese" in directive
        assert "Spanish" not in directive

    def test_malformed_settings_json_falls_back_to_english(
        self, tmp_home: Path,
    ):
        # Broken JSON should degrade silently to English — not crash.
        settings = tmp_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("{not valid json", encoding="utf-8")
        ss.language_directive.cache_clear()
        assert language_directive() == "Respond in English."

    def test_settings_json_without_language_field_falls_back(
        self, tmp_home: Path,
    ):
        # settings.json exists but no `language` key → English.
        settings = tmp_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('{"theme": "dark"}', encoding="utf-8")
        ss.language_directive.cache_clear()
        assert language_directive() == "Respond in English."


class TestSynthesizeOverallForPeriod:
    def test_empty_input_returns_none(self, tmp_home: Path):
        out = synthesize_overall_for_period(
            period_key="2026-05",
            category_hours=[],
            sessions_by_category={},
        )
        assert out is None

    def test_cache_hit_skips_claude_call(self, tmp_home: Path, monkeypatch):
        # Pre-seed the period_aggregates table with an overall narrative.
        from ccstory.session_summarizer import _connect
        import time as _time
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO period_aggregates
                   (period_key, category, summary, session_ids, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                ("2026-05", OVERALL_KEY, "cached prose", "sess-a,sess-b", _time.time()),
            )
            conn.commit()
        finally:
            conn.close()

        # If claude_bin_available is reached we'd return None — assert we don't
        # get there. Patch to raise so a cache miss would surface as an error.
        def boom():
            raise AssertionError("claude_bin_available should not be called on cache hit")
        monkeypatch.setattr(ss, "claude_bin_available", boom)

        out = synthesize_overall_for_period(
            period_key="2026-05",
            category_hours=[("coding", 2.0), ("ops", 1.0)],
            sessions_by_category={
                "coding": [("sess-a", "did A")],
                "ops": [("sess-b", "did B")],
            },
        )
        assert out == "cached prose"

    def test_cache_invalidates_when_session_ids_change(self, tmp_home: Path, monkeypatch):
        from ccstory.session_summarizer import _connect
        import time as _time
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO period_aggregates
                   (period_key, category, summary, session_ids, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                ("2026-05", OVERALL_KEY, "stale prose", "sess-a", _time.time()),
            )
            conn.commit()
        finally:
            conn.close()

        # Different session set → cache should miss and try to call claude.
        # We stub claude as unavailable so we get None (instead of running it),
        # which proves we *attempted* a refresh.
        monkeypatch.setattr(ss, "claude_bin_available", lambda: False)
        out = synthesize_overall_for_period(
            period_key="2026-05",
            category_hours=[("coding", 2.0)],
            sessions_by_category={"coding": [("sess-a", "A"), ("sess-c", "C")]},
        )
        assert out is None

    def test_get_overall_narrative_roundtrip(self, tmp_home: Path):
        from ccstory.session_summarizer import _connect
        import time as _time
        assert get_overall_narrative("2026-05") is None
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO period_aggregates
                   (period_key, category, summary, session_ids, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                ("2026-05", OVERALL_KEY, "overall text", "s1", _time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        assert get_overall_narrative("2026-05") == "overall text"


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
