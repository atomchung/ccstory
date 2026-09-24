"""Aggregate token usage across registered coding-agent providers.

Each assistant message carries a `usage` block with input / cache_creation /
cache_read / output token counts. We sum these per model over a date range,
and produce an API-list-price equivalent cost (Max subscription is flat-fee,
so this is "value at API rates," not actual spend).

Extracted from ting/personal_os/core/token_usage.py for ccstory v1. Removed:
  - subscription.json loading (ccstory doesn't model plan quotas — that's
    ccusage's `blocks` command)
  - plan_burn_ratio / quota_used_ratio (same reason)
"""

from __future__ import annotations

import importlib.resources
import json
import logging
import re
from copy import deepcopy
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

LOG = logging.getLogger("ccstory.token_usage")
PROJECTS_DIR = Path.home() / ".claude" / "projects"


# Anthropic API list prices, USD per 1M tokens.
# inp = fresh input, out = output, cw = cache creation (write), cr = cache read.
PRICES_SNAPSHOT_DATE = "2026-07"
PRICING_SNAPSHOT_STALE_DAYS = 90

DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "fable":  dict(inp=10.00, out=50.00, cw=12.50, cr=1.00),
    "mythos": dict(inp=10.00, out=50.00, cw=12.50, cr=1.00),
    "opus":   dict(inp=5.00,  out=25.00, cw=6.25,  cr=0.50),
    "sonnet": dict(inp=3.00,  out=15.00, cw=3.75,  cr=0.30),
    "haiku":  dict(inp=1.00,  out=5.00,  cw=1.25,  cr=0.10),
}

# Lazy loading cache for vendored model prices table (packaged data)
_VENDORED_PRICES_CACHE: dict[str, dict[str, float]] | None = None
_VENDORED_SNAPSHOT_DATE: str | None = None


def load_vendored_prices() -> tuple[dict[str, dict[str, float]], str]:
    """Load packaged ccstory/model_prices.json lazily on first use.

    Returns `(vendored_prices_dict, snapshot_date)`.
    """
    global _VENDORED_PRICES_CACHE, _VENDORED_SNAPSHOT_DATE
    if _VENDORED_PRICES_CACHE is None:
        try:
            ref = importlib.resources.files("ccstory").joinpath("model_prices.json")
            content = json.loads(ref.read_text(encoding="utf-8"))
            prices = content.get("prices", {})
            snap = content.get("generated_at", PRICES_SNAPSHOT_DATE)
            if isinstance(prices, dict):
                _VENDORED_PRICES_CACHE = prices
            else:
                _VENDORED_PRICES_CACHE = {}
            _VENDORED_SNAPSHOT_DATE = str(snap)
        except Exception as e:
            LOG.warning("failed to load vendored model_prices.json: %s", e)
            _VENDORED_PRICES_CACHE = {}
            _VENDORED_SNAPSHOT_DATE = PRICES_SNAPSHOT_DATE
    return _VENDORED_PRICES_CACHE, _VENDORED_SNAPSHOT_DATE or PRICES_SNAPSHOT_DATE


# Mutable active price table — `apply_prices()` swaps it. Defaults to
# DEFAULT_PRICES + vendored table until the cli loads a user override from config.toml.
# Tests can monkeypatch this attribute directly to isolate behavior.
_active_prices: dict[str, dict[str, float]] = {k: dict(v) for k, v in DEFAULT_PRICES.items()}
_active_provenance: dict[str, str] = {k: "default" for k in DEFAULT_PRICES}
_active_snapshot_date: str = PRICES_SNAPSHOT_DATE
_vendored_initialized: bool = False


def _ensure_vendored_loaded() -> None:
    global _vendored_initialized, _active_snapshot_date
    if not _vendored_initialized:
        vendored_prices, vendored_snap = load_vendored_prices()
        for k, v in vendored_prices.items():
            if k not in _active_prices:
                _active_prices[k] = deepcopy(v)
                _active_provenance[k] = "litellm"
        if _active_snapshot_date == PRICES_SNAPSHOT_DATE and vendored_snap:
            _active_snapshot_date = vendored_snap
        _vendored_initialized = True


MODEL_ALIASES: dict[str, str] = {
    "gemini-3-flash-a": "gemini-3-flash-preview",
    "gemini-3-flash-agent": "gemini-3-flash-preview",
}

_GROK_BUILD_ALIAS_RE = re.compile(r"^grok-(\d+\.\d+)-build$")
_SERVICE_TIER_SUFFIX_RE = re.compile(r"_(batches|batch|priority|flex)$")


def _canonical_service_tier(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"batch", "batches"}:
        return "batches"
    if normalized == "standard":
        return "default"
    return normalized


def _alias_price_target(
    model_key: str,
    price_table: Mapping[str, dict[str, float]],
) -> str | None:
    """Resolve explicit aliases and Grok Build version suffixes safely.

    Grok's local CLI uses IDs such as ``grok-4.6-build`` while LiteLLM
    publishes the same version under its first-party ``xai/grok-4.6`` ID.
    Strip only the exact ``-build`` suffix and only when LiteLLM has that exact
    canonical version; arbitrary fuzzy/substring matches remain unsupported.
    """
    target = MODEL_ALIASES.get(model_key)
    if target:
        return target
    match = _GROK_BUILD_ALIAS_RE.fullmatch(model_key)
    if match is None:
        return None
    candidate = f"xai/grok-{match.group(1)}"
    return candidate if candidate in price_table else None


