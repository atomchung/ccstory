"""Tests for ccstory.token_usage.

Locks: pricing math, token aggregation, streaming-chunk dedup by message id,
fmt_tokens output shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ccstory.token_usage import (
    ModelUsage,
    _price_for,
    collect_usage,
    fmt_tokens,
)

from tests.conftest import _ts, make_assistant_msg, make_user_msg, write_jsonl


class TestPriceFor:
    def test_opus_price(self):
        p = _price_for("claude-opus-4-7")
        assert p is not None
        assert p["inp"] == 5.00
        assert p["out"] == 25.00

    def test_sonnet_price(self):
        p = _price_for("claude-sonnet-4-6")
        assert p is not None
        assert p["inp"] == 3.00

    def test_haiku_price(self):
        p = _price_for("claude-haiku-4-5-20251001")
        assert p is not None
        assert p["inp"] == 1.00

    def test_unknown_model_returns_none(self):
        assert _price_for("nonexistent-dummy-model-xyz") is None
        assert _price_for("") is None


class TestModelUsage:
    def test_total_tokens_sums_all_fields(self):
        mu = ModelUsage(
            model="claude-opus-4-7",
            input_tokens=100,
            cache_creation=200,
            cache_read=300,
            output_tokens=50,
        )
        assert mu.total_tokens == 650

    def test_cost_calculation_opus(self):
        # 1M input * $5 + 1M output * $25 = $30
        mu = ModelUsage(
            model="claude-opus-4-7",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        assert mu.cost_usd == 30.0

    def test_cost_calculation_with_cache(self):
        # cache_read is much cheaper: 1M cache_read * $0.50 = $0.50
        mu = ModelUsage(
            model="claude-opus-4-7",
            cache_read=1_000_000,
        )
        assert mu.cost_usd == 0.50

    def test_uncached_cost_treats_cache_as_fresh_input(self):
        # cache_read counted at input rate: 1M * $5 = $5
        mu = ModelUsage(
            model="claude-opus-4-7",
            cache_read=1_000_000,
        )
        assert mu.cost_uncached_usd == 5.0

    def test_unknown_model_zero_cost(self):
        mu = ModelUsage(model="nonexistent-dummy-model-xyz", input_tokens=1_000_000)
        assert mu.cost_usd == 0.0


class TestFmtTokens:
    def test_under_1k(self):
        assert fmt_tokens(0) == "0"
        assert fmt_tokens(42) == "42"
        assert fmt_tokens(999) == "999"

    def test_thousands(self):
        assert fmt_tokens(1_000) == "1.0k"
        assert fmt_tokens(12_345) == "12.3k"

    def test_millions(self):
        assert fmt_tokens(1_500_000) == "1.50M"
        assert fmt_tokens(2_920_000) == "2.92M"

    def test_billions(self):
        assert fmt_tokens(3_400_000_000) == "3.40B"


class TestCollectUsage:
    def _records(self):
        return [
            make_user_msg("hello", _ts(2026, 5, 10, 10, 0, 0)),
            make_assistant_msg(
                "hi", _ts(2026, 5, 10, 10, 0, 5), "msg_a",
                model="claude-opus-4-7",
                input_tokens=1000, output_tokens=500, cache_read=20000,
            ),
            make_user_msg("more", _ts(2026, 5, 10, 10, 1, 0)),
            make_assistant_msg(
                "ok", _ts(2026, 5, 10, 10, 1, 10), "msg_b",
                model="claude-sonnet-4-6",
                input_tokens=200, output_tokens=100, cache_read=5000,
            ),
        ]

    def test_basic_aggregation(self, jsonl_factory):
        jsonl_factory("-Users-alice-code-myapp", "session-1", self._records())
        rep = collect_usage(
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 31, tzinfo=timezone.utc),
        )
        assert rep.assistant_turns == 2
        assert "claude-opus-4-7" in rep.by_model
        assert "claude-sonnet-4-6" in rep.by_model
        opus = rep.by_model["claude-opus-4-7"]
        assert opus.input_tokens == 1000
        assert opus.output_tokens == 500
        assert opus.cache_read == 20000

    def test_dedup_by_message_id(self, jsonl_factory):
        # Claude Code streaming writes the same message id multiple times
        records = [
            make_user_msg("hello", _ts(2026, 5, 10, 10, 0, 0)),
            make_assistant_msg("hi", _ts(2026, 5, 10, 10, 0, 5), "msg_dup",
                               input_tokens=100, output_tokens=50),
            # Same msg_id repeated — should NOT double-count
            make_assistant_msg("hi", _ts(2026, 5, 10, 10, 0, 5), "msg_dup",
                               input_tokens=100, output_tokens=50),
        ]
        jsonl_factory("-Users-alice-code-app", "session-dup", records)
        rep = collect_usage(
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 31, tzinfo=timezone.utc),
        )
        # Should see exactly one assistant turn, not two
        assert rep.assistant_turns == 1
        assert rep.total_input == 100
        assert rep.total_output == 50

    def test_out_of_range_filtered(self, jsonl_factory):
        records = [
            make_user_msg("old", _ts(2026, 1, 1, 0, 0, 0)),
            make_assistant_msg("reply", _ts(2026, 1, 1, 0, 0, 5), "msg_old",
                               input_tokens=999, output_tokens=999),
        ]
        jsonl_factory("-Users-alice-code-app", "session-old", records)
        rep = collect_usage(
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 31, tzinfo=timezone.utc),
        )
        assert rep.assistant_turns == 0
        assert rep.total_input == 0

    def test_malformed_line_doesnt_crash(self, tmp_home, jsonl_factory):
        # Write a file with a broken line — should be silently skipped
        path = jsonl_factory("-Users-alice-code-app", "session-bad", [])
        with path.open("w", encoding="utf-8") as f:
            f.write("not json\n")
            import json
            f.write(
                json.dumps(
                    make_assistant_msg(
                        "ok", _ts(2026, 5, 10, 10, 0, 0), "msg_ok",
                        input_tokens=42,
                    )
                )
                + "\n"
            )
        rep = collect_usage(
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 31, tzinfo=timezone.utc),
        )
        # The valid record still parses
        assert rep.assistant_turns == 1
        assert rep.total_input == 42

    def test_cache_hit_ratio(self, jsonl_factory):
        # 9000 cache_read out of 10000 total input-side = 0.9
        records = [
            make_user_msg("hi", _ts(2026, 5, 10, 10, 0, 0)),
            make_assistant_msg(
                "ok", _ts(2026, 5, 10, 10, 0, 5), "msg_1",
                input_tokens=1000, cache_read=9000,
                cache_creation=0, output_tokens=0,
            ),
        ]
        jsonl_factory("-Users-alice-code-app", "session-cache", records)
        rep = collect_usage(
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 31, tzinfo=timezone.utc),
        )
        assert rep.cache_hit_ratio == 0.9

    def test_claude_subagent_usage_included(self, jsonl_factory):
        records = [
            make_user_msg("hello subagent", _ts(2026, 5, 10, 10, 0, 0)),
            make_assistant_msg(
                "subagent response", _ts(2026, 5, 10, 10, 0, 5), "msg_sub1",
                model="claude-sonnet-4-6",
                input_tokens=300, output_tokens=150,
            ),
        ]
        jsonl_factory("myproj/subagents", "subagent-session", records)
        rep = collect_usage(
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 31, tzinfo=timezone.utc),
            agent="claude",
        )
        assert rep.assistant_turns == 1
        assert rep.total_input == 300
        assert rep.total_output == 150

def make_codex_token_count(
    ts: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int = 0,
    cache_write_input_tokens: int = 0,
) -> dict:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "cache_write_input_tokens": cache_write_input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": reasoning_output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }
            },
        },
    }


class TestCodexTokenUsage:
    def test_cumulative_semantics_uses_last_token_count_record(self, codex_factory):
        records = [
            {
                "timestamp": "2026-07-22T12:00:00Z",
                "type": "session_meta",
                "payload": {"session_id": "sid-1", "cwd": "/Users/test/app"},
            },
            {
                "timestamp": "2026-07-22T12:01:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol"},
            },
            make_codex_token_count("2026-07-22T12:02:00Z", input_tokens=1000, cached_input_tokens=200, output_tokens=100),
            make_codex_token_count("2026-07-22T12:03:00Z", input_tokens=2500, cached_input_tokens=500, output_tokens=300),
        ]
        codex_factory("sid-1", records)
        rep = collect_usage(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            agent="codex",
        )
        assert "gpt-5.6-sol" in rep.by_model
        mu = rep.by_model["gpt-5.6-sol"]
        assert mu.input_tokens == 2000
        assert mu.cache_read == 500
        assert mu.output_tokens == 300

    def test_cache_conversion_subtracts_cached_input_tokens(self, codex_factory):
        records = [
            {
                "timestamp": "2026-07-22T12:00:00Z",
                "type": "session_meta",
                "payload": {"session_id": "sid-2", "cwd": "/Users/test/app"},
            },
            {
                "timestamp": "2026-07-22T12:01:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-terra"},
            },
            make_codex_token_count(
                "2026-07-22T12:02:00Z",
                input_tokens=26583,
                cached_input_tokens=6912,
                output_tokens=236,
            ),
        ]
        codex_factory("sid-2", records)
        rep = collect_usage(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            agent="codex",
        )
        mu = rep.by_model["gpt-5.6-terra"]
        assert mu.input_tokens == 19671
        assert mu.cache_read == 6912

    def test_reasoning_output_tokens_not_added_to_output(self, codex_factory):
        records = [
            {
                "timestamp": "2026-07-22T12:00:00Z",
                "type": "session_meta",
                "payload": {"session_id": "sid-3", "cwd": "/Users/test/app"},
            },
            {
                "timestamp": "2026-07-22T12:01:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-luna"},
            },
            make_codex_token_count(
                "2026-07-22T12:02:00Z",
                input_tokens=1000,
                cached_input_tokens=100,
                output_tokens=236,
                reasoning_output_tokens=94,
            ),
        ]
        codex_factory("sid-3", records)
        rep = collect_usage(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            agent="codex",
        )
        mu = rep.by_model["gpt-5.6-luna"]
        assert mu.output_tokens == 236

    def test_agent_filter_claude_vs_codex(self, jsonl_factory, codex_factory):
        claude_records = [
            make_user_msg("hello", _ts(2026, 7, 22, 10, 0, 0)),
            make_assistant_msg(
                "hi", _ts(2026, 7, 22, 10, 0, 5), "msg_c1",
                model="claude-sonnet-4-6",
                input_tokens=500, output_tokens=100,
            ),
        ]
        jsonl_factory("-Users-alice-code-app", "session-claude", claude_records)

        codex_records = [
            {
                "timestamp": "2026-07-22T12:00:00Z",
                "type": "session_meta",
                "payload": {"session_id": "sid-4", "cwd": "/Users/test/app"},
            },
            {
                "timestamp": "2026-07-22T12:01:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol"},
            },
            make_codex_token_count("2026-07-22T12:02:00Z", input_tokens=1000, cached_input_tokens=0, output_tokens=200),
        ]
        codex_factory("sid-4", codex_records)

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 7, 31, tzinfo=timezone.utc)

        rep_claude = collect_usage(since, until, agent="claude")
        assert "claude-sonnet-4-6" in rep_claude.by_model
        assert "gpt-5.6-sol" not in rep_claude.by_model

        rep_codex = collect_usage(since, until, agent="codex")
        assert "gpt-5.6-sol" in rep_codex.by_model
        assert "claude-sonnet-4-6" not in rep_codex.by_model

        rep_all = collect_usage(since, until, agent="all")
        assert "claude-sonnet-4-6" in rep_all.by_model
        assert "gpt-5.6-sol" in rep_all.by_model

    def test_unknown_model_does_not_crash(self, codex_factory):
        records = [
            {
                "timestamp": "2026-07-22T12:00:00Z",
                "type": "session_meta",
                "payload": {"session_id": "sid-5", "cwd": "/Users/test/app"},
            },
            {
                "timestamp": "2026-07-22T12:01:00Z",
                "type": "turn_context",
                "payload": {"model": "codex-auto-review"},
            },
            make_codex_token_count("2026-07-22T12:02:00Z", input_tokens=5000, cached_input_tokens=1000, output_tokens=300),
        ]
        codex_factory("sid-5", records)
        rep = collect_usage(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            agent="codex",
        )
        assert "codex-auto-review" in rep.by_model
        mu = rep.by_model["codex-auto-review"]
        assert mu.input_tokens == 4000
        assert mu.cache_read == 1000
        assert mu.output_tokens == 300
        assert mu.cost_usd == 0.0

    def test_codex_subagent_usage_included(self, codex_factory):
        records = [
            {
                "timestamp": "2026-07-22T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "session_id": "sub-1",
                    "parent_thread_id": "parent-123",
                    "source": {"subagent": "task-runner"},
                    "cwd": "/Users/test/app",
                },
            },
            {
                "timestamp": "2026-07-22T12:01:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol"},
            },
            make_codex_token_count(
                "2026-07-22T12:02:00Z",
                input_tokens=2000,
                cached_input_tokens=500,
                output_tokens=400,
            ),
        ]
        codex_factory("sub-1", records)
        rep = collect_usage(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            agent="codex",
        )
        assert "gpt-5.6-sol" in rep.by_model
        mu = rep.by_model["gpt-5.6-sol"]
        assert mu.input_tokens == 1500
        assert mu.cache_read == 500
        assert mu.output_tokens == 400

