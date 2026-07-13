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

import pytest

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
    def test_combines_first_and_last_user_body(self):
        excerpt = "[USER 1]\nRefactor the auth flow\n\n[USER 2]\nMore stuff"
        assert _fallback_narrative(excerpt) == "Refactor the auth flow → More stuff"

    def test_single_message_caps_at_120_chars(self):
        long_text = "x" * 200
        excerpt = f"[USER 1]\n{long_text}"
        assert len(_fallback_narrative(excerpt)) == 120

    def test_multi_message_uses_excerpt_endpoints_and_caps_each(self):
        first = "first " * 20
        last = "last " * 20
        excerpt = (
            f"[USER 1]\n{first}\n\n"
            "[USER 2]\nmiddle one\n\n"
            "[USER 3]\nmiddle two\n\n"
            "...\n\n"
            f"[USER LATE]\n{last}\n\n"
            "[ASSISTANT END]\ndone"
        )
        result = _fallback_narrative(excerpt)
        start, end = result.split(" → ")
        assert start == first.strip()[:60]
        assert end == last.strip()[:60]

    def test_collapses_multiline_user_messages(self):
        excerpt = (
            "[USER 1]\nBuild the CLI\nwith JSON output\n\n"
            "[USER LATE]\nShip it\nafter tests"
        )
        assert _fallback_narrative(excerpt) == (
            "Build the CLI with JSON output → Ship it after tests"
        )

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