def _match_price_in_table(
    model_key: str,
    price_table: dict[str, dict[str, float]],
    provenance: dict[str, str],
) -> dict[str, float] | None:
    """Resolve price using precedence ladder:
    1. config.toml [prices] user override for exact model_key (or substring match for user_keys in model_key)
    2. Explicit alias target user override if model_key is an alias (exact, then substring for target)
    3. Active price table exact match (canonical target if alias, or model_key)
    4. DEFAULT_PRICES short-key substring
    """
    mk = model_key.lower().strip()
    if not mk:
        return None

    target_k = _alias_price_target(mk, price_table)

    # Tier 1: User override in config.toml for requested model_key (exact key, then substring)
    user_keys = [k for k, prov in provenance.items() if prov == "user"]
    if mk in price_table and provenance.get(mk) == "user":
        return price_table[mk]
    user_matches_mk = [k for k in user_keys if k in mk]
    if user_matches_mk:
        best_key = max(user_matches_mk, key=len)
        return price_table[best_key]

    # Tier 2: User override for canonical target if model_key is an alias
    if target_k:
        if target_k in price_table and provenance.get(target_k) == "user":
            return price_table[target_k]
        user_matches_target = [k for k in user_keys if k in target_k]
        if user_matches_target:
            best_key = max(user_matches_target, key=len)
            return price_table[best_key]

    # Tier 3: Active price table exact match (canonical target if alias, or model_key)
    if target_k and target_k in price_table:
        return price_table[target_k]
    if mk in price_table:
        return price_table[mk]

    # Tier 4: DEFAULT_PRICES short-key substring
    default_keys = [k for k in DEFAULT_PRICES if k in price_table]
    default_matches = [k for k in default_keys if k in mk or (target_k and k in target_k)]
    if default_matches:
        best_key = max(default_matches, key=len)
        return price_table[best_key]

    return None



def _price_for(model: str) -> dict[str, float] | None:
    _ensure_vendored_loaded()
    return _match_price_in_table(model, _active_prices, _active_provenance)


def get_snapshot_date() -> str:
    """Date the active price table was captured. Used for report disclosure."""
    _ensure_vendored_loaded()
    return _active_snapshot_date


def pricing_snapshot_age_days(
    snapshot_date: str,
    report_until: date | datetime,
) -> int | None:
    """Return snapshot age at a report window's end, or ``None`` if invalid.

    The built-in and documented config format is ``YYYY-MM``. Treat that as
    the first day of the named month so the check has deterministic semantics;
    ``YYYY-MM-DD`` is also accepted for users who maintain a more exact custom
    snapshot. This is deliberately date-only: no live pricing lookup happens.
    """
    raw = snapshot_date.strip() if isinstance(snapshot_date, str) else ""
    if raw.startswith("litellm-"):
        raw = raw[len("litellm-") :]
    if len(raw) == 7:
        raw = f"{raw}-01"
    try:
        captured = date.fromisoformat(raw)
    except ValueError:
        return None

    window_end = (
        report_until.date()
        if isinstance(report_until, datetime)
        else report_until
    )
    return (window_end - captured).days


def pricing_snapshot_warning(
    report_until: date | datetime,
    snapshot_date: str | None = None,
) -> str | None:
    """One-line warning when the effective price snapshot is over 90 days old."""
    effective = snapshot_date if snapshot_date is not None else get_snapshot_date()
    age = pricing_snapshot_age_days(effective, report_until)
    if age is None or age <= PRICING_SNAPSHOT_STALE_DAYS:
        return None
    return (
        f"Pricing snapshot {effective} may be stale ({age} days old); "
        "verify current provider pricing."
    )


# Map user-facing config keys to the internal short keys used by _PRICES.
_CONFIG_KEY_MAP = {
    "input": "inp",
    "output": "out",
    "cache_write": "cw",
    "cache_read": "cr",
}


