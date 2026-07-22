"""Provider package registry for multi-agent session sources."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from ..time_tracking import SessionStat
from .antigravity import AntigravityProvider
from .base import BaseAgentProvider
from .claude import ClaudeCodeProvider
from .codex import CodexProvider

_PROVIDERS: dict[str, type[BaseAgentProvider]] = {
    "claude": ClaudeCodeProvider,
    "antigravity": AntigravityProvider,
    "codex": CodexProvider,
}


def get_provider(agent_name: str) -> BaseAgentProvider:
    """Instantiate a provider by name."""
    if agent_name not in _PROVIDERS:
        raise ValueError(f"Unknown agent provider: '{agent_name}'. Available: {list(_PROVIDERS.keys())}")
    return _PROVIDERS[agent_name]()


def list_providers() -> list[str]:
    """Return available provider names."""
    return list(_PROVIDERS.keys())


def collect_multi_agent_sessions(
    since: datetime,
    until: datetime | None = None,
    engaged_only: bool = True,
    agent: str = "all",
) -> list[SessionStat]:
    """Collect sessions across one or all registered agent providers."""
    if agent == "all":
        providers_to_run = [cls() for cls in _PROVIDERS.values()]
    elif agent in _PROVIDERS:
        providers_to_run = [_PROVIDERS[agent]()]
    else:
        raise ValueError(f"Unsupported agent filter '{agent}'. Expected 'all' or one of {list_providers()}")

    all_stats: list[SessionStat] = []
    for provider in providers_to_run:
        all_stats.extend(provider.collect_sessions(since, until, engaged_only=engaged_only))
    return all_stats
