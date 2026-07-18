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

from ccstory import cli, recap  # noqa: E402
from ccstory import session_summarizer as ss  # noqa: E402
from tests.conftest import make_assistant_msg, make_user_msg  # noqa: E402

from ccstory.mcp_server import (  # noqa: E402
    compare_to_previous,
    get_recap,
    get_trend,
    list_categories,
)


def _recent_ts(hours_ago: float) -> str:
    # Named _recent_ts, not _ts, to avoid colliding with conftest.py's own
    # exported _ts(year, month, day, ...) — same name, unrelated signature.
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _seed_session(jsonl_factory, project: str, sid: str, hours_ago: float) -> None:
    """One engaged session (2 real user messages) `hours_ago` hours back."""
    records = [
        make_user_msg("Fix the login bug", _recent_ts(hours_ago)),
        make_assistant_msg("Looking at auth.py", _recent_ts(hours_ago - 0.05),
                           f"{sid}-m1"),
        make_user_msg("Also add a regression test", _recent_ts(hours_ago - 0.1)),
        make_assistant_msg("Done — patched and tested.",
                           _recent_ts(hours_ago - 0.15), f"{sid}-m2"),
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
        explicitly or it kills the whole MCP server process. It also
        chains the real diagnostic via `from e`, which must survive into
        the response instead of collapsing to the bare exit code."""
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-1", hours_ago=2)

        def _boom(*a, **kw):
            raise SystemExit(1) from ValueError("cache.db is corrupted")

        monkeypatch.setattr(recap, "_classify_cache_get_many", _boom)
        out = get_recap(window="week")
        assert out == {"ok": False, "error": "cache.db is corrupted"}

    def test_cache_unavailable_is_normalized(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        """CacheUnavailable is what _connect() actually raises since #119 —
        the tool must return it as a normal error payload, message intact."""
        from ccstory.session_summarizer import CacheUnavailable

        _seed_session(jsonl_factory, "-Users-me-proj", "sess-1", hours_ago=2)

        def _boom(*a, **kw):
            raise CacheUnavailable("ccstory: error: cache at /x is corrupted")

        monkeypatch.setattr(recap, "_classify_cache_get_many", _boom)
        out = get_recap(window="week")
        assert out == {
            "ok": False,
            "error": "ccstory: error: cache at /x is corrupted",
        }

    def test_allow_llm_false_never_synthesizes_narrative(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        """The core promise: allow_llm=False (default) must not run ANY
        synthesis step, not just per-session polish — top_focus and every
        category's narrative stay null rather than triggering claude -p."""
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-1", hours_ago=2)

        def _boom(*a, **kw):
            raise AssertionError("allow_llm=False must not synthesize narrative")

        monkeypatch.setattr(recap, "_synthesize_overall", _boom)
        monkeypatch.setattr(recap, "_synthesize_categories", _boom)
        out = get_recap(window="week", allow_llm=False)
        assert out["ok"] is True
        assert out["top_focus"] is None
        assert all(c["narrative"] is None for c in out["categories"])

    def test_allow_llm_true_synthesizes_both_narrative_levels(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        """allow_llm=True is the opt-in: both the overall and per-category
        synthesis steps must actually run (previously narrative= was never
        passed through, so category narratives stayed null regardless)."""
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-1", hours_ago=2)
        calls = {"overall": 0, "categories": 0}

        def _fake_overall(*a, **kw):
            calls["overall"] += 1
            return "fake overall narrative"

        def _fake_categories(label, sessions, rollups, summaries, console):
            calls["categories"] += 1
            return {r.category: "fake category narrative" for r in rollups}

        monkeypatch.setattr(recap, "_synthesize_overall", _fake_overall)
        monkeypatch.setattr(recap, "_synthesize_categories", _fake_categories)
        out = get_recap(window="week", allow_llm=True)
        assert calls == {"overall": 1, "categories": 1}
        assert out["top_focus"] == "fake overall narrative"
        assert out["categories"][0]["narrative"] == "fake category narrative"


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

    def test_window_all_is_rejected(self, tmp_home):
        """No meaningful "previous window" for an open-ended range — must
        reject before doing any work, not silently scan ~26 years back."""
        out = compare_to_previous(window="all")
        assert out["ok"] is False
        assert "does not support window='all'" in out["error"]

    def test_applies_configured_prices_before_computing_cost(
        self, tmp_home, jsonl_factory,
    ):
        """Regression: cost figures must reflect the user's config.toml
        [prices] override, not whatever DEFAULT_PRICES / leftover global
        state token_usage._active_prices happens to hold at call time."""
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-cur", hours_ago=2)
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-prev", hours_ago=9 * 24)
        # make_assistant_msg() defaults to model="claude-opus-4-7"; _price_for()
        # substring-matches "opus", so [prices.opus] is the override that
        # actually applies to these fixture sessions.
        config_path = tmp_home / ".ccstory" / "config.toml"
        config_path.write_text(
            "[prices.opus]\ninput = 999.0\noutput = 999.0\n", encoding="utf-8",
        )
        out = compare_to_previous(window="week")
        assert out["ok"] is True
        # Default opus pricing on the fixture's 100in/50out-token exchanges
        # is ~$0.004; the extreme override must move the number well past
        # that without needing an exact-value assertion.
        assert out["current_cost_usd"] > 0.1


class TestGetTrend:
    def test_happy_path_shape(self, tmp_home, jsonl_factory):
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-now", hours_ago=2)
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-old",
                      hours_ago=9 * 24)
        out = get_trend(period="week", count=2)
        assert out["ok"] is True
        assert out["period"] == "week"
        assert out["count"] == 2
        assert len(out["points"]) == 2
        for p in out["points"]:
            assert set(p) == {"label", "since", "until", "active_hours",
                              "cost_usd", "buckets"}
        # Windows come back oldest first; the seeded sessions put activity
        # in both of them.
        assert out["points"][0]["since"] < out["points"][1]["since"]
        assert out["points"][-1]["active_hours"] > 0
        assert out["points"][-1]["buckets"][0]["sessions"] >= 1

    def test_count_is_clamped(self, tmp_home):
        out = get_trend(period="week", count=999)
        assert out["ok"] is True
        assert out["count"] == 24
        assert len(out["points"]) == 24
        out = get_trend(period="week", count=0)
        assert out["count"] == 1

    def test_bad_period_normalizes_instead_of_raising(self, tmp_home):
        out = get_trend(period="day")
        assert out["ok"] is False
        assert "unsupported" in out["error"]

    def test_never_fires_llm_even_with_hybrid(
        self, tmp_home, jsonl_factory, monkeypatch,
    ):
        """The docstring guarantee: no parameter combination runs claude -p.
        Fence off the subprocess chokepoint itself so ANY LLM attempt fails
        loudly, then ask for the LLM-most classify mode."""
        _seed_session(jsonl_factory, "-Users-me-unrecognized-x", "sess-1",
                      hours_ago=2)

        def _boom(*a, **kw):
            raise AssertionError("get_trend must never invoke claude -p")

        monkeypatch.setattr(ss, "run_claude_p", _boom)
        out = get_trend(period="week", count=1, classify="hybrid")
        assert out["ok"] is True

    def test_applies_configured_prices_before_computing_cost(
        self, tmp_home, jsonl_factory,
    ):
        """Same regression fence compare_to_previous carries (#115): cost
        must reflect the config.toml [prices] override, not global state."""
        _seed_session(jsonl_factory, "-Users-me-proj", "sess-cur", hours_ago=2)
        config_path = tmp_home / ".ccstory" / "config.toml"
        config_path.write_text(
            "[prices.opus]\ninput = 999.0\noutput = 999.0\n", encoding="utf-8",
        )
        out = get_trend(period="week", count=1)
        assert out["ok"] is True
        assert out["points"][-1]["cost_usd"] > 0.1


class TestListCategories:
    def test_returns_default_rules_shape(self, tmp_home):
        out = list_categories()
        assert out["ok"] is True
        names = [c["name"] for c in out["categories"]]
        assert "coding" in names  # built-in default bucket, no config.toml yet
        for c in out["categories"]:
            assert isinstance(c["needles"], list) and c["needles"]

    def test_error_normalizes_instead_of_raising(self, tmp_home, monkeypatch):
        """Same contract as the other two tools: an exception from the
        underlying call must come back as {"ok": False, ...}, not propagate
        and hand the caller an MCP-level ToolError instead."""
        from ccstory import mcp_server

        def _boom():
            raise ValueError("config.toml is unreadable")

        monkeypatch.setattr(mcp_server, "load_rules", _boom)
        out = list_categories()
        assert out == {"ok": False, "error": "config.toml is unreadable"}


def test_no_stdout_leak_across_tools(tmp_home, jsonl_factory, capsys):
    """Regression guard: stdout is the MCP protocol stream. Any stray
    `print()` reachable from these tools would corrupt it silently."""
    _seed_session(jsonl_factory, "-Users-me-proj", "sess-cur", hours_ago=2)
    _seed_session(jsonl_factory, "-Users-me-proj", "sess-prev", hours_ago=9 * 24)
    get_recap(window="week")
    compare_to_previous(window="week")
    get_trend(period="week", count=2)
    list_categories()
    assert capsys.readouterr().out == ""


class TestRunMcpArgvHandling:
    """`ccstory mcp` takes no flags in v0, but must not silently ignore
    argv and fall into the stdio blocking read loop on --help or a typo —
    that looks identical to a hang from the terminal."""

    def test_help_prints_usage_and_exits_zero(self, capsys):
        rc = cli._run_mcp(["--help"])
        assert rc == 0
        assert "usage: ccstory mcp" in capsys.readouterr().err

    def test_unrecognized_arg_errors_instead_of_hanging(self, capsys):
        rc = cli._run_mcp(["--bogus"])
        assert rc == 1
        assert "unrecognized argument" in capsys.readouterr().err
