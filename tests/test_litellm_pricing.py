"""Tests for release-time vendored LiteLLM pricing table & precedence in token_usage."""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ccstory import token_usage
from ccstory.token_usage import (
    DEFAULT_PRICES,
    PRICES_SNAPSHOT_DATE,
    _price_for,
    apply_prices,
    load_prices_config,
    load_vendored_prices,
)


@pytest.fixture(autouse=True)
def _reset_active_prices(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        token_usage,
        "_active_prices",
        {k: dict(v) for k, v in DEFAULT_PRICES.items()},
    )
    monkeypatch.setattr(token_usage, "_active_provenance", {k: "default" for k in DEFAULT_PRICES})
    monkeypatch.setattr(token_usage, "_active_snapshot_date", PRICES_SNAPSHOT_DATE)
    monkeypatch.setattr(token_usage, "_vendored_initialized", False)


class TestVendoredPriceTable:
    def test_vendored_file_loads_via_importlib_resources(self):
        ref = importlib.resources.files("ccstory").joinpath("model_prices.json")
        assert ref.is_file()
        content = json.loads(ref.read_text(encoding="utf-8"))
        assert "generated_at" in content
        assert "source_url" in content
        assert "entry_count" in content
        assert "prices" in content
        assert isinstance(content["prices"], dict)
        assert content["entry_count"] == len(content["prices"])
        assert content["entry_count"] < 50

    def test_vendored_file_contains_required_models(self):
        prices, _ = load_vendored_prices()
        required_models = [
            "claude-opus-4-8",
            "claude-sonnet-5",
            "claude-fable-5",
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "claude-opus-4-6",
        ]
        for m in required_models:
            assert m in prices, f"Required model {m} missing from vendored model_prices.json"
            assert all(k in prices[m] for k in ("inp", "out", "cw", "cr"))


class TestPrecedenceLadder:
    def test_user_config_opus_override_wins_over_vendored_table(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("[prices.opus]\ninput = 999.0\n", encoding="utf-8")

        prices, snapshot, prov = load_prices_config(cfg)
        apply_prices(prices, snapshot, prov)

        p = _price_for("claude-opus-4-7")
        assert p is not None
        assert p["inp"] == 999.0

    def test_exact_user_model_override_wins(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("[prices.claude-sonnet-5]\ninput = 123.0\n", encoding="utf-8")

        prices, snapshot, prov = load_prices_config(cfg)
        apply_prices(prices, snapshot, prov)

        p = _price_for("claude-sonnet-5")
        assert p is not None
        assert p["inp"] == 123.0

    def test_exact_vendored_table_model_resolution(self):
        p = _price_for("claude-sonnet-5")
        assert p is not None
        assert p["inp"] == pytest.approx(2.0)
        assert p["out"] == pytest.approx(10.0)

    def test_unknown_model_id_falls_back_to_short_key_price(self):
        # claude-opus-4-9 is unknown to vendored table, falls back to short-key 'opus'
        p = _price_for("claude-opus-4-9")
        assert p is not None
        assert p["inp"] == DEFAULT_PRICES["opus"]["inp"]

    def test_completely_unknown_model_returns_none(self):
        p = _price_for("completely-unknown-model-xyz")
        assert p is None

    def test_plain_dict_apply_prices_unknown_provenance_fallback(self):
        litellm_keys = {
            "claude-opus-4-7": {"inp": 5.0, "out": 25.0, "cw": 6.25, "cr": 0.5},
            "opus": {"inp": 999.0, "out": 25.0, "cw": 6.25, "cr": 0.5},
        }
        apply_prices(dict(litellm_keys), provenance={"opus": "user"})
        p = _price_for("claude-opus-4-7")
        assert p is not None
        assert p["inp"] == 999.0


class TestNoRuntimeNetworkCalls:
    def test_load_prices_config_makes_no_network_calls(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("[prices]\nsnapshot_date = '2026-08'\n", encoding="utf-8")

        with patch("urllib.request.urlopen", side_effect=AssertionError("Network call attempted!")):
            prices, snap, _ = load_prices_config(cfg)
            assert "claude-sonnet-5" in prices
            assert snap == "2026-08"
