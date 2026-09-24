"""Tests for scripts/refresh_prices.py maintenance script."""

from __future__ import annotations

import pytest
import json

from scripts import refresh_prices
from scripts.refresh_prices import (
    filter_prices,
    is_allowed_model,
    parse_and_validate_rates,
)


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
            "gemini-3.6-flash",
            "gemini-3-flash-preview",
            "gpt-6-luna",
            "xai/grok-4.6",
            "xai/grok-code-fast-1",
        ],
    )
    def test_allows_valid_claude_gpt5_and_gemini_models(self, model_id: str):
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
            "vertex_ai/gemini-3.6-flash",
            "gemini-3.6-flash@1",
            "gemini-3.6-flash:latest",
            "gpt-4.1",
            "grok-4.6",
            "azure_ai/grok-4.6",
            "xai/grok-4.6:latest",
            "xai//grok-4.6",
        ],
    )
    def test_rejects_disallowed_models_and_variants(self, model_id: str):
        assert is_allowed_model(model_id) is False


class TestParseAndValidateRates:
    def test_parses_explicit_rates_without_inventing_missing_cache_rates(self):
        raw_info = {
            "input_cost_per_token": 0.000010,  # $10.0 / M
            "output_cost_per_token": 0.000030,  # $30.0 / M
            "cache_creation_input_token_cost": None,
            "cache_read_input_token_cost": 0.000002,  # $2.0 / M
        }
        rates = parse_and_validate_rates("gpt-5.6-terra", raw_info)
        assert rates is not None
        assert rates["inp"] == 10.0
        assert rates["out"] == 30.0
        assert "cw" not in rates
        assert rates["cr"] == 2.0

    def test_explicit_zero_is_retained_but_missing_required_rates_are_not(self):
        rates = parse_and_validate_rates(
            "xai/grok-4.6",
            {"input_cost_per_token": 0, "output_cost_per_token": 0.000006},
        )
        assert rates == {"inp": 0.0, "out": 6.0}

    def test_exact_litellm_context_tiers_and_cache_age_rate_are_retained(self):
        rates = parse_and_validate_rates(
            "xai/grok-4.6",
            {
                "input_cost_per_token": 0.000002,
                "output_cost_per_token": 0.000006,
                "cache_read_input_token_cost": 0.0000005,
                "input_cost_per_token_above_200k_tokens": 0.0000025,
                "cache_read_input_token_cost_above_200k_tokens": 0.0000004,
                "cache_creation_input_token_cost": 0.000005,
                "cache_creation_input_token_cost_above_1hr": 0.000008,
                "input_cost_per_image_token": 0.000002,
            },
        )
        assert rates == {
            "inp": 2.0,
            "out": 6.0,
            "cr": 0.5,
            "cw": 5.0,
            "_context_tiers": {"200000": {"cr": 0.4, "inp": 2.5}},
            "_cache_creation_above_1hr": 8.0,
            "_unsupported_dimensions": ["input_cost_per_image_token"],
        }

    def test_context_and_service_tier_rates_are_retained(self):
        rates = parse_and_validate_rates(
            "gpt-6-luna",
            {
                "input_cost_per_token": 0.0000001,
                "input_cost_per_token_above_272k_tokens": 0.0000002,
                "input_cost_per_token_above_272k_tokens_priority": 0.0000004,
                "input_cost_per_token_priority": 0.0000002,
                "output_cost_per_token": 0.0000005,
                "output_cost_per_token_priority": 0.000001,
                "cache_creation_input_token_cost_flex": 0.0000000625,
                "cache_read_input_token_cost": 0.00000001,
                "cache_read_input_token_cost_priority": None,
                "input_cost_per_image_token": 0.0000003,
            },
        )
        assert rates == {
            "inp": 0.1,
            "out": 0.5,
            "cr": 0.01,
            "_context_tiers": {"272000": {"inp": 0.2}},
            "_service_tiers": {
                "flex": {"rates": {"cw": 0.0625}},
                "priority": {
                    "context_tiers": {"272000": {"inp": 0.4}},
                    "rates": {"inp": 0.2, "out": 1.0},
                },
            },
            "_unsupported_dimensions": [
                "cache_read_input_token_cost_priority",
                "input_cost_per_image_token",
            ],
        }

    @pytest.mark.parametrize(
        "invalid_rate",
        [-0.000001, float("nan"), float("inf"), 0.001001, "0.000001", True],
    )
    def test_rejects_invalid_or_implausible_explicit_rates(self, invalid_rate):
        with pytest.raises(ValueError):
            parse_and_validate_rates(
                "xai/grok-4.6",
                {"input_cost_per_token": invalid_rate},
            )

    def test_filter_uses_xai_namespace_only_and_preserves_partial_rows(self):
        prices = filter_prices(
            {
                "xai/grok-4.6": {
                    "input_cost_per_token": 0.000002,
                    "output_cost_per_token": 0.000006,
                    "cache_creation_input_token_cost": None,
                    "cache_read_input_token_cost": 0.0000005,
                },
                "azure_ai/grok-4.6": {
                    "input_cost_per_token": 0.000003,
                    "output_cost_per_token": 0.000015,
                },
                "gpt-6-luna": {
                    "input_cost_per_token": 0.000001,
                    "output_cost_per_token": 0.000008,
                },
            }
        )
        assert prices == {
            "gpt-6-luna": {"inp": 1.0, "out": 8.0},
            "xai/grok-4.6": {"inp": 2.0, "out": 6.0, "cr": 0.5},
        }

    def test_diff_reports_unsupported_pricing_dimensions(self):
        summary = refresh_prices.format_diff_summary(
            {},
            {
                "xai/grok-4.20": {
                    "inp": 2.0,
                    "out": 6.0,
                    "_unsupported_dimensions": [
                        "input_cost_per_token_above_200k_tokens"
                    ],
                }
            },
        )
        assert "unsupported dimensions=input_cost_per_token_above_200k_tokens" in summary

    def test_rejects_invalid_registry_shape_and_empty_filtered_registry(self):
        with pytest.raises(ValueError, match="JSON object"):
            filter_prices([])
        with pytest.raises(ValueError, match="no valid ccstory"):
            filter_prices({"azure_ai/grok-4.6": {}})


