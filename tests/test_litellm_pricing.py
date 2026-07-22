"""Tests for LiteLLM dynamic pricing sync & caching in token_usage."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ccstory import token_usage
from ccstory.token_usage import (
    DEFAULT_PRICES,
    PRICES_SNAPSHOT_DATE,
    _parse_litellm_json,
    _price_for,
    apply_prices,
    load_prices_config,
    sync_litellm_prices,
)


@pytest.fixture(autouse=True)
def _reset_active_prices(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        token_usage,
        "_active_prices",
        {k: dict(v) for k, v in DEFAULT_PRICES.items()},
    )
    monkeypatch.setattr(token_usage, "_active_snapshot_date", PRICES_SNAPSHOT_DATE)


class TestLiteLLMJsonParsing:
    def test_parse_valid_litellm_data(self):
        raw = {
            "claude-3-5-sonnet-20241022": {
                "input_cost_per_token": 0.000003,
                "output_cost_per_token": 0.000015,
                "cache_creation_input_token_cost": 0.00000375,
                "cache_read_input_token_cost": 0.0000003,
            },
            "gpt-4o": {
                "input_cost_per_token": 0.0000025,
                "output_cost_per_token": 0.00001,
            },
        }
        parsed = _parse_litellm_json(raw)

        assert "claude-3-5-sonnet-20241022" in parsed
        assert parsed["claude-3-5-sonnet-20241022"]["inp"] == pytest.approx(3.0)
        assert parsed["claude-3-5-sonnet-20241022"]["out"] == pytest.approx(15.0)
        assert parsed["claude-3-5-sonnet-20241022"]["cw"] == pytest.approx(3.75)
        assert parsed["claude-3-5-sonnet-20241022"]["cr"] == pytest.approx(0.3)

        # gpt-4o missing cache costs will fall back to defaults
        assert "gpt-4o" in parsed
        assert parsed["gpt-4o"]["inp"] == pytest.approx(2.5)
        assert parsed["gpt-4o"]["out"] == pytest.approx(10.0)

    def test_parse_invalid_or_empty_data(self):
        assert _parse_litellm_json({}) == {}
        assert _parse_litellm_json("not a dict") == {}
        assert _parse_litellm_json({"bad_model": "not a dict"}) == {}


class TestSyncLiteLLMPrices:
    def test_sync_fetches_remote_and_caches(self, tmp_path: Path):
        cache_file = tmp_path / "cache" / "model_prices.json"
        raw_json = json.dumps({
            "claude-3-7-sonnet-20250219": {
                "input_cost_per_token": 0.000003,
                "output_cost_per_token": 0.000015,
            }
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = raw_json
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            res = sync_litellm_prices(cache_file=cache_file, timeout=1.0)
            assert res is not None
            prices, snap = res
            assert "claude-3-7-sonnet-20250219" in prices
            assert snap.startswith("litellm-")
            assert cache_file.exists()

    def test_sync_uses_fresh_cache(self, tmp_path: Path):
        cache_file = tmp_path / "cache" / "model_prices.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_content = {
            "snapshot_date": "litellm-2026-07",
            "prices": {
                "cached-model": {
                    "input_cost_per_token": 0.000001,
                    "output_cost_per_token": 0.000002,
                }
            },
        }
        cache_file.write_text(json.dumps(cache_content), encoding="utf-8")

        with patch("urllib.request.urlopen") as mock_url:
            res = sync_litellm_prices(cache_file=cache_file, max_age_days=7)
            assert res is not None
            prices, snap = res
            assert "cached-model" in prices
            assert snap == "litellm-2026-07"
            # Should NOT call network when cache is fresh
            mock_url.assert_not_called()

    def test_sync_fallback_to_stale_cache_on_network_error(self, tmp_path: Path):
        cache_file = tmp_path / "cache" / "model_prices.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_content = {
            "snapshot_date": "litellm-old",
            "prices": {
                "stale-model": {
                    "input_cost_per_token": 0.000005,
                    "output_cost_per_token": 0.000010,
                }
            },
        }
        cache_file.write_text(json.dumps(cache_content), encoding="utf-8")

        # Set mtime to 10 days ago
        old_time = cache_file.stat().st_mtime - (10 * 86400)
        import os
        os.utime(cache_file, (old_time, old_time))

        with patch("urllib.request.urlopen", side_effect=Exception("Network error")):
            res = sync_litellm_prices(cache_file=cache_file, max_age_days=7)
            assert res is not None
            prices, snap = res
            assert "stale-model" in prices
            assert snap == "litellm-old"


class TestPrecedence:
    def test_config_override_takes_precedence_over_litellm(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[prices.custom-model]\ninput = 99.0\n",
            encoding="utf-8",
        )

        litellm_mock = (
            {
                "custom-model": {"inp": 1.0, "out": 2.0, "cw": 0.5, "cr": 0.1},
                "litellm-only": {"inp": 5.0, "out": 10.0, "cw": 2.0, "cr": 0.5},
            },
            "litellm-2026-07",
        )

        with patch("ccstory.token_usage.sync_litellm_prices", return_value=litellm_mock):
            prices, snap = load_prices_config(cfg, sync_remote=True)
            assert prices["custom-model"]["inp"] == 99.0
            assert prices["litellm-only"]["inp"] == 5.0
            assert snap == "litellm-2026-07"
