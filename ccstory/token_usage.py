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

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

LOG = logging.getLogger("ccstory.token_usage")
PROJECTS_DIR = Path.home() / ".claude" / "projects"


# Anthropic API list prices, USD per 1M tokens (as of 2026-01).
# inp = fresh input, out = output, cw = cache creation (write), cr = cache read.
_PRICES = {
    "opus":   dict(inp=15.00, out=75.00, cw=18.75, cr=1.50),
    "sonnet": dict(inp=3.00,  out=15.00, cw=3.75,  cr=0.30),
    "haiku":  dict(inp=0.80,  out=4.00,  cw=1.00,  cr=0.08),
}


def _price_for(model: str) -> dict | None:
    m = (model or "").lower()
    for key, p in _PRICES.items():
        if key in m:
            return p
    return None


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


def collect_usage(since: datetime, until: datetime | None = None) -> UsageReport:
    """Scan all jsonl files and aggregate token usage in [since, until].

    Both bounds are normalized to UTC for comparison against the tz-aware
    UTC timestamps in jsonl. Naive inputs are treated as UTC (not system
    local) so test behavior is deterministic across hosts.
    """
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

    by_model: dict[str, ModelUsage] = {}
    assistant_turns = 0
    seen_ids: set[str] = set()  # dedup: Claude Code writes streaming chunks 2-3×

    for fp in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            with fp.open() as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = d.get("message")
                    ts = d.get("timestamp")
                    if not (
                        isinstance(msg, dict)
                        and msg.get("role") == "assistant"
                        and "usage" in msg
                        and ts
                    ):
                        continue
                    try:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if t < since or t > until:
                        continue

                    mid = msg.get("id")
                    if mid:
                        if mid in seen_ids:
                            continue
                        seen_ids.add(mid)

                    u = msg["usage"]
                    model = msg.get("model") or "unknown"
                    mu = by_model.setdefault(model, ModelUsage(model=model))
                    mu.turns += 1
                    mu.input_tokens   += u.get("input_tokens", 0) or 0
                    mu.cache_creation += u.get("cache_creation_input_tokens", 0) or 0
                    mu.cache_read     += u.get("cache_read_input_tokens", 0) or 0
                    mu.output_tokens  += u.get("output_tokens", 0) or 0
                    assistant_turns += 1
        except OSError as e:
            LOG.debug("failed to read %s: %s", fp, e)
            continue

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
