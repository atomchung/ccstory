"""Tests for ccstory.token_usage.

Locks: pricing math, token aggregation, streaming-chunk dedup by message id,
fmt_tokens output shape.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ccstory.token_usage import (
    ModelUsage,
    UsageReport,
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
    def test_cumulative_semantics_differences_adjacent_records(self, codex_factory):
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

    def test_window_uses_pre_window_snapshot_as_baseline(self, codex_factory):
        records = [
            {
                "timestamp": "2026-07-01T00:00:00Z",
                "type": "session_meta",
                "payload": {"session_id": "spanning", "cwd": "/Users/test/app"},
            },
            {
                "timestamp": "2026-07-01T00:01:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol"},
            },
            make_codex_token_count(
                "2026-07-09T23:59:00Z", input_tokens=1000,
                cached_input_tokens=200, cache_write_input_tokens=50,
                output_tokens=100,
            ),
            make_codex_token_count(
                "2026-07-10T00:01:00Z", input_tokens=1600,
                cached_input_tokens=500, cache_write_input_tokens=90,
                output_tokens=180,
            ),
        ]
        codex_factory("spanning", records)
        rep = collect_usage(
            datetime(2026, 7, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 20, tzinfo=timezone.utc),
            agent="codex",
        )
        mu = rep.by_model["gpt-5.6-sol"]
        assert rep.assistant_turns == 1
        assert mu.input_tokens == 300  # 600 input delta - 300 cached
        assert mu.cache_read == 300
        assert mu.cache_creation == 40
        assert mu.output_tokens == 80

    def test_post_window_continuation_does_not_erase_in_window_delta(
        self, codex_factory,
    ):
        records = [
            {
                "timestamp": "2026-07-09T23:00:00Z",
                "type": "session_meta",
                "payload": {"session_id": "continued", "cwd": "/Users/test/app"},
            },
            {
                "timestamp": "2026-07-09T23:01:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol"},
            },
            make_codex_token_count(
                "2026-07-09T23:59:00Z", input_tokens=100,
                cached_input_tokens=0, output_tokens=10,
            ),
            make_codex_token_count(
                "2026-07-10T12:00:00Z", input_tokens=400,
                cached_input_tokens=100, output_tokens=50,
            ),
            make_codex_token_count(
                "2026-07-21T00:01:00Z", input_tokens=900,
                cached_input_tokens=300, output_tokens=120,
            ),
        ]
        codex_factory("continued", records)
        rep = collect_usage(
            datetime(2026, 7, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 20, tzinfo=timezone.utc),
            agent="codex",
        )
        assert rep.assistant_turns == 1
        assert rep.total_input == 200
        assert rep.total_cache_read == 100
        assert rep.total_output == 40

    def test_delta_is_attributed_to_model_active_at_snapshot(self, codex_factory):
        records = [
            {
                "timestamp": "2026-07-10T00:00:00Z",
                "type": "session_meta",
                "payload": {"session_id": "models", "cwd": "/Users/test/app"},
            },
            {
                "timestamp": "2026-07-10T00:01:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-terra"},
            },
            make_codex_token_count(
                "2026-07-10T00:02:00Z", input_tokens=500,
                cached_input_tokens=100, output_tokens=50,
            ),
            {
                "timestamp": "2026-07-10T00:03:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol"},
            },
            make_codex_token_count(
                "2026-07-10T00:04:00Z", input_tokens=900,
                cached_input_tokens=250, output_tokens=120,
            ),
        ]
        codex_factory("models", records)
        rep = collect_usage(
            datetime(2026, 7, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 11, tzinfo=timezone.utc),
            agent="codex",
        )
        terra = rep.by_model["gpt-5.6-terra"]
        sol = rep.by_model["gpt-5.6-sol"]
        assert (terra.input_tokens, terra.cache_read, terra.output_tokens) == (
            400, 100, 50,
        )
        assert (sol.input_tokens, sol.cache_read, sol.output_tokens) == (
            250, 150, 70,
        )
        assert terra.turns == sol.turns == 1

    def test_resume_uses_baseline_from_older_rollout_file(self, codex_factory):
        root_id = "shared-thread-root"
        old_path = codex_factory(
            "old-rollout",
            [
                {
                    "timestamp": "2026-07-01T00:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": root_id,
                        "id": root_id,
                        "cwd": "/Users/test/app",
                    },
                },
                {
                    "timestamp": "2026-07-01T00:01:00Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-sol"},
                },
                make_codex_token_count(
                    "2026-07-01T00:02:00Z", input_tokens=1000,
                    cached_input_tokens=200, output_tokens=100,
                ),
            ],
        )
        # Reproduce a genuinely old root rollout: its mtime predates the
        # report window even though a resumed file for the same thread is new.
        old_mtime = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
        os.utime(old_path, (old_mtime, old_mtime))
        codex_factory(
            "new-rollout",
            [
                {
                    "timestamp": "2026-07-10T00:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": root_id,
                        "id": root_id,
                        "cwd": "/Users/test/app",
                    },
                },
                {
                    "timestamp": "2026-07-10T00:01:00Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-sol"},
                },
                make_codex_token_count(
                    "2026-07-10T00:02:00Z", input_tokens=1600,
                    cached_input_tokens=500, output_tokens=180,
                ),
            ],
        )
        rep = collect_usage(
            datetime(2026, 7, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 11, tzinfo=timezone.utc),
            agent="codex",
        )
        assert rep.total_input == 300
        assert rep.total_cache_read == 300
        assert rep.total_output == 80

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

    def test_subagent_inherited_baseline_is_subtracted_but_child_cost_is_included(
        self, codex_factory,
    ):
        parent_id = "parent-branch"
        root = [
            {
                "timestamp": "2026-07-22T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "session_id": parent_id,
                    "id": parent_id,
                    "cwd": "/Users/test/app",
                },
            },
            {
                "timestamp": "2026-07-22T12:01:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-terra"},
            },
            make_codex_token_count(
                "2026-07-22T12:02:00Z",
                input_tokens=100,
                cached_input_tokens=20,
                output_tokens=10,
            ),
            make_codex_token_count(
                "2026-07-22T12:03:00Z",
                input_tokens=300,
                cached_input_tokens=60,
                output_tokens=30,
            ),
        ]
        child = [
            {
                "timestamp": "2026-07-22T12:04:00Z",
                "type": "session_meta",
                "payload": {
                    "session_id": parent_id,
                    "id": "child-branch",
                    "parent_thread_id": parent_id,
                    "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                    "cwd": "/Users/test/app",
                },
            },
            {
                "timestamp": "2026-07-22T12:04:01Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol"},
            },
            # Copied ancestor baseline: not child usage.
            make_codex_token_count(
                "2026-07-22T12:04:02Z",
                input_tokens=100,
                cached_input_tokens=20,
                output_tokens=10,
            ),
            # Branch-local delta: 400 input (100 cached), 60 output.
            make_codex_token_count(
                "2026-07-22T12:05:00Z",
                input_tokens=500,
                cached_input_tokens=120,
                output_tokens=70,
            ),
        ]
        codex_factory(parent_id, root)
        codex_factory("child-branch", child)
        rep = collect_usage(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            agent="codex",
        )
        # Parent: 240 fresh + 60 cached + 30 output.
        # Child: 300 fresh + 100 cached + 60 output. The copied 100/20/10
        # baseline is present in both files but charged only on the parent.
        assert rep.total_input == 540
        assert rep.total_cache_read == 160
        assert rep.total_output == 90
        assert rep.assistant_turns == 3
        assert rep.by_model["gpt-5.6-terra"].output_tokens == 30
        assert rep.by_model["gpt-5.6-sol"].output_tokens == 60

    def test_nested_subagent_copied_prefix_is_not_replayed(
        self, codex_factory,
    ):
        root_id = "root-branch"
        codex_factory(
            root_id,
            [
                {
                    "timestamp": "2026-07-22T12:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": root_id,
                        "id": root_id,
                        "cwd": "/Users/test/app",
                    },
                },
                {
                    "timestamp": "2026-07-22T12:00:01Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-terra"},
                },
                make_codex_token_count(
                    "2026-07-22T12:01:00Z", 100, 20, 10,
                ),
                make_codex_token_count(
                    "2026-07-22T12:02:00Z", 300, 60, 30,
                ),
            ],
        )
        # Real depth-2 rollouts put their own session_meta first, then copy
        # parent-shaped records into the child file. The whole matching prefix
        # is inherited history; only growth after its terminal snapshot is
        # branch-local usage.
        codex_factory(
            "nested-branch",
            [
                {
                    "timestamp": "2026-07-22T12:03:00Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": root_id,
                        "id": "nested-branch",
                        "parent_thread_id": root_id,
                        "source": {"subagent": {"thread_spawn": {"depth": 2}}},
                    },
                },
                {
                    "timestamp": "2026-07-22T12:03:00Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": root_id,
                        "id": root_id,
                    },
                },
                {
                    "timestamp": "2026-07-22T12:03:00Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-sol"},
                },
                make_codex_token_count(
                    "2026-07-22T12:03:00Z", 100, 20, 10,
                ),
                make_codex_token_count(
                    "2026-07-22T12:03:00Z", 300, 60, 30,
                ),
                make_codex_token_count(
                    "2026-07-22T12:04:00Z", 500, 100, 70,
                ),
            ],
        )
        rep = collect_usage(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            agent="codex",
        )
        # Root output 30 + nested branch-local output (70 - 30) = 70.
        assert rep.total_output == 70
        assert rep.total_input == 400  # root 240 fresh + nested 160 fresh
        assert rep.total_cache_read == 100
        assert rep.assistant_turns == 3

    def test_real_shape_44_of_108_child_snapshots_are_copied_prefix(
        self, codex_factory,
    ):
        """Regression for rollout 019f84f3…: its first 44 of 108 cumulative
        snapshots match the parent, ending at output=12,232; child-local
        output is therefore 31,380 - 12,232 = 19,148."""
        root_id = "real-shape-root"
        start = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
        parent_snapshots = [
            make_codex_token_count(
                (start + timedelta(seconds=i)).isoformat().replace("+00:00", "Z"),
                input_tokens=1000 * i,
                cached_input_tokens=800 * i,
                output_tokens=(12232 * i) // 44,
            )
            for i in range(1, 45)
        ]
        codex_factory(
            root_id,
            [
                {
                    "timestamp": start.isoformat().replace("+00:00", "Z"),
                    "type": "session_meta",
                    "payload": {"session_id": root_id, "id": root_id},
                },
                {
                    "timestamp": (start + timedelta(milliseconds=1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-terra"},
                },
                *parent_snapshots,
            ],
        )

        copied_prefix = [
            make_codex_token_count(
                (start + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                input_tokens=1000 * i,
                cached_input_tokens=800 * i,
                output_tokens=(12232 * i) // 44,
            )
            for i in range(1, 45)
        ]
        child_growth = [
            make_codex_token_count(
                (start + timedelta(minutes=3, seconds=i))
                .isoformat()
                .replace("+00:00", "Z"),
                input_tokens=44000 + 1000 * i,
                cached_input_tokens=35200 + 800 * i,
                output_tokens=12232 + ((31380 - 12232) * i) // 64,
            )
            for i in range(1, 65)
        ]
        codex_factory(
            "real-shape-child",
            [
                {
                    "timestamp": (start + timedelta(minutes=2))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "type": "session_meta",
                    "payload": {
                        "session_id": root_id,
                        "id": "real-shape-child",
                        "parent_thread_id": root_id,
                        "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                    },
                },
                {
                    "timestamp": (start + timedelta(minutes=2, milliseconds=1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-sol"},
                },
                *copied_prefix,
                *child_growth,
            ],
        )

        rep = collect_usage(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 31, tzinfo=timezone.utc),
            agent="codex",
        )
        assert rep.by_model["gpt-5.6-terra"].output_tokens == 12232
        assert rep.by_model["gpt-5.6-sol"].output_tokens == 19148
        assert rep.total_output == 31380
        assert rep.total_input == 21600
        assert rep.total_cache_read == 86400
        assert rep.assistant_turns == 108

    def test_subagent_window_keeps_current_delta_and_ignores_post_window(
        self, codex_factory,
    ):
        parent_id = "window-parent"
        codex_factory(
            parent_id,
            [
                {
                    "timestamp": "2026-07-09T23:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": parent_id,
                        "id": parent_id,
                    },
                },
                {
                    "timestamp": "2026-07-09T23:01:00Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-sol"},
                },
                make_codex_token_count(
                    "2026-07-09T23:02:00Z", 100, 20, 10,
                ),
            ],
        )
        codex_factory(
            "window-child",
            [
                {
                    "timestamp": "2026-07-09T23:30:00Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": parent_id,
                        "id": "window-child",
                        "parent_thread_id": parent_id,
                        "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                    },
                },
                {
                    "timestamp": "2026-07-09T23:31:00Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-sol"},
                },
                make_codex_token_count(
                    "2026-07-09T23:32:00Z", 100, 20, 10,
                ),
                make_codex_token_count(
                    "2026-07-10T12:00:00Z", 400, 80, 50,
                ),
                make_codex_token_count(
                    "2026-07-21T00:01:00Z", 900, 200, 120,
                ),
            ],
        )
        rep = collect_usage(
            datetime(2026, 7, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 20, tzinfo=timezone.utc),
            agent="codex",
        )
        assert rep.assistant_turns == 1
        assert rep.total_input == 240
        assert rep.total_cache_read == 60
        assert rep.total_output == 40


class TestUsageReportUnpricedModels:
    def test_unpriced_models_only_includes_positive_tokens_without_price(self):
        rep = UsageReport(
            since=datetime(2026, 7, 1, tzinfo=timezone.utc),
            until=datetime(2026, 7, 8, tzinfo=timezone.utc),
        )
        # Priced model with tokens -> excluded
        rep.by_model["claude-opus-4-7"] = ModelUsage(
            model="claude-opus-4-7", input_tokens=100, output_tokens=50
        )
        # Unpriced model with tokens -> included
        rep.by_model["unknown-model-b"] = ModelUsage(
            model="unknown-model-b", input_tokens=200
        )
        rep.by_model["unknown-model-a"] = ModelUsage(
            model="unknown-model-a", output_tokens=100
        )
        # Unpriced model with zero tokens -> excluded
        rep.by_model["unknown-model-zero"] = ModelUsage(
            model="unknown-model-zero", input_tokens=0, output_tokens=0
        )
        assert rep.unpriced_models == ["unknown-model-a", "unknown-model-b"]
