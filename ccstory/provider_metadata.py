"""Lightweight canonical metadata for bundled session providers.

This module intentionally imports no provider implementation.  CLI discovery
can therefore expose the current bundled choices without importing transcript
parsers, while the full registry can turn the same definitions into lazy
factories when real provider work begins.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any, Literal


UsageCoverage = Literal["complete", "partial", "unavailable"]


@dataclass(frozen=True)
class BundledProviderDefinition:
    """Metadata plus a lazy import target for one bundled provider."""

    name: str
    label: str
    usage_coverage: UsageCoverage
    module: str
    class_name: str

    def load_type(self) -> type[Any]:
        """Import and return the provider implementation class on demand."""
        module = import_module(self.module, package=__package__)
        return getattr(module, self.class_name)

    def create(self) -> Any:
        """Import and instantiate the provider implementation on demand."""
        return self.load_type()()


# Adding a bundled provider is a one-definition change.  Bootstrap choices and
# the full runtime registry both derive their order and metadata from here.
BUNDLED_PROVIDER_DEFINITIONS = (
    BundledProviderDefinition(
        name="claude",
        label="Claude Code",
        usage_coverage="complete",
        module=".providers.claude",
        class_name="ClaudeCodeProvider",
    ),
    BundledProviderDefinition(
        name="codex",
        label="Codex",
        usage_coverage="complete",
        module=".providers.codex",
        class_name="CodexProvider",
    ),
    BundledProviderDefinition(
        name="antigravity",
        label="Antigravity",
        usage_coverage="complete",
        module=".providers.antigravity",
        class_name="AntigravityProvider",
    ),
)


def bundled_provider_names() -> tuple[str, ...]:
    """Names in canonical registry order, without loading implementations."""
    return tuple(definition.name for definition in BUNDLED_PROVIDER_DEFINITIONS)
