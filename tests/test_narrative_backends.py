"""Explicit local narrator selection, fallback, and provenance tests."""

from __future__ import annotations

import subprocess

from ccstory import session_summarizer as ss
from ccstory.categorizer import add_category_keywords
from ccstory.init_categories import _write_config


def _result(stdout: str = "generated prose", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout, "failed")


def test_default_backends_are_ordered_and_explicitly_modelled():
    assert [
        (backend.provider, backend.model, backend.effort)
        for backend in ss.narrative_backends()
    ] == [
        ("claude", "sonnet", None),
        ("codex", "gpt-5.6-terra", None),
        ("antigravity", "gemini-3.6-flash-low", "low"),
    ]


def test_config_can_select_and_override_backends(tmp_home):
    ss.CCSTORY_CONFIG_PATH.write_text(
        """[narrative]
providers = ["codex", "antigravity"]

[narrative.codex]
model = "gpt-5.6-terra"

[narrative.antigravity]
model = "gemini-3.6-flash-medium"
effort = "medium"
""",
        encoding="utf-8",
    )
    assert [
        (backend.provider, backend.model, backend.effort)
        for backend in ss.narrative_backends()
    ] == [
        ("codex", "gpt-5.6-terra", None),
        ("antigravity", "gemini-3.6-flash-medium", "medium"),
    ]


def test_dispatch_falls_back_from_claude_to_ephemeral_codex(monkeypatch):
    monkeypatch.setattr(ss, "claude_bin_available", lambda: True)
    monkeypatch.setattr(ss, "codex_bin_available", lambda: True)
    monkeypatch.setattr(ss, "antigravity_bin_available", lambda: False)
    calls = []

    def claude(prompt, timeout, model):
        calls.append(("claude", prompt, timeout, model))
        return _result("", 1)

    def codex(prompt, timeout, model):
        calls.append(("codex", prompt, timeout, model))
        return _result("codex prose")

    monkeypatch.setattr(ss, "run_claude_p", claude)
    monkeypatch.setattr(ss, "_run_codex_p", codex)

    call = ss.run_llm_p("summarize this", 20)
    assert call == ss.NarrativeCall("codex prose", "codex", "gpt-5.6-terra")
    assert [(kind, model) for kind, _, _, model in calls] == [
        ("claude", "sonnet"),
        ("codex", "gpt-5.6-terra"),
    ]


def test_budget_trace_records_lane_provider_fallback_and_success(monkeypatch):
    monkeypatch.setattr(ss, "claude_bin_available", lambda: True)
    monkeypatch.setattr(ss, "codex_bin_available", lambda: True)
    monkeypatch.setattr(ss, "antigravity_bin_available", lambda: False)
    monkeypatch.setattr(ss, "run_claude_p", lambda *_, **__: _result("", 1))
    monkeypatch.setattr(ss, "_run_codex_p", lambda *_: _result("codex prose"))

    budget = ss.NarrativeBudget(total_sec=90, batch_deadline_sec=45)
    assert ss.run_llm_p("summarize this", 60, budget=budget, lane="overall") == (
        ss.NarrativeCall("codex prose", "codex", "gpt-5.6-terra")
    )

    status = budget.status()
    assert status["lanes"] == {
        "overall": {
            "calls": 1, "successful_calls": 1, "failed_calls": 0,
            "timed_out_calls": 0, "fallback_successes": 1, "batches": 0,
        },
    }
    # Trace is metadata-only: it records the attempted provider/model and
    # outcome, not the prompt or generated narrative.
    attempts = [item for item in status["trace"] if item["event"] == "provider_attempt"]
    assert [(item["provider"], item["outcome"], item["fallback"]) for item in attempts] == [
        ("claude", "failed", False),
        ("codex", "success", True),
    ]
    assert all("prompt" not in item and "stdout" not in item for item in attempts)


def test_budget_lane_deadline_rejection_preserves_legacy_reservation_shape():
    budget = ss.NarrativeBudget(total_sec=0)
    # Older callers still receive the same tuple-or-None interface; lane is
    # optional execution metadata rather than a new scheduling requirement.
    assert budget.begin_call(30, lane="content classification") is None
    assert budget.status()["trace"] == [{
        "event": "call", "lane": "content_classification",
        "outcome": "budget_exhausted", "elapsed_sec": 0.0,
        "requested_timeout_sec": 30.0,
    }]


