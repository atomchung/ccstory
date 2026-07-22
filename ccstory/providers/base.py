"""Base provider abstraction for multi-agent session sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from ..time_tracking import SessionStat


class BaseAgentProvider(ABC):
    """Abstract base class for AI Agent session providers."""

    @property
    @abstractmethod
    def agent_name(self) -> str:
        """Identifier for the agent (e.g., 'claude', 'antigravity')."""

    @abstractmethod
    def collect_sessions(
        self,
        since: datetime,
        until: datetime | None = None,
        engaged_only: bool = True,
    ) -> list[SessionStat]:
        """Collect and parse all sessions for this agent in [since, until)."""

    @abstractmethod
    def parse_session(self, path: Path) -> SessionStat | None:
        """Parse a single session transcript file into a SessionStat."""
