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
                _active_prices[k] = dict(v)
                _active_provenance[k] = "litellm"
        if _active_snapshot_date == PRICES_SNAPSHOT_DATE and vendored_snap:
            _active_snapshot_date = vendored_snap
        _vendored_initialized = True


MODEL_ALIASES: dict[str, str] = {
    "gemini-3-flash-a": "gemini-3-flash-preview",
    "gemini-3-flash-agent": "gemini-3-flash-preview",
}

_GROK_BUILD_ALIAS_RE = re.compile(r"^grok-(\d+\.\d+)-build$")


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
    merged: dict[str, dict[str, float]] = {k: dict(v) for k, v in DEFAULT_PRICES.items()}
    provenance: dict[str, str] = {k: "default" for k in DEFAULT_PRICES}

    for k, v in vendored_prices.items():
        if k not in merged:
            merged[k] = dict(v)
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
        target = dict(base_price) if base_price else {}
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
    _active_prices.update({k: dict(v) for k, v in prices.items()})
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

    def __setstate__(self, state: dict[str, object]) -> None:
        """Keep PersonalOS snapshots pickled before this field readable."""
        self.__dict__.update(state)
        self.__dict__.setdefault("max_request_prompt_tokens", None)

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
            return []
        prompt_tokens = getattr(self, "max_request_prompt_tokens", None)
        if prompt_tokens is None:
            prompt_tokens = self.input_tokens + self.cache_creation + self.cache_read
        relevant: list[str] = []
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
        if self.unsupported_price_dimensions:
            return False
        return not self.missing_price_components

    @property
    def cost_usd(self) -> float:
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
