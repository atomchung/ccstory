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

    def test_non_base_litellm_cost_dimensions_are_preserved_for_fail_closed_costs(self):
        rates = parse_and_validate_rates(
            "xai/grok-4.20",
            {
                "input_cost_per_token": 0.000002,
                "output_cost_per_token": 0.000006,
                "input_cost_per_token_above_200k_tokens": 0.0000025,
                "cache_read_input_token_cost_above_200k_tokens": 0.0000004,
            },
        )
        assert rates == {
            "inp": 2.0,
            "out": 6.0,
            "_unsupported_dimensions": [
                "cache_read_input_token_cost_above_200k_tokens",
                "input_cost_per_token_above_200k_tokens",
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
                    b'{"xai/grok-4.6":{"input_cost_per_token":0.000002,"output_cost_per_token":0.000006,"cache_read_input_token_cost":0.0000005},"gpt-6-luna":{"input_cost_per_token":0.000001,"output_cost_per_token":0.000008}}'
                )

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(refresh_prices.urllib.request, "urlopen", lambda *a, **k: Response())
        refresh_prices.refresh_prices()

        snapshot = json.loads(output.read_text(encoding="utf-8"))
        assert snapshot["entry_count"] == 2
        assert snapshot["prices"]["xai/grok-4.6"] == {"inp": 2.0, "out": 6.0, "cr": 0.5}
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
