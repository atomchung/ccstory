"""Tests for ccstory.corrections (issue #191 PR A: storage + resolution API).

Covers only what PR A owns: the session_corrections migration (schema
version 7), the set/get/unset repository primitives, and the pure
resolve_summary/resolve_category precedence functions. Nothing here
exercises a CLI, renderer, MCP surface, or live evidence-fingerprint
comparison — those are later PRs in the same issue. All fixtures are
synthetic; no real transcripts or session ids are used.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ccstory import categorizer
from ccstory import corrections
from ccstory import session_summarizer as ss

# A self-contained worker for the real-subprocess concurrency test below.
# Mirrors tests/test_session_summarizer.py's _CACHE_SESSION_WORKER_SCRIPT:
# only imports the installed `ccstory` package so it needs no pytest
# sys.path setup, and runs under the same interpreter/venv as the test.
_CORRECTIONS_WORKER_SCRIPT = """
import sys
from pathlib import Path

from ccstory import session_summarizer as ss
from ccstory import corrections

ss.DB_PATH = Path(sys.argv[1])
session_id, value = sys.argv[2], sys.argv[3]
corrections.set_session_correction(session_id, "summary", value, "fp-worker")
"""


class TestSetSessionCorrectionCrud:
    def test_create_new_summary_correction(self, tmp_home: Path):
        result = corrections.set_session_correction(
            "sess1", "summary", "Refactored provider scanning", "fp-1",
            note="excerpt clipped the real outcome",
        )
        assert result.outcome == corrections.OUTCOME_CREATED
        row = result.correction
        assert row.session_id == "sess1"
        assert row.field == "summary"
        assert row.value == "Refactored provider scanning"
        assert row.base_evidence_fingerprint == "fp-1"
        assert row.status == corrections.STATUS_CURRENT
        assert row.note == "excerpt clipped the real outcome"
        assert row.created_at == row.updated_at

    def test_create_new_category_correction_against_default_vocabulary(
        self, tmp_home: Path,
    ):
        result = corrections.set_session_correction("sess1", "category", "coding", "fp-1")
        assert result.outcome == corrections.OUTCOME_CREATED
        assert result.correction.value == "coding"

    def test_replace_changes_value_bumps_updated_at_keeps_created_at(
        self, tmp_home: Path,
    ):
        first = corrections.set_session_correction("sess1", "summary", "first text", "fp-1")
        second = corrections.set_session_correction("sess1", "summary", "second text", "fp-1")

        assert second.outcome == corrections.OUTCOME_REPLACED
        assert second.correction.value == "second text"
        assert second.correction.created_at == first.correction.created_at
        assert second.correction.updated_at >= first.correction.updated_at
        assert second.correction.status == corrections.STATUS_CURRENT

        stored = corrections.get_session_corrections(["sess1"])["sess1"]["summary"]
        assert stored.value == "second text"

    def test_resubmitting_identical_correction_is_true_noop(self, tmp_home: Path):
        first = corrections.set_session_correction(
            "sess1", "summary", "same text", "fp-1", note="same note",
        )
        second = corrections.set_session_correction(
            "sess1", "summary", "same text", "fp-1", note="same note",
        )
        assert second.outcome == corrections.OUTCOME_UNCHANGED
        # A genuine no-op must not even bump updated_at -- otherwise
        # "idempotent" is not literally true.
        assert second.correction.updated_at == first.correction.updated_at
        assert second.correction.created_at == first.correction.created_at

    def test_replace_is_field_scoped(self, tmp_home: Path):
        """Setting `category` must not disturb an existing `summary` row."""
        corrections.set_session_correction("sess1", "summary", "the summary", "fp-1")
        corrections.set_session_correction("sess1", "category", "coding", "fp-1")
        stored = corrections.get_session_corrections(["sess1"])["sess1"]
        assert stored["summary"].value == "the summary"
        assert stored["category"].value == "coding"

    def test_blank_session_id_raises(self, tmp_home: Path):
        with pytest.raises(corrections.InvalidCorrectionValueError):
            corrections.set_session_correction("   ", "summary", "text", "fp-1")

    def test_blank_value_raises(self, tmp_home: Path):
        with pytest.raises(corrections.InvalidCorrectionValueError):
            corrections.set_session_correction("sess1", "summary", "   ", "fp-1")

    def test_unknown_field_raises(self, tmp_home: Path):
        with pytest.raises(corrections.UnknownFieldError):
            corrections.set_session_correction("sess1", "project", "ccstory", "fp-1")

    def test_summary_value_over_length_limit_raises(self, tmp_home: Path):
        too_long = "x" * (corrections.MAX_SUMMARY_VALUE_CHARS + 1)
        with pytest.raises(corrections.InvalidCorrectionValueError):
            corrections.set_session_correction("sess1", "summary", too_long, "fp-1")

    def test_summary_value_at_length_limit_is_accepted(self, tmp_home: Path):
        exactly = "x" * corrections.MAX_SUMMARY_VALUE_CHARS
        result = corrections.set_session_correction("sess1", "summary", exactly, "fp-1")
        assert result.correction.value == exactly

    def test_length_is_counted_in_characters_not_utf8_bytes(self, tmp_home: Path):
        """A CJK string is 3 bytes/char in UTF-8; a byte-length bug would
        reject this well below the character limit."""
        cjk_at_limit = "測" * corrections.MAX_SUMMARY_VALUE_CHARS
        result = corrections.set_session_correction("sess1", "summary", cjk_at_limit, "fp-1")
        assert result.correction.value == cjk_at_limit

        cjk_over_limit = "測" * (corrections.MAX_SUMMARY_VALUE_CHARS + 1)
        with pytest.raises(corrections.InvalidCorrectionValueError):
            corrections.set_session_correction("sess1", "summary", cjk_over_limit, "fp-1")

    def test_note_over_length_limit_raises(self, tmp_home: Path):
        too_long = "n" * (corrections.MAX_NOTE_CHARS + 1)
        with pytest.raises(corrections.InvalidCorrectionValueError):
            corrections.set_session_correction("sess1", "summary", "text", "fp-1", note=too_long)

    def test_blank_note_normalizes_to_none(self, tmp_home: Path):
        result = corrections.set_session_correction("sess1", "summary", "text", "fp-1", note="   ")
        assert result.correction.note is None

    def test_unknown_category_rejected_without_override(self, tmp_home: Path):
        with pytest.raises(corrections.UnknownCategoryError):
            corrections.set_session_correction("sess1", "category", "not-a-real-bucket", "fp-1")

    def test_unknown_category_accepted_with_explicit_override(self, tmp_home: Path):
        result = corrections.set_session_correction(
            "sess1", "category", "one-off-bucket", "fp-1", allow_new_category=True,
        )
        assert result.correction.value == "one-off-bucket"

    def test_category_matches_user_configured_vocabulary(self, tmp_home: Path):
        categorizer.add_category_keywords("work", ["internal-tool"])
        result = corrections.set_session_correction("sess1", "category", "work", "fp-1")
        assert result.correction.value == "work"

    def test_missing_evidence_fingerprint_stores_empty_string(self, tmp_home: Path):
        result = corrections.set_session_correction("sess1", "summary", "text", "")
        assert result.correction.base_evidence_fingerprint == ""


class TestNeverOverwritesGeneratedRows:
    """'generated/imported cache rows remain separate and are never
    overwritten' (issue #191 PR A rule)."""

    def test_summary_correction_does_not_touch_session_summaries(self, tmp_home: Path):
        ss.upsert("sess1", "the generated summary", "generated")
        corrections.set_session_correction("sess1", "summary", "corrected summary", "fp-1")

        auto_row = ss.get("sess1")
        assert auto_row is not None
        assert auto_row.summary == "the generated summary"
        assert auto_row.source == "generated"

    def test_category_correction_does_not_touch_session_content_buckets(self, tmp_home: Path):
        ss._classify_cache_upsert_many({"sess1": "coding"}, input_fingerprint="fp-classify")
        corrections.set_session_correction("sess1", "category", "writing", "fp-1")

        cached = ss._classify_cache_get_many(["sess1"], input_fingerprint="fp-classify")
        assert cached == {"sess1": "coding"}


