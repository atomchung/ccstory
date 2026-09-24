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
    ModelUsage,
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
        assert content["entry_count"] < 200
        assert "xai/grok-4.6" in content["prices"]
        assert "gpt-6-luna" in content["prices"]

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
            "gemini-3.6-flash",
            "gemini-3-flash-preview",
        ]
        for m in required_models:
            assert m in prices, f"Required model {m} missing from vendored model_prices.json"
            assert all(k in prices[m] for k in ("inp", "out"))
            assert set(prices[m]).issubset(
                {
                    "inp", "out", "cw", "cr", "_unsupported_dimensions",
                    "_context_tiers", "_cache_creation_above_1hr",
                    "_service_tiers",
                }
            )
            assert all(
                prices[m][key] >= 0
                for key in ("inp", "out", "cw", "cr")
                if key in prices[m]
            )

    def test_request_dependent_rates_are_disclosed_and_not_flat_priced(self):
        prices, _ = load_vendored_prices()
        dimensions = prices["xai/grok-4.20"]["_unsupported_dimensions"]
        assert "input_cost_per_image_token" in dimensions
        assert prices["xai/grok-4.20"]["_context_tiers"]["200000"] == {
            "cr": 0.4,
            "inp": 2.5,
            "out": 5.0,
        }

        usage = ModelUsage(
            model="xai/grok-4.20",
            input_tokens=250_000,
            output_tokens=10,
        )
        assert not usage.cost_is_priced
        assert usage.cost_usd == 0.0
        assert usage.cost_uncached_usd == 0.0
        assert usage.cache_savings_usd == 0.0

        small_request = ModelUsage(
            model="xai/grok-4.20",
            input_tokens=100_000,
            output_tokens=10,
        )
        assert small_request.cost_is_priced
        assert small_request.cost_usd > 0.0

    def test_missing_cache_rates_are_not_synthesized(self):
        prices, _ = load_vendored_prices()
        assert "cw" not in prices["gemini-3.6-flash"]
        assert "cr" in prices["gemini-3.6-flash"]
        assert "cw" not in prices["xai/grok-4.6"]


