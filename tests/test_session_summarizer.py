"""Tests for ccstory.session_summarizer.

Focuses on what can be tested without invoking `claude -p`:
  - sqlite roundtrip (upsert/get/get_many/missing_ids)
  - first-user-message excerpt extraction + filtering
  - extractive narrative path (use_llm=False)
  - cache schema migrations, including the #206 source-vocabulary rewrite
"""

from __future__ import annotations

import contextvars
import json
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ccstory import session_summarizer as ss
from ccstory.session_summarizer import (
    _extract_excerpt,
    _compact_batch_excerpt,
    _fallback_narrative,
    OVERALL_KEY,
    cache_session,
    get,
    get_many,
    get_overall_narrative,
    language_directive,
    missing_ids,
    summarize_session,
    synthesize_overall_for_period,
    upsert,
)
from ccstory.providers.claude import ClaudeCodeProvider

from tests.conftest import _ts, make_assistant_msg, make_user_msg, write_jsonl

# A self-contained worker for the real-subprocess concurrency test below.
# Deliberately only imports the installed `ccstory` package (never `tests.*`)
# so it needs no pytest sys.path setup to run under a fresh interpreter --
# `python -c <this>` is spawned directly via `sys.executable`, so it always
# runs in the same environment/venv as the test itself.
_CACHE_SESSION_WORKER_SCRIPT = """
import sys
from pathlib import Path

from ccstory import session_summarizer as ss

ss.DB_PATH = Path(sys.argv[1])
session_id, summary = sys.argv[2], sys.argv[3]
with ss.cache_session():
    ss.upsert(session_id, summary, "generated")
"""


class TestSqliteRoundtrip:
    def test_upsert_then_get(self, tmp_home: Path):
        upsert("sess1", "did a thing", "generated", project="myproj")
        s = get("sess1")
        assert s is not None
        assert s.session_id == "sess1"
        assert s.summary == "did a thing"
        assert s.source == "generated"
        assert s.project == "myproj"

    def test_upsert_replaces_existing(self, tmp_home: Path):
        upsert("sess1", "first", "extracted")
        upsert("sess1", "second", "generated")
        s = get("sess1")
        assert s.summary == "second"
        assert s.source == "generated"

    def test_upsert_empty_summary_is_noop(self, tmp_home: Path):
        upsert("sess1", "", "generated")
        assert get("sess1") is None

    def test_upsert_empty_id_is_noop(self, tmp_home: Path):
        upsert("", "x", "generated")

    def test_get_missing_returns_none(self, tmp_home: Path):
        assert get("nonexistent") is None

    def test_get_many(self, tmp_home: Path):
        upsert("a", "a-summary", "generated")
        upsert("b", "b-summary", "extracted")
        result = get_many(["a", "b", "missing"])
        assert set(result.keys()) == {"a", "b"}
        assert result["a"].summary == "a-summary"

    def test_get_many_empty_input(self, tmp_home: Path):
        assert get_many([]) == {}

    def test_missing_ids(self, tmp_home: Path):
        upsert("present", "x", "generated")
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
        assert result.source == "extracted"
        assert "Build a CLI subcommand" in result.summary

    def test_cached_result_returned_immediately(self, tmp_home: Path, jsonl_factory):
        path = jsonl_factory(
            "-Users-alice-code-myapp",
            "sess-cached",
            [make_user_msg("X", _ts(2026, 5, 10, 10, 0, 0))],
        )
        upsert("sess-cached", "pre-existing summary", "generated", project="myproj")
        result = summarize_session("sess-cached", path, use_llm=False)
        assert result is not None
        assert result.summary == "pre-existing summary"
        assert result.source == "generated"  # cached entry untouched

    def test_empty_session_marks_skipped(self, tmp_home: Path, jsonl_factory):
        # File with no meaningful user content
        path = jsonl_factory("-Users-alice-code-x", "sess-empty", [])
        path.write_text("", encoding="utf-8")
        result = summarize_session("sess-empty", path, use_llm=False)
        assert result is not None
        assert result.source == "no_evidence"