class TestNeverBecomesGlobalRule:
    """A session-level correction must stay scoped to that one physical
    session and must never mutate the shared category vocabulary (#69)."""

    def test_new_category_correction_does_not_mutate_config(self, tmp_home: Path):
        before = categorizer.list_user_categories()
        corrections.set_session_correction(
            "sess1", "category", "one-off-bucket", "fp-1", allow_new_category=True,
        )
        after = categorizer.list_user_categories()
        assert after == before

    def test_correction_scoped_to_its_own_session(self, tmp_home: Path):
        corrections.set_session_correction("sess1", "category", "coding", "fp-1")
        assert corrections.get_session_corrections(["sess2"]) == {}


class TestUnsetSessionCorrection:
    def test_unset_existing_removes_and_returns_true(self, tmp_home: Path):
        corrections.set_session_correction("sess1", "summary", "text", "fp-1")
        assert corrections.unset_session_correction("sess1", "summary") is True
        assert corrections.get_session_corrections(["sess1"]) == {}

    def test_unset_missing_returns_false(self, tmp_home: Path):
        assert corrections.unset_session_correction("sess1", "summary") is False

    def test_unset_is_field_scoped(self, tmp_home: Path):
        corrections.set_session_correction("sess1", "summary", "s text", "fp-1")
        corrections.set_session_correction("sess1", "category", "coding", "fp-1")
        corrections.unset_session_correction("sess1", "summary")
        remaining = corrections.get_session_corrections(["sess1"])["sess1"]
        assert "summary" not in remaining
        assert remaining["category"].value == "coding"

    def test_unset_unknown_field_raises(self, tmp_home: Path):
        with pytest.raises(corrections.UnknownFieldError):
            corrections.unset_session_correction("sess1", "project")

    def test_unset_blank_session_id_raises(self, tmp_home: Path):
        with pytest.raises(corrections.InvalidCorrectionValueError):
            corrections.unset_session_correction("  ", "summary")


