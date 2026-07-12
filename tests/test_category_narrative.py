"""Tests for #57 — per-category narrative mode."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pytest

from ccstory import session_summarizer as ss
from ccstory.report import build_report_json, render_report
from ccstory.session_summarizer import OVERALL_KEY, synthesize_category_for_period
from ccstory.time_tracking import CategoryRollup, SessionStat
from ccstory.token_usage import ModelUsage, UsageReport

SINCE = datetime(2026, 7, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 7, 8, tzinfo=timezone.utc)


class _FakeRun:
    """Stub subprocess.run for claude -p; records calls."""

    def __init__(self, stdout: str = "Did X, then Y.\nShipped Z.", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        run = self

        class R:
            returncode = run.returncode
            stdout = run.stdout
            stderr = ""

        return R()


class TestSynthesizeCategoryCache:
    @pytest.fixture(autouse=True)
    def _env(self, tmp_home, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(ss, "claude_bin_available", lambda: True)

    def test_success_writes_cache_then_hits(self, monkeypatch: pytest.MonkeyPatch):
        fake = _FakeRun()
        monkeypatch.setattr(ss.subprocess, "run", fake)
        first = synthesize_category_for_period(
            "2026-W27", "coding", ["s1", "s2"], ["fixed auth", "wrote tests"],
        )
        assert first == "Did X, then Y.\nShipped Z."
        assert fake.calls == 1
        # Same ids (different order — must normalize) → cache hit, no new call.
        second = synthesize_category_for_period(
            "2026-W27", "coding", ["s2", "s1"], ["fixed auth", "wrote tests"],
        )
        assert second == first
        assert fake.calls == 1

    def test_changed_ids_recompute(self, monkeypatch: pytest.MonkeyPatch):
        fake = _FakeRun()
        monkeypatch.setattr(ss.subprocess, "run", fake)
        synthesize_category_for_period("2026-W27", "coding", ["s1"], ["a"])
        synthesize_category_for_period("2026-W27", "coding", ["s1", "s3"], ["a", "b"])
        assert fake.calls == 2

    def test_llm_failure_returns_none_and_caches_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        fail = _FakeRun(returncode=1)
        monkeypatch.setattr(ss.subprocess, "run", fail)
        assert synthesize_category_for_period("k", "coding", ["s1"], ["a"]) is None
        ok = _FakeRun()
        monkeypatch.setattr(ss.subprocess, "run", ok)
        # No poisoned cache row — retry actually calls the LLM again.
        assert synthesize_category_for_period("k", "coding", ["s1"], ["a"]) is not None
        assert ok.calls == 1

    def test_degenerate_output_rejected(self, monkeypatch: pytest.MonkeyPatch):
        short = _FakeRun(stdout="ok")
        monkeypatch.setattr(ss.subprocess, "run", short)
        assert synthesize_category_for_period("k", "coding", ["s1"], ["a"]) is None

    def test_reserved_overall_key_skipped(self, monkeypatch: pytest.MonkeyPatch):
        fake = _FakeRun()
        monkeypatch.setattr(ss.subprocess, "run", fake)
        assert synthesize_category_for_period("k", OVERALL_KEY, ["s1"], ["a"]) is None
        assert fake.calls == 0

    def test_empty_input_none(self):
        assert synthesize_category_for_period("k", "coding", [], []) is None

    def test_isolated_from_overall_row(self, monkeypatch: pytest.MonkeyPatch):
        fake = _FakeRun()
        monkeypatch.setattr(ss.subprocess, "run", fake)
        synthesize_category_for_period("2026-W27", "coding", ["s1"], ["a"])
        # The overall row for the same period stays untouched.
        assert ss.get_overall_narrative("2026-W27") is None


def _stat(bucket: str, sid: str) -> SessionStat:
    base = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    return SessionStat(
        project="-Users-t-myapp", category=bucket, session_id=sid,
        start=base, end=base, active_sec=600, msg_count=5,
        first_user_text="hello",
    )


def _fixtures():
    s1, s2 = _stat("coding", "s1"), _stat("writing", "s2")
    rollups = [
        CategoryRollup(category="coding", active_min=60.0, sessions=1,
                       messages=5, top_sessions=[s1]),
        CategoryRollup(category="writing", active_min=30.0, sessions=1,
                       messages=5, top_sessions=[s2]),
    ]
    usage = UsageReport(since=SINCE, until=UNTIL)
    usage.by_model["m"] = ModelUsage(model="m", turns=1, input_tokens=10, output_tokens=5)
    return [s1, s2], rollups, usage


class TestRendering:
    def test_markdown_section_rollup_order_and_gaps(self):
        sessions, rollups, usage = _fixtures()
        md = render_report(
            label="L", since=SINCE, until=UNTIL, sessions=sessions,
            rollups=rollups, usage=usage, summaries={},
            category_narratives={"writing": "Wrote the launch post."},
        )
        assert "## What you did, by category" in md
        section = md.split("## What you did, by category")[1].split("## Sessions")[0]
        assert "### writing" in section
        assert "Wrote the launch post." in section
        assert "### coding" not in section  # no narrative for that bucket → omitted

    def test_markdown_section_absent_when_none(self):
        sessions, rollups, usage = _fixtures()
        md = render_report(
            label="L", since=SINCE, until=UNTIL, sessions=sessions,
            rollups=rollups, usage=usage, summaries={},
        )
        assert "by category" not in md.split("## Sessions")[0]

    def test_json_bucket_narrative_fill_and_null(self):
        sessions, rollups, usage = _fixtures()
        p = build_report_json(
            label="L", since=SINCE, until=UNTIL, sessions=sessions,
            rollups=rollups, usage=usage, summaries={},
            category_narratives={"coding": "Refactored the CLI."},
        )
        by_name = {b["name"]: b for b in p["buckets"]}
        assert by_name["coding"]["narrative"] == "Refactored the CLI."
        assert by_name["writing"]["narrative"] is None

    def test_json_narrative_null_by_default(self):
        sessions, rollups, usage = _fixtures()
        p = build_report_json(
            label="L", since=SINCE, until=UNTIL, sessions=sessions,
            rollups=rollups, usage=usage, summaries={},
        )
        assert all(b["narrative"] is None for b in p["buckets"])


class TestNarrativeFlag:
    """Mirror the parser shape from cli.main(), per test_cli_flags convention."""

    def _parser(self) -> argparse.ArgumentParser:
        p = argparse.ArgumentParser()
        p.add_argument("window", nargs="?", default="month")
        p.add_argument("--narrative", choices=["overall", "per-category", "both"],
                       default="overall")
        return p

    def test_default_overall(self):
        assert self._parser().parse_args(["week"]).narrative == "overall"

    def test_choices(self):
        for v in ("overall", "per-category", "both"):
            assert self._parser().parse_args(["week", "--narrative", v]).narrative == v

    def test_invalid_rejected(self):
        with pytest.raises(SystemExit):
            self._parser().parse_args(["week", "--narrative", "everything"])