def load_prices_config(
    config_path: Path,
) -> tuple[dict[str, dict[str, float]], str, dict[str, str]]:
    """Read `[prices]` table from config.toml; merge with vendored table and defaults.

    Returns `(prices_dict, snapshot_date, provenance_dict)`. Returns vendored+defaults if file or
    `[prices]` block is absent or malformed.
    """
    from .categorizer import _load_toml  # categorizer doesn't import from us

    vendored_prices, vendored_snap = load_vendored_prices()
    merged: dict[str, dict[str, float]] = {
        k: deepcopy(v) for k, v in DEFAULT_PRICES.items()
    }
    provenance: dict[str, str] = {k: "default" for k in DEFAULT_PRICES}

    for k, v in vendored_prices.items():
        if k not in merged:
            merged[k] = deepcopy(v)
            provenance[k] = "litellm"

    effective_snapshot = vendored_snap or PRICES_SNAPSHOT_DATE

    cfg = _load_toml(config_path) or {}
    block = cfg.get("prices")
    prices_block = block if isinstance(block, dict) else None

    if prices_block is None:
        return merged, effective_snapshot, provenance

    snapshot = prices_block.get("snapshot_date", effective_snapshot)
    if not isinstance(snapshot, str):
        snapshot = effective_snapshot

    for model_key, override in prices_block.items():
        if model_key == "snapshot_date":
            continue
        if not isinstance(override, dict):
            LOG.warning("ignoring malformed [prices.%s] (must be a table)", model_key)
            continue
        mk = model_key.lower()
        base_price = _match_price_in_table(mk, merged, provenance)
        target = deepcopy(base_price) if base_price else {}
        applied_overrides: set[str] = set()
        for cfg_key, internal_key in _CONFIG_KEY_MAP.items():
            if cfg_key in override:
                try:
                    target[internal_key] = float(override[cfg_key])
                    applied_overrides.add(cfg_key)
                except (TypeError, ValueError):
                    LOG.warning(
                        "ignoring non-numeric [prices.%s].%s", model_key, cfg_key,
                    )
        context_tiers = target.get("_context_tiers")
        if isinstance(context_tiers, dict):
            for cfg_key, internal_key in _CONFIG_KEY_MAP.items():
                if cfg_key not in applied_overrides:
                    continue
                for threshold, tier in list(context_tiers.items()):
                    if isinstance(tier, dict):
                        tier.pop(internal_key, None)
                        if not tier:
                            context_tiers.pop(threshold, None)
            if not context_tiers:
                target.pop("_context_tiers", None)
        service_tiers = target.get("_service_tiers")
        if isinstance(service_tiers, dict):
            for service_tier, service_info in list(service_tiers.items()):
                if not isinstance(service_info, dict):
                    continue
                service_rates = service_info.get("rates")
                if isinstance(service_rates, dict):
                    for cfg_key, internal_key in _CONFIG_KEY_MAP.items():
                        if cfg_key in applied_overrides:
                            service_rates.pop(internal_key, None)
                    if not service_rates:
                        service_info.pop("rates", None)
                service_context = service_info.get("context_tiers")
                if isinstance(service_context, dict):
                    for cfg_key, internal_key in _CONFIG_KEY_MAP.items():
                        if cfg_key not in applied_overrides:
                            continue
                        for threshold, tier in list(service_context.items()):
                            if isinstance(tier, dict):
                                tier.pop(internal_key, None)
                                if not tier:
                                    service_context.pop(threshold, None)
                    if not service_context:
                        service_info.pop("context_tiers", None)
                if "cache_write" in applied_overrides:
                    service_info.pop("cache_creation_above_1hr", None)
                if not service_info:
                    service_tiers.pop(service_tier, None)
            if not service_tiers:
                target.pop("_service_tiers", None)
        if "cache_write" in applied_overrides:
            target.pop("_cache_creation_above_1hr", None)
        dimensions = target.get("_unsupported_dimensions")
        if isinstance(dimensions, list):
            remaining_dimensions = []
            for dimension in dimensions:
                matched_component = next(
                    (
                        internal_key
                        for source_key, internal_key in (
                            ("input_cost_per_token", "inp"),
                            ("output_cost_per_token", "out"),
                            ("cache_creation_input_token_cost", "cw"),
                            ("cache_read_input_token_cost", "cr"),
                        )
                        if isinstance(dimension, str) and dimension.startswith(source_key)
                    ),
                    None,
                )
                config_key = next(
                    (
                        key
                        for key, internal_key in _CONFIG_KEY_MAP.items()
                        if internal_key == matched_component
                    ),
                    None,
                )
                if config_key is None or config_key not in applied_overrides:
                    remaining_dimensions.append(dimension)
            if remaining_dimensions:
                target["_unsupported_dimensions"] = remaining_dimensions
            else:
                target.pop("_unsupported_dimensions", None)
        missing = [k for k in ("inp", "out", "cw", "cr") if k not in target]
        if missing:
            LOG.warning(
                "[prices.%s] missing %s; usage requiring these rates remains unpriced",
                model_key, ", ".join(missing),
            )
        merged[mk] = target
        provenance[mk] = "user"

    return merged, snapshot, provenance


def apply_prices(
    prices: dict[str, dict[str, float]],
    snapshot_date: str | None = None,
    provenance: dict[str, str] | None = None,
) -> None:
    """Replace the active price table. Called by cli on startup."""
    global _active_snapshot_date, _vendored_initialized
    _active_prices.clear()
    _active_prices.update({k: deepcopy(v) for k, v in prices.items()})
    _active_provenance.clear()
    prov_map = provenance if provenance is not None else getattr(prices, "_provenance", None)
    for k in _active_prices:
        if prov_map and k in prov_map:
            _active_provenance[k] = prov_map[k]
        elif k in DEFAULT_PRICES:
            _active_provenance[k] = "default"
        else:
            _active_provenance[k] = "litellm"

    if snapshot_date:
        _active_snapshot_date = snapshot_date
    _vendored_initialized = True


@dataclass
class RequestTokenUsage:
    """Token facts for one provider receipt, which may aggregate many calls.

    ``prompt_is_exact`` is false when the provider receipt combines multiple
    model calls and exposes no per-call context sizes. Such a row can use base
    rates only when its aggregate prompt tokens are below every tier boundary.
    Cache creation is split by TTL when the provider exposes those facts;
    ``cache_creation_unknown`` keeps any unclassified amount explicit.
    """

    input_tokens: int
    cache_creation_5m: int
    cache_creation_1h: int
    cache_creation_unknown: int
    cache_read: int
    output_tokens: int
    prompt_tokens: int
    prompt_is_exact: bool
    service_tier: str | None = None