class TestAliasPriceResolution:
    def test_explicit_alias_names_resolve_to_canonical_price(self):
        canonical_p = _price_for("gemini-3-flash-preview")
        assert canonical_p is not None

        alias_a_p = _price_for("gemini-3-flash-a")
        alias_agent_p = _price_for("gemini-3-flash-agent")

        assert alias_a_p == canonical_p
        assert alias_agent_p == canonical_p

    @pytest.mark.parametrize("version", ["4.5", "4.6", "4.7"])
    def test_grok_build_version_resolves_to_exact_litellm_model(self, version):
        canonical_p = _price_for(f"xai/grok-{version}")
        assert canonical_p is not None
        assert _price_for(f"grok-{version}-build") == canonical_p

    def test_grok_build_alias_requires_an_exact_canonical_price(self):
        assert _price_for("grok-99.99-build") is None
        assert _price_for("grok-4.6-builder") is None

    def test_canonical_user_override_honored_by_alias(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("[prices.gemini-3-flash-preview]\ninput = 88.0\n", encoding="utf-8")

        prices, snapshot, prov = load_prices_config(cfg)
        apply_prices(prices, snapshot, prov)

        p = _price_for("gemini-3-flash-a")
        assert p is not None
        assert p["inp"] == 88.0

    def test_exact_alias_user_override_wins_over_canonical_user_override(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[prices.gemini-3-flash-preview]\ninput = 88.0\n"
            "[prices.gemini-3-flash-a]\ninput = 99.0\n",
            encoding="utf-8",
        )

        prices, snapshot, prov = load_prices_config(cfg)
        apply_prices(prices, snapshot, prov)

        p_alias = _price_for("gemini-3-flash-a")
        assert p_alias is not None
        assert p_alias["inp"] == 99.0

        p_canonical = _price_for("gemini-3-flash-preview")
        assert p_canonical is not None
        assert p_canonical["inp"] == 88.0

    def test_canonical_grok_user_override_is_honored_by_build_alias(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[prices."xai/grok-4.6"]\ninput = 88.0\n',
            encoding="utf-8",
        )

        prices, snapshot, prov = load_prices_config(cfg)
        apply_prices(prices, snapshot, prov)

        p = _price_for("grok-4.6-build")
        assert p is not None
        assert p["inp"] == 88.0

    def test_request_context_tier_uses_exact_per_request_maximum(self):
        # Aggregate usage spans three requests; none crossed the 200k tier.
        usage = ModelUsage(
            model="grok-4.6-build",
            turns=3,
            input_tokens=300_000,
            output_tokens=30,
            max_request_prompt_tokens=100_000,
        )

        assert usage.cost_is_priced
        assert usage.cost_usd == pytest.approx(0.60018)

    def test_request_above_context_tier_stays_unpriced(self):
        prices, snapshot = load_vendored_prices()
        apply_prices(prices, snapshot)
        usage = ModelUsage(model="grok-4.6-build")
        usage.input_tokens = 250_000
        usage.output_tokens = 30
        usage.record_request_usage(
            input_tokens=250_000,
            cache_creation=0,
            cache_read=0,
            output_tokens=30,
            prompt_tokens=250_000,
        )

        assert usage.cost_is_priced
        assert usage.cost_usd == pytest.approx(1.00036)

    def test_aggregated_multi_call_context_remains_unpriced(self):
        usage = ModelUsage(model="grok-4.6-build")
        usage.input_tokens = 250_000
        usage.output_tokens = 30
        usage.record_request_usage(
            input_tokens=250_000,
            cache_creation=0,
            cache_read=0,
            output_tokens=30,
            prompt_tokens=250_000,
            prompt_is_exact=False,
        )

        assert not usage.cost_is_priced
        assert usage.cost_usd == 0.0

    @pytest.mark.parametrize(
        ("service_tier", "expected_cost"),
        [("default", 0.00503), ("priority", 0.00755)],
    )
    def test_service_tier_and_context_rates_use_exact_request_facts(
        self, service_tier, expected_cost,
    ):
        prices = dict(load_vendored_prices()[0])
        prices["test-service-tier-model"] = {
            "inp": 1.0,
            "out": 2.0,
            "_context_tiers": {"1000": {"inp": 2.0, "out": 3.0}},
            "_service_tiers": {
                "priority": {
                    "rates": {"inp": 2.0, "out": 4.0},
                    "context_tiers": {"1000": {"inp": 3.0, "out": 5.0}},
                },
            },
        }
        apply_prices(prices)
        usage = ModelUsage(model="test-service-tier-model")
        usage.input_tokens = 2_500
        usage.output_tokens = 10
        usage.record_request_usage(
            input_tokens=2_500,
            cache_creation=0,
            cache_read=0,
            output_tokens=10,
            prompt_tokens=2_500,
            service_tier=service_tier,
        )

        assert usage.cost_is_priced
        assert usage.cost_usd == pytest.approx(expected_cost)

    def test_unknown_explicit_service_tier_stays_unpriced(self):
        prices = dict(load_vendored_prices()[0])
        prices["test-service-tier-model"] = {
            "inp": 1.0,
            "out": 2.0,
            "_service_tiers": {"priority": {"rates": {"inp": 2.0, "out": 4.0}}},
        }
        apply_prices(prices)
        usage = ModelUsage(model="test-service-tier-model")
        usage.input_tokens = 100
        usage.output_tokens = 10
        usage.record_request_usage(
            input_tokens=100,
            cache_creation=0,
            cache_read=0,
            output_tokens=10,
            prompt_tokens=100,
            service_tier="unknown",
        )

        assert not usage.cost_is_priced
        assert usage.cost_usd == 0.0

    def test_absent_service_tier_uses_explicit_standard_base_rates(self):
        prices = dict(load_vendored_prices()[0])
        prices["test-service-tier-model"] = {
            "inp": 1.0,
            "out": 2.0,
            "_service_tiers": {"priority": {"rates": {"inp": 2.0, "out": 4.0}}},
        }
        apply_prices(prices)
        usage = ModelUsage(model="test-service-tier-model")
        usage.input_tokens = 100
        usage.output_tokens = 10
        usage.record_request_usage(
            input_tokens=100,
            cache_creation=0,
            cache_read=0,
            output_tokens=10,
            prompt_tokens=100,
        )

        assert usage.cost_is_priced
        assert usage.cost_usd == pytest.approx(0.00012)

    def test_null_service_tier_rate_stays_unpriced_only_for_that_tier(self):
        prices = dict(load_vendored_prices()[0])
        prices["test-service-tier-model"] = {
            "cr": 0.1,
            "_service_tiers": {"priority": {"rates": {"inp": 2.0}}},
            "_unsupported_dimensions": ["cache_read_input_token_cost_priority"],
        }
        apply_prices(prices)

        standard = ModelUsage(model="test-service-tier-model", cache_read=10)
        standard.record_request_usage(
            input_tokens=0,
            cache_creation=0,
            cache_read=10,
            output_tokens=0,
            prompt_tokens=10,
        )
        priority = ModelUsage(model="test-service-tier-model", cache_read=10)
        priority.record_request_usage(
            input_tokens=0,
            cache_creation=0,
            cache_read=10,
            output_tokens=0,
            prompt_tokens=10,
            service_tier="priority",
        )

        assert standard.cost_is_priced
        assert standard.cost_usd == pytest.approx(0.000001)
        assert not priority.cost_is_priced
        assert priority.cost_usd == 0.0

    def test_partial_model_cost_includes_only_exactly_priced_requests(self):
        usage = ModelUsage(model="grok-4.6-build")
        usage.input_tokens = 10_000 + 250_000
        usage.output_tokens = 10 + 30
        usage.record_request_usage(
            input_tokens=10_000,
            cache_creation=0,
            cache_read=0,
            output_tokens=10,
            prompt_tokens=10_000,
        )
        usage.record_request_usage(
            input_tokens=250_000,
            cache_creation=0,
            cache_read=0,
            output_tokens=30,
            prompt_tokens=250_000,
            prompt_is_exact=False,
        )

        assert not usage.cost_is_priced
        assert usage.cost_usd == pytest.approx(0.02006)

    def test_anthropic_one_hour_cache_writes_use_litellm_age_rate(self):
        prices = dict(load_vendored_prices()[0])
        prices["test-anthropic-cache-model"] = {
            "inp": 4.0,
            "out": 20.0,
            "cw": 5.0,
            "cr": 0.2,
            "_cache_creation_above_1hr": 8.0,
        }
        apply_prices(prices)
        usage = ModelUsage(
            model="test-anthropic-cache-model",
            cache_creation=5_000_000,
        )
        usage.record_request_usage(
            input_tokens=0,
            cache_creation=5_000_000,
            cache_read=0,
            output_tokens=0,
            prompt_tokens=5_000_000,
            cache_creation_5m=2_000_000,
            cache_creation_1h=3_000_000,
        )

        assert usage.cost_is_priced
        assert usage.cost_usd == pytest.approx(34.0)

    def test_unknown_cache_write_age_stays_unpriced_when_source_has_age_tier(self):
        prices = dict(load_vendored_prices()[0])
        prices["test-anthropic-cache-model"] = {
            "inp": 4.0,
            "out": 20.0,
            "cw": 5.0,
            "cr": 0.2,
            "_cache_creation_above_1hr": 8.0,
        }
        apply_prices(prices)
        usage = ModelUsage(model="test-anthropic-cache-model", cache_creation=10)
        usage.record_request_usage(
            input_tokens=0,
            cache_creation=10,
            cache_read=0,
            output_tokens=0,
            prompt_tokens=10,
        )

        assert not usage.cost_is_priced
        assert usage.cost_usd == 0.0


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
