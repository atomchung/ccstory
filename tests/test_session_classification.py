"""Tests for #25 — session-level content classification + cache + hybrid."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from ccstory.categorizer import user_rule_match
from ccstory.session_summarizer import (
    _classify_cache_get_many,
    _classify_cache_upsert_many,
    _parse_classification_lines,
    classify_sessions_by_content,
)


class TestUserRuleMatch:
    def test_no_config_returns_none(self, tmp_path: Path):
        nonexistent = tmp_path / "config.toml"
        assert user_rule_match("-Users-alice-code-myrepo", nonexistent) is None

    def test_matches_user_rule(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[categories]\n'
            '"work" = ["myrepo"]\n',
            encoding="utf-8",
        )
        assert user_rule_match("-Users-alice-code-myrepo", cfg) == "work"

    def test_unmatched_returns_none(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[categories]\n'
            '"work" = ["specific-repo-only"]\n',
            encoding="utf-8",
        )
        # Default rules would catch "myapp" as coding, but user_rule_match
        # only looks at user rules.
        assert user_rule_match("-Users-alice-code-myapp", cfg) is None

    def test_hyphenated_needle(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[categories]\n'
            '"client-work" = ["acme-inc"]\n',
            encoding="utf-8",
        )
        assert user_rule_match("-Users-alice-code-acme-inc", cfg) == "client-work"


class TestParseClassificationLines:
    def test_well_formed_lines(self):
        text = (
            '{"session_id": "s1", "bucket": "coding"}\n'
            '{"session_id": "s2", "bucket": "investment"}\n'
        )
        assert _parse_classification_lines(text) == {
            "s1": "coding",
            "s2": "investment",
        }

    def test_strips_code_fences(self):
        text = (
            "```json\n"
            '{"session_id": "s1", "bucket": "Coding"}\n'
            "```"
        )
        assert _parse_classification_lines(text) == {"s1": "coding"}

    def test_skips_malformed_lines(self):
        text = (
            '{"session_id": "s1", "bucket": "coding"}\n'
            'this is not json\n'
            '{"session_id": "s2", "bucket": "writing"}\n'
        )
        result = _parse_classification_lines(text)
        assert result == {"s1": "coding", "s2": "writing"}

    def test_skips_missing_keys(self):
        text = (
            '{"session_id": "s1"}\n'
            '{"bucket": "writing"}\n'
            '{"session_id": "s2", "bucket": "coding"}\n'
        )
        assert _parse_classification_lines(text) == {"s2": "coding"}

    def test_empty_input(self):
        assert _parse_classification_lines("") == {}


class TestCacheOps:
    def test_upsert_and_get(self, tmp_home: Path):
        _classify_cache_upsert_many({"a": "coding", "b": "writing"})
        result = _classify_cache_get_many(["a", "b", "missing"])
        assert result == {"a": "coding", "b": "writing"}

    def test_get_empty(self, tmp_home: Path):
        assert _classify_cache_get_many([]) == {}


class TestClassifySessionsByContent:
    def test_empty_items_returns_empty(self, tmp_home: Path):
        assert classify_sessions_by_content([]) == {}

    def test_full_cache_hit_no_claude_call(self, tmp_home: Path):
        _classify_cache_upsert_many({"s1": "coding", "s2": "writing"})

        with patch("ccstory.session_summarizer.claude_bin_available",
                   side_effect=AssertionError("should not be called")):
            result = classify_sessions_by_content([
                ("s1", "myapp", "did stuff"),
                ("s2", "blog", "wrote post"),
            ])
        assert result == {"s1": "coding", "s2": "writing"}

    def test_partial_cache_hits_call_claude_for_rest(self, tmp_home: Path):
        _classify_cache_upsert_many({"s1": "coding"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"session_id": "s2", "bucket": "writing"}\n',
            stderr="",
        )
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   return_value=mock_proc):
            result = classify_sessions_by_content([
                ("s1", "myapp", "did stuff"),
                ("s2", "blog", "wrote post"),
            ])
        assert result == {"s1": "coding", "s2": "writing"}
        # Subsequent identical call hits cache fully (no subprocess)
        with patch("ccstory.session_summarizer.subprocess.run",
                   side_effect=AssertionError("should not run")):
            again = classify_sessions_by_content([
                ("s1", "myapp", "did stuff"),
                ("s2", "blog", "wrote post"),
            ])
        assert again == {"s1": "coding", "s2": "writing"}

    def test_claude_unavailable_returns_cache_only(self, tmp_home: Path):
        _classify_cache_upsert_many({"s1": "coding"})
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=False):
            result = classify_sessions_by_content([
                ("s1", "myapp", "did stuff"),
                ("s2", "blog", "wrote post"),
            ])
        assert result == {"s1": "coding"}  # s2 not classified

    def test_claude_failure_returns_cache_only(self, tmp_home: Path):
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="oops",
        )
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   return_value=failed):
            result = classify_sessions_by_content([
                ("s1", "myapp", "did stuff"),
            ])
        assert result == {}

    def test_force_refresh_ignores_cache(self, tmp_home: Path):
        _classify_cache_upsert_many({"s1": "investment"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"session_id": "s1", "bucket": "coding"}\n',
            stderr="",
        )
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   return_value=mock_proc):
            result = classify_sessions_by_content(
                [("s1", "x", "y")], force_refresh=True,
            )
        # Refreshed value, not the stale cached "investment"
        assert result == {"s1": "coding"}

    def test_drops_invented_session_ids(self, tmp_home: Path):
        # Claude hallucinates a session_id we didn't ask about — drop it
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=(
                '{"session_id": "s1", "bucket": "coding"}\n'
                '{"session_id": "ghost", "bucket": "writing"}\n'
            ),
            stderr="",
        )
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   return_value=mock_proc):
            result = classify_sessions_by_content([("s1", "x", "y")])
        assert result == {"s1": "coding"}
        assert "ghost" not in result