@dataclass
class ModelUsage:
    model: str
    turns: int = 0
    input_tokens: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    output_tokens: int = 0
    # Maximum exact per-request prompt size when a provider exposes it.
    # ``None`` keeps legacy/aggregate callers on the conservative total-token
    # threshold check.
    max_request_prompt_tokens: int | None = None
    request_token_usage: list[RequestTokenUsage] = field(
        default_factory=list,
        compare=False,
    )

    def __setstate__(self, state: dict[str, object]) -> None:
        """Keep PersonalOS snapshots pickled before request facts readable."""
        self.__dict__.update(state)
        self.__dict__.setdefault("max_request_prompt_tokens", None)
        self.__dict__.setdefault("request_token_usage", [])

    def record_request_usage(
        self,
        *,
        input_tokens: int,
        cache_creation: int,
        cache_read: int,
        output_tokens: int,
        prompt_tokens: int,
        prompt_is_exact: bool = True,
        cache_creation_5m: int | None = None,
        cache_creation_1h: int | None = None,
        service_tier: str | None = None,
    ) -> None:
        """Retain the smallest token facts needed for source-defined price tiers."""
        if cache_creation == 0:
            cache_5m = cache_1h = cache_unknown = 0
        elif (
            cache_creation_5m is not None
            and cache_creation_1h is not None
            and cache_creation_5m >= 0
            and cache_creation_1h >= 0
            and cache_creation_5m + cache_creation_1h == cache_creation
        ):
            cache_5m = cache_creation_5m
            cache_1h = cache_creation_1h
            cache_unknown = 0
        else:
            cache_5m = cache_1h = 0
            cache_unknown = cache_creation
        normalized_service_tier = (
            _canonical_service_tier(service_tier)
            if isinstance(service_tier, str) and service_tier.strip()
            else None
        )
        self.request_token_usage.append(
            RequestTokenUsage(
                input_tokens=input_tokens,
                cache_creation_5m=cache_5m,
                cache_creation_1h=cache_1h,
                cache_creation_unknown=cache_unknown,
                cache_read=cache_read,
                output_tokens=output_tokens,
                prompt_tokens=prompt_tokens,
                prompt_is_exact=prompt_is_exact,
                service_tier=normalized_service_tier,
            )
        )

    def merge_request_usage(self, incoming: "ModelUsage") -> None:
        """Merge request facts, falling back to an explicitly aggregated row."""
        if incoming.request_token_usage:
            self.request_token_usage.extend(incoming.request_token_usage)
            return
        if incoming.total_tokens <= 0:
            return
        prompt_tokens = getattr(incoming, "max_request_prompt_tokens", None)
        self.record_request_usage(
            input_tokens=incoming.input_tokens,
            cache_creation=incoming.cache_creation,
            cache_read=incoming.cache_read,
            output_tokens=incoming.output_tokens,
            prompt_tokens=(
                prompt_tokens
                if prompt_tokens is not None
                else incoming.input_tokens + incoming.cache_creation + incoming.cache_read
            ),
            prompt_is_exact=False,
        )

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation
            + self.cache_read
            + self.output_tokens
        )

    @property
    def missing_price_components(self) -> list[str]:
        """Rates absent for token categories used by this model's usage."""
        required = (
            ("inp", self.input_tokens),
            ("out", self.output_tokens),
            ("cw", self.cache_creation),
            ("cr", self.cache_read),
        )
        rates = _price_for(self.model) or {}
        return [rate for rate, tokens in required if tokens > 0 and rate not in rates]

    def _request_pricing_result(
        self,
        *,
        uncached: bool = False,
    ) -> tuple[float, bool] | None:
        """Return (known cost, fully priced) from retained request facts.

        Costs for requests whose context or cache-write tier is ambiguous are
        omitted from the known subtotal. Their model remains in
        ``unpriced_models`` so the subtotal is never presented as complete.
        """
        if not self.request_token_usage:
            return None
        rates = _price_for(self.model)
        if not rates:
            return 0.0, False
        request_totals = {
            "input_tokens": sum(row.input_tokens for row in self.request_token_usage),
            "cache_creation": sum(
                row.cache_creation_5m
                + row.cache_creation_1h
                + row.cache_creation_unknown
                for row in self.request_token_usage
            ),
            "cache_read": sum(row.cache_read for row in self.request_token_usage),
            "output_tokens": sum(row.output_tokens for row in self.request_token_usage),
        }
        if request_totals != {
            "input_tokens": self.input_tokens,
            "cache_creation": self.cache_creation,
            "cache_read": self.cache_read,
            "output_tokens": self.output_tokens,
        }:
            # A request-granularity gap must never make the known subtotal look
            # more complete than the aggregated token totals.
            return 0.0, False

        raw_tiers = rates.get("_context_tiers", {})
        context_tiers: list[tuple[int, dict[str, float]]] = []
        if isinstance(raw_tiers, Mapping):
            for threshold, tier in raw_tiers.items():
                try:
                    threshold_tokens = int(threshold)
                except (TypeError, ValueError):
                    continue
                if isinstance(tier, Mapping):
                    context_tiers.append((
                        threshold_tokens,
                        {key: float(value) for key, value in tier.items()
                         if key in {"inp", "out", "cw", "cr"}
                         and isinstance(value, (int, float))
                         and not isinstance(value, bool)},
                    ))
        context_tiers.sort(key=lambda item: item[0])

        raw_service_tiers = rates.get("_service_tiers", {})
        service_tiers = (
            raw_service_tiers if isinstance(raw_service_tiers, Mapping) else {}
        )
        service_rate_components: set[str] = set()
        service_context_components: dict[int, set[str]] = {}
        service_cache_age_rate_exists = False
        for service_info in service_tiers.values():
            if not isinstance(service_info, Mapping):
                continue
            service_rates = service_info.get("rates", {})
            if isinstance(service_rates, Mapping):
                service_rate_components.update(
                    key for key in service_rates if key in {"inp", "out", "cw", "cr"}
                )
            service_context = service_info.get("context_tiers", {})
            if isinstance(service_context, Mapping):
                for threshold, tier in service_context.items():
                    try:
                        threshold_tokens = int(threshold)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(tier, Mapping):
                        service_context_components.setdefault(
                            threshold_tokens, set(),
                        ).update(
                            key for key in tier if key in {"inp", "out", "cw", "cr"}
                        )
            if service_info.get("cache_creation_above_1hr") is not None:
                service_cache_age_rate_exists = True

        dimensions = rates.get("_unsupported_dimensions", [])
        if not isinstance(dimensions, list):
            dimensions = []
        known_cost = 0.0
        fully_priced = True
        long_cache_rate = rates.get("_cache_creation_above_1hr")

        for request in self.request_token_usage:
            # Unsuffixed LiteLLM rates are the standard/default class. A
            # service-specific rate is selected only when the provider log
            # records that explicit tier; absence does not mean a free rate.
            service_tier_name = request.service_tier or "default"
            selected_service_info: Mapping[str, object] = {}
            selected_service_tier_available = True
            if service_tier_name not in (None, "default"):
                selected = service_tiers.get(service_tier_name)
                if isinstance(selected, Mapping):
                    selected_service_info = selected
                else:
                    selected_service_tier_available = False

            prompt_tokens = request.prompt_tokens
            applicable_tiers = [
                (threshold, tier)
                for threshold, tier in context_tiers
                if prompt_tokens > threshold
            ]
            service_context = selected_service_info.get("context_tiers", {})
            if isinstance(service_context, Mapping):
                context_by_threshold = {
                    threshold: dict(tier)
                    for threshold, tier in applicable_tiers
                }
                for threshold, tier in service_context.items():
                    try:
                        threshold_tokens = int(threshold)
                    except (TypeError, ValueError):
                        continue
                    if prompt_tokens <= threshold_tokens or not isinstance(tier, Mapping):
                        continue
                    context_by_threshold.setdefault(threshold_tokens, {}).update(tier)
                applicable_tiers = sorted(context_by_threshold.items())

            request_rates = {
                key: rates[key]
                for key in ("inp", "out", "cw", "cr")
                if key in rates
            }
            service_rates = selected_service_info.get("rates", {})
            if isinstance(service_rates, Mapping):
                request_rates.update({
                    key: value
                    for key, value in service_rates.items()
                    if key in {"inp", "out", "cw", "cr"}
                })
            for _threshold, tier in applicable_tiers:
                request_rates.update(tier)

            in_tokens = request.input_tokens
            cache_read_tokens = request.cache_read
            cache_5m_tokens = request.cache_creation_5m
            cache_1h_tokens = request.cache_creation_1h
            cache_unknown_tokens = request.cache_creation_unknown
            if uncached:
                in_tokens += (
                    cache_read_tokens + cache_5m_tokens + cache_1h_tokens
                    + cache_unknown_tokens
                )
                cache_read_tokens = cache_5m_tokens = cache_1h_tokens = 0
                cache_unknown_tokens = 0

            request_components = {
                "inp": in_tokens,
                "out": request.output_tokens,
                "cr": cache_read_tokens,
                "cw": cache_5m_tokens + cache_1h_tokens + cache_unknown_tokens,
            }
            affected_components: set[str] = set()
            service_affected_components = {
                component
                for component in service_rate_components
                if request_components.get(component, 0) > 0
            }
            for threshold, components in service_context_components.items():
                if prompt_tokens > threshold:
                    service_affected_components.update(
                        component
                        for component in components
                        if request_components.get(component, 0) > 0
                    )
            if (
                service_cache_age_rate_exists
                and cache_1h_tokens > 0
            ):
                service_affected_components.add("cw")

            if service_tier_name is None or not selected_service_tier_available:
                affected_components.update(service_affected_components)
            elif service_tier_name != "default":
                selected_service_rates = (
                    service_rates if isinstance(service_rates, Mapping) else {}
                )
                for component in service_affected_components:
                    if (
                        request_components.get(component, 0) > 0
                        and component not in selected_service_rates
                    ):
                        affected_components.add(component)
                selected_service_context = (
                    service_context if isinstance(service_context, Mapping) else {}
                )
                for threshold, components in service_context_components.items():
                    if prompt_tokens <= threshold:
                        continue
                    selected_context_rates = selected_service_context.get(
                        str(threshold), {},
                    )
                    if not isinstance(selected_context_rates, Mapping):
                        selected_context_rates = {}
                    for component in components:
                        if (
                            request_components.get(component, 0) > 0
                            and component not in selected_context_rates
                        ):
                            affected_components.add(component)

            selected_cache_age_rate = selected_service_info.get(
                "cache_creation_above_1hr",
            )
            if selected_cache_age_rate is not None:
                long_cache_rate = selected_cache_age_rate
            elif (
                service_tier_name not in (None, "default")
                and service_cache_age_rate_exists
                and cache_1h_tokens > 0
            ):
                affected_components.add("cw")
            for dimension in dimensions:
                if not isinstance(dimension, str):
                    continue
                source_dimension = dimension
                service_suffix = _SERVICE_TIER_SUFFIX_RE.search(dimension)
                if service_suffix is not None:
                    if _canonical_service_tier(service_suffix.group(1)) != service_tier_name:
                        continue
                    source_dimension = dimension[:service_suffix.start()]
                if "cache_creation_input_token_cost_above_1hr" in source_dimension:
                    if service_suffix is not None and (
                        cache_unknown_tokens > 0 or cache_1h_tokens > 0
                    ):
                        affected_components.add("cw")
                        continue
                    if cache_unknown_tokens > 0 or (
                        cache_1h_tokens > 0 and long_cache_rate is None
                    ):
                        affected_components.add("cw")
                    continue
                threshold = re.search(
                    r"_above_(\d+)(k)?_tokens", source_dimension,
                )
                if threshold is None:
                    component = next(
                        (
                            key for prefix, key in (
                                ("input_cost_per_token", "inp"),
                                ("output_cost_per_token", "out"),
                                ("cache_creation_input_token_cost", "cw"),
                                ("cache_read_input_token_cost", "cr"),
                            )
                            if source_dimension.startswith(prefix)
                        ),
                        None,
                    )
                    if (
                        service_suffix is not None
                        and component is not None
                        and request_components.get(component, 0) > 0
                    ):
                        affected_components.add(component)
                    continue
                limit = int(threshold.group(1)) * (
                    1000 if threshold.group(2) else 1
                )
                if prompt_tokens <= limit:
                    continue
                if source_dimension.startswith((
                    "input_cost_per_token",
                    "cache_creation_input_token_cost",
                )):
                    affected_components.add("inp" if source_dimension.startswith(
                        "input_cost_per_token"
                    ) else "cw")
                elif source_dimension.startswith("cache_read_input_token_cost"):
                    affected_components.add("cr")
                elif source_dimension.startswith("output_cost_per_token"):
                    affected_components.add("out")

            unresolved = bool(
                applicable_tiers
                and not request.prompt_is_exact
                and any(
                    request_components.get(component, 0) > 0
                    for _threshold, tier in applicable_tiers
                    for component in tier
                )
            )
            if applicable_tiers and not request.prompt_is_exact:
                # A summed multi-call receipt above a boundary cannot tell us
                # which calls received the higher rate, even if one category
                # happens not to have a tier-specific source rate.
                unresolved = True
            if cache_unknown_tokens > 0 and long_cache_rate is not None:
                unresolved = True
            if (
                cache_1h_tokens > 0
                and long_cache_rate is not None
                and any("cw" in tier for _threshold, tier in applicable_tiers)
            ):
                # The source does not expose a combined context-size × cache
                # TTL price. Do not multiply or otherwise synthesize one.
                unresolved = True
            if affected_components and any(
                request_components.get(component, 0) > 0
                for component in affected_components
            ):
                unresolved = True

            if unresolved:
                fully_priced = False
                continue

            required_components = [
                ("inp", request_components["inp"]),
                ("out", request_components["out"]),
                ("cr", request_components["cr"]),
            ]
            if any(tokens > 0 and component not in request_rates
                   for component, tokens in required_components):
                fully_priced = False
                continue
            if (
                cache_5m_tokens + cache_unknown_tokens > 0
                and "cw" not in request_rates
            ) or (
                cache_1h_tokens > 0
                and long_cache_rate is None
                and "cw" not in request_rates
            ):
                fully_priced = False
                continue
            request_cost = sum(
                request_rates.get(component, 0.0) * tokens
                for component, tokens in required_components
                if component != "cw"
            )
            cache_write_rate = request_rates.get("cw", 0.0)
            long_cache_write_rate = (
                float(long_cache_rate)
                if long_cache_rate is not None
                else cache_write_rate
            )
            request_cost += (
                cache_5m_tokens + cache_unknown_tokens
            ) * cache_write_rate
            request_cost += cache_1h_tokens * long_cache_write_rate
            known_cost += request_cost / 1_000_000

        return known_cost, fully_priced

    @property
    def unsupported_price_dimensions(self) -> list[str]:
        """Source tariffs that cannot be resolved from this aggregate usage.

        When available, use an exact provider-observed per-request prompt
        maximum to decide if any request crossed a context threshold. Legacy
        aggregates fall back to the total, which can conservatively mark a
        model unpriced. Cache-write duration tiers need cache-age facts which
        the shared usage shape does not currently retain. Non-token add-ons
        remain outside this token-equivalent cost contract.
        """
        rates = _price_for(self.model) or {}
        dimensions = rates.get("_unsupported_dimensions", [])
        if not isinstance(dimensions, list):
            dimensions = []
        raw_service_tiers = rates.get("_service_tiers", {})
        service_tiers = (
            raw_service_tiers if isinstance(raw_service_tiers, Mapping) else {}
        )
        if self.request_token_usage:
            result: set[str] = set()
            for request in self.request_token_usage:
                if (
                    service_tiers
                    and request.service_tier not in (None, "default")
                    and request.service_tier not in service_tiers
                ):
                    result.add("service tier is unavailable or unpriced")
                prompt_tokens = request.prompt_tokens
                request_service_tier = request.service_tier or "default"
                if (
                    request.cache_creation_unknown > 0
                    and rates.get("_cache_creation_above_1hr") is not None
                ):
                    result.add("cache creation TTL is unavailable for some writes")
                for dimension in dimensions:
                    if not isinstance(dimension, str):
                        continue
                    source_dimension = dimension
                    service_suffix = _SERVICE_TIER_SUFFIX_RE.search(dimension)
                    if service_suffix is not None:
                        if (
                            _canonical_service_tier(service_suffix.group(1))
                            != request_service_tier
                        ):
                            continue
                        source_dimension = dimension[:service_suffix.start()]
                    if "cache_creation_input_token_cost_above_1hr" in source_dimension:
                        if service_suffix is not None and (
                            request.cache_creation_unknown > 0
                            or request.cache_creation_1h > 0
                        ):
                            result.add(dimension)
                            continue
                        if request.cache_creation_unknown > 0 or (
                            request.cache_creation_1h > 0
                            and rates.get("_cache_creation_above_1hr") is None
                        ):
                            result.add(dimension)
                        continue
                    threshold = re.search(
                        r"_above_(\d+)(k)?_tokens", source_dimension,
                    )
                    if threshold is None:
                        if service_suffix is not None:
                            component = next(
                                (
                                    key for prefix, key in (
                                        ("input_cost_per_token", "inp"),
                                        ("output_cost_per_token", "out"),
                                        ("cache_creation_input_token_cost", "cw"),
                                        ("cache_read_input_token_cost", "cr"),
                                    )
                                    if source_dimension.startswith(prefix)
                                ),
                                None,
                            )
                            token_counts = {
                                "inp": request.input_tokens,
                                "out": request.output_tokens,
                                "cw": (
                                    request.cache_creation_5m
                                    + request.cache_creation_1h
                                    + request.cache_creation_unknown
                                ),
                                "cr": request.cache_read,
                            }
                            if component and token_counts[component] > 0:
                                result.add(dimension)
                        continue
                    limit = int(threshold.group(1)) * (
                        1000 if threshold.group(2) else 1
                    )
                    if prompt_tokens <= limit:
                        continue
                    if source_dimension.startswith("input_cost_per_token"):
                        used = request.input_tokens
                    elif source_dimension.startswith("cache_creation_input_token_cost"):
                        used = (request.cache_creation_5m + request.cache_creation_1h
                                + request.cache_creation_unknown)
                    elif source_dimension.startswith("cache_read_input_token_cost"):
                        used = request.cache_read
                    elif source_dimension.startswith("output_cost_per_token"):
                        used = request.output_tokens
                    else:
                        used = 0
                    if used > 0:
                        result.add(dimension)
            if any(
                not request.prompt_is_exact
                and any(request.prompt_tokens > threshold for threshold, _ in (
                    (int(k), v)
                    for k, v in (rates.get("_context_tiers", {}) or {}).items()
                    if isinstance(v, Mapping)
                ))
                for request in self.request_token_usage
            ):
                result.add("context-tier prompt size is aggregated across model calls")
            return sorted(result)
        prompt_tokens = getattr(self, "max_request_prompt_tokens", None)
        if prompt_tokens is None:
            prompt_tokens = self.input_tokens + self.cache_creation + self.cache_read
        relevant: list[str] = []
        raw_tiers = rates.get("_context_tiers", {})
        if isinstance(raw_tiers, Mapping):
            for threshold in raw_tiers:
                try:
                    threshold_tokens = int(threshold)
                except (TypeError, ValueError):
                    continue
                if prompt_tokens > threshold_tokens and self.total_tokens > 0:
                    relevant.append(
                        f"context-tier request distribution above {threshold_tokens} tokens"
                    )
        for dimension in dimensions:
            if not isinstance(dimension, str):
                continue
            if "cache_creation_input_token_cost_above_1hr" in dimension:
                if self.cache_creation > 0:
                    relevant.append(dimension)
                continue
            threshold = re.search(r"_above_(\d+)k_tokens", dimension)
            if threshold is None or prompt_tokens <= int(threshold.group(1)) * 1000:
                continue
            if dimension.startswith(("input_cost_per_token", "cache_creation_input_token_cost")):
                relevant_tokens = self.input_tokens + self.cache_creation
            elif dimension.startswith("cache_read_input_token_cost"):
                relevant_tokens = self.cache_read
            elif dimension.startswith("output_cost_per_token"):
                relevant_tokens = self.output_tokens
            else:
                relevant_tokens = 0
            if relevant_tokens > 0:
                relevant.append(dimension)
        return relevant

    @property
    def cost_is_priced(self) -> bool:
        """Whether all observed token categories have an explicit rate."""
        request_result = self._request_pricing_result()
        if request_result is not None:
            return request_result[1]
        if self.unsupported_price_dimensions:
            return False
        return not self.missing_price_components

    @property
    def cost_usd(self) -> float:
        request_result = self._request_pricing_result()
        if request_result is not None:
            return request_result[0]
        p = _price_for(self.model)
        if not p or not self.cost_is_priced:
            return 0.0
        return (
            self.input_tokens * p.get("inp", 0.0)
            + self.output_tokens * p.get("out", 0.0)
            + self.cache_creation * p.get("cw", 0.0)
            + self.cache_read * p.get("cr", 0.0)
        ) / 1_000_000

    @property
    def cost_uncached_is_priced(self) -> bool:
        request_result = self._request_pricing_result(uncached=True)
        if request_result is not None:
            return request_result[1]
        p = _price_for(self.model) or {}
        if self.unsupported_price_dimensions:
            return False
        return not (
            (self.input_tokens + self.cache_creation + self.cache_read > 0 and "inp" not in p)
            or (self.output_tokens > 0 and "out" not in p)
        )

    @property
    def cost_uncached_usd(self) -> float:
        """Hypothetical cost if no caching had been used."""
        request_result = self._request_pricing_result(uncached=True)
        if request_result is not None:
            return request_result[0]
        p = _price_for(self.model)
        if not p or not self.cost_uncached_is_priced:
            return 0.0
        return (
            self.input_tokens * p.get("inp", 0.0)
            + self.output_tokens * p.get("out", 0.0)
            + self.cache_creation * p.get("inp", 0.0)
            + self.cache_read * p.get("inp", 0.0)
        ) / 1_000_000

    @property
    def cache_savings_usd(self) -> float:
        """Known cache savings; incomplete rates never create fictitious savings."""
        if not self.cost_is_priced or not self.cost_uncached_is_priced:
            return 0.0
        return self.cost_uncached_usd - self.cost_usd


