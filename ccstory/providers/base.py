"""Base provider abstraction for multi-agent session sources.

One provider per coding agent (Claude Code, OpenAI Codex, ...). A provider
owns *everything* that is agent-specific about reading that agent's sessions:
where the transcripts live, how to parse one into a `SessionStat`, and how to
find a transcript again from a session id. Nothing outside `ccstory/providers/`
should hardcode an agent's on-disk layout — that knowledge leaking upward is
what made `recap._backfill_summaries` rglob the whole `~/.codex` tree once per
session.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from ..time_tracking import SessionStat


class BaseAgentProvider(ABC):
    """Abstract base class for AI coding-agent session providers."""

    @property
    @abstractmethod
    def agent_name(self) -> str:
        """Identifier for the agent (e.g. ``claude``, ``codex``)."""

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

    @abstractmethod
    def collect_usage(
        self,
        since: datetime,
        until: datetime,
        by_model: dict,
    ) -> int:
        """Collect token usage for sessions in [since, until] into by_model dictionary.

        Returns the count of assistant turns processed.
        """

    def transcript_path(self, sess: SessionStat) -> Path | None:
        """Locate the transcript backing ``sess``, or None if it is gone.

        `parse_session` stamps `SessionStat.path`, so the common case is a
        single `exists()` call. Subclasses override the miss path (a stat read
        from cache, a transcript moved to an archive dir) — and must keep it
        O(1)-ish per call: this runs once per session in the summary backfill.
        """
        path = getattr(sess, "path", None)
        if path is not None and path.exists():
            return path
        return None
