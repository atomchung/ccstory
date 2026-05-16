"""Tests for #23 — price-config override + snapshot date disclosure."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccstory import token_usage
from ccstory.token_usage import (
    DEFAULT_PRICES,
    PRICES_SNAPSHOT_DATE,
    ModelUsage,
    _price_for,
    apply_prices,
    collect_usage,
    get_snapshot_date,
    load_prices_config,
)

from tests.conftest import _ts, make_assistant_msg, make_user_msg


@pytest.fixture(autouse=True)
def _reset_active_prices(monkeypatch: pytest.MonkeyPatch):
    """Each test gets a fresh active price table."""
    monkeypatch.setattr(
        token_usage, "_active_prices",
        {k: dict(v) for k, v in DEFAULT_PRICES.items()},
    )
    monkeypatch.setattr(token_usage, "_active_snapshot_date", PRICES_SNAPSHOT_DATE)


class TestDefaults:
    def test_default_snapshot_date(self):
        assert get_snapshot_date() == PRICES_SNAPSHOT_DATE

    def test_default_opus_price_unchanged(self):
        assert _price_for("claude-opus-4-7")["inp"] == 15.0


class TestLoadPricesConfig:
    def test_missing_config_returns_defaults(self, tmp_path: Path):
        prices, snapshot = load_prices_config(tmp_path / "nope.toml")
        assert prices == DEFAULT_PRICES
        assert snapshot == PRICES_SNAPSHOT_DATE

    def test_config_without_prices_block_returns_defaults(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('default_bucket = "writing"\n', encoding="utf-8")
        prices, snapshot = load_prices_config(cfg)
        assert prices == DEFAULT_PRICES
        assert snapshot == PRICES_SNAPSHOT_DATE

    def test_snapshot_date_override(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[prices]\nsnapshot_date = "2026-03"\n',
            encoding="utf-8",
        )
        _, snapshot = load_prices_config(cfg)
        assert snapshot == "2026-03"

    def test_per_model_override(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[prices.opus]\n"
            "input = 10.0\n"
            "output = 50.0\n"
            "cache_read = 1.0\n"
            "cache_write = 12.5\n",
            encoding="utf-8",
        )
        prices, _ = load_prices_config(cfg)
        assert prices["opus"]["inp"] == 10.0
        assert prices["opus"]["out"] == 50.0
        assert prices["opus"]["cr"] == 1.0
        assert prices["opus"]["cw"] == 12.5
        # other models untouched
        assert prices["sonnet"] == DEFAULT_PRICES["sonnet"]

    def test_partial_override_preserves_other_keys(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[prices.opus]\ninput = 20.0\n",
            encoding="utf-8",
        )
        prices, _ = load_prices_config(cfg)
        assert prices["opus"]["inp"] == 20.0
        # output preserved from defaults
        assert prices["opus"]["out"] == DEFAULT_PRICES["opus"]["out"]

    def test_malformed_value_ignored(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[prices.opus]\ninput = \"not a number\"\n",
            encoding="utf-8",
        )
        prices, _ = load_prices_config(cfg)
        # Bad value silently ignored, default kept
        assert prices["opus"]["inp"] == DEFAULT_PRICES["opus"]["inp"]

    def test_new_model_via_config(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[prices.custom]\n"
            "input = 1.0\noutput = 2.0\ncache_write = 0.5\ncache_read = 0.1\n",
            encoding="utf-8",
        )
        prices, _ = load_prices_config(cfg)
        assert "custom" in prices
        assert prices["custom"]["inp"] == 1.0

    def test_partial_new_model_fills_missing_keys_as_zero(self, tmp_path: Path):
        # User defines a new model with ONLY `input` — missing keys must
        # default to 0.0 so cost code can index the dict directly without
        # KeyError. Regression test for #23 follow-up.
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[prices.custom]\ninput = 1.0\n",
            encoding="utf-8",
        )
        prices, _ = load_prices_config(cfg)
        assert prices["custom"] == {"inp": 1.0, "out": 0.0, "cw": 0.0, "cr": 0.0}

    def test_partial_new_model_cost_does_not_crash(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[prices.custom]\ninput = 1.0\n",
            encoding="utf-8",
        )
        prices, snapshot = load_prices_config(cfg)
        apply_prices(prices, snapshot_date=snapshot)
        mu = ModelUsage(
            model="custom-model-v1",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_creation=1_000_000,
            cache_read=1_000_000,
        )
        # Only `input` priced; output / cache_write / cache_read are 0.
        # cost_usd = (1M*1 + 1M*0 + 1M*0 + 1M*0) / 1M = 1.0
        assert mu.cost_usd == 1.0
        # cost_uncached_usd charges cache tokens at the input rate, so:
        # (1M*1 + 1M*0 + 1M*1 + 1M*1) / 1M = 3.0
        assert mu.cost_uncached_usd == 3.0


class TestApplyPrices:
    def test_apply_replaces_active(self):
        apply_prices({"opus": {"inp": 99.0, "out": 100.0, "cw": 50.0, "cr": 5.0}})
        p = _price_for("claude-opus-4-7")
        assert p["inp"] == 99.0

    def test_apply_updates_snapshot_date(self):
        apply_prices(
            {"opus": DEFAULT_PRICES["opus"]},
            snapshot_date="2026-04",
        )
        assert get_snapshot_date() == "2026-04"

    def test_apply_without_snapshot_keeps_existing(self):
        apply_prices({"opus": DEFAULT_PRICES["opus"]})
        assert get_snapshot_date() == PRICES_SNAPSHOT_DATE


class TestCollectUsageWithOverride:
    def test_cost_uses_overridden_price(self, tmp_home: Path, jsonl_factory):
        # Override opus input price to $1.00 / 1M (much cheaper than default $15)
        apply_prices({
            "opus": dict(inp=1.0, out=5.0, cw=2.0, cr=0.5),
            "sonnet": DEFAULT_PRICES["sonnet"],
            "haiku": DEFAULT_PRICES["haiku"],
        })
        jsonl_factory(
            "-Users-alice-code-x",
            "sess-pricing",
            [
                make_user_msg("hi", _ts(2026, 5, 10, 10, 0, 0)),
                make_assistant_msg(
                    "ok", _ts(2026, 5, 10, 10, 0, 5), "msg_1",
                    model="claude-opus-4-7",
                    input_tokens=1_000_000, output_tokens=0,
                    cache_creation=0, cache_read=0,
                ),
            ],
        )
        rep = collect_usage(
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 31, tzinfo=timezone.utc),
        )
        # 1M input * $1 = $1.0, not the default $15.0
        assert rep.total_cost_usd == 1.0


class TestModelUsageCostUsesActive:
    def test_cost_picks_up_overridden_active_prices(self):
        apply_prices({"opus": dict(inp=2.0, out=4.0, cw=3.0, cr=0.5),
                      "sonnet": DEFAULT_PRICES["sonnet"],
                      "haiku": DEFAULT_PRICES["haiku"]})
        mu = ModelUsage(
            model="claude-opus-4-7",
            input_tokens=1_000_000, output_tokens=1_000_000,
        )
        # 1M input * 2 + 1M output * 4 = 6
        assert mu.cost_usd == 6.0
