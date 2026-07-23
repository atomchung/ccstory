"""Aggregate Claude token usage from ~/.claude/projects/**/*.jsonl.

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
from collections import defaultdict
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


def _match_price_in_table(
    model_key: str,
    price_table: dict[str, dict[str, float]],
    provenance: dict[str, str],
) -> dict[str, float] | None:
    """Resolve price using 3-tier precedence ladder:
    1. config.toml [prices] user override (exact key, then substring)
    2. vendored table, exact model id
    3. DEFAULT_PRICES short-key substring (opus, sonnet, haiku, fable, mythos...)
    """
    mk = model_key.lower().strip()
    if not mk:
        return None

    # Tier 1: User override in config.toml (exact key, then substring)
    user_keys = [k for k, prov in provenance.items() if prov == "user"]
    if mk in price_table and provenance.get(mk) == "user":
        return price_table[mk]
    user_matches = [k for k in user_keys if k in mk]
    if user_matches:
        best_key = max(user_matches, key=len)
        return price_table[best_key]

    # Tier 2: Vendored table, exact model id match
    if mk in price_table:
        return price_table[mk]

    # Tier 3: DEFAULT_PRICES short-key substring
    default_keys = [k for k in DEFAULT_PRICES if k in price_table]
    default_matches = [k for k in default_keys if k in mk]
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
        "verify current Anthropic API pricing."
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
        for cfg_key, internal_key in _CONFIG_KEY_MAP.items():
            if cfg_key in override:
                try:
                    target[internal_key] = float(override[cfg_key])
                except (TypeError, ValueError):
                    LOG.warning(
                        "ignoring non-numeric [prices.%s].%s", model_key, cfg_key,
                    )
        missing = [k for k in ("inp", "out", "cw", "cr") if k not in target]
        if missing:
            for k in missing:
                target[k] = 0.0
            LOG.warning(
                "[prices.%s] missing %s; treating as $0.0/M",
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

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation
            + self.cache_read
            + self.output_tokens
        )

    @property
    def cost_usd(self) -> float:
        p = _price_for(self.model)
        if not p:
            return 0.0
        return (
            self.input_tokens   * p["inp"]
            + self.output_tokens  * p["out"]
            + self.cache_creation * p["cw"]
            + self.cache_read     * p["cr"]
        ) / 1_000_000

    @property
    def cost_uncached_usd(self) -> float:
        """Hypothetical cost if no caching had been used."""
        p = _price_for(self.model)
        if not p:
            return 0.0
        return (
            self.input_tokens   * p["inp"]
            + self.output_tokens  * p["out"]
            + self.cache_creation * p["inp"]
            + self.cache_read     * p["inp"]
        ) / 1_000_000


@dataclass
class UsageReport:
    since: datetime
    until: datetime
    by_model: dict[str, ModelUsage] = field(default_factory=dict)
    assistant_turns: int = 0

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
        return self.total_cost_uncached_usd - self.total_cost_usd

    @property
    def cache_hit_ratio(self) -> float:
        denom = self.total_cache_read + self.total_cache_creation + self.total_input
        return (self.total_cache_read / denom) if denom else 0.0


def collect_usage(
    since: datetime,
    until: datetime | None = None,
    agent: str = "all",
) -> UsageReport:
    """Scan session files and aggregate token usage in [since, until].

    Both bounds are normalized to UTC for comparison against the tz-aware
    UTC timestamps in jsonl. Naive inputs are treated as UTC (not system
    local) so test behavior is deterministic across hosts.
    """
    from .providers import _PROVIDERS, list_providers

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

    if agent == "all":
        providers_to_run = [cls() for cls in _PROVIDERS.values()]
    elif agent in _PROVIDERS:
        providers_to_run = [_PROVIDERS[agent]()]
    else:
        raise ValueError(
            f"Unsupported agent filter '{agent}'. "
            f"Expected 'all' or one of {list_providers()}"
        )

    by_model: dict[str, ModelUsage] = {}
    assistant_turns = 0

    for provider in providers_to_run:
        assistant_turns += provider.collect_usage(since, until, by_model)

    return UsageReport(
        since=since, until=until, by_model=by_model, assistant_turns=assistant_turns
    )


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
