#!/usr/bin/env python3
"""Maintainer script to fetch and vendor LiteLLM pricing data for ccstory.

Filter upstream registry (~2,970 entries) to only bare Anthropic Claude model IDs
that ccstory actually encounters (~20 entries). Ship as packaged JSON in releases.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

LITELLM_PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "ccstory" / "model_prices.json"


# Model ID allowlist rule for ccstory pricing table filtering.
# Matches bare first-party Anthropic Claude IDs (`claude-`) and OpenAI GPT-5 family IDs (`gpt-5`).
# Provider prefixes (e.g. `azure/`) and variant delimiters (`@`, `:`) are excluded.
# Note: `.` is allowed to support subversion identifiers like `gpt-5.6-terra`.
#
# WHY `gpt-5` AND NOT `gpt-`:
# Restricting to `gpt-5` specifically targets active OpenAI models used by Codex
# (e.g., gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna) while preventing bloat from 110+ legacy
# models (e.g., gpt-3.5-turbo, gpt-4) present in upstream LiteLLM registries.
def is_allowed_model(model_id: str) -> bool:
    lk = model_id.lower().strip()
    is_valid_prefix = lk.startswith(("claude-", "gpt-5"))
    has_excluded_delim = any(ch in lk for ch in ("/", "@", ":"))
    return is_valid_prefix and not has_excluded_delim


MODEL_ALLOWLIST_RULE = is_allowed_model


def parse_and_validate_rates(model_id: str, raw_info: dict) -> dict[str, float] | None:
    if not isinstance(raw_info, dict):
        return None
    inp = raw_info.get("input_cost_per_token")
    out = raw_info.get("output_cost_per_token")
    if inp is None or out is None:
        return None

    try:
        inp_f = float(inp) * 1_000_000
        out_f = float(out) * 1_000_000
        cw = raw_info.get("cache_creation_input_token_cost")
        cr = raw_info.get("cache_read_input_token_cost")
        cw_f = float(cw) * 1_000_000 if cw is not None else inp_f * 1.25
        cr_f = float(cr) * 1_000_000 if cr is not None else inp_f * 0.1
    except (TypeError, ValueError) as e:
        raise ValueError(f"Failed to parse rates for model '{model_id}': {e}") from e

    rates = {
        "inp": round(inp_f, 4),
        "out": round(out_f, 4),
        "cw": round(cw_f, 4),
        "cr": round(cr_f, 4),
    }

    for rate_name, val in rates.items():
        if val < 0 or val > 1000.0:
            raise ValueError(
                f"Implausible price rate {val} for model '{model_id}' ({rate_name}); must be between 0 and 1000 USD/M"
            )

    return rates


def format_diff_summary(old_prices: dict[str, dict[str, float]], new_prices: dict[str, dict[str, float]]) -> str:
    old_keys = set(old_prices.keys())
    new_keys = set(new_prices.keys())

    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    changed = []

    for k in sorted(old_keys & new_keys):
        if old_prices[k] != new_prices[k]:
            changed.append((k, old_prices[k], new_prices[k]))

    lines = []
    if not added and not removed and not changed:
        lines.append("No pricing changes detected.")
        return "\n".join(lines)

    lines.append(f"Pricing diff summary ({len(added)} added, {len(removed)} removed, {len(changed)} changed):")
    if added:
        lines.append("\nAdded models:")
        for k in added:
            rates = new_prices[k]
            lines.append(f"  + {k}: inp=${rates['inp']}/M, out=${rates['out']}/M, cw=${rates['cw']}/M, cr=${rates['cr']}/M")
    if removed:
        lines.append("\nRemoved models:")
        for k in removed:
            lines.append(f"  - {k}")
    if changed:
        lines.append("\nChanged models:")
        for k, old_r, new_r in changed:
            lines.append(f"  ~ {k}:")
            for r_key in ("inp", "out", "cw", "cr"):
                if old_r[r_key] != new_r[r_key]:
                    lines.append(f"      {r_key}: ${old_r[r_key]} -> ${new_r[r_key]}")

    return "\n".join(lines)


def refresh_prices() -> None:
    print(f"Fetching LiteLLM pricing registry from {LITELLM_PRICES_URL}...")
    req = urllib.request.Request(
        LITELLM_PRICES_URL,
        headers={"User-Agent": "ccstory-pricing-refresh/1.0"},
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        raw_data = json.loads(resp.read().decode("utf-8"))

    filtered_prices: dict[str, dict[str, float]] = {}
    for model_id, info in raw_data.items():
        if not isinstance(model_id, str):
            continue
        lk = model_id.lower().strip()
        if MODEL_ALLOWLIST_RULE(lk):
            rates = parse_and_validate_rates(lk, info)
            if rates:
                filtered_prices[lk] = rates

    old_prices: dict[str, dict[str, float]] = {}
    if OUTPUT_FILE.exists():
        try:
            with OUTPUT_FILE.open("r", encoding="utf-8") as f:
                old_content = json.load(f)
                old_prices = old_content.get("prices", {})
        except Exception as e:
            print(f"Warning: could not read existing file {OUTPUT_FILE}: {e}", file=sys.stderr)

    diff_summary = format_diff_summary(old_prices, filtered_prices)
    print(diff_summary)

    output_data = {
        "generated_at": date.today().isoformat(),
        "source_url": LITELLM_PRICES_URL,
        "entry_count": len(filtered_prices),
        "prices": filtered_prices,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)
        f.write("\n")

    print(f"\nWrote {len(filtered_prices)} model prices to {OUTPUT_FILE}")


if __name__ == "__main__":
    try:
        refresh_prices()
    except Exception as err:
        print(f"Error refreshing prices: {err}", file=sys.stderr)
        sys.exit(1)
