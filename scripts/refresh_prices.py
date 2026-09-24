#!/usr/bin/env python3
"""Fetch, validate, and vendor ccstory's LiteLLM price snapshot.

The packaged snapshot is the sole upstream price source. Missing cache rates
stay missing: callers can disclose that a model is unpriced for the usage
categories that actually occurred instead of estimating a rate.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import urllib.request
from collections.abc import Mapping
from datetime import date
from pathlib import Path

LITELLM_PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "ccstory" / "model_prices.json"

_RATE_FIELDS = (
    ("input_cost_per_token", "inp"),
    ("output_cost_per_token", "out"),
    ("cache_creation_input_token_cost", "cw"),
    ("cache_read_input_token_cost", "cr"),
)
_CONTEXT_RATE_RE = re.compile(
    r"^(input_cost_per_token|output_cost_per_token|"
    r"cache_creation_input_token_cost|cache_read_input_token_cost)_above_(\d+)(k)?_tokens$"
)
_SERVICE_TIER_SUFFIX_RE = re.compile(r"_(batches|batch|priority|flex)$")
_CACHE_CREATION_AGE_RATE = "cache_creation_input_token_cost_above_1hr"
_CONTEXT_COMPONENTS = {
    "input_cost_per_token": "inp",
    "output_cost_per_token": "out",
    "cache_creation_input_token_cost": "cw",
    "cache_read_input_token_cost": "cr",
}


def _service_tier_rate_base(source_key: str) -> str | None:
    """Return the token-rate key for a recognized service-tier field."""
    suffix = _SERVICE_TIER_SUFFIX_RE.search(source_key)
    if suffix is None:
        return None
    base_key = source_key[:suffix.start()]
    if (
        _CONTEXT_RATE_RE.fullmatch(base_key)
        or base_key == _CACHE_CREATION_AGE_RATE
        or base_key in {source for source, _ in _RATE_FIELDS}
    ):
        return base_key
    return None


def is_allowed_model(model_id: str) -> bool:
    """Accept the model namespaces used by ccstory and first-party xAI IDs.

    LiteLLM stores xAI models under ``xai/``. Other provider namespaces (for
    example ``azure_ai/grok-*``) are deliberately excluded. A Grok client that
    uses another backend must retain that backend's actual model identity.
    """
    if not isinstance(model_id, str):
        return False
    model_key = model_id.lower().strip()
    if any(ch in model_key for ch in ("@", ":")):
        return False
    if "/" in model_key:
        return model_key.startswith("xai/grok-") and model_key.count("/") == 1
    return model_key.startswith(("claude-", "gpt-5", "gpt-6", "gemini-"))


MODEL_ALLOWLIST_RULE = is_allowed_model


def parse_and_validate_rates(
    model_id: str,
    raw_info: Mapping[str, object],
) -> dict[str, object] | None:
    """Convert explicit LiteLLM USD/token fields to USD per million tokens.

    A ``None`` source field is absent, not free. Explicit zero remains a valid
    rate. Return a partial row when LiteLLM exposes only some components; the
    shared usage calculator marks a model unpriced if observed tokens need a
    missing component.
    """
    if not isinstance(raw_info, Mapping):
        raise ValueError(f"Malformed pricing record for model '{model_id}'")

    rates: dict[str, object] = {}

    def validated_rate(source_key: str, raw_rate: object) -> float:
        if isinstance(raw_rate, bool) or not isinstance(raw_rate, (int, float)):
            raise ValueError(
                f"Invalid {source_key} for model '{model_id}': expected a number or null"
            )
        rate = float(raw_rate) * 1_000_000
        if not math.isfinite(rate) or rate < 0 or rate > 1000.0:
            raise ValueError(
                f"Implausible price rate {rate} for model '{model_id}' ({source_key}); "
                "must be finite and between 0 and 1000 USD/M"
            )
        return round(rate, 4)

    for source_key, rate_key in _RATE_FIELDS:
        raw_rate = raw_info.get(source_key)
        if raw_rate is None:
            continue
        rates[rate_key] = validated_rate(source_key, raw_rate)

    context_tiers: dict[str, dict[str, float]] = {}
    service_tiers: dict[str, dict[str, object]] = {}
    for source_key, raw_rate in raw_info.items():
        if not isinstance(source_key, str) or raw_rate is None:
            continue
        service_tier: str | None = None
        base_source_key = source_key
        suffix = _SERVICE_TIER_SUFFIX_RE.search(source_key)
        if suffix:
            service_tier = "batches" if suffix.group(1) == "batch" else suffix.group(1)
            base_source_key = source_key[:suffix.start()]

        match = _CONTEXT_RATE_RE.fullmatch(base_source_key)
        if match is None:
            component = next(
                (internal for source, internal in _RATE_FIELDS
                 if source == base_source_key),
                None,
            )
            if base_source_key == _CACHE_CREATION_AGE_RATE:
                if service_tier is None:
                    continue
                service_tiers.setdefault(service_tier, {})[
                    "cache_creation_above_1hr"
                ] = validated_rate(source_key, raw_rate)
            elif component is not None and service_tier is not None:
                service_tiers.setdefault(service_tier, {}).setdefault(
                    "rates", {},
                )[component] = validated_rate(source_key, raw_rate)
            continue

        component = _CONTEXT_COMPONENTS[match.group(1)]
        threshold = int(match.group(2)) * (1000 if match.group(3) else 1)
        if service_tier is None:
            context_tiers.setdefault(str(threshold), {})[component] = validated_rate(
                source_key, raw_rate,
            )
        else:
            tier_contexts = service_tiers.setdefault(service_tier, {}).setdefault(
                "context_tiers", {},
            )
            tier_contexts.setdefault(str(threshold), {})[component] = validated_rate(
                source_key, raw_rate,
            )
    if context_tiers:
        rates["_context_tiers"] = {
            threshold: dict(sorted(tier.items()))
            for threshold, tier in sorted(context_tiers.items(), key=lambda item: int(item[0]))
        }

    if service_tiers:
        normalized_service_tiers: dict[str, dict[str, object]] = {}
        for service_tier, service_info in sorted(service_tiers.items()):
            normalized_info: dict[str, object] = {}
            for key, value in sorted(service_info.items()):
                if key == "context_tiers" and isinstance(value, Mapping):
                    normalized_info[key] = {
                        threshold: dict(sorted(components.items()))
                        for threshold, components in sorted(
                            value.items(), key=lambda item: int(item[0]),
                        )
                    }
                elif key == "rates" and isinstance(value, Mapping):
                    normalized_info[key] = dict(sorted(value.items()))
                else:
                    normalized_info[key] = value
            normalized_service_tiers[service_tier] = normalized_info
        rates["_service_tiers"] = normalized_service_tiers

    raw_long_cache_write = raw_info.get(_CACHE_CREATION_AGE_RATE)
    if raw_long_cache_write is not None:
        rates["_cache_creation_above_1hr"] = validated_rate(
            _CACHE_CREATION_AGE_RATE, raw_long_cache_write,
        )

    # Preserve other LiteLLM billing dimensions. Context-size, service-class,
    # and Anthropic 1h cache-write rates are retained when the local provider
    # record carries the corresponding request facts.
    supported_source_fields = {source for source, _ in _RATE_FIELDS}
    supported_source_fields.update(
        key
        for key, value in raw_info.items()
        if isinstance(key, str)
        and value is not None
        and (
            _CONTEXT_RATE_RE.fullmatch(key)
            or key == _CACHE_CREATION_AGE_RATE
            or _service_tier_rate_base(key) is not None
        )
    )
    unsupported_dimensions = sorted(
        key
        for key, value in raw_info.items()
        if isinstance(key, str)
        and "cost" in key.lower()
        and (
            (key not in supported_source_fields and value is not None)
            or (value is None and _service_tier_rate_base(key) is not None)
        )
    )
    if rates and unsupported_dimensions:
        rates["_unsupported_dimensions"] = unsupported_dimensions

    return rates or None


def filter_prices(raw_data: object) -> dict[str, dict[str, object]]:
    """Validate a complete registry object and return its selected rows."""
    if not isinstance(raw_data, dict):
        raise ValueError("LiteLLM pricing registry must be a JSON object")

    filtered: dict[str, dict[str, object]] = {}
    for model_id, info in raw_data.items():
        if not isinstance(model_id, str):
            continue
        normalized = model_id.lower().strip()
        if not is_allowed_model(normalized):
            continue
        rates = parse_and_validate_rates(normalized, info)
        if rates:
            filtered[normalized] = rates

    if not filtered:
        raise ValueError("LiteLLM registry contained no valid ccstory model prices")
    return dict(sorted(filtered.items()))


def _rate_text(rates: Mapping[str, object], key: str) -> str:
    value = rates.get(key)
    return f"${value}/M" if value is not None else "unpriced"


def format_diff_summary(
    old_prices: dict[str, dict[str, object]],
    new_prices: dict[str, dict[str, object]],
) -> str:
    old_keys = set(old_prices)
    new_keys = set(new_prices)
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    changed = [
        (key, old_prices[key], new_prices[key])
        for key in sorted(old_keys & new_keys)
        if old_prices[key] != new_prices[key]
    ]

    if not added and not removed and not changed:
        return "No pricing changes detected."

    lines = [
        f"Pricing diff summary ({len(added)} added, {len(removed)} removed, {len(changed)} changed):"
    ]
    if added:
        lines.append("\nAdded models:")
        for key in added:
            rates = new_prices[key]
            details = ", ".join(
                f"{rate}={_rate_text(rates, rate)}" for _, rate in _RATE_FIELDS
            )
            dimensions = rates.get("_unsupported_dimensions", [])
            if isinstance(dimensions, list) and dimensions:
                details += "; unsupported dimensions=" + ", ".join(dimensions)
            lines.append(f"  + {key}: {details}")
    if removed:
        lines.append("\nRemoved models:")
        lines.extend(f"  - {key}" for key in removed)
    if changed:
        lines.append("\nChanged models:")
        for key, old_rates, new_rates in changed:
            lines.append(f"  ~ {key}:")
            for _, rate_key in _RATE_FIELDS:
                if old_rates.get(rate_key) != new_rates.get(rate_key):
                    lines.append(
                        f"      {rate_key}: {_rate_text(old_rates, rate_key)} -> "
                        f"{_rate_text(new_rates, rate_key)}"
                    )
            if old_rates.get("_unsupported_dimensions") != new_rates.get(
                "_unsupported_dimensions"
            ):
                lines.append(
                    "      unsupported dimensions: "
                    f"{old_rates.get('_unsupported_dimensions', [])} -> "
                    f"{new_rates.get('_unsupported_dimensions', [])}"
                )
            for metadata_key in (
                "_context_tiers",
                "_cache_creation_above_1hr",
                "_service_tiers",
            ):
                if old_rates.get(metadata_key) != new_rates.get(metadata_key):
                    lines.append(
                        f"      {metadata_key}: "
                        f"{old_rates.get(metadata_key)} -> "
                        f"{new_rates.get(metadata_key)}"
                    )
    return "\n".join(lines)


def _read_existing_prices() -> dict[str, dict[str, object]]:
    if not OUTPUT_FILE.exists():
        return {}
    try:
        existing = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        prices = existing.get("prices") if isinstance(existing, dict) else None
        if not isinstance(prices, dict):
            raise ValueError("snapshot has no prices object")
        return prices
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"Warning: could not read existing file {OUTPUT_FILE}: {exc}", file=sys.stderr)
        return {}


def _atomic_write_snapshot(output_data: dict[str, object]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=OUTPUT_FILE.parent,
            prefix=f".{OUTPUT_FILE.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_path = stream.name
            json.dump(output_data, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, OUTPUT_FILE)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def refresh_prices() -> None:
    print(f"Fetching LiteLLM pricing registry from {LITELLM_PRICES_URL}...")
    request = urllib.request.Request(
        LITELLM_PRICES_URL,
        headers={"User-Agent": "ccstory-pricing-refresh/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20.0) as response:
        raw_data = json.loads(response.read().decode("utf-8"))

    # Complete network/shape/rate validation before touching the usable file.
    filtered_prices = filter_prices(raw_data)
    old_prices = _read_existing_prices()
    print(format_diff_summary(old_prices, filtered_prices))

    output_data: dict[str, object] = {
        "generated_at": date.today().isoformat(),
        "source_url": LITELLM_PRICES_URL,
        "entry_count": len(filtered_prices),
        "prices": filtered_prices,
    }
    _atomic_write_snapshot(output_data)
    print(f"\nWrote {len(filtered_prices)} model prices to {OUTPUT_FILE}")


if __name__ == "__main__":
    try:
        refresh_prices()
    except Exception as err:
        print(f"Error refreshing prices: {err}", file=sys.stderr)
        sys.exit(1)