def test_codex_command_is_ephemeral_and_read_only(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _result()

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    ss._run_codex_p("prompt", 30, "gpt-5.6-terra")
    argv = calls[0][0]
    assert argv[:6] == [
        "codex", "exec", "--ephemeral", "--sandbox", "read-only", "--model",
    ]
    assert argv[6:8] == ["gpt-5.6-terra", "prompt"]


def test_antigravity_command_uses_explicit_flash_model_and_effort(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _result("flash prose")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    result = ss._run_antigravity_p(
        "prompt", 10, "gemini-3.6-flash-low", "low",
    )
    assert result is not None and result.stdout == "flash prose"
    argv, kwargs = calls[0]
    assert argv[1:] == [
        "-p", "prompt", "--model", "gemini-3.6-flash-low", "--effort", "low",
    ]
    assert kwargs["timeout"] == 180


def test_budgeted_antigravity_call_is_capped_at_deadline(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _result("flash prose")

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    monkeypatch.setattr(
        ss,
        "narrative_backends",
        lambda: (ss.NarrativeBackend("antigravity", "gemini-3.6-flash-low", "low"),),
    )
    monkeypatch.setattr(ss, "narrative_backend_available", lambda _backend: True)

    budget = ss.NarrativeBudget(total_sec=90, batch_deadline_sec=45)
    assert ss.run_llm_p("prompt", 120, budget=budget) == ss.NarrativeCall(
        "flash prose", "antigravity", "gemini-3.6-flash-low",
    )
    assert calls[0][1]["timeout"] <= 45


def test_session_provenance_and_backend_config_change_invalidate_auto_cache():
    fingerprint = ss.narrative_config_fingerprint()
    ss.upsert(
        "s1", "generated", "auto", prompt_version=ss.PROMPT_VERSION,
        narrator_provider="codex", narrator_model="gpt-5.6-terra",
        narrator_fingerprint=fingerprint,
    )
    stored = ss.get("s1")
    assert stored is not None
    assert stored.narrator_provider == "codex"
    assert stored.narrator_model == "gpt-5.6-terra"
    assert ss._needs_llm(stored) is False

    # The model choice is part of the cache identity, so a policy change does
    # not reuse prose from the previous model under a new label.
    ss.CCSTORY_CONFIG_PATH.write_text(
        """[narrative]
providers = ["codex"]

[narrative.codex]
model = "gpt-5.6-terra-next"
""",
        encoding="utf-8",
    )
    assert ss._needs_llm(stored) is True


def test_period_cache_records_actual_narrator(monkeypatch):
    monkeypatch.setattr(ss, "llm_available", lambda: True)
    monkeypatch.setattr(
        ss, "run_llm_p",
        lambda *_: ss.NarrativeCall("A sufficiently long narrative.", "codex", "gpt-5.6-terra"),
    )
    assert ss.synthesize_overall_for_period(
        "2026-07", [("coding", 2.0)], {"coding": [("s1", "did work")]},
    ) == "A sufficiently long narrative."
    assert ss.get_period_narrative_provenance("2026-07") == {
        "provider": "codex", "model": "gpt-5.6-terra",
    }


def test_category_edit_preserves_narrative_backend_policy(tmp_home):
    ss.CCSTORY_CONFIG_PATH.write_text(
        """[narrative]
providers = ["codex", "antigravity"]

[narrative.codex]
model = "gpt-5.6-terra"

[narrative.antigravity]
model = "gemini-3.6-flash-low"
effort = "low"

[categories]
"writing" = ["blog"]
""",
        encoding="utf-8",
    )
    add_category_keywords("writing", ["newsletter"], ss.CCSTORY_CONFIG_PATH)
    assert [
        (backend.provider, backend.model, backend.effort)
        for backend in ss.narrative_backends()
    ] == [
        ("codex", "gpt-5.6-terra", None),
        ("antigravity", "gemini-3.6-flash-low", "low"),
    ]


def test_init_rewrite_preserves_narrative_backend_policy(tmp_home):
    ss.CCSTORY_CONFIG_PATH.write_text(
        """[narrative]
providers = ["codex"]

[narrative.codex]
model = "gpt-5.6-terra"

[categories]
"writing" = ["blog"]
""",
        encoding="utf-8",
    )
    _write_config(ss.CCSTORY_CONFIG_PATH, {"coding": ["project"]})
    assert [
        (backend.provider, backend.model)
        for backend in ss.narrative_backends()
    ] == [("codex", "gpt-5.6-terra")]
