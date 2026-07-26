"""Provider registry for multi-agent session sources."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from ..time_tracking import SessionStat
from .antigravity import AntigravityProvider
from .base import BaseAgentProvider
from .claude import ClaudeCodeProvider
from .codex import CodexProvider


UsageCoverage = Literal["complete", "partial", "unavailable"]


@dataclass(frozen=True)
class AgentProviderSpec:
    """Shared metadata and factory for one coding-agent session source.

    Registering one spec is the only cross-provider edit a new bundled source
    should need. CLI choices, MCP validation, display labels, transcript
    preflight, session collection, and usage aggregation all derive from this
    registry. ``usage_coverage`` declares whether ``collect_usage`` sees every
    exact token record; non-complete providers remain usable, but reports must
    disclose that their aggregate token and cost totals are incomplete.
    """

    name: str
    label: str
    factory: Callable[[], BaseAgentProvider]
    usage_coverage: UsageCoverage = "unavailable"


_PROVIDER_SPECS: dict[str, AgentProviderSpec] = {}
_PROVIDER_NAME_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")


def register_provider(spec: AgentProviderSpec, *, replace: bool = False) -> None:
    """Register one provider descriptor.

    ``replace`` exists for controlled embedding and tests; bundled providers
    must not silently shadow an existing agent id.
    """
    name = spec.name.strip()
    if not name or name == "all":
        raise ValueError("Provider name must be non-empty and cannot be 'all'")
    if name != spec.name:
        raise ValueError("Provider names cannot contain surrounding whitespace")
    if _PROVIDER_NAME_RE.fullmatch(name) is None:
        raise ValueError(
            "Provider names must be lowercase filename-safe slugs containing "
            "only letters, numbers, hyphens, or underscores"
        )
    if name in _PROVIDER_SPECS and not replace:
        raise ValueError(f"Provider '{name}' is already registered")
    if spec.usage_coverage not in ("complete", "partial", "unavailable"):
        raise ValueError(
            f"Provider '{name}' has invalid usage coverage "
            f"'{spec.usage_coverage}'"
        )
    _PROVIDER_SPECS[name] = spec


register_provider(
    AgentProviderSpec(
        "claude",
        "Claude Code",
        ClaudeCodeProvider,
        usage_coverage="complete",
    )
)
register_provider(
    AgentProviderSpec(
        "codex",
        "Codex",
        CodexProvider,
        usage_coverage="complete",
    )
)
register_provider(
    AgentProviderSpec(
        "antigravity",
        "Antigravity",
        AntigravityProvider,
        usage_coverage="complete",
    )
)


def provider_specs(agent: str = "all") -> list[AgentProviderSpec]:
    """Descriptors selected by ``all`` or a concrete registered agent id."""
    if agent == "all":
        return list(_PROVIDER_SPECS.values())
    spec = _PROVIDER_SPECS.get(agent)
    if spec is None:
        raise ValueError(
            f"Unsupported agent filter '{agent}'. "
            f"Expected 'all' or one of {list_providers()}"
        )
    return [spec]


def create_providers(agent: str = "all") -> list[BaseAgentProvider]:
    """Instantiate the provider population selected for one operation."""
    providers: list[BaseAgentProvider] = []
    for spec in provider_specs(agent):
        provider = spec.factory()
        if provider.agent_name != spec.name:
            raise ValueError(
                f"Provider descriptor '{spec.name}' created an adapter with "
                f"agent_name '{provider.agent_name}'"
            )
        providers.append(provider)
    return providers


def get_provider(agent_name: str) -> BaseAgentProvider:
    """Instantiate a provider by name."""
    try:
        return create_providers(agent_name)[0]
    except ValueError as exc:
        raise ValueError(
            f"Unknown agent provider: '{agent_name}'. "
            f"Available: {list_providers()}"
        ) from exc


def list_providers() -> list[str]:
    """Return available provider names."""
    return list(_PROVIDER_SPECS)


def agent_label(agent_name: str) -> str:
    """Human-readable name for an agent, falling back to the raw id."""
    spec = _PROVIDER_SPECS.get(agent_name)
    return spec.label if spec else agent_name


def provider_data_roots(agent: str = "all") -> list[tuple[str, Path]]:
    """Provider-owned transcript roots for a selected agent population."""
    roots: list[tuple[str, Path]] = []
    for spec in provider_specs(agent):
        provider_roots = spec.factory().data_roots()
        if not provider_roots:
            raise ValueError(
                f"Provider '{spec.name}' declared no data roots; "
                "implement BaseAgentProvider.data_roots()."
            )
        roots.extend((spec.name, root) for root in provider_roots)
    return roots


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

    def provider_for(self, sess: SessionStat) -> BaseAgentProvider | None:
        """Cached provider selected by the session's registered agent id."""
        name = getattr(sess, "agent", "claude") or "claude"
        provider = self._providers.get(name)
        if provider is None:
            try:
                provider = get_provider(name)
            except ValueError:
                return None
            self._providers[name] = provider
        return provider

    def path_for(self, sess: SessionStat) -> Path | None:
        """Transcript backing ``sess``, or None when it is gone."""
        provider = self.provider_for(sess)
        if provider is None:
            return None
        return provider.transcript_path(sess)

    def excerpt_for(self, sess: SessionStat) -> tuple[str, str] | None:
        """Provider-owned narrative excerpt for ``sess``, or None if missing."""
        provider = self.provider_for(sess)
        if provider is None:
            return None
        path = provider.transcript_path(sess)
        if path is None:
            return None
        return provider.extract_excerpt(path)


def collect_multi_agent_sessions(
    since: datetime,
    until: datetime | None = None,
    engaged_only: bool = True,
    agent: str = "all",
) -> list[SessionStat]:
    """Collect sessions across one or all registered agent providers."""
    all_stats: list[SessionStat] = []
    for provider in create_providers(agent):
        all_stats.extend(
            provider.collect_sessions(since, until, engaged_only=engaged_only)
        )
    return all_stats