class TestRetroactiveRefresh:
    """Retroactive upgrade/refresh of cached narratives (the freeze fix).

    `--llm-narrative` must be able to upgrade a cached `extracted` row to
    `generated` and refresh a stale `generated` row, while never re-burning
    an up-to-date one and never downgrading a good summary on a transient
    `claude -p` failure.
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
        upsert("sess-r", "stale fallback line", "extracted", project="myapp")
        monkeypatch.setattr(ss, "summarize_via_claude_p",
                            lambda *a, **k: "polished outcome")
        result = summarize_session("sess-r", path, use_llm=True)
        assert result.source == "generated"
        assert result.summary == "polished outcome"
        assert result.prompt_version == ss.PROMPT_VERSION

    def test_current_auto_not_reburned(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        path = self._jsonl(jsonl_factory)
        project, excerpt = _extract_excerpt(path)
        evidence = ss._prepare_summary_evidence(
            "sess-r", project, excerpt, lane=ss._SUMMARY_EVIDENCE_SINGLE_LANE,
        )
        upsert("sess-r", "good summary", "generated", project="myapp",
               prompt_version=ss.PROMPT_VERSION,
               narrator_provider="claude", narrator_model="sonnet",
               narrator_fingerprint=ss.narrative_config_fingerprint(),
               evidence_fingerprint=evidence.evidence_fingerprint,
               observed_evidence_fingerprint=evidence.evidence_fingerprint)

        def _boom(*a, **k):
            raise AssertionError("claude -p must not run for an up-to-date generated row")

        monkeypatch.setattr(ss, "summarize_via_claude_p", _boom)
        result = summarize_session("sess-r", path, use_llm=True)
        assert result.source == "generated"
        assert result.summary == "good summary"

    def test_stale_auto_refreshed(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        path = self._jsonl(jsonl_factory)
        upsert("sess-r", "old-model summary", "generated", project="myapp",
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
        upsert("sess-r", "good summary", "generated", project="myapp",
               prompt_version=ss.PROMPT_VERSION)
        monkeypatch.setattr(ss, "summarize_via_claude_p",
                            lambda *a, **k: "forced refresh")
        result = summarize_session("sess-r", path, use_llm=True, force=True)
        assert result.summary == "forced refresh"

    def test_failed_refresh_keeps_existing_auto(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        # Non-destructive: a claude -p failure must not downgrade a good
        # generated summary to an extraction.
        path = self._jsonl(jsonl_factory)
        upsert("sess-r", "good summary", "generated", project="myapp",
               prompt_version=ss.PROMPT_VERSION - 1)
        monkeypatch.setattr(ss, "summarize_via_claude_p", lambda *a, **k: None)
        result = summarize_session("sess-r", path, use_llm=True)
        assert result.source == "generated"
        assert result.summary == "good summary"

    def test_skipped_is_retried_when_evidence_later_becomes_valid(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        path = self._jsonl(jsonl_factory)
        upsert("sess-r", "(no meaningful conversation)", "no_evidence", project="myapp")
        monkeypatch.setattr(
            ss, "summarize_via_claude_p",
            lambda *_a, **_k: "Recovered a now-valid session",
        )
        result = summarize_session("sess-r", path, use_llm=True)
        assert result.source == "generated"
        assert result.summary == "Recovered a now-valid session"
        assert ss.summary_evidence_status(result) == "current"

    def test_use_llm_false_never_upgrades(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        path = self._jsonl(jsonl_factory)
        upsert("sess-r", "fallback line", "extracted", project="myapp")

        def _boom(*a, **k):
            raise AssertionError("claude -p must not run without use_llm")

        monkeypatch.setattr(ss, "summarize_via_claude_p", _boom)
        result = summarize_session("sess-r", path, use_llm=False)
        assert result.source == "extracted"


class TestBatchedNarrativeBackfill:
    def test_batch_compaction_keeps_all_endpoints_with_exact_700_bound(self):
        excerpt = (
            "[USER 1]\nFIRST_HEAD " + "甲  \n" * 350 + " FIRST_TAIL\n\n"
            "[USER 2]\nintermediate context\n\n"
            "[USER LATE]\nLATEST_HEAD " + "乙  \n" * 350 + " LATEST_TAIL\n\n"
            "[ASSISTANT END]\nASSISTANT_HEAD "
            + "丙  \n" * 350
            + " ROOT_CAUSE_FIXED TESTS_PASSED PATCH_SHIPPED\n"
        )

        compacted = _compact_batch_excerpt(excerpt)

        assert len(compacted) <= ss._SUMMARY_BATCH_EXCERPT_CHARS
        assert "FIRST_HEAD" in compacted
        assert "FIRST_TAIL" in compacted
        assert "LATEST_HEAD" in compacted
        assert "LATEST_TAIL" in compacted
        assert "ASSISTANT_HEAD" in compacted
        assert "ROOT_CAUSE_FIXED TESTS_PASSED PATCH_SHIPPED" in compacted
        assert "  " not in compacted

    def test_batch_compaction_small_budget_still_keeps_assistant_outcome(self):
        excerpt = (
            "[USER 1]\ninitial intent " + "x" * 180 + "\n\n"
            "[USER LATE]\nlatest request " + "y" * 180 + "\n\n"
            "[ASSISTANT END]\nstarted diagnosis "
            + "z" * 180
            + " TESTS_PASSED\n"
        )

        compacted = _compact_batch_excerpt(excerpt, max_chars=120)

        assert len(compacted) <= 120
        assert "[USER 1]" in compacted
        assert "[USER LATE]" in compacted
        assert "[ASSISTANT END]" in compacted
        assert "TESTS_PASSED" in compacted

    def test_batch_compaction_every_positive_small_budget_retains_assistant_tail(self):
        excerpt = (
            "[USER 1]\ninitial intent\n\n"
            "[ASSISTANT END]\nassistant head "
            + "z" * 180
            + "Z"
        )

        for budget in range(1, 121):
            compacted = _compact_batch_excerpt(excerpt, max_chars=budget)
            assert len(compacted) <= budget
            assert compacted.endswith("Z")

    def test_batch_compaction_does_not_reparse_escaped_marker_body(self):
        excerpt = (
            "[USER 1]\nfirst request\n\\[ASSISTANT END]\nquoted body\n\n"
            "[USER LATE]\nlatest request\n\n"
            "[ASSISTANT END]\nreal outcome TESTS_PASSED\n"
        )

        compacted = _compact_batch_excerpt(excerpt)

        assert "\\[ASSISTANT END] quoted body" in compacted
        assert "latest request" in compacted
        assert "real outcome TESTS_PASSED" in compacted

    def _sessions(self, jsonl_factory, count: int = 3):
        provider = ClaudeCodeProvider()
        sessions = []
        for index in range(count):
            session_id = f"batch-{index}"
            path = jsonl_factory(
                "-Users-alice-code-myapp", session_id,
                [
                    make_user_msg(
                        f"Implement outcome {index}",
                        _ts(2026, 5, 10, 10, index, 0),
                    ),
                    make_assistant_msg(
                        f"Completed outcome {index}",
                        _ts(2026, 5, 10, 10, index, 5),
                        f"batch-msg-{index}",
                    ),
                ],
            )
            stat = provider.parse_session(path)
            assert stat is not None
            sessions.append(stat)
        return sessions

    def test_backfill_batches_and_records_exact_provenance(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        sessions = self._sessions(jsonl_factory, 3)
        calls: list[str] = []

        def fake_run(prompt: str, timeout: int):
            calls.append(prompt)
            ids = re.findall(r"\[session_id=([^;]+);", prompt)
            return ss.NarrativeCall(
                "\n".join(
                    json.dumps({
                        "session_id": sid,
                        "summary": f"Completed {sid} safely",
                    })
                    for sid in ids
                ),
                "antigravity", "gemini-3.6-flash-low",
            )

        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(ss, "run_llm_p", fake_run)
        result = ss.backfill_for_sessions(
            sessions, use_llm=True, batch_size=2,
        )

        assert len(calls) == 2  # 2 + 1, never one subprocess per session
        assert "[ASSISTANT END]" in calls[0]
        assert "Completed outcome 0" in calls[0]
        assert result["summarized"] == 3
        for session in sessions:
            stored = get(session.session_id)
            assert stored is not None
            assert stored.source == "generated"
            assert stored.narrator_provider == "antigravity"
            assert stored.narrator_model == "gemini-3.6-flash-low"

    def test_batch_trace_reports_progress_without_session_content(
        self, tmp_home: Path, jsonl_factory, monkeypatch,
    ):
        sessions = self._sessions(jsonl_factory, 3)

        def fake_run(prompt: str, _timeout: int, *, budget):
            ids = re.findall(r"\[session_id=([^;]+);", prompt)
            return ss.NarrativeCall(
                "\n".join(
                    json.dumps({"session_id": sid, "summary": f"Done {sid}"})
                    for sid in ids
                ),
                "codex", "gpt-5.6-terra",
            )

        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(ss, "run_llm_p", fake_run)
        budget = ss.NarrativeBudget()
        result = ss.backfill_for_sessions(
            sessions, use_llm=True, batch_size=2, budget=budget,
        )

        assert result["summarized"] == 3
        batches = [event for event in budget.status()["trace"] if event["event"] == "batch"]
        assert [
            (
                event["lane"], event["completed"], event["total"],
                event["item_count"], event["outcome"],
                event["requested_count"], event["valid_count"],
                event["unresolved_count"],
            )
            for event in batches
        ] == [
            ("session_summaries", 1, 2, 2, "completed", 2, 2, 0),
            ("session_summaries", 2, 2, 1, "completed", 1, 1, 0),
        ]
        assert all(event["elapsed_sec"] >= 0 for event in batches)
        assert all(
            not {"session_id", "summary", "prompt", "response"} & event.keys()
            for event in batches
        )

    def test_batch_omission_falls_back_and_never_overwrites_auto(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        sessions = self._sessions(jsonl_factory, 3)
        kept = sessions[1]
        upsert(
            kept.session_id, "keep this proven summary", "generated",
            project=kept.project, prompt_version=ss.PROMPT_VERSION - 1,
        )

        def fake_run(_prompt: str, _timeout: int):
            return ss.NarrativeCall(
                "\n".join([
                    json.dumps({
                        "session_id": sessions[0].session_id,
                        "summary": "Completed the first task safely",
                    }),
                    json.dumps({
                        "session_id": "invented-id",
                        "summary": "This must never reach the cache",
                    }),
                ]),
                "claude", "sonnet",
            )

        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(ss, "run_llm_p", fake_run)
        result = ss.backfill_for_sessions(sessions, use_llm=True)

        assert result["summarized"] == 1
        assert get(kept.session_id).summary == "keep this proven summary"
        assert get(kept.session_id).source == "generated"
        assert get(sessions[2].session_id).source == "extracted"
        assert get("invented-id") is None

    def test_batch_omission_retry_fully_recovers_and_parses_once(
        self, tmp_home: Path, jsonl_factory, monkeypatch,
    ):
        sessions = self._sessions(jsonl_factory, 3)
        calls = 0
        parse_calls = 0
        real_parse = ss._parse_batch_summaries

        def fake_run(prompt: str, _timeout: int, *, budget):
            nonlocal calls
            calls += 1
            ids = re.findall(r"\[session_id=([^;]+);", prompt)
            selected = ids[:2] if calls == 1 else ids
            return ss.NarrativeCall(
                "\n".join(
                    json.dumps({"session_id": sid, "summary": f"Done {sid}"})
                    for sid in selected
                ),
                "claude", "sonnet",
            )

        def counting_parse(text: str, requested_ids: set[str]):
            nonlocal parse_calls
            parse_calls += 1
            return real_parse(text, requested_ids)

        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(ss, "run_llm_p", fake_run)
        monkeypatch.setattr(ss, "_parse_batch_summaries", counting_parse)
        budget = ss.NarrativeBudget()
        result = ss.backfill_for_sessions(
            sessions, use_llm=True, budget=budget,
        )

        assert calls == 2
        assert parse_calls == 2  # primary once, retry once
        assert result["summarized"] == 3
        assert all(get(session.session_id).source == "generated" for session in sessions)
        retry = next(
            event for event in budget.trace
            if event.get("lane") == "session_summaries_retry"
            and event["event"] == "batch"
        )
        assert retry["outcome"] == "recovered"
        assert retry["requested_count"] == 1
        assert retry["valid_count"] == 1
        assert retry["recovered_count"] == 1
        assert retry["untried_count"] == 0
        assert retry["unresolved_count"] == 0

    def test_batch_omission_retry_unknown_only_is_empty_and_fails_closed(
        self, tmp_home: Path, jsonl_factory, monkeypatch,
    ):
        sessions = self._sessions(jsonl_factory, 4)
        calls = 0

        def fake_run(prompt: str, _timeout: int, *, budget):
            nonlocal calls
            calls += 1
            ids = re.findall(r"\[session_id=([^;]+);", prompt)
            if calls == 1:
                stdout = "\n".join(
                    json.dumps({"session_id": sid, "summary": f"Done {sid}"})
                    for sid in ids[:2]
                )
            else:
                stdout = "\n".join([
                    "not-json",
                    json.dumps({
                        "session_id": "invented-id",
                        "summary": "Must never reach cache",
                    }),
                ])
            return ss.NarrativeCall(stdout, "claude", "sonnet")

        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(ss, "run_llm_p", fake_run)
        budget = ss.NarrativeBudget()

        result = ss.backfill_for_sessions(
            sessions, use_llm=True, budget=budget,
        )

        assert calls == 2
        assert result["summarized"] == 2
        assert result["fallback"] == 2
        assert get("invented-id") is None
        retry = next(
            event for event in budget.trace
            if event.get("lane") == "session_summaries_retry"
            and event["event"] == "batch"
        )
        assert retry["outcome"] == "empty"
        assert retry["requested_count"] == 2
        assert retry["valid_count"] == 0
        assert retry["recovered_count"] == 0
        assert retry["unresolved_count"] == 2
        assert not {"session_id", "summary", "prompt", "response"} & retry.keys()

    def test_batch_omission_retry_partial_recovery_counts_all_unresolved(
        self, tmp_home: Path, jsonl_factory, monkeypatch,
    ):
        sessions = self._sessions(jsonl_factory, 6)
        calls = 0

        def fake_run(prompt: str, _timeout: int, *, budget):
            nonlocal calls
            calls += 1
            ids = re.findall(r"\[session_id=([^;]+);", prompt)
            selected = ids[:2] if calls == 1 else ids[:1]
            return ss.NarrativeCall(
                "\n".join(
                    json.dumps({"session_id": sid, "summary": f"Done {sid}"})
                    for sid in selected
                ),
                "claude", "sonnet",
            )

        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(ss, "run_llm_p", fake_run)
        budget = ss.NarrativeBudget()

        result = ss.backfill_for_sessions(
            sessions, use_llm=True, budget=budget,
        )

        assert calls == 2
        assert result["summarized"] == 3
        assert result["fallback"] == 3
        retry = next(
            event for event in budget.trace
            if event.get("lane") == "session_summaries_retry"
            and event["event"] == "batch"
        )
        assert retry["outcome"] == "partial"
        assert retry["requested_count"] == 3
        assert retry["recovered_count"] == 1
        assert retry["untried_count"] == 1
        assert retry["unresolved_count"] == 3

    def test_all_ten_probe_omissions_retry_strictly_smaller_subset(
        self, tmp_home: Path, jsonl_factory, monkeypatch,
    ):
        sessions = self._sessions(jsonl_factory, 10)
        seen_sizes: list[int] = []

        def fake_run(prompt: str, _timeout: int, *, budget):
            ids = re.findall(r"\[session_id=([^;]+);", prompt)
            seen_sizes.append(len(ids))
            selected = [] if len(seen_sizes) == 1 else ids
            return ss.NarrativeCall(
                "\n".join(
                    json.dumps({"session_id": sid, "summary": f"Done {sid}"})
                    for sid in selected
                ),
                "claude", "sonnet",
            )

        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(ss, "run_llm_p", fake_run)
        budget = ss.NarrativeBudget()

        result = ss.backfill_for_sessions(
            sessions, use_llm=True, budget=budget,
        )

        assert seen_sizes == [10, 5]
        assert result["summarized"] == 5
        assert result["fallback"] == 5
        retry = next(
            event for event in budget.trace
            if event.get("lane") == "session_summaries_retry"
            and event["event"] == "batch"
        )
        assert retry["requested_count"] == 5
        assert retry["untried_count"] == 5
        assert retry["unresolved_count"] == 5

    def test_omissions_over_retry_limit_leave_untried_rows_as_fallback(
        self, tmp_home: Path, jsonl_factory, monkeypatch,
    ):
        sessions = self._sessions(jsonl_factory, 10)
        calls = 0

        def fake_run(prompt: str, _timeout: int, *, budget):
            nonlocal calls
            calls += 1
            ids = re.findall(r"\[session_id=([^;]+);", prompt)
            selected = ids[:2] if calls == 1 else ids
            return ss.NarrativeCall(
                "\n".join(
                    json.dumps({"session_id": sid, "summary": f"Done {sid}"})
                    for sid in selected
                ),
                "claude", "sonnet",
            )

        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(ss, "run_llm_p", fake_run)
        budget = ss.NarrativeBudget()

        result = ss.backfill_for_sessions(
            sessions, use_llm=True, budget=budget,
        )

        assert calls == 2
        assert result["summarized"] == 7
        assert result["fallback"] == 3
        retry = next(
            event for event in budget.trace
            if event.get("lane") == "session_summaries_retry"
            and event["event"] == "batch"
        )
        assert retry["requested_count"] == ss.SUMMARY_OMISSION_RETRY_SIZE
        assert retry["recovered_count"] == 5
        assert retry["untried_count"] == 3
        assert retry["unresolved_count"] == 3

    def test_budget_exhausted_before_retry_records_zero_valid_rows(
        self, tmp_home: Path, jsonl_factory, monkeypatch,
    ):
        sessions = self._sessions(jsonl_factory, 3)
        budget = ss.NarrativeBudget(total_sec=90)
        calls = 0

        def fake_run(prompt: str, _timeout: int, *, budget):
            nonlocal calls
            calls += 1
            ids = re.findall(r"\[session_id=([^;]+);", prompt)
            budget.total_sec = 0
            return ss.NarrativeCall(
                "\n".join(
                    json.dumps({"session_id": sid, "summary": f"Done {sid}"})
                    for sid in ids[:2]
                ),
                "claude", "sonnet",
            )

        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(ss, "run_llm_p", fake_run)

        result = ss.backfill_for_sessions(
            sessions, use_llm=True, budget=budget,
        )

        assert calls == 1
        assert result["summarized"] == 2
        assert result["fallback"] == 1
        retry = next(
            event for event in budget.trace
            if event.get("lane") == "session_summaries_retry"
            and event["event"] == "batch"
        )
        assert retry["outcome"] == "budget_exhausted"
        assert retry["requested_count"] == 1
        assert retry["valid_count"] == 0
        assert retry["recovered_count"] == 0
        assert retry["unresolved_count"] == 1
        assert budget.stopped_reason == "budget_exhausted"

    def test_slow_retry_does_not_shrink_batch_after_fast_primary(
        self, monkeypatch,
    ):
        items = [
            (f"session-{index}", "project", f"[USER 1]\nTask {index}")
            for index in range(25)
        ]
        seen_sizes: list[int] = []
        clock = iter([0.0, 1.0, 2.0, 100.0, 101.0, 102.0])

        def fake_run(prompt: str, _timeout: int):
            ids = re.findall(r"\[session_id=([^;]+);", prompt)
            seen_sizes.append(len(ids))
            selected = ids[:-1] if len(seen_sizes) == 1 else ids
            return ss.NarrativeCall(
                "\n".join(
                    json.dumps({"session_id": sid, "summary": f"Done {sid}"})
                    for sid in selected
                ),
                "claude", "sonnet",
            )

        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(ss, "run_llm_p", fake_run)
        monkeypatch.setattr(ss.time, "monotonic", lambda: next(clock))

        result = ss.summarize_sessions_via_llm_batch(items, batch_size=40)

        assert len(result) == 25
        assert seen_sizes == [10, 1, 15]

    def test_probe_grows_next_batch_and_budget_exhaustion_falls_back(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        sessions = self._sessions(jsonl_factory, 25)
        seen_sizes: list[int] = []

        def fake_run(prompt: str, _timeout: int, *, budget):
            ids = re.findall(r"\[session_id=([^;]+);", prompt)
            seen_sizes.append(len(ids))
            return ss.NarrativeCall(
                "\n".join(
                    json.dumps({"session_id": sid, "summary": f"Done {sid}"})
                    for sid in ids
                ),
                "antigravity", "gemini-3.6-flash-low",
            )

        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(ss, "run_llm_p", fake_run)
        budget = ss.NarrativeBudget(total_sec=90)
        result = ss.backfill_for_sessions(
            sessions, use_llm=True, batch_size=40, budget=budget,
        )

        assert seen_sizes == [10, 15]  # probe, then bounded growth to remaining work
        assert result["summarized"] == 25

    def test_slow_probe_keeps_following_batches_at_ten(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        sessions = self._sessions(jsonl_factory, 25)
        seen_sizes: list[int] = []

        def fake_run(prompt: str, _timeout: int, *, budget):
            ids = re.findall(r"\[session_id=([^;]+);", prompt)
            seen_sizes.append(len(ids))
            return ss.NarrativeCall(
                "\n".join(
                    json.dumps({"session_id": sid, "summary": f"Done {sid}"})
                    for sid in ids
                ),
                "antigravity", "gemini-3.6-flash-low",
            )

        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(ss, "run_llm_p", fake_run)
        monkeypatch.setattr(ss, "SUMMARY_BATCH_SLOW_SEC", 0)

        ss.backfill_for_sessions(
            sessions,
            use_llm=True,
            batch_size=40,
            budget=ss.NarrativeBudget(),
        )

        assert seen_sizes == [10, 10, 5]

    def test_exhausted_budget_makes_no_call_and_uses_fallback(
        self, tmp_home: Path, jsonl_factory, monkeypatch
    ):
        sessions = self._sessions(jsonl_factory, 2)
        budget = ss.NarrativeBudget(total_sec=0)
        monkeypatch.setattr(ss, "llm_available", lambda: True)
        monkeypatch.setattr(
            ss, "run_llm_p",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no call")),
        )

        result = ss.backfill_for_sessions(sessions, use_llm=True, budget=budget)

        assert result["summarized"] == 0
        assert result["fallback"] == 2
        assert budget.stopped_reason == "budget_exhausted"


class TestNeedsLlm:
    def test_matrix(self):
        SS = ss.SessionSummary
        assert ss._needs_llm(None) is True
        assert ss._needs_llm(SS("i", "s", "no_evidence")) is False
        assert ss._needs_llm(SS("i", "s", "extracted")) is True
        cur = SS(
            "i", "s", "generated", prompt_version=ss.PROMPT_VERSION,
            narrator_provider="claude", narrator_model="sonnet",
            narrator_fingerprint=ss.narrative_config_fingerprint(),
        )
        assert ss._needs_llm(cur) is False
        assert ss._needs_llm(cur, force=True) is True
        stale = SS("i", "s", "generated", prompt_version=ss.PROMPT_VERSION - 1)
        assert ss._needs_llm(stale) is True
        # legacy NULL prompt_version coerces to 0 → stale
        assert ss._needs_llm(SS("i", "s", "generated", prompt_version=None)) is True


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
            assert {
                "evidence_fingerprint",
                "observed_evidence_fingerprint",
            } <= ss._table_columns(conn, "session_summaries")
        finally:
            conn.close()

    def test_legacy_rows_keep_prompt_but_get_unknown_evidence(self, tmp_home: Path):
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
            # 'auto' is a fixed historical literal here, not SOURCE_GENERATED:
            # this row simulates a pre-migration-5 database, and migration 5's
            # evidence-identity backfill matches on the literal pre-#206 value.
            ("legacy", "old summary", "auto", "proj", 1.0),
        )
        raw.commit()
        raw.close()
        # First ccstory connect preserves the old prompt adoption and leaves
        # narrator provenance blank, while evidence stays explicitly unknown.
        # It remains selected for lazy refresh rather than causing a global
        # transcript scan during migration.
        row = get("legacy")
        assert row is not None
        assert row.prompt_version == ss.PROMPT_VERSION
        assert row.evidence_fingerprint == ss.LEGACY_UNKNOWN_EVIDENCE
        assert ss.summary_evidence_status(row) == "legacy"
        assert ss._needs_llm(row) is True
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
            # Narrative families keep their rows but are deliberately NOT
            # adopted: their prompts changed after v0.5.1, so re-synthesis
            # (a few calls per window) is the intended outcome (#118).
            assert migrated.execute(
                "SELECT summary, input_fingerprint FROM period_aggregates"
            ).fetchone() == ("aggregate", "")
            assert migrated.execute(
                "SELECT narrative, input_fingerprint "
                "FROM comparison_narratives"
            ).fetchone() == ("comparison", "")
        finally:
            migrated.close()
        # The classification family must survive *behaviorally*: adopted
        # under the current fingerprint and readable through the production
        # path. Row survival alone certified dead cache as "preserved" (#118).
        assert ss._classify_cache_get_many(["s1"]) == {"s1": "coding"}

    def test_v2_db_with_orphaned_fingerprints_adopts_classifications(
        self, tmp_home: Path,
    ):
        # An install that already upgraded to 0.5.1: user_version=2 with
        # rows migration 2 stamped '' (#118). Migration 3 must resurrect
        # the classification family through the production read path,
        # while narrative families intentionally stay unstamped.
        raw = sqlite3.connect(str(ss.DB_PATH))
        raw.execute("BEGIN")
        ss._migration_1_baseline(raw)
        ss._migration_2_cache_fingerprints(raw)
        raw.execute("PRAGMA user_version = 2")
        raw.execute(
            "INSERT INTO session_content_buckets VALUES (?, ?, ?, '')",
            ("s1", "coding", 1.0),
        )
        raw.execute(
            "INSERT INTO period_aggregates VALUES (?, ?, ?, ?, ?, '')",
            ("p", "coding", "aggregate", "s1", 1.0),
        )
        raw.commit()
        raw.close()

        assert ss._classify_cache_get_many(["s1"]) == {"s1": "coding"}
        check = sqlite3.connect(str(ss.DB_PATH))
        try:
            assert check.execute("PRAGMA user_version").fetchone()[0] == (
                ss.CACHE_SCHEMA_VERSION
            )
            assert check.execute(
                "SELECT input_fingerprint FROM period_aggregates"
            ).fetchone() == ("",)
        finally:
            check.close()

    def test_adopted_rows_invalidate_when_config_changes(
        self, tmp_home: Path,
    ):
        # Adoption stamps the *current* fingerprint — a later config/vocab
        # change must still invalidate adopted rows like any other row.
        raw = sqlite3.connect(str(ss.DB_PATH))
        raw.execute("BEGIN")
        ss._migration_1_baseline(raw)
        ss._migration_2_cache_fingerprints(raw)
        raw.execute("PRAGMA user_version = 2")
        raw.execute(
            "INSERT INTO session_content_buckets VALUES (?, ?, ?, '')",
            ("s1", "coding", 1.0),
        )
        raw.commit()
        raw.close()

        assert ss._classify_cache_get_many(["s1"]) == {"s1": "coding"}
        assert ss._classify_cache_get_many(
            ["s1"], input_fingerprint="different-config"
        ) == {}

    def test_corrupt_db_raises_catchable_error_with_recovery_hint(
        self, tmp_home: Path,
    ):
        ss.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        ss.DB_PATH.write_bytes(b"definitely not a sqlite database" * 4)

        with pytest.raises(ss.CacheUnavailable) as exc:
            ss._connect()
        msg = str(exc.value)
        assert "corrupted" in msg
        assert f"rm {ss.DB_PATH}" in msg
        # The whole point of #119: a host's plain `except Exception` works.
        assert isinstance(exc.value, Exception)

    def test_locked_db_is_not_misreported_as_corruption(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Two processes racing the one-shot migration surface `database is
        # locked` — a transient condition. Advising `rm` here (the corrupt-
        # cache hint) would tell the user to destroy a healthy cache (#119).
        def _locked(_conn: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(ss, "_MIGRATIONS", (_locked,))
        with pytest.raises(ss.CacheUnavailable) as exc:
            ss._connect()
        msg = str(exc.value)
        assert "locked" in msg
        assert "retry" in msg
        assert "rm " not in msg

    def test_already_current_db_skips_migrations(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        upsert("kept", "preserved summary", "generated")

        def _boom(_conn):
            raise AssertionError("current schema must not rerun migrations")

        monkeypatch.setattr(ss, "_MIGRATIONS", (_boom, _boom))
        assert get("kept").summary == "preserved summary"

    def test_unchanged_cache_verifies_schema_once_per_process(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        original = ss._run_migrations
        calls = 0

        def tracking_migrations(conn: sqlite3.Connection) -> None:
            nonlocal calls
            calls += 1
            original(conn)

        ss._verified_db_stamps.clear()
        monkeypatch.setattr(ss, "_run_migrations", tracking_migrations)
        try:
            first = ss._connect()
            first.close()
            second = ss._connect()
            second.close()
            assert calls == 1
        finally:
            ss._verified_db_stamps.clear()

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

    def test_newer_schema_is_left_untouched(self, tmp_home: Path):
        raw = sqlite3.connect(str(ss.DB_PATH))
        raw.execute(f"PRAGMA user_version = {ss.CACHE_SCHEMA_VERSION + 1}")
        raw.commit()
        raw.close()

        # A catchable exception, not SystemExit: in-process hosts (library
        # consumers, the MCP server) must survive an incompatible cache (#119).
        with pytest.raises(ss.CacheUnavailable, match="newer ccstory"):
            ss._connect()

        check = sqlite3.connect(str(ss.DB_PATH))
        try:
            assert check.execute("PRAGMA user_version").fetchone()[0] == (
                ss.CACHE_SCHEMA_VERSION + 1
            )
        finally:
            check.close()

    def test_v6_migration_rewrites_known_source_values_and_preserves_caller_defined(
        self, tmp_home: Path,
    ):
        """#206: record/auto/fallback/skipped rename; other values survive."""
        raw = sqlite3.connect(str(ss.DB_PATH))
        raw.execute("BEGIN")
        ss._migration_5_summary_evidence_identity(raw)
        raw.execute("PRAGMA user_version = 5")
        raw.executemany(
            "INSERT INTO session_summaries "
            "(session_id, summary, source, project, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("r1", "human text", "record", "proj", 1.0),
                ("a1", "auto text", "auto", "proj", 2.0),
                ("f1", "fallback text", "fallback", "proj", 3.0),
                ("s1", "skipped text", "skipped", "proj", 4.0),
                ("c1", "cloud text", "cloud:mybranch", "proj", 5.0),
            ],
        )
        raw.commit()
        raw.close()

        migrated = ss._connect()
        try:
            assert migrated.execute("PRAGMA user_version").fetchone()[0] == (
                ss.CACHE_SCHEMA_VERSION
            )
            rows = dict(
                migrated.execute(
                    "SELECT session_id, source FROM session_summaries"
                ).fetchall()
            )
        finally:
            migrated.close()

        assert rows == {
            "r1": "provided",
            "a1": "generated",
            "f1": "extracted",
            "s1": "no_evidence",
            # Caller-defined values never matched the legacy vocabulary and
            # must survive byte-for-byte.
            "c1": "cloud:mybranch",
        }


