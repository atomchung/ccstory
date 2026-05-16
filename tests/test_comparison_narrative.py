"""Tests for #26 — cross-period narrative synthesis + render integration."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

from ccstory.report import render_comparison_markdown
from ccstory.session_summarizer import (
    _comparison_signature,
    synthesize_comparison,
)
from ccstory.trends import CategoryDelta, PeriodComparison


def _mk_cmp(narrative: str | None = None) -> PeriodComparison:
    return PeriodComparison(
        current_label="2026-W19",
        previous_label="2026-05-01 → 2026-05-08",
        deltas=[
            CategoryDelta(category="coding", current_min=600, previous_min=300),
        ],
        current_total_h=10.0,
        previous_total_h=5.0,
        current_output_tokens=1_000_000,
        previous_output_tokens=500_000,
        current_cost_usd=100.0,
        previous_cost_usd=50.0,
        narrative=narrative,
    )


class TestSignature:
    def test_same_sets_same_signature(self):
        s1 = _comparison_signature(
            [("a", "sa"), ("b", "sb")], [("c", "sc"), ("d", "sd")],
        )
        s2 = _comparison_signature(
            [("b", "sb"), ("a", "sa")], [("d", "sd"), ("c", "sc")],
        )  # different order
        assert s1 == s2

    def test_different_current_changes_signature(self):
        s1 = _comparison_signature([("a", "sa")], [("c", "sc")])
        s2 = _comparison_signature(
            [("a", "sa"), ("e", "se")], [("c", "sc")],
        )
        assert s1 != s2

    def test_different_previous_changes_signature(self):
        s1 = _comparison_signature([("a", "sa")], [("b", "sb")])
        s2 = _comparison_signature([("a", "sa")], [("c", "sc")])
        assert s1 != s2

    def test_different_summary_content_changes_signature(self):
        # Same session ids, different summary text — must invalidate the
        # cached narrative (regression: previous version only hashed ids).
        s1 = _comparison_signature([("a", "old summary")], [("b", "x")])
        s2 = _comparison_signature([("a", "new summary")], [("b", "x")])
        assert s1 != s2

    def test_deltas_change_signature(self):
        s1 = _comparison_signature([("a", "sa")], [("b", "sb")], deltas=None)
        s2 = _comparison_signature(
            [("a", "sa")], [("b", "sb")],
            deltas=[("coding", 600.0, 300.0)],
        )
        assert s1 != s2


class TestSynthesizeComparison:
    def test_empty_current_returns_none(self, tmp_home: Path):
        result = synthesize_comparison(
            current_key="2026-W19",
            previous_key="2026-W18",
            current_summaries=[],
            previous_summaries=[("a", "did stuff")],
        )
        assert result is None

    def test_empty_previous_returns_none(self, tmp_home: Path):
        result = synthesize_comparison(
            current_key="2026-W19",
            previous_key="2026-W18",
            current_summaries=[("a", "did stuff")],
            previous_summaries=[],
        )
        assert result is None

    def test_cache_hit_skips_claude(self, tmp_home: Path):
        # Prime the cache directly
        from ccstory.session_summarizer import DB_PATH, _connect
        _connect().close()  # ensure schema exists

        current = [("c1", "current")]
        previous = [("p1", "previous")]
        sig = _comparison_signature(current, previous)
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute(
                """INSERT INTO comparison_narratives
                   (current_key, previous_key, signature, narrative, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                ("k_cur", "k_prev", sig, "cached prose", 1.0),
            )
            conn.commit()
        finally:
            conn.close()

        # claude_bin_available should never be called when cache hits
        with patch("ccstory.session_summarizer.claude_bin_available",
                   side_effect=AssertionError("should not be called")):
            result = synthesize_comparison(
                current_key="k_cur",
                previous_key="k_prev",
                current_summaries=current,
                previous_summaries=previous,
            )
        assert result == "cached prose"

    def test_cache_signature_change_triggers_regen(self, tmp_home: Path):
        # Prime cache with one signature
        from ccstory.session_summarizer import DB_PATH, _connect
        _connect().close()
        old_sig = _comparison_signature([("c1", "x")], [("p1", "z")])
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute(
                """INSERT INTO comparison_narratives VALUES (?, ?, ?, ?, ?)""",
                ("k_cur", "k_prev", old_sig, "stale", 1.0),
            )
            conn.commit()
        finally:
            conn.close()

        # New call with different session ids — cache signature mismatches
        # → fall through to claude_bin_available check. We mock claude as
        # absent so it returns None (rather than running subprocess).
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=False):
            result = synthesize_comparison(
                current_key="k_cur",
                previous_key="k_prev",
                current_summaries=[("c1", "x"), ("c_new", "y")],  # changed
                previous_summaries=[("p1", "z")],
            )
        # claude unavailable → None (not the stale cached value)
        assert result is None

    def test_refreshed_summary_invalidates_cache(self, tmp_home: Path):
        # Same session ids, summary content was refreshed → cache must
        # regenerate. Regression for the id-only signature.
        from ccstory.session_summarizer import DB_PATH, _connect
        _connect().close()
        stale_sig = _comparison_signature(
            [("c1", "old summary")], [("p1", "p")],
        )
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute(
                """INSERT INTO comparison_narratives VALUES (?, ?, ?, ?, ?)""",
                ("k_cur", "k_prev", stale_sig, "stale prose", 1.0),
            )
            conn.commit()
        finally:
            conn.close()

        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=False):
            result = synthesize_comparison(
                current_key="k_cur",
                previous_key="k_prev",
                current_summaries=[("c1", "REFRESHED summary")],
                previous_summaries=[("p1", "p")],
            )
        assert result is None  # not the stale value

    def test_deltas_passed_into_prompt(self, tmp_home: Path):
        captured = {}

        def capture(cmd, *, capture_output, text, timeout, check):
            captured["prompt"] = cmd[-1]
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ok narrative\n", stderr="",
            )

        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   side_effect=capture):
            synthesize_comparison(
                current_key="k_cur",
                previous_key="k_prev",
                current_summaries=[("c1", "x")],
                previous_summaries=[("p1", "y")],
                deltas=[
                    ("coding", 600.0, 300.0),
                    ("writing", 60.0, 120.0),
                ],
            )
        prompt = captured["prompt"]
        assert "coding: 5.0h → 10.0h (+5.0h)" in prompt
        assert "writing: 2.0h → 1.0h (-1.0h)" in prompt

    def test_claude_unavailable_returns_none(self, tmp_home: Path):
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=False):
            result = synthesize_comparison(
                current_key="k_cur",
                previous_key="k_prev",
                current_summaries=[("c1", "x")],
                previous_summaries=[("p1", "y")],
            )
        assert result is None

    def test_claude_success_caches_result(self, tmp_home: Path):
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="synthesized prose result\n", stderr="",
        )
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   return_value=mock_proc):
            result = synthesize_comparison(
                current_key="k_cur",
                previous_key="k_prev",
                current_summaries=[("c1", "x")],
                previous_summaries=[("p1", "y")],
            )
        assert result == "synthesized prose result"

        # Second call should hit cache without invoking subprocess
        with patch("ccstory.session_summarizer.subprocess.run",
                   side_effect=AssertionError("should not run")):
            cached = synthesize_comparison(
                current_key="k_cur",
                previous_key="k_prev",
                current_summaries=[("c1", "x")],
                previous_summaries=[("p1", "y")],
            )
        assert cached == "synthesized prose result"

    def test_claude_failure_returns_none(self, tmp_home: Path):
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="oops",
        )
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   return_value=failed):
            result = synthesize_comparison(
                current_key="k_cur",
                previous_key="k_prev",
                current_summaries=[("c1", "x")],
                previous_summaries=[("p1", "y")],
            )
        assert result is None