class TestCacheSchemaMigrations:
    def test_fresh_db_reaches_current_version(self, tmp_home: Path):
        conn = ss._connect()
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == ss.CACHE_SCHEMA_VERSION
            for table in (
                "period_aggregates",
                "comparison_narratives",
                "session_content_buckets",
            ):
                assert "input_fingerprint" in ss._table_columns(conn, table)
        finally:
            conn.close()

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
        conn = sqlite3.connect(str(ss.DB_PATH))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == (
                ss.CACHE_SCHEMA_VERSION
            )
        finally:
            conn.close()

    def test_unversioned_current_db_preserves_every_cache_family(
        self, tmp_home: Path,
    ):
        raw = sqlite3.connect(str(ss.DB_PATH))
        ss._migration_1_baseline(raw)
        raw.execute(
            "INSERT INTO period_aggregates VALUES (?, ?, ?, ?, ?)",
            ("p", "coding", "aggregate", "s1", 1.0),
        )
        raw.execute(
            "INSERT INTO comparison_narratives VALUES (?, ?, ?, ?, ?)",
            ("cur", "prev", "sig", "comparison", 1.0),
        )
        raw.execute(
            "INSERT INTO session_content_buckets VALUES (?, ?, ?)",
            ("s1", "coding", 1.0),
        )
        raw.commit()
        raw.close()

        migrated = ss._connect()
        try:
            assert migrated.execute(
                "SELECT summary, input_fingerprint FROM period_aggregates"
            ).fetchone() == ("aggregate", "")
            assert migrated.execute(
                "SELECT narrative, input_fingerprint "
                "FROM comparison_narratives"
            ).fetchone() == ("comparison", "")
            assert migrated.execute(
                "SELECT bucket, input_fingerprint "
                "FROM session_content_buckets"
            ).fetchone() == ("coding", "")
        finally:
            migrated.close()

    def test_already_current_db_skips_migrations(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        upsert("kept", "preserved summary", "auto")

        def _boom(_conn):
            raise AssertionError("current schema must not rerun migrations")

        monkeypatch.setattr(ss, "_MIGRATIONS", (_boom, _boom))
        assert get("kept").summary == "preserved summary"

    def test_each_migration_is_transactional(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        raw = sqlite3.connect(str(ss.DB_PATH))
        raw.execute("BEGIN")
        ss._migration_1_baseline(raw)
        raw.execute("PRAGMA user_version = 1")
        raw.commit()
        raw.close()

        def _broken(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE should_roll_back (id INTEGER)")
            raise RuntimeError("migration failed")

        monkeypatch.setattr(
            ss, "_MIGRATIONS", (ss._migration_1_baseline, _broken),
        )
        with pytest.raises(RuntimeError, match="migration failed"):
            ss._connect()

        check = sqlite3.connect(str(ss.DB_PATH))
        try:
            assert check.execute("PRAGMA user_version").fetchone()[0] == 1
            tables = {
                row[0] for row in check.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            assert "should_roll_back" not in tables
        finally:
            check.close()

    def test_newer_schema_is_left_untouched(
        self, tmp_home: Path, capsys: pytest.CaptureFixture[str],
    ):
        raw = sqlite3.connect(str(ss.DB_PATH))
        raw.execute(f"PRAGMA user_version = {ss.CACHE_SCHEMA_VERSION + 1}")
        raw.commit()
        raw.close()

        with pytest.raises(SystemExit):
            ss._connect()
        assert "newer ccstory" in capsys.readouterr().err

        check = sqlite3.connect(str(ss.DB_PATH))
        try:
            assert check.execute("PRAGMA user_version").fetchone()[0] == (
                ss.CACHE_SCHEMA_VERSION + 1
            )
        finally:
            check.close()


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

    def test_ccstory_lang_env_wins_over_claude_md(
        self, tmp_home: Path, monkeypatch,
    ):
        # CLAUDE.md says Japanese, but $CCSTORY_LANG is the user's explicit
        # override and must take precedence.
        md_path = tmp_home / ".claude" / "CLAUDE.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("Respond in Japanese.\n", encoding="utf-8")
        monkeypatch.setenv(ss.CCSTORY_LANG_ENV, "Traditional Chinese")
        ss.language_directive.cache_clear()
        directive = language_directive()
        assert directive == (
            "Respond in Traditional Chinese. "
            "Keep the same length / format limits regardless of language."
        )
        assert "--- CLAUDE.md ---" not in directive

    def test_ccstory_config_language_used_when_no_env(
        self, tmp_home: Path,
    ):
        # ccstory's own config.toml `language` field wins over CLAUDE.md /
        # settings.json but loses to $CCSTORY_LANG.
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('language = "Korean"\n', encoding="utf-8")
        ss.language_directive.cache_clear()
        directive = language_directive()
        assert directive == (
            "Respond in Korean. "
            "Keep the same length / format limits regardless of language."
        )

    def test_ccstory_config_language_loses_to_env(
        self, tmp_home: Path, monkeypatch,
    ):
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('language = "Korean"\n', encoding="utf-8")
        monkeypatch.setenv(ss.CCSTORY_LANG_ENV, "Spanish")
        ss.language_directive.cache_clear()
        directive = language_directive()
        assert "Spanish" in directive
        assert "Korean" not in directive

    def test_ccstory_config_wins_over_claude_md(self, tmp_home: Path):
        # Tool-specific config beats Claude Code's global CLAUDE.md, by
        # design: if a user bothered to write `language = X` in ccstory's
        # own config they want THIS tool to use X regardless of what
        # CLAUDE.md says about global response language.
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('language = "Korean"\n', encoding="utf-8")
        md_path = tmp_home / ".claude" / "CLAUDE.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("Respond in Japanese.\n", encoding="utf-8")
        ss.language_directive.cache_clear()
        directive = language_directive()
        assert "Korean" in directive
        assert "Japanese" not in directive
        # No CLAUDE.md block emitted — single-line directive wins.
        assert "--- CLAUDE.md ---" not in directive

    def test_system_locale_used_when_nothing_else_set(
        self, tmp_home: Path, monkeypatch,
    ):
        # No env, no config, no CLAUDE.md, no settings.json — but locale
        # detection returns a non-English language. That should drive the
        # directive instead of the English final fallback.
        monkeypatch.setattr(ss, "_detect_system_locale", lambda: "Traditional Chinese")
        ss.language_directive.cache_clear()
        directive = language_directive()
        assert directive == (
            "Respond in Traditional Chinese. "
            "Keep the same length / format limits regardless of language."
        )

    def test_blank_env_var_falls_through(self, tmp_home: Path, monkeypatch):
        # Empty / whitespace env var must not poison the chain.
        monkeypatch.setenv(ss.CCSTORY_LANG_ENV, "   ")
        ss.language_directive.cache_clear()
        assert language_directive() == "Respond in English."

    def test_malformed_ccstory_config_falls_through(self, tmp_home: Path):
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("this is = not [ valid toml", encoding="utf-8")
        ss.language_directive.cache_clear()
        assert language_directive() == "Respond in English."


class TestDetectSystemLocale:
    """Locale-tag → friendly-language-name mapping. Returns None for English
    locales so the directive lands on the hardcoded English fallback (i.e.
    English users see no behavior change from the new locale layer)."""

    def test_english_returns_none(self, monkeypatch):
        import locale
        monkeypatch.setattr(locale, "getlocale", lambda: ("en_US", "UTF-8"))
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("LANG", raising=False)
        assert ss._detect_system_locale() is None

    def test_c_posix_returns_none(self, monkeypatch):
        import locale
        monkeypatch.setattr(locale, "getlocale", lambda: (None, None))
        monkeypatch.setenv("LANG", "C.UTF-8")
        monkeypatch.delenv("LC_ALL", raising=False)
        assert ss._detect_system_locale() is None

    def test_zh_tw_maps_to_traditional_chinese(self, monkeypatch):
        import locale
        monkeypatch.setattr(locale, "getlocale", lambda: ("zh_TW", "UTF-8"))
        assert ss._detect_system_locale() == "Traditional Chinese"

    def test_unknown_locale_passes_through_raw(self, monkeypatch):
        import locale
        monkeypatch.setattr(locale, "getlocale", lambda: ("xx_YY", "UTF-8"))
        # No mapping entry → return the base tag verbatim so claude -p still
        # has *something* to work with rather than silently dropping to English.
        assert ss._detect_system_locale() == "xx_YY"

    def test_env_fallback_when_getlocale_returns_none(self, monkeypatch):
        import locale
        monkeypatch.setattr(locale, "getlocale", lambda: (None, None))
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.setenv("LANG", "ja_JP.UTF-8")
        assert ss._detect_system_locale() == "Japanese"


class TestSynthesizeOverallForPeriod:
    def test_empty_input_returns_none(self, tmp_home: Path):
        out = synthesize_overall_for_period(
            period_key="2026-05",
            category_hours=[],
            sessions_by_category={},
        )
        assert out is None

    def test_cache_hit_skips_claude_call(self, tmp_home: Path, monkeypatch):
        class Result:
            returncode = 0
            stdout = "cached prose"
            stderr = ""

        monkeypatch.setattr(ss, "claude_bin_available", lambda: True)
        monkeypatch.setattr(ss, "run_claude_p", lambda *_args: Result())
        kwargs = dict(
            period_key="2026-05",
            category_hours=[("coding", 2.0), ("ops", 1.0)],
            sessions_by_category={
                "coding": [("sess-a", "did A")],
                "ops": [("sess-b", "did B")],
            },
        )
        assert synthesize_overall_for_period(**kwargs) == "cached prose"

        # A matching input fingerprint must now hit without probing Claude.
        def boom():
            raise AssertionError("claude_bin_available should not be called on cache hit")
        monkeypatch.setattr(ss, "claude_bin_available", boom)
        assert synthesize_overall_for_period(**kwargs) == "cached prose"

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

    def test_cache_invalidates_when_prompt_changes(
        self, tmp_home: Path, monkeypatch,
    ):
        class Result:
            returncode = 0
            stdout = "generated narrative"
            stderr = ""

        kwargs = dict(
            period_key="2026-05",
            category_hours=[("coding", 2.0)],
            sessions_by_category={"coding": [("sess-a", "did A")]},
        )
        monkeypatch.setattr(ss, "claude_bin_available", lambda: True)
        monkeypatch.setattr(ss, "run_claude_p", lambda *_args: Result())
        assert synthesize_overall_for_period(**kwargs) == "generated narrative"

        monkeypatch.setattr(ss, "_OVERALL_PROMPT", ss._OVERALL_PROMPT + "\nBe direct.")
        monkeypatch.setattr(ss, "claude_bin_available", lambda: False)
        assert synthesize_overall_for_period(**kwargs) is None

    def test_cache_invalidates_when_summary_text_changes(
        self, tmp_home: Path, monkeypatch,
    ):
        class Result:
            returncode = 0
            stdout = "generated narrative"
            stderr = ""

        monkeypatch.setattr(ss, "claude_bin_available", lambda: True)
        monkeypatch.setattr(ss, "run_claude_p", lambda *_args: Result())
        assert synthesize_overall_for_period(
            "2026-05", [("coding", 2.0)], {"coding": [("s1", "old")]},
        ) == "generated narrative"

        monkeypatch.setattr(ss, "claude_bin_available", lambda: False)
        assert synthesize_overall_for_period(
            "2026-05", [("coding", 2.0)], {"coding": [("s1", "new")]},
        ) is None

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