@dataclass
class UsageReport:
    since: datetime
    until: datetime
    by_model: dict[str, ModelUsage] = field(default_factory=dict)
    assistant_turns: int = 0
    provider_coverage: dict[str, str] = field(default_factory=dict)

    @property
    def usage_complete(self) -> bool:
        """Whether every selected provider exposed complete exact usage."""
        return all(
            coverage == "complete"
            for coverage in self.provider_coverage.values()
        )

    @property
    def incomplete_agents(self) -> list[str]:
        """Selected active providers without complete exact usage."""
        return sorted(
            name
            for name, coverage in self.provider_coverage.items()
            if coverage != "complete"
        )

    @property
    def partial_agents(self) -> list[str]:
        return sorted(
            name
            for name, coverage in self.provider_coverage.items()
            if coverage == "partial"
        )

    @property
    def unavailable_agents(self) -> list[str]:
        return sorted(
            name
            for name, coverage in self.provider_coverage.items()
            if coverage == "unavailable"
        )

    @property
    def total_input(self) -> int:
        return sum(m.input_tokens for m in self.by_model.values())

    @property
    def total_cache_creation(self) -> int:
        return sum(m.cache_creation for m in self.by_model.values())

    @property
    def total_cache_read(self) -> int:
        return sum(m.cache_read for m in self.by_model.values())

    @property
    def total_output(self) -> int:
        return sum(m.output_tokens for m in self.by_model.values())

    @property
    def total_tokens(self) -> int:
        return sum(m.total_tokens for m in self.by_model.values())

    @property
    def total_cost_usd(self) -> float:
        return sum(m.cost_usd for m in self.by_model.values())

    @property
    def total_cost_uncached_usd(self) -> float:
        return sum(m.cost_uncached_usd for m in self.by_model.values())

    @property
    def cache_savings_usd(self) -> float:
        return sum(m.cache_savings_usd for m in self.by_model.values())

    @property
    def cache_hit_ratio(self) -> float:
        denom = self.total_cache_read + self.total_cache_creation + self.total_input
        return (self.total_cache_read / denom) if denom else 0.0

    @property
    def unpriced_models(self) -> list[str]:
        """Models whose observed token categories are not fully priced.

        Their tokens remain in usage totals. Their model cost is excluded until
        each category used has an explicit rate, so reports can disclose the
        incomplete cost instead of implying the missing category was free.
        """
        return sorted([
            mu.model
            for mu in self.by_model.values()
            if mu.total_tokens > 0 and not mu.cost_is_priced
        ])