class TestRenderWithNarrative:
    def test_markdown_includes_narrative_when_present(self):
        cmp = _mk_cmp(narrative="Coding doubled as ccstory shipped.")
        md = render_comparison_markdown(cmp)
        assert "> Coding doubled as ccstory shipped." in md

    def test_markdown_omits_narrative_when_absent(self):
        cmp = _mk_cmp(narrative=None)
        md = render_comparison_markdown(cmp)
        assert "Coding doubled" not in md
        # Ensure the table still renders (numeric path unaffected)
        assert "## vs previous window" in md
        assert "| `coding` |" in md

    def test_multiline_narrative_preserves_blockquote(self):
        # If the LLM returns 2 sentences with a newline, every line must
        # still start with `> ` or the blockquote breaks at line 2.
        narrative = "Coding doubled as ccstory shipped.\nInvestment work shrank by 40%."
        cmp = _mk_cmp(narrative=narrative)
        md = render_comparison_markdown(cmp)
        assert "> Coding doubled as ccstory shipped." in md
        assert "> Investment work shrank by 40%." in md

    def test_blank_line_in_narrative_uses_bare_quote_marker(self):
        narrative = "Line one.\n\nLine three."
        cmp = _mk_cmp(narrative=narrative)
        md = render_comparison_markdown(cmp)
        # A blank quoted line should appear as a bare ">" so the blockquote
        # remains a single contiguous block in markdown renderers.
        assert "> Line one." in md
        assert ">\n" in md  # bare-marker blank line
        assert "> Line three." in md
