"""Tests for scripts/refresh_prices.py maintenance script."""

from __future__ import annotations

import pytest

from scripts.refresh_prices import is_allowed_model, parse_and_validate_rates


class TestIsAllowedModel:
    @pytest.mark.parametrize(
        "model_id",
        [
            "claude-sonnet-5",
            "gpt-5.6-terra",
            "gpt-5.6-sol",
            "gpt-5.6-luna",
            "gpt-5",
            "gpt-5.1-codex",
        ],
    )
    def test_allows_valid_claude_and_gpt5_models(self, model_id: str):
        assert is_allowed_model(model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        [
            "azure/gpt-5.6-terra",
            "gpt-4o",
            "gpt-3.5-turbo",
            "claude-3-opus@default",
            "gpt-5.6-terra:latest",
            "bedrock:claude-3",
        ],
    )
    def test_rejects_disallowed_models_and_variants(self, model_id: str):
        assert is_allowed_model(model_id) is False


class TestParseAndValidateRates:
    def test_parses_explicit_cache_rates_for_gpt5_shape_raw_info(self):
        raw_info = {
            "input_cost_per_token": 0.000010,  # $10.0 / M
            "output_cost_per_token": 0.000030,  # $30.0 / M
            "cache_creation_input_token_cost": 0.000014,  # $14.0 / M (distinct from fallback 12.5)
            "cache_read_input_token_cost": 0.000002,  # $2.0 / M (distinct from fallback 1.0)
        }
        rates = parse_and_validate_rates("gpt-5.6-terra", raw_info)
        assert rates is not None
        assert rates["inp"] == 10.0
        assert rates["out"] == 30.0
        assert rates["cw"] == 14.0  # verifies reading actual field, not inp * 1.25
        assert rates["cr"] == 2.0  # verifies reading actual field, not inp * 0.1
