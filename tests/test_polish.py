"""Tests for v1 polish: loud TOML warnings + sqlite corruption recovery.

Breadcrumb hint at end of `ccstory week` is a single console.print line
gated on `args.classify != "folder"` — exercised by an end-to-end CLI
smoke instead of a unit assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ccstory import categorizer, session_summarizer


class TestMalformedTomlLoudWarn:
    def test_parse_error_emits_to_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ):
        cfg = tmp_path / "broken.toml"
        cfg.write_text("this = is = not = valid toml\n", encoding="utf-8")
        assert categorizer._load_toml(cfg) is None
        captured = capsys.readouterr()
        # User-facing warning surfaces on stderr (default-verbosity users see it)
        assert "could not parse" in captured.err
        assert str(cfg) in captured.err
        assert "fall" in captured.err.lower()  # mentions fallback

    def test_missing_file_is_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ):
        # A simply-absent config is not an error — silent return None.
        assert categorizer._load_toml(tmp_path / "absent.toml") is None
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_valid_toml_no_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ):
        cfg = tmp_path / "ok.toml"
        cfg.write_text('default_bucket = "writing"\n', encoding="utf-8")
        data = categorizer._load_toml(cfg)
        assert data == {"default_bucket": "writing"}
        captured = capsys.readouterr()
        assert "could not parse" not in captured.err


class TestSqliteCorruptionRecovery:
    def test_corrupt_db_exits_with_recovery_hint(
        self, tmp_home: Path, capsys: pytest.CaptureFixture[str],
    ):
        # Write garbage where the cache db would live, then probe _connect.
        session_summarizer.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        session_summarizer.DB_PATH.write_bytes(b"this is not a sqlite file")
        with pytest.raises(SystemExit) as excinfo:
            session_summarizer._connect()
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "corrupted" in captured.err
        # Recovery instruction names the path the user should rm
        assert str(session_summarizer.DB_PATH) in captured.err
        assert "rm " in captured.err

    def test_fresh_db_connects_fine(self, tmp_home: Path):
        # Sanity: no file yet → _connect creates one without issue
        conn = session_summarizer._connect()
        conn.close()
        assert session_summarizer.DB_PATH.exists()