@pytest.fixture
def connect_counter(monkeypatch: pytest.MonkeyPatch):
    """Counts real ``_connect()`` calls, i.e. actual ``sqlite3.connect()``
    opens -- the cost #175 PR A's scoping is meant to reduce. Schema
    verification itself was already deduped per-process by #185's stamp
    check (see ``test_unchanged_cache_verifies_schema_once_per_process``
    above); this isolates the separate, remaining cost of one OS-level file
    open per cache call.
    """
    counter = [0]
    original = ss._connect

    def counting_connect():
        counter[0] += 1
        return original()

    monkeypatch.setattr(ss, "_connect", counting_connect)
    return counter


class TestCacheSession:
    """Tests for the scoped-connection API (#175 PR A).

    ``cache_session()`` lets several cache calls in a row share one
    verified/migrated connection instead of one open per call; a plain
    direct call outside any ``with cache_session():`` block keeps opening
    (and closing) its own, unchanged from before this API existed. See the
    module comment above ``CacheSession`` in ``session_summarizer.py`` for
    the full design rationale.
    """

    # --- the core reuse contract --------------------------------------

    def test_standalone_calls_each_open_their_own_connection(
        self, tmp_home: Path, connect_counter,
    ):
        """Baseline, unchanged from before #175: no surrounding scope means
        every call still gets its own short-lived connection."""
        upsert("a", "summary a", "generated")
        upsert("b", "summary b", "generated")
        get("a")
        get_many(["a", "b"])
        assert connect_counter[0] == 4

    def test_nested_calls_inside_a_scope_share_one_connection(
        self, tmp_home: Path, connect_counter,
    ):
        """The same four calls as above, now wrapped in one scope: the
        defining before/after evidence for this PR's operation-count claim.
        """
        with cache_session():
            upsert("a", "summary a", "generated")
            upsert("b", "summary b", "generated")
            get("a")
            get_many(["a", "b"])
        assert connect_counter[0] == 1
        # And the data is identical either way -- fewer connections, same
        # observable result.
        assert get("a").summary == "summary a"
        assert get("b").summary == "summary b"

    def test_backfill_scale_nested_calls_share_one_connection(
        self, tmp_home: Path, connect_counter,
    ):
        """A larger echo of #175's original motivating scenario -- a
        100-session backfill loop, each session a separate `upsert()` call.
        Before this API, that was 100 connection opens (one per call, as
        the standalone-calls test above shows for N=4); wrapped in one
        scope, it is 1 regardless of N. PR B (not this PR) is what will
        wire this scope around the real `build_recap()` backfill loop --
        this test proves the mechanism it will rely on.
        """
        with cache_session():
            for i in range(100):
                upsert(f"bulk-{i}", f"summary {i}", "generated")
        assert connect_counter[0] == 1
        assert get("bulk-0").summary == "summary 0"
        assert get("bulk-99").summary == "summary 99"

    def test_triple_nested_scopes_still_share_one_connection(
        self, tmp_home: Path, connect_counter,
    ):
        with cache_session():
            with cache_session():
                with cache_session() as innermost:
                    innermost.execute("SELECT 1")
        assert connect_counter[0] == 1

    def test_repeated_sequential_scopes_do_not_leak_between_calls(
        self, tmp_home: Path, connect_counter,
    ):
        """Simulates the MCP server handling several tool calls in a row in
        one process: each call's scope is independent -- nothing "active"
        survives from one call into the next.
        """
        for i in range(3):
            with cache_session():
                upsert(f"req-{i}", f"summary {i}", "generated")
            assert ss._active_session.get() is None
        assert connect_counter[0] == 3
        for i in range(3):
            assert get(f"req-{i}").summary == f"summary {i}"

    # --- reused across a real narrator-call boundary ---------------------

    def test_write_leaves_no_open_transaction_for_a_subsequent_slow_call(
        self, tmp_home: Path,
    ):
        """A shared connection may still be open while a narrator (LLM)
        subprocess, `git`, or network call runs between two nested cache
        calls in the same outer scope -- that alone never blocks another
        process. What must never happen is a *write transaction* left open
        across such a call. Every write in this module commits immediately
        after its own `execute()`, so the connection is always
        transaction-free the moment control returns to the caller, shared
        or not.
        """
        with cache_session() as conn:
            upsert("sess", "summary", "generated")
            # The point, in real callers, right after a write returns and
            # before a narrator subprocess call would run (see
            # `synthesize_overall_for_period()`, which does exactly this).
            assert conn.in_transaction is False

    def test_synthesize_overall_reuses_the_outer_scope_across_the_narrator_call(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch, connect_counter,
    ):
        """`synthesize_overall_for_period()` itself opens two cache scopes
        with a narrator subprocess call in between (a read to check the
        cache, then a write to store the result). Wrapped in one outer
        scope, both nested calls reuse the same connection instead of each
        opening their own -- proving the seam works through a real
        production function, not just synthetic calls.
        """
        class Result:
            returncode = 0
            stdout = "generated narrative"
            stderr = ""

        monkeypatch.setattr(ss, "claude_bin_available", lambda: True)
        monkeypatch.setattr(ss, "run_claude_p", lambda *_a: Result())

        with cache_session():
            narrative = synthesize_overall_for_period(
                period_key="2026-05",
                category_hours=[("coding", 2.0)],
                sessions_by_category={"coding": [("sess-a", "did A")]},
            )
        assert narrative == "generated narrative"
        assert connect_counter[0] == 1

    # --- safety guards: DB_PATH / process / thread ----------------------

    def test_db_path_change_mid_scope_never_reuses_the_old_connection(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """A host (or a test) that repoints DB_PATH between calls must
        never keep writing through a session opened against the old path.
        """
        old_db = ss.DB_PATH
        new_db = tmp_home / ".ccstory" / "cache2.db"
        with cache_session():
            upsert("old-path-sess", "before", "generated")
            monkeypatch.setattr(ss, "DB_PATH", new_db)
            upsert("new-path-sess", "after", "generated")
            assert get("new-path-sess").summary == "after"

        assert new_db.exists()
        monkeypatch.setattr(ss, "DB_PATH", old_db)
        assert get("old-path-sess") is not None
        # The new-path row landed only in the new file.
        assert get("new-path-sess") is None

    def test_pid_mismatch_is_never_reused(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch, connect_counter,
    ):
        """Drives the same guard a forked child process would hit: the
        *same* CacheSession object (same connection) sitting in a copied
        context-local, but created by a pid that is no longer the current
        one. A real fork is POSIX-only (this suite also runs on Windows,
        see .github/workflows/test.yml) and timing-dependent to exercise
        deterministically, so this drives the guard condition directly
        instead.
        """
        with cache_session():
            upsert("parent-sess", "from parent pid", "generated")
            real_getpid = ss.os.getpid()
            monkeypatch.setattr(ss.os, "getpid", lambda: real_getpid + 1)
            upsert("child-sess", "from other pid", "generated")
        # Outer scope's own connect (1) + the pid-mismatched nested call's
        # own fresh connect, since it correctly refused to reuse (1) = 2.
        # (Verification reads below are standalone calls outside any scope
        # and intentionally excluded from this count.)
        assert connect_counter[0] == 2

        assert get("parent-sess") is not None
        assert get("child-sess") is not None

    def test_plain_thread_never_sees_the_parent_threads_session(
        self, tmp_home: Path, connect_counter,
    ):
        """A raw ``threading.Thread`` starts with a fresh context by
        default (unlike an async task, which copies one) -- so this proves
        the observable end-to-end behavior: a new thread transparently gets
        its own connection and works correctly, never touching the parent
        thread's.
        """
        with cache_session():
            upsert("main-thread-sess", "from main", "generated")

            def _in_thread():
                upsert("other-thread-sess", "from thread", "generated")

            t = threading.Thread(target=_in_thread)
            t.start()
            t.join(timeout=10)
        # Outer scope's own connect (1) + the new thread's own fresh
        # connect, since a plain Thread starts with no inherited context to
        # reuse (1) = 2. (Verification reads below are standalone calls
        # outside any scope and intentionally excluded from this count.)
        assert connect_counter[0] == 2

        assert get("main-thread-sess") is not None
        other = get("other-thread-sess")
        assert other is not None
        assert other.summary == "from thread"

    def test_context_copied_into_a_thread_still_refuses_cross_thread_reuse(
        self, tmp_home: Path, connect_counter,
    ):
        """Some hosts (e.g. anyio's to-thread offloading, which an MCP
        server may use to run a sync tool handler) explicitly copy the
        calling context into a worker thread via ``contextvars.
        copy_context().run(...)`` -- unlike a plain ``threading.Thread``
        (see the sibling test above), the CacheSession reference itself
        *does* cross into the new thread this way. The connection it wraps
        must still never be touched there: sqlite3.Connection is not
        thread-safe, and silently reusing it could corrupt the cache. This
        is what the ``thread_id`` field on CacheSession specifically
        guards against.
        """
        result = {}
        with cache_session():
            upsert("main-sess", "from main", "generated")
            main_thread_id = threading.get_ident()
            ctx = contextvars.copy_context()

            def _in_copied_context_thread():
                propagated = ss._active_session.get()
                # Confirms the session *object* really did propagate into
                # this thread (a plain Thread would not) -- otherwise the
                # rest of this test would not be exercising the guard.
                result["session_propagated"] = (
                    propagated is not None
                    and propagated.thread_id == main_thread_id
                    and threading.get_ident() != main_thread_id
                )
                upsert("thread-sess", "from copied-context thread", "generated")

            t = threading.Thread(target=lambda: ctx.run(_in_copied_context_thread))
            t.start()
            t.join(timeout=10)

        assert result["session_propagated"] is True
        # Main scope's upsert (1 connect) + the copied-context thread's own
        # fresh connect (its threading.get_ident() differs from the one
        # recorded on the propagated session, so it is never reused) = 2.
        # (Verification read below is a standalone call outside any scope
        # and intentionally excluded from this count.)
        assert connect_counter[0] == 2

        thread_row = get("thread-sess")
        assert thread_row is not None
        assert thread_row.summary == "from copied-context thread"

    def test_two_concurrent_processes_each_get_their_own_connection(
        self, tmp_home: Path,
    ):
        """A real second OS process (not a fork -- genuinely no shared
        memory, no shared context-local) must never be blocked or
        corrupted by the first. Pre-migrating isolates this to "two
        processes writing through cache_session() at once"; first-run
        migration-lock behavior is already covered by
        ``test_locked_db_is_not_misreported_as_corruption``.
        """
        ss._connect().close()
        db_path = ss.DB_PATH

        procs = [
            subprocess.Popen(
                [
                    sys.executable, "-c", _CACHE_SESSION_WORKER_SCRIPT,
                    str(db_path), f"sess-{i}", f"summary {i}",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(2)
        ]
        for i, p in enumerate(procs):
            _out, err = p.communicate(timeout=30)
            assert p.returncode == 0, f"worker {i} failed:\n{err}"

        assert get("sess-0") is not None
        assert get("sess-0").summary == "summary 0"
        assert get("sess-1") is not None
        assert get("sess-1").summary == "summary 1"

    # --- exception / rollback safety -------------------------------------

    def test_exception_in_nested_scope_rolls_back_without_closing_shared_connection(
        self, tmp_home: Path,
    ):
        with cache_session() as outer_conn:
            upsert("committed-before", "should survive", "generated")

            with pytest.raises(sqlite3.IntegrityError):
                with cache_session() as inner_conn:
                    # Two inserts under the same primary key: the second
                    # raises mid-transaction, leaving a partial write that
                    # the nested scope must roll back on its way out --
                    # otherwise it would linger on the connection the outer
                    # scope still owns and later reuses.
                    inner_conn.execute(
                        "INSERT INTO session_summaries "
                        "(session_id, summary, source, project, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        ("dup", "first", "generated", None, 1.0),
                    )
                    inner_conn.execute(
                        "INSERT INTO session_summaries "
                        "(session_id, summary, source, project, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        ("dup", "second", "generated", None, 2.0),
                    )

            # The failed insert never landed (proving the rollback actually
            # ran), and the shared connection is still usable afterward --
            # the inner failure did not tear down the outer scope.
            assert outer_conn.execute(
                "SELECT COUNT(*) FROM session_summaries WHERE session_id = 'dup'"
            ).fetchone()[0] == 0
            upsert("committed-after", "also survives", "generated")

        assert get("committed-before").summary == "should survive"
        assert get("committed-after").summary == "also survives"
        assert get("dup") is None

    def test_migration_failure_rolls_back_and_leaves_no_active_session(
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
            with cache_session():
                pass

        # No broken session was left registered as "active" for the next
        # caller to (unsafely) inherit.
        assert ss._active_session.get() is None
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

    # --- existing corruption/lock/newer-schema messages, unchanged -------

    def test_corrupt_db_message_preserved_through_cache_session(
        self, tmp_home: Path,
    ):
        ss.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        ss.DB_PATH.write_bytes(b"definitely not a sqlite database" * 4)

        with pytest.raises(ss.CacheUnavailable) as exc:
            with cache_session():
                pass
        msg = str(exc.value)
        assert "corrupted" in msg
        assert f"rm {ss.DB_PATH}" in msg

    def test_locked_db_message_preserved_through_cache_session(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        def _locked(_conn: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(ss, "_MIGRATIONS", (_locked,))
        with pytest.raises(ss.CacheUnavailable) as exc:
            with cache_session():
                pass
        msg = str(exc.value)
        assert "locked" in msg
        assert "retry" in msg
        assert "rm " not in msg

    def test_newer_schema_message_preserved_through_cache_session(
        self, tmp_home: Path,
    ):
        raw = sqlite3.connect(str(ss.DB_PATH))
        raw.execute(f"PRAGMA user_version = {ss.CACHE_SCHEMA_VERSION + 1}")
        raw.commit()
        raw.close()

        with pytest.raises(ss.CacheUnavailable, match="newer ccstory"):
            with cache_session():
                pass


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
            "The input summaries may be in a different language — still respond ONLY in Traditional Chinese, translating concepts as needed. "
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
            "The input summaries may be in a different language — still respond ONLY in Traditional Chinese, translating concepts as needed. "
            "Keep the same length / format limits regardless of language."
        )
        assert "--- CLAUDE.md ---" not in directive

    def test_short_language_code_is_expanded_for_the_model(
        self, tmp_home: Path, monkeypatch,
    ):
        """``--lang en`` must be an unambiguous English instruction."""
        monkeypatch.setenv(ss.CCSTORY_LANG_ENV, "en")
        ss.language_directive.cache_clear()
        directive = language_directive()
        assert "Respond in English." in directive
        assert "ONLY in English" in directive

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
            "The input summaries may be in a different language — still respond ONLY in Korean, translating concepts as needed. "
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
            "The input summaries may be in a different language — still respond ONLY in Traditional Chinese, translating concepts as needed. "
            "Keep the same length / format limits regardless of language."
        )

    def test_blank_env_var_falls_through(self, tmp_home: Path, monkeypatch):
        # Empty / whitespace env var must not poison the chain.
        monkeypatch.setenv(ss.CCSTORY_LANG_ENV, "   ")
        ss.language_directive.cache_clear()
        assert language_directive() == "Respond in English."

    def test_explicit_override_directive_includes_multilingual_reinforcement(
        self, tmp_home: Path, monkeypatch,
    ):
        """Issue #131: explicit language overrides must instruct Claude to respond
        ONLY in the target language even if input summaries are in another language.
        The default hardcoded fallback ('Respond in English.') does NOT include this line.
        """
        monkeypatch.setenv(ss.CCSTORY_LANG_ENV, "English")
        ss.language_directive.cache_clear()
        directive_override = language_directive()
        assert (
            "The input summaries may be in a different language — still respond ONLY in English, translating concepts as needed."
            in directive_override
        )

        monkeypatch.delenv(ss.CCSTORY_LANG_ENV, raising=False)
        monkeypatch.setattr(ss, "_detect_system_locale", lambda: None)
        ss.language_directive.cache_clear()
        directive_fallback = language_directive()
        assert directive_fallback == "Respond in English."
        assert "The input summaries may be in a different language" not in directive_fallback

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
    def test_prompt_discourages_padding_bullets_to_the_cap(self):
        # Regression guard: real cached narratives showed EVERY thread
        # landing on exactly 3/3 bullets (the max) across 8+ real weekly
        # windows — the range was being read as a fixed count, not a cap.
        # A live regen with this wording produced 2-4 bullets (avg 2.5).
        # Guards against silently dropping the anti-padding instruction.
        assert "don't pad" in ss._OVERALL_PROMPT
        assert "1-3 bullet" in ss._OVERALL_PROMPT

    def test_prompt_names_inferred_narrative_as_work_themes(self):
        assert "WORK THEMES" in ss._OVERALL_PROMPT
        old_term = "GOAL" + " THREADS"
        assert old_term not in ss._OVERALL_PROMPT
        assert old_term.lower() not in ss._OVERALL_PROMPT.lower()

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

    def test_subhour_drift_keeps_cache_warm(self, tmp_home: Path, monkeypatch):
        """#121: the active window's hours creep between reruns (the
        running session itself accrues time). Sub-hour drift must stay a
        cache hit instead of re-burning a 90s claude -p call."""
        class Result:
            returncode = 0
            stdout = "warm prose"
            stderr = ""

        monkeypatch.setattr(ss, "claude_bin_available", lambda: True)
        monkeypatch.setattr(ss, "run_claude_p", lambda *_a: Result())
        base = dict(
            period_key="2026-07",
            sessions_by_category={"coding": [("sess-a", "did A")]},
        )
        assert synthesize_overall_for_period(
            category_hours=[("coding", 2.0)], **base) == "warm prose"

        def boom(*_a):
            raise AssertionError("sub-hour drift must not re-run claude -p")

        monkeypatch.setattr(ss, "run_claude_p", boom)
        assert synthesize_overall_for_period(
            category_hours=[("coding", 2.4)], **base) == "warm prose"

    def test_whole_hour_crossing_still_invalidates(
        self, tmp_home: Path, monkeypatch,
    ):
        class Result:
            returncode = 0
            stdout = "warm prose"
            stderr = ""

        monkeypatch.setattr(ss, "claude_bin_available", lambda: True)
        monkeypatch.setattr(ss, "run_claude_p", lambda *_a: Result())
        base = dict(
            period_key="2026-07",
            sessions_by_category={"coding": [("sess-a", "did A")]},
        )
        assert synthesize_overall_for_period(
            category_hours=[("coding", 2.0)], **base) == "warm prose"
        # 2.0 → 3.1 crosses a whole hour: must attempt a refresh (claude
        # stubbed unavailable → None proves the attempt).
        monkeypatch.setattr(ss, "claude_bin_available", lambda: False)
        assert synthesize_overall_for_period(
            category_hours=[("coding", 3.1)], **base) is None

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
