"""Provider registry for multi-agent session sources."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..provider_metadata import (
    BUNDLED_PROVIDER_DEFINITIONS,
    UsageCoverage,
)
from ..time_tracking import SessionStat
from ..token_usage import ModelUsage, UsageReport
from .base import (
    BaseAgentProvider,
    ProviderRecord,
    SnapshotMetrics as SnapshotMetrics,
    _usage_windows_utc,
)

__all__ = [
    "AgentProviderSpec",
    "ProviderSnapshot",
    "BaseAgentProvider",
    "ProviderRecord",
    "SnapshotMetrics",
    "UsageCoverage",
    "ClaudeCodeProvider",
    "CodexProvider",
    "AntigravityProvider",
    "TranscriptResolver",
    "register_provider",
    "provider_specs",
    "create_providers",
    "collect_provider_snapshot",
    "get_provider",
    "list_providers",
    "agent_label",
    "provider_data_roots",
    "collect_multi_agent_sessions",
]


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


@dataclass
class ProviderSnapshot:
    """Ordered, invocation-local facts from the selected provider registry."""

    windows: dict[str, tuple[datetime, datetime]]
    records: tuple[ProviderRecord, ...]
    sessions_by_window: dict[str, list[SessionStat]]
    usage_by_window: dict[str, UsageReport]


_PROVIDER_SPECS: dict[str, AgentProviderSpec] = {}
_PROVIDER_NAME_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
_BUNDLED_CLASS_DEFINITIONS = {
    definition.class_name: definition
    for definition in BUNDLED_PROVIDER_DEFINITIONS
}


def __getattr__(name: str):
    """Lazily preserve the provider classes historically exposed here."""
    definition = _BUNDLED_CLASS_DEFINITIONS.get(name)
    if definition is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    provider_type = definition.load_type()
    globals()[name] = provider_type
    return provider_type


def __dir__() -> list[str]:
    """Include lazy compatibility exports without importing their modules."""
    return sorted(set(globals()) | set(_BUNDLED_CLASS_DEFINITIONS))


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


for _definition in BUNDLED_PROVIDER_DEFINITIONS:
    register_provider(
        AgentProviderSpec(
            name=_definition.name,
            label=_definition.label,
            factory=_definition.create,
            usage_coverage=_definition.usage_coverage,
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


def _selected_provider_pairs(
    agent: str = "all",
) -> list[tuple[AgentProviderSpec, BaseAgentProvider]]:
    """Instantiate selected registry entries once, preserving registry order."""

    selected: list[tuple[AgentProviderSpec, BaseAgentProvider]] = []
    for spec in provider_specs(agent):
        provider = spec.factory()
        if provider.agent_name != spec.name:
            raise ValueError(
                f"Provider descriptor '{spec.name}' created an adapter with "
                f"agent_name '{provider.agent_name}'"
            )
        selected.append((spec, provider))
    return selected


def create_providers(agent: str = "all") -> list[BaseAgentProvider]:
    """Instantiate the provider population selected for one operation."""
    return [provider for _spec, provider in _selected_provider_pairs(agent)]


def _merge_model_usage(
    destination: dict[str, ModelUsage],
    source: Mapping[str, ModelUsage],
) -> None:
    """Accumulate exact provider facts without sharing mutable usage objects."""

    for model, incoming in source.items():
        combined = destination.setdefault(model, ModelUsage(model=model))
        combined.turns += incoming.turns
        combined.input_tokens += incoming.input_tokens
        combined.cache_creation += incoming.cache_creation
        combined.cache_read += incoming.cache_read
        combined.output_tokens += incoming.output_tokens


def collect_provider_snapshot(
    windows: Mapping[str, tuple[datetime, datetime]],
    *,
    engaged_only: bool = True,
    agent: str = "all",
) -> ProviderSnapshot:
    """Collect selected provider contributions once and aggregate by window.

    Registry order is retained both in ``records`` and in each aggregated
    session list.  Each selected factory is instantiated exactly once for this
    operation; its instance owns the complete provider-level snapshot call.
    Usage reports derive coverage from the agents that produced sessions in
    that exact window, matching ``build_recap``'s historical behavior while
    still aggregating every exact usage fact the selected providers expose.
    """

    window_map = dict(windows)
    if not window_map:
        return ProviderSnapshot(
            windows={},
            records=(),
            sessions_by_window={},
            usage_by_window={},
        )

    selected = _selected_provider_pairs(agent)
    records: list[ProviderRecord] = []
    expected_keys = set(window_map)
    for spec, provider in selected:
        record = provider.collect_snapshot(
            window_map,
            engaged_only=engaged_only,
        )
        if record.agent != spec.name:
            raise ValueError(
                f"Provider snapshot for '{spec.name}' identified itself as "
                f"'{record.agent}'"
            )
        for field_name, actual in (
            ("sessions_by_window", record.sessions_by_window),
            ("by_model_by_window", record.by_model_by_window),
            ("assistant_turns_by_window", record.assistant_turns_by_window),
        ):
            missing = expected_keys.difference(actual)
            if missing:
                raise ValueError(
                    f"Provider '{spec.name}' snapshot omitted "
                    f"{field_name} keys: {sorted(missing)}"
                )
        records.append(record)

    sessions_by_window: dict[str, list[SessionStat]] = {
        key: [] for key in window_map
    }
    by_model_by_window: dict[str, dict[str, ModelUsage]] = {
        key: {} for key in window_map
    }
    assistant_turns_by_window = {key: 0 for key in window_map}
    for record in records:
        for key in window_map:
            sessions_by_window[key].extend(record.sessions_by_window[key])
            _merge_model_usage(
                by_model_by_window[key],
                record.by_model_by_window[key],
            )
            assistant_turns_by_window[key] += (
                record.assistant_turns_by_window[key]
            )

    usage_windows = _usage_windows_utc(window_map)
    specs_by_name = {spec.name: spec for spec, _provider in selected}
    usage_by_window: dict[str, UsageReport] = {}
    for key, (since, until) in usage_windows.items():
        active_agents = {
            session.agent for session in sessions_by_window[key]
        }
        usage_by_window[key] = UsageReport(
            since=since,
            until=until,
            by_model=by_model_by_window[key],
            assistant_turns=assistant_turns_by_window[key],
            provider_coverage={
                name: spec.usage_coverage
                for name, spec in specs_by_name.items()
                if name in active_agents
            },
        )

    return ProviderSnapshot(
        windows=window_map,
        records=tuple(records),
        sessions_by_window=sessions_by_window,
        usage_by_window=usage_by_window,
    )


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
