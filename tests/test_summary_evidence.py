"""Evidence-bound session-summary cache behavior (#188 PR B)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from rich.console import Console

from ccstory import recap
from ccstory import session_summarizer as ss
from ccstory.providers.claude import ClaudeCodeProvider
from ccstory.report import build_report_json, render_report, top_focus_narrative
from ccstory.time_tracking import CategoryRollup, SessionStat
from ccstory.token_usage import UsageReport
from tests.conftest import _ts, make_assistant_msg, make_user_msg


def _session(jsonl_factory, sid: str = "evidence-session"):
    path = jsonl_factory(
        "-Users-alice-code-evidence-project",
        sid,
        [
            make_user_msg("Implement the evidence cache", _ts(2026, 5, 10, 10)),
            make_assistant_msg(
                "Implemented the first version",
                _ts(2026, 5, 10, 10, 1),
                f"{sid}-assistant-1",
            ),
        ],
    )
    stat = ClaudeCodeProvider().parse_session(path)
    assert stat is not None
    return stat, path


def _batch_basis(path, sid: str) -> ss.PreparedSummaryEvidence:
    project, excerpt = ss._extract_excerpt(path)
    return ss._prepare_summary_evidence(
        sid,
        project,
        excerpt,
        lane=ss._SUMMARY_EVIDENCE_BATCH_LANE,
    )


def _append_growth(path, sid: str) -> None:
    records = [
        make_user_msg("Also cover transcript growth", _ts(2026, 5, 10, 10, 2)),
        make_assistant_msg(
            "Growth handling shipped and tests passed",
            _ts(2026, 5, 10, 10, 3),
            f"{sid}-assistant-2",
        ),
    ]
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _successful_batch(summary: str):
    def run(prompt: str, _timeout: int, **_kwargs):
        ids = [
            part.split(";", 1)[0]
            for part in prompt.split("[session_id=")[1:]
        ]
        return ss.NarrativeCall(
            "\n".join(
                json.dumps({"session_id": sid, "summary": summary})
                for sid in ids
            ),
            "claude",
            "sonnet",
        )

    return run


def test_v5_migration_marks_auto_legacy_without_transcript_or_narrator(
    tmp_home, monkeypatch,
):
    raw = sqlite3.connect(str(ss.DB_PATH))
    ss._migration_4_narrator_provenance(raw)
    raw.execute(
        """INSERT INTO session_summaries
           (session_id, summary, source, project, created_at, prompt_version,
            narrator_provider, narrator_model, narrator_fingerprint)
           VALUES ('legacy', 'kept history', 'auto', 'project', 42, ?,
                   'claude', 'sonnet', 'old-policy')""",
        (ss.PROMPT_VERSION,),
    )
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()

    monkeypatch.setattr(
        ss,
        "_extract_excerpt",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("migration must not scan transcripts")
        ),
    )
    monkeypatch.setattr(
        ss,
        "run_llm_p",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("migration must not call a narrator")
        ),
    )

    row = ss.get("legacy")
    assert row is not None
    assert row.summary == "kept history"
    assert row.created_at == 42
    assert row.evidence_fingerprint == ss.LEGACY_UNKNOWN_EVIDENCE
    assert row.observed_evidence_fingerprint == ss.LEGACY_UNKNOWN_EVIDENCE
    assert ss.summary_evidence_status(row) == "legacy"
    # Idempotent on another open; no duplicate ALTER and no row rewrite.
    assert ss.get("legacy").created_at == 42


def test_exact_bounded_evidence_is_stable_and_neighbor_independent():
    excerpt = (
        "[USER 1]\nFIRST " + "x" * 900 + "\n\n"
        "[USER LATE]\nLATEST " + "y" * 900 + "\n\n"
        "[ASSISTANT END]\nDONE " + "z" * 900 + " TESTS_PASSED"
    )
    first = ss._prepare_summary_evidence(
        "one", "project", excerpt, lane=ss._SUMMARY_EVIDENCE_BATCH_LANE,
    )
    repeated = ss._prepare_summary_evidence(
        "one", "project", excerpt, lane=ss._SUMMARY_EVIDENCE_BATCH_LANE,
    )
    neighbor = ss._prepare_summary_evidence(
        "two", "other-project", "[USER 1]\nneighbor",
        lane=ss._SUMMARY_EVIDENCE_BATCH_LANE,
    )

    assert len(first.bounded_evidence) == ss._SUMMARY_BATCH_EXCERPT_CHARS
    assert first.evidence_fingerprint == repeated.evidence_fingerprint
    assert neighbor.evidence_fingerprint != first.evidence_fingerprint
    # Re-preparing the first item after an unrelated item cannot rotate it.
    assert ss._prepare_summary_evidence(
        "one", "project", excerpt, lane=ss._SUMMARY_EVIDENCE_BATCH_LANE,
    ).evidence_fingerprint == first.evidence_fingerprint
    assert ss._prepare_summary_evidence(
        "one", "renamed-project", excerpt,
        lane=ss._SUMMARY_EVIDENCE_BATCH_LANE,
    ).evidence_fingerprint != first.evidence_fingerprint
    assert ss._prepare_summary_evidence(
        "one", "project", excerpt, lane=ss._SUMMARY_EVIDENCE_SINGLE_LANE,
    ).evidence_fingerprint != first.evidence_fingerprint


def test_stable_evidence_hits_cache_then_growth_rotates_and_refreshes(
    tmp_home, jsonl_factory, monkeypatch,
):
    stat, path = _session(jsonl_factory)
    monkeypatch.setattr(ss, "llm_available", lambda: True)
    calls = 0

    def first_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _successful_batch("Generated from first evidence")(*args, **kwargs)

    monkeypatch.setattr(ss, "run_llm_p", first_run)
    result = ss.backfill_for_sessions([stat], use_llm=True)
    assert result["summarized"] == 1
    initial = ss.get(stat.session_id)
    assert initial is not None
    assert ss.summary_evidence_status(initial) == "current"
    initial_basis = initial.evidence_fingerprint

    monkeypatch.setattr(
        ss,
        "run_llm_p",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("stable evidence must hit the cache")
        ),
    )
    stable = ss.backfill_for_sessions([stat], use_llm=True)
    assert stable["already"] == 1
    assert calls == 1

    _append_growth(path, stat.session_id)
    monkeypatch.setattr(
        ss, "run_llm_p", _successful_batch("Regenerated after transcript growth"),
    )
    grown = ss.backfill_for_sessions([stat], use_llm=True)
    refreshed = ss.get(stat.session_id)
    assert grown["regenerated"] == 1
    assert refreshed.summary == "Regenerated after transcript growth"
    assert refreshed.evidence_fingerprint != initial_basis
    assert refreshed.evidence_fingerprint == refreshed.observed_evidence_fingerprint
    assert ss.summary_evidence_status(refreshed) == "current"


@pytest.mark.parametrize("failure", ["call", "omission", "budget"])
def test_failed_growth_refresh_preserves_history_and_marks_stale(
    failure, tmp_home, jsonl_factory, monkeypatch,
):
    stat, path = _session(jsonl_factory, f"stale-{failure}")
    basis = _batch_basis(path, stat.session_id)
    ss.upsert(
        stat.session_id,
        "Previously proven outcome",
        "generated",
        project=basis.project,
        prompt_version=ss.PROMPT_VERSION,
        narrator_provider="claude",
        narrator_model="sonnet",
        narrator_fingerprint=ss.narrative_config_fingerprint(),
        evidence_fingerprint=basis.evidence_fingerprint,
        observed_evidence_fingerprint=basis.evidence_fingerprint,
    )
    before = ss.get(stat.session_id)
    _append_growth(path, stat.session_id)
    monkeypatch.setattr(ss, "llm_available", lambda: True)
    budget = None
    if failure == "call":
        monkeypatch.setattr(ss, "run_llm_p", lambda *_a, **_k: None)
    elif failure == "omission":
        monkeypatch.setattr(
            ss,
            "run_llm_p",
            lambda *_a, **_k: ss.NarrativeCall(
                '{"session_id":"neighbor","summary":"not this row"}',
                "claude",
                "sonnet",
            ),
        )
    else:
        budget = ss.NarrativeBudget(total_sec=0)
        monkeypatch.setattr(
            ss,
            "run_llm_p",
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("exhausted budget must make no call")
            ),
        )

    counts = ss.backfill_for_sessions(
        [stat], use_llm=True, budget=budget,
    )
    after = ss.get(stat.session_id)
    assert counts["regenerated"] == 0
    assert after.summary == before.summary
    assert after.source == before.source
    assert after.created_at == before.created_at
    assert after.evidence_fingerprint == before.evidence_fingerprint
    assert after.observed_evidence_fingerprint != before.evidence_fingerprint
    assert ss.summary_evidence_status(after) == "stale"


def test_record_is_authoritative_even_force_and_generated_upsert(
    tmp_home, jsonl_factory, monkeypatch,
):
    stat, _path = _session(jsonl_factory, "human-record")
    ss.upsert(stat.session_id, "Human accepted outcome", "provided")
    before = ss.get(stat.session_id)
    monkeypatch.setattr(
        ss,
        "_extract_excerpt",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("provided rows must not be extracted")
        ),
    )
    counts = ss.backfill_for_sessions(
        [stat], use_llm=True, force=True,
    )
    ss.upsert(stat.session_id, "Generated overwrite", "generated")

    after = ss.get(stat.session_id)
    assert counts["already"] == 1
    assert after.summary == before.summary
    assert after.source == "provided"
    assert after.created_at == before.created_at
    assert ss.summary_evidence_status(after) == "not_applicable"


def test_recap_preflight_extracts_each_candidate_once(
    tmp_home, jsonl_factory, monkeypatch,
):
    sessions = [_session(jsonl_factory, f"once-{index}")[0] for index in range(3)]
    real_extract = ss._extract_excerpt
    seen: list[str] = []

    def counting_extract(path, provider=None):
        seen.append(path.stem)
        return real_extract(path, provider=provider)

    monkeypatch.setattr(ss, "_extract_excerpt", counting_extract)
    monkeypatch.setattr(ss, "llm_available", lambda: True)
    monkeypatch.setattr(ss, "run_llm_p", _successful_batch("One extraction"))

    counts = recap._backfill_summaries(
        sessions,
        Console(quiet=True),
        use_llm=True,
    )
    assert counts["summarized"] == 3
    assert sorted(seen) == sorted(session.session_id for session in sessions)


def test_public_status_is_hash_free_and_stale_is_not_synthesized(monkeypatch):
    now = datetime(2026, 5, 10, tzinfo=timezone.utc)
    stale_stat = SessionStat(
        project="project", category="coding", session_id="stale",
        start=now, end=now, active_sec=3600, msg_count=2,
    )
    current_stat = SessionStat(
        project="project", category="coding", session_id="current",
        start=now, end=now, active_sec=1800, msg_count=2,
    )
    secret_basis = "SECRET_BASIS_HASH"
    secret_observed = "SECRET_OBSERVED_HASH"
    summaries = {
        "stale": ss.SessionSummary(
            "stale", "Old generated claim", "generated",
            evidence_fingerprint=secret_basis,
            observed_evidence_fingerprint=secret_observed,
        ),
        "current": ss.SessionSummary(
            "current", "Fresh generated claim", "generated",
            evidence_fingerprint="same",
            observed_evidence_fingerprint="same",
        ),
    }
    rollup = CategoryRollup(
        category="coding", active_min=90, sessions=2, messages=4,
        top_sessions=[stale_stat, current_stat],
    )
    usage = UsageReport(since=now, until=now)

    payload = build_report_json(
        label="window", since=now, until=now,
        sessions=[stale_stat, current_stat], rollups=[rollup],
        usage=usage, summaries=summaries,
    )
    encoded = json.dumps(payload)
    assert payload["sessions"][0]["summary_evidence"]["status"] == "stale"
    assert secret_basis not in encoded
    assert secret_observed not in encoded
    markdown = render_report(
        label="window", since=now, until=now,
        sessions=[stale_stat, current_stat], rollups=[rollup],
        usage=usage, summaries=summaries,
    )
    assert "_summary evidence: stale_" in markdown
    assert secret_basis not in markdown
    assert top_focus_narrative(
        [rollup], summaries,
    ).strongest_session_summaries == ("Fresh generated claim",)

    captured = {}

    def synthesize(**kwargs):
        captured.update(kwargs["sessions_by_category"])
        return "fresh synthesis"

    monkeypatch.setattr(recap, "synthesize_overall_for_period", synthesize)
    assert recap._synthesize_overall(
        "window", [stale_stat, current_stat], [rollup], summaries,
        Console(quiet=True),
    ) == "fresh synthesis"
    assert captured == {"coding": [("current", "Fresh generated claim")]}
    assert recap._eligible_summary_items(
        ["stale", "current"], summaries,
    ) == [("current", "Fresh generated claim")]

    classified: list[tuple[str, str, str]] = []
    monkeypatch.setattr(recap, "_classify_cache_get_many", lambda _ids: {})
    monkeypatch.setattr(
        recap,
        "classify_sessions_by_content",
        lambda items, **_kwargs: classified.extend(items) or {},
    )
    recap._resolve_all_sessions(
        [stale_stat, current_stat], summaries, "content", "other",
        Console(quiet=True),
    )
    assert [item[0] for item in classified] == ["current"]


def test_status_helper_covers_native_fallback_skipped_and_legacy():
    assert ss.summary_evidence_status(None) == "not_applicable"  # native title
    assert ss.summary_evidence_status(
        ss.SessionSummary("f", "fallback", "extracted")
    ) == "not_applicable"
    assert ss.summary_evidence_status(
        ss.SessionSummary("s", "skipped", "no_evidence")
    ) == "unavailable"
    assert ss.summary_evidence_status(
        ss.SessionSummary(
            "l", "legacy", "generated",
            evidence_fingerprint=ss.LEGACY_UNKNOWN_EVIDENCE,
            observed_evidence_fingerprint=ss.LEGACY_UNKNOWN_EVIDENCE,
        )
    ) == "legacy"