def collect_usage(
    since: datetime,
    until: datetime | None = None,
    agent: str = "all",
    active_agents: set[str] | None = None,
) -> UsageReport:
    """Scan session files and aggregate token usage in [since, until].

    Both bounds are normalized to UTC for comparison against the tz-aware
    UTC timestamps in jsonl. Naive inputs are treated as UTC (not system
    local) so test behavior is deterministic across hosts. ``active_agents``
    is the session-derived population for this exact window; when supplied,
    dormant registered providers do not create false incomplete-coverage
    warnings.
    """
    return collect_usage_for_windows(
        {"window": (since, until)},
        agent=agent,
        active_agents_by_window={"window": active_agents},
    )["window"]


def collect_usage_for_windows(
    windows: Mapping[str, tuple[datetime, datetime | None]],
    agent: str = "all",
    active_agents_by_window: Mapping[str, set[str] | None] | None = None,
) -> dict[str, UsageReport]:
    """Aggregate several windows from one scan per bundled provider.

    Adjacent comparison windows deliberately retain the existing inclusive
    boundary rule (an event exactly at the boundary belongs to both), while
    avoiding a second full JSONL parse for every provider.
    """
    from .providers import create_providers, provider_specs

    if not windows:
        return {}

    normalized: dict[str, tuple[datetime, datetime]] = {}
    for key, (since, until) in windows.items():
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        else:
            since = since.astimezone(timezone.utc)
        if until is None:
            until = datetime.now(timezone.utc)
        elif until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        else:
            until = until.astimezone(timezone.utc)
        normalized[key] = (since, until)

    specs = provider_specs(agent)
    by_model_by_window: dict[str, dict[str, ModelUsage]] = {
        key: {} for key in normalized
    }
    turns_by_window = {key: 0 for key in normalized}
    for provider in create_providers(agent):
        provider_turns = provider.collect_usage_for_windows(
            normalized, by_model_by_window,
        )
        for key, turns in provider_turns.items():
            turns_by_window[key] += turns

    return {
        key: UsageReport(
            since=since,
            until=until,
            by_model=by_model_by_window[key],
            assistant_turns=turns_by_window[key],
            provider_coverage={
                spec.name: spec.usage_coverage
                for spec in specs
                if active_agents_by_window is None
                or active_agents_by_window.get(key) is None
                or spec.name in active_agents_by_window[key]
            },
        )
        for key, (since, until) in normalized.items()
    }