class TestRefreshPrices:
    def test_refresh_atomically_replaces_snapshot_after_validation(self, monkeypatch, tmp_path, capsys):
        output = tmp_path / "model_prices.json"
        output.write_text(
            '{"generated_at":"old","source_url":"old","entry_count":2,"prices":{"gpt-5":{"inp":1,"out":2,"cw":1.25,"cr":0.1},"xai/grok-4.6":{"inp":2.0,"out":6.0,"cw":2.5,"cr":0.5}}}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(refresh_prices, "OUTPUT_FILE", output)

        class Response:
            def __enter__(self):
                import io
                return io.BytesIO(
                    b'{"xai/grok-4.6":{"input_cost_per_token":0.000002,"output_cost_per_token":0.000006,"cache_read_input_token_cost":0.0000005,"input_cost_per_token_above_200k_tokens":0.000004},"gpt-6-luna":{"input_cost_per_token":0.000001,"output_cost_per_token":0.000008}}'
                )

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(refresh_prices.urllib.request, "urlopen", lambda *a, **k: Response())
        refresh_prices.refresh_prices()

        snapshot = json.loads(output.read_text(encoding="utf-8"))
        assert snapshot["entry_count"] == 2
        assert snapshot["prices"]["xai/grok-4.6"] == {
            "inp": 2.0,
            "out": 6.0,
            "cr": 0.5,
            "_context_tiers": {"200000": {"inp": 4.0}},
        }
        assert "cw: $2.5/M -> unpriced" in capsys.readouterr().out

    def test_failed_refresh_preserves_existing_snapshot_bytes(self, monkeypatch, tmp_path):
        output = tmp_path / "model_prices.json"
        original = b'{"generated_at":"known-good","prices":{"grok":{"inp":1}}}\n'
        output.write_bytes(original)
        monkeypatch.setattr(refresh_prices, "OUTPUT_FILE", output)

        def fail(*args, **kwargs):
            raise OSError("offline")

        monkeypatch.setattr(refresh_prices.urllib.request, "urlopen", fail)
        with pytest.raises(OSError, match="offline"):
            refresh_prices.refresh_prices()
        assert output.read_bytes() == original

    def test_malformed_refresh_preserves_existing_snapshot_bytes(self, monkeypatch, tmp_path):
        output = tmp_path / "model_prices.json"
        original = b'{"generated_at":"known-good","prices":{"xai/grok-4.6":{"inp":1}}}\n'
        output.write_bytes(original)
        monkeypatch.setattr(refresh_prices, "OUTPUT_FILE", output)

        class Response:
            def __enter__(self):
                import io
                return io.BytesIO(
                    b'{"xai/grok-4.6":{"input_cost_per_token":NaN,"output_cost_per_token":0.000006}}'
                )

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(refresh_prices.urllib.request, "urlopen", lambda *a, **k: Response())
        with pytest.raises(ValueError, match="finite"):
            refresh_prices.refresh_prices()
        assert output.read_bytes() == original