class TestGetSessionCorrections:
    def test_empty_list_returns_empty_dict(self, tmp_home: Path):
        assert corrections.get_session_corrections([]) == {}

    def test_blank_ids_are_dropped(self, tmp_home: Path):
        assert corrections.get_session_corrections(["", "   "]) == {}

    def test_bulk_fetch_across_sessions_and_fields(self, tmp_home: Path):
        corrections.set_session_correction("sess1", "summary", "s1 summary", "fp-1")
        corrections.set_session_correction("sess1", "category", "coding", "fp-1")
        corrections.set_session_correction("sess2", "summary", "s2 summary", "fp-2")

        result = corrections.get_session_corrections(["sess1", "sess2", "sess3"])

        assert set(result.keys()) == {"sess1", "sess2"}
        assert result["sess1"]["summary"].value == "s1 summary"
        assert result["sess1"]["category"].value == "coding"
        assert result["sess2"]["summary"].value == "s2 summary"
        assert "sess3" not in result

    def test_single_session_list_works(self, tmp_home: Path):
        corrections.set_session_correction("sess1", "summary", "text", "fp-1")
        result = corrections.get_session_corrections(["sess1"])
        assert result["sess1"]["summary"].value == "text"


class TestResolvePrecedence:
    def test_resolve_summary_correction_wins_over_generated(self, tmp_home: Path):
        auto = ss.SessionSummary("sess1", "auto text", "generated")
        result = corrections.set_session_correction("sess1", "summary", "corrected text", "fp-1")

        resolved = corrections.resolve_summary(auto, result.correction)

        assert resolved.value == "corrected text"
        assert resolved.source == corrections.SOURCE_USER_CORRECTION
        assert resolved.status == corrections.STATUS_CURRENT

    def test_resolve_summary_no_correction_falls_back_to_auto(self, tmp_home: Path):
        auto = ss.SessionSummary("sess1", "auto text", "generated")
        resolved = corrections.resolve_summary(auto, None)
        assert resolved.value == "auto text"
        assert resolved.source == "generated"
        assert resolved.status is None

    def test_resolve_summary_nothing_at_all(self, tmp_home: Path):
        resolved = corrections.resolve_summary(None, None)
        assert resolved.value is None
        assert resolved.source == corrections.SOURCE_NONE

    def test_resolve_category_correction_wins_over_rule(self, tmp_home: Path):
        rule_or_llm = ("coding", "builtin_rule")
        result = corrections.set_session_correction("sess1", "category", "writing", "fp-1")

        resolved = corrections.resolve_category(rule_or_llm, result.correction)

        assert resolved.value == "writing"
        assert resolved.source == corrections.SOURCE_USER_CORRECTION

    def test_resolve_category_no_correction_falls_back_to_rule(self, tmp_home: Path):
        resolved = corrections.resolve_category(("coding", "user_rule"), None)
        assert resolved.value == "coding"
        assert resolved.source == "user_rule"
        assert resolved.status is None

    def test_resolve_category_nothing_at_all(self, tmp_home: Path):
        resolved = corrections.resolve_category(None, None)
        assert resolved.value is None
        assert resolved.source == corrections.SOURCE_NONE

    def test_resolve_summary_rejects_category_correction(self, tmp_home: Path):
        result = corrections.set_session_correction("sess1", "category", "coding", "fp-1")
        with pytest.raises(corrections.UnknownFieldError):
            corrections.resolve_summary(None, result.correction)

    def test_resolve_category_rejects_summary_correction(self, tmp_home: Path):
        result = corrections.set_session_correction("sess1", "summary", "text", "fp-1")
        with pytest.raises(corrections.UnknownFieldError):
            corrections.resolve_category(None, result.correction)

    def test_resolve_is_pure_no_cache_access(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
        """resolve_* must never touch the cache -- corrupting DB_PATH must
        not affect a resolve call that is only ever given plain objects."""
        monkeypatch.setattr(ss, "DB_PATH", Path("/nonexistent/does-not-exist.db"))
        correction = corrections.SessionCorrection(
            session_id="sess1", field="summary", value="corrected",
            created_at=1.0, updated_at=1.0, base_evidence_fingerprint="fp",
            status=corrections.STATUS_CURRENT,
        )
        resolved = corrections.resolve_summary(None, correction)
        assert resolved.value == "corrected"


class TestEvidenceChangedRetainsAuthority:
    """The correction layer must never silently delete, downgrade, or
    overwrite a correction -- status is informational, not a precedence
    override (issue #191 body, "Transcript growth and staleness")."""

    def test_status_round_trips_through_the_database(self, tmp_home: Path):
        corrections.set_session_correction("sess1", "summary", "text", "fp-1")
        with ss.cache_session() as conn:
            conn.execute(
                "UPDATE session_corrections SET status = ? "
                "WHERE session_id = ? AND field = ?",
                (corrections.STATUS_EVIDENCE_CHANGED, "sess1", "summary"),
            )
            conn.commit()

        stored = corrections.get_session_corrections(["sess1"])["sess1"]["summary"]
        assert stored.status == corrections.STATUS_EVIDENCE_CHANGED

    def test_resolve_summary_still_wins_when_evidence_changed(self, tmp_home: Path):
        stale_correction = corrections.SessionCorrection(
            session_id="sess1", field="summary", value="user's corrected text",
            created_at=1.0, updated_at=1.0, base_evidence_fingerprint="old-fp",
            status=corrections.STATUS_EVIDENCE_CHANGED,
        )
        auto = ss.SessionSummary("sess1", "fresh generated text", "generated")

        resolved = corrections.resolve_summary(auto, stale_correction)

        # Authority is retained: the correction's value wins regardless.
        assert resolved.value == "user's corrected text"
        assert resolved.source == corrections.SOURCE_USER_CORRECTION
        # But the review signal is surfaced, not silently dropped.
        assert resolved.status == corrections.STATUS_EVIDENCE_CHANGED

    def test_resolve_category_still_wins_when_evidence_changed(self, tmp_home: Path):
        stale_correction = corrections.SessionCorrection(
            session_id="sess1", field="category", value="writing",
            created_at=1.0, updated_at=1.0, base_evidence_fingerprint="old-fp",
            status=corrections.STATUS_EVIDENCE_CHANGED,
        )
        resolved = corrections.resolve_category(("coding", "builtin_rule"), stale_correction)
        assert resolved.value == "writing"
        assert resolved.status == corrections.STATUS_EVIDENCE_CHANGED

    def test_true_noop_resubmit_preserves_evidence_changed_status(self, tmp_home: Path):
        corrections.set_session_correction("sess1", "summary", "text", "fp-1")
        with ss.cache_session() as conn:
            conn.execute(
                "UPDATE session_corrections SET status = ? "
                "WHERE session_id = ? AND field = ?",
                (corrections.STATUS_EVIDENCE_CHANGED, "sess1", "summary"),
            )
            conn.commit()

        # Resubmitting the exact same value/fingerprint addresses nothing
        # about the evidence-changed state, so it must not clear it.
        result = corrections.set_session_correction("sess1", "summary", "text", "fp-1")

        assert result.outcome == corrections.OUTCOME_UNCHANGED
        assert result.correction.status == corrections.STATUS_EVIDENCE_CHANGED

    def test_replacing_value_resets_status_to_current(self, tmp_home: Path):
        corrections.set_session_correction("sess1", "summary", "text", "fp-1")
        with ss.cache_session() as conn:
            conn.execute(
                "UPDATE session_corrections SET status = ? "
                "WHERE session_id = ? AND field = ?",
                (corrections.STATUS_EVIDENCE_CHANGED, "sess1", "summary"),
            )
            conn.commit()

        # An explicit edit is a deliberate reassertion -- fresh again.
        result = corrections.set_session_correction("sess1", "summary", "new text", "fp-1")

        assert result.outcome == corrections.OUTCOME_REPLACED
        assert result.correction.status == corrections.STATUS_CURRENT


class TestMigration:
    def test_fresh_db_reaches_v7_with_session_corrections_table(self, tmp_home: Path):
        conn = ss._connect()
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
            assert ss.CACHE_SCHEMA_VERSION == 7
            columns = ss._table_columns(conn, "session_corrections")
            assert columns == {
                "session_id", "field", "value", "created_at", "updated_at",
                "base_evidence_fingerprint", "status", "note",
            }
        finally:
            conn.close()

    def test_v6_db_upgrades_and_preserves_existing_summaries(self, tmp_home: Path):
        raw = sqlite3.connect(str(ss.DB_PATH))
        raw.execute("BEGIN")
        ss._migration_6_source_vocabulary(raw)
        raw.execute("PRAGMA user_version = 6")
        raw.execute(
            "INSERT INTO session_summaries "
            "(session_id, summary, source, project, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("legacy-sess", "pre-existing summary", "generated", "proj", 1.0),
        )
        raw.commit()
        raw.close()

        # First touch through the public corrections API must trigger the
        # migration to v7 transparently.
        result = corrections.set_session_correction(
            "legacy-sess", "summary", "corrected text", "fp-1",
        )
        assert result.outcome == corrections.OUTCOME_CREATED

        check = sqlite3.connect(str(ss.DB_PATH))
        try:
            assert check.execute("PRAGMA user_version").fetchone()[0] == 7
            assert check.execute(
                "SELECT summary FROM session_summaries WHERE session_id = ?",
                ("legacy-sess",),
            ).fetchone() == ("pre-existing summary",)
        finally:
            check.close()

    def test_migration_7_is_idempotent_on_already_current_db(self, tmp_home: Path):
        conn = ss._connect()
        conn.close()
        # Running the migration function again directly (simulating a
        # manually re-adopted / partially-edited DB) must not raise or
        # duplicate the table.
        conn = sqlite3.connect(str(ss.DB_PATH))
        try:
            ss._migration_7_session_corrections(conn)  # no error
            conn.commit()
        finally:
            conn.close()

    def test_newer_schema_is_rejected_and_left_untouched(self, tmp_home: Path):
        raw = sqlite3.connect(str(ss.DB_PATH))
        raw.execute(f"PRAGMA user_version = {ss.CACHE_SCHEMA_VERSION + 1}")
        raw.commit()
        raw.close()

        with pytest.raises(ss.CacheUnavailable, match="newer ccstory"):
            corrections.set_session_correction("sess1", "summary", "text", "fp-1")

        check = sqlite3.connect(str(ss.DB_PATH))
        try:
            assert check.execute("PRAGMA user_version").fetchone()[0] == (
                ss.CACHE_SCHEMA_VERSION + 1
            )
        finally:
            check.close()

    def test_migration_7_rollback_leaves_v6_intact(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        raw = sqlite3.connect(str(ss.DB_PATH))
        raw.execute("BEGIN")
        ss._migration_6_source_vocabulary(raw)
        raw.execute("PRAGMA user_version = 6")
        raw.commit()
        raw.close()

        def _broken(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE should_roll_back (id INTEGER)")
            raise RuntimeError("simulated migration 7 failure")

        monkeypatch.setattr(ss, "_MIGRATIONS", (
            ss._migration_1_baseline, ss._migration_2_cache_fingerprints,
            ss._migration_3_adopt_legacy_classifications,
            ss._migration_4_narrator_provenance,
            ss._migration_5_summary_evidence_identity,
            ss._migration_6_source_vocabulary,
            _broken,
        ))
        with pytest.raises(RuntimeError, match="simulated migration 7 failure"):
            ss._connect()

        check = sqlite3.connect(str(ss.DB_PATH))
        try:
            assert check.execute("PRAGMA user_version").fetchone()[0] == 6
            tables = {
                row[0] for row in check.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            assert "should_roll_back" not in tables
            assert "session_corrections" not in tables
        finally:
            check.close()


class TestConcurrency:
    def test_two_concurrent_processes_writing_corrections(self, tmp_home: Path):
        """A real second OS process (not a fork) must never be blocked or
        corrupted by the first writing through the same corrections API."""
        ss._connect().close()
        db_path = ss.DB_PATH

        procs = [
            subprocess.Popen(
                [
                    sys.executable, "-c", _CORRECTIONS_WORKER_SCRIPT,
                    str(db_path), f"conc-sess-{i}", f"corrected {i}",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(2)
        ]
        for i, p in enumerate(procs):
            _out, err = p.communicate(timeout=30)
            assert p.returncode == 0, f"worker {i} failed:\n{err}"

        result = corrections.get_session_corrections(["conc-sess-0", "conc-sess-1"])
        assert result["conc-sess-0"]["summary"].value == "corrected 0"
        assert result["conc-sess-1"]["summary"].value == "corrected 1"

    def test_locked_db_surfaces_cache_unavailable_not_corruption(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        def _locked(_conn: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(ss, "_MIGRATIONS", (_locked,))
        with pytest.raises(ss.CacheUnavailable) as exc:
            corrections.set_session_correction("sess1", "summary", "text", "fp-1")
        msg = str(exc.value)
        assert "locked" in msg
        assert "rm " not in msg


class TestEffectiveCategoryVocabulary:
    def test_includes_default_buckets(self, tmp_home: Path):
        vocab = corrections.effective_category_vocabulary()
        assert {"coding", "investment", "writing", "other"} <= vocab

    def test_includes_user_configured_buckets(self, tmp_home: Path):
        categorizer.add_category_keywords("work", ["internal-tool"])
        vocab = corrections.effective_category_vocabulary()
        assert "work" in vocab
