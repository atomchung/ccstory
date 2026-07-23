"""Provider registry for multi-agent session sources."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..time_tracking import SessionStat
from .base import BaseAgentProvider
from .claude import ClaudeCodeProvider
from .codex import CodexProvider

_PROVIDERS: dict[str, type[BaseAgentProvider]] = {
    "claude": ClaudeCodeProvider,
    "codex": CodexProvider,
}

# Display names for the report's agent breakdown.
AGENT_LABELS = {
    "claude": "Claude Code",
    "codex": "OpenAI Codex",
}


def get_provider(agent_name: str) -> BaseAgentProvider:
    """Instantiate a provider by name."""
    if agent_name not in _PROVIDERS:
        raise ValueError(
            f"Unknown agent provider: '{agent_name}'. "
            f"Available: {list(_PROVIDERS)}"
        )
    return _PROVIDERS[agent_name]()


def list_providers() -> list[str]:
    """Return available provider names."""
    return list(_PROVIDERS)


def agent_label(agent_name: str) -> str:
    """Human-readable name for an agent, falling back to the raw id."""
    return AGENT_LABELS.get(agent_name, agent_name)


class TranscriptResolver:
    """Session → transcript resolver that reuses one provider per agent.

    Providers may need an index to map a session id back to a file (Codex file
    names embed a timestamp, so the id alone is not the path). Building that
    index is a tree walk, so it must happen once per run — resolving through a
    fresh provider per session is what made the summary backfill cost ~270ms ×
    every session.
    """

    def __init__(self) -> None:
        self._providers: dict[str, BaseAgentProvider] = {}

    def path_for(self, sess: SessionStat) -> Path | None:
        """Transcript backing ``sess``, or None when it is gone."""
        name = getattr(sess, "agent", "claude") or "claude"
        provider = self._providers.get(name)
        if provider is None:
            try:
                provider = get_provider(name)
            except ValueError:
                return None
            self._providers[name] = provider
        return provider.transcript_path(sess)


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
        raise ValueError(
            f"Unsupported agent filter '{agent}'. "
            f"Expected 'all' or one of {list_providers()}"
        )

    all_stats: list[SessionStat] = []
    for provider in providers_to_run:
        all_stats.extend(
            provider.collect_sessions(since, until, engaged_only=engaged_only)
        )
    return all_stats
