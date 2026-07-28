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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from datetime import timezone
from pathlib import Path

from ..time_tracking import SessionStat
from ..token_usage import ModelUsage


@dataclass(frozen=True)
class SnapshotMetrics:
    """Privacy-safe operation counts for one provider snapshot.

    Legacy providers cannot report these counts without changing their old
    collection methods, so every counter is unknown by default.  ``None`` is
    intentionally different from zero: zero is an observed fact, while the
    default adapter simply has no complete record inventory yet.

    Only aggregate operation counts belong here.  Transcript text, excerpts,
    filesystem paths, and session/project identifiers must never be retained
    as snapshot diagnostics.
    """

    sources_enumerated: int | None = None
    source_opens: int | None = None
    records_parsed: int | None = None
    companion_reads: int | None = None
    record_inventory_complete: bool = False


@dataclass
class ProviderRecord:
    """One selected provider's contribution to an invocation snapshot."""

    agent: str
    sessions_by_window: dict[str, list[SessionStat]]
    by_model_by_window: dict[str, dict[str, ModelUsage]]
    assistant_turns_by_window: dict[str, int]
    metrics: SnapshotMetrics = field(default_factory=SnapshotMetrics)


def _datetime_utc(value: datetime) -> datetime:
    """Treat naive provider timestamps as UTC and normalize aware values."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _usage_windows_utc(
    windows: Mapping[str, tuple[datetime, datetime]],
) -> dict[str, tuple[datetime, datetime]]:
    """Normalize usage bounds exactly like ``token_usage`` public helpers."""

    normalized: dict[str, tuple[datetime, datetime]] = {}
    for key, (since, until) in windows.items():
        normalized[key] = (_datetime_utc(since), _datetime_utc(until))
    return normalized


class BaseAgentProvider(ABC):
    """Abstract base class for AI coding-agent session providers."""

    @property
    @abstractmethod
    def agent_name(self) -> str:
        """Identifier for the agent (e.g. ``claude``, ``codex``)."""

    @abstractmethod
    def data_roots(self) -> tuple[Path, ...]:
        """Directories that may contain this provider's transcripts.

        The recap preflight uses this provider-owned declaration to decide
        whether data exists before collection starts. Keep every supported
        location here so collection and availability checks cannot drift
        apart (for example, Codex has both live and archived roots).
        """

    @abstractmethod
    def extract_excerpt(self, path: Path) -> tuple[str, str]:
        """Return ``(project, bounded user-facing excerpt)`` for one transcript.

        Summary generation calls this through the provider selected for the
        session. New transcript formats therefore stay inside their provider
        module instead of adding format branches to ``session_summarizer``.
        """

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

        Returns the count of assistant turns with exact usage processed. Never
        synthesize tokens from character counts or other heuristics. A provider
        whose normal logs lack exact usage returns zero and declares partial or
        unavailable coverage in its ``AgentProviderSpec``.
        """

    def collect_usage_for_windows(
        self,
        windows: Mapping[str, tuple[datetime, datetime]],
        by_model_by_window: Mapping[str, dict],
    ) -> dict[str, int]:
        """Collect exact usage for several windows in one invocation.

        The conservative default preserves third-party provider compatibility.
        Bundled providers override it to parse their source files once and
        attribute each exact event to every matching window.
        """
        return {
            key: self.collect_usage(since, until, by_model_by_window[key])
            for key, (since, until) in windows.items()
        }

    def collect_snapshot(
        self,
        windows: Mapping[str, tuple[datetime, datetime]],
        *,
        engaged_only: bool = True,
    ) -> ProviderRecord:
        """Collect this provider's legacy session and exact-usage facts.

        This conservative adapter keeps third-party providers source
        compatible: implementations that only provide the pre-snapshot
        abstract methods continue to work unchanged.  Session collection scans
        the enclosing range once and reuses each ``SessionStat`` object in
        every overlapping window.  Usage collection delegates to the existing
        multi-window hook, preserving inclusive event boundaries, cumulative
        provider lineage, model aggregation, and coverage semantics.

        Bundled providers can override this method in later issue slices to
        derive both fact sets from one physical pass and report complete
        operation metrics.
        """

        if not windows:
            return ProviderRecord(
                agent=self.agent_name,
                sessions_by_window={},
                by_model_by_window={},
                assistant_turns_by_window={},
            )

        earliest = min(since for since, _until in windows.values())
        latest = max(until for _since, until in windows.values())
        scanned = self.collect_sessions(
            earliest,
            latest,
            engaged_only=engaged_only,
        )
        comparison_windows = _usage_windows_utc(windows)
        sessions_by_window = {
            key: [
                session
                for session in scanned
                if _datetime_utc(session.end) >= since
                and _datetime_utc(session.start) < until
            ]
            for key, (since, until) in comparison_windows.items()
        }

        usage_windows = comparison_windows
        by_model_by_window = {key: {} for key in usage_windows}
        assistant_turns = self.collect_usage_for_windows(
            usage_windows,
            by_model_by_window,
        )
        assistant_turns_by_window = {
            key: assistant_turns.get(key, 0) for key in usage_windows
        }

        return ProviderRecord(
            agent=self.agent_name,
            sessions_by_window=sessions_by_window,
            by_model_by_window=by_model_by_window,
            assistant_turns_by_window=assistant_turns_by_window,
        )

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