def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


if __name__ == "__main__":
    from datetime import timedelta

    logging.basicConfig(level=logging.INFO)
    since = datetime.now(timezone.utc) - timedelta(days=7)
    rep = collect_usage(since)
    print(f"\n=== Past 7 days usage ===")
    print(f"Turns: {rep.assistant_turns:,}")
    print(f"Total tokens: {fmt_tokens(rep.total_tokens)}")
    print(f"  input:          {fmt_tokens(rep.total_input)}")
    print(f"  cache creation: {fmt_tokens(rep.total_cache_creation)}")
    print(f"  cache read:     {fmt_tokens(rep.total_cache_read)}")
    print(f"  output:         {fmt_tokens(rep.total_output)}")
    print(f"Cache hit: {rep.cache_hit_ratio*100:.1f}%")
    print(f"API-equivalent cost: ${rep.total_cost_usd:,.2f}")
    print(f"  uncached would be: ${rep.total_cost_uncached_usd:,.2f}")
    print(f"  cache saved: ${rep.cache_savings_usd:,.2f}")
    print(f"\nBy model:")
    for model, mu in sorted(rep.by_model.items(), key=lambda x: -x[1].total_tokens):
        print(
            f"  {model:35s} turns={mu.turns:5d}  "
            f"out={fmt_tokens(mu.output_tokens):>8s}  "
            f"cost=${mu.cost_usd:8,.2f}"
        )
