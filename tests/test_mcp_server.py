"""Tests for the ccstory MCP server tools (#35).

`@mcp.tool()` returns the original function unchanged (verified against the
installed `mcp` SDK), so these call `get_recap` / `compare_to_previous` /
`list_categories` directly — no stdio transport or MCP client needed.

`claude -p` is fenced off the same way test_recap_entry.py does it
(`claude_bin_available → False`), so this stays deterministic and offline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("mcp")

from ccstory import recap  # noqa: E402
from ccstory import session_summarizer as ss  # noqa: E402
from tests.conftest import make_assistant_msg, make_user_msg  # noqa: E402

from ccstory.mcp_server import (  # noqa: E402
    compare_to_previous,
    get_recap,
    list_categories,
)


def _ts(hours_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _seed_session(jsonl_factory, project: str, sid: str, hours_ago: float) -> None:
    """One engaged session (2 real user messages) `hours_ago` hours back."""
    records = [
        make_user_msg("Fix the login bug", _ts(hours_ago)),
        make_assistant_msg("Looking at auth.py", _ts(hours_ago - 0.05), f"{sid}-m1"),
        make_user_msg("Also add a regression test", _ts(hours_ago - 0.1)),
        make_assistant_msg("Done — patched and tested.", _ts(hours_ago - 0.15),
                           f"{sid}-m2"),
    ]
    jsonl_factory(project, sid, records)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.setattr(ss, "claude_bin_available", lambda: False)


class TestGetRecap:
    def test_happy_path_shape(self, tmp_home, jsonl_factory):
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-1", hours_ago=2)
        out = get_recap(window="week")
        assert out["ok"] is True
        assert out["active_hours"] > 0
        assert isinstance(out["categories"], list) and out["categories"]
        assert {"name", "active_hours", "narrative"} <= out["categories"][0].keys()
        assert len(out["top_sessions"]) == 1
        top = out["top_sessions"][0]
        assert {"id", "project", "active_hours", "summary"} <= top.keys()
        assert top["summary"]  # instant fallback summary, never empty here
        assert out["cost_usd"] >= 0
        # Compact, not the full --json envelope: no per-session id list
        # beyond top_sessions, no raw transcript text anywhere in the shape.
        assert "sessions" not in out

    def test_no_report_file_written(self, tmp_home, jsonl_factory):
        """get_recap must not have report-writing side effects (write_report=False)."""
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-1", hours_ago=2)
        get_recap(window="week")
        reports_dir = tmp_home / ".ccstory" / "reports"
        assert not reports_dir.exists() or not any(reports_dir.glob("*.md"))

    def test_empty_window_normalizes_instead_of_raising(self, tmp_home):
        out = get_recap(window="week")
        assert out == {"ok": False, "error": "No engaged sessions in this window."}

    def test_bad_window_normalizes_instead_of_raising(self, tmp_home):
        out = get_recap(window="not-a-real-window")
        assert out["ok"] is False
        assert "unrecognized window" in out["error"]

    def test_default_classify_never_fires_llm_content_classification(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        """classify="folder" (the default) must not batch-classify via
        claude -p on a cache miss — the whole point of the default."""
        _seed_session(jsonl_factory, "-Users-me-unrecognized-proj", "sess-1",
                      hours_ago=2)

        def _boom(*a, **kw):
            raise AssertionError("must not batch-classify with default classify=folder")

        monkeypatch.setattr(recap, "classify_sessions_by_content", _boom)
        out = get_recap(window="week")
        assert out["ok"] is True  # falls back to the fallback bucket, not LLM

    def test_system_exit_from_cache_layer_is_caught(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        """session_summarizer._connect() raises SystemExit on a corrupt
        cache.db — BaseException, not Exception, so it must be caught
        explicitly or it kills the whole MCP server process."""
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-1", hours_ago=2)

        def _boom(*a, **kw):
            raise SystemExit(1)

        monkeypatch.setattr(recap, "_classify_cache_get_many", _boom)
        out = get_recap(window="week")
        assert out == {"ok": False, "error": "1"}


class TestCompareToPrevious:
    def test_happy_path_shape(self, tmp_home, jsonl_factory):
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-cur", hours_ago=2)
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-prev", hours_ago=9 * 24)
        out = compare_to_previous(window="week")
        assert out["ok"] is True
        assert out["narrative"] is None  # cache-only tool, never synthesizes
        assert out["current_active_hours"] > 0
        assert out["previous_active_hours"] > 0
        assert out["deltas"]
        delta = out["deltas"][0]
        assert {"category", "current_hours", "previous_hours", "pct_change"} <= delta.keys()

    def test_categories_are_resolved_not_empty(self, tmp_home, jsonl_factory):
        """Regression: rollup_by_category() must run against resolved
        categories, not whatever collect_sessions() leaves .category as."""
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-cur", hours_ago=2)
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-prev", hours_ago=9 * 24)
        out = compare_to_previous(window="week")
        names = {d["category"] for d in out["deltas"]}
        assert "" not in names

    def test_no_current_sessions_normalizes(self, tmp_home):
        out = compare_to_previous(window="week")
        assert out == {"ok": False, "error": "No engaged sessions in this window."}

    def test_no_previous_sessions_normalizes(self, tmp_home, jsonl_factory):
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-cur", hours_ago=2)
        out = compare_to_previous(window="week")
        assert out == {
            "ok": False,
            "error": "No sessions in the previous window to compare.",
        }

    def test_bad_window_normalizes_instead_of_raising(self, tmp_home):
        out = compare_to_previous(window="not-a-real-window")
        assert out["ok"] is False
        assert "unrecognized window" in out["error"]


class TestListCategories:
    def test_returns_default_rules_shape(self, tmp_home):
        out = list_categories()
        assert out["ok"] is True
        names = [c["name"] for c in out["categories"]]
        assert "coding" in names  # built-in default bucket, no config.toml yet
        for c in out["categories"]:
            assert isinstance(c["needles"], list) and c["needles"]


def test_no_stdout_leak_across_tools(tmp_home, jsonl_factory, capsys):
    """Regression guard: stdout is the MCP protocol stream. Any stray
    `print()` reachable from these tools would corrupt it silently."""
    _seed_session(jsonl_factory, "-Users-me-proj", "sess-cur", hours_ago=2)
    _seed_session(jsonl_factory, "-Users-me-proj", "sess-prev", hours_ago=9 * 24)
    get_recap(window="week")
    compare_to_previous(window="week")
    list_categories()
    assert capsys.readouterr().out == ""
