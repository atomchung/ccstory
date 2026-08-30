"""Contract tests for the invocation-local provider snapshot (#174 PR A)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccstory import providers, recap
import ccstory.providers.claude as claude_module
from ccstory.providers import (
    AgentProviderSpec,
    ProviderSnapshot,
    collect_provider_snapshot,
)
from ccstory.providers.base import (
    BaseAgentProvider,
    ProviderRecord,
    SnapshotMetrics,
)
from ccstory.providers.claude import ClaudeCodeProvider
from ccstory.report import build_report_json, render_report
from ccstory.session_identity import public_session_id
from ccstory.time_tracking import (
    SessionSlice,
    SessionStat,
    collect_sessions_for_windows,
    rollup_by_category,
)
from ccstory.token_usage import (
    ModelUsage,
    collect_usage_for_windows,
)
from tests.conftest import make_assistant_msg, make_user_msg


BOUNDARY = datetime(2026, 7, 20, tzinfo=timezone.utc)
PREVIOUS_START = BOUNDARY - timedelta(days=7)
CURRENT_END = BOUNDARY + timedelta(days=7)
WINDOWS = {
    "previous": (PREVIOUS_START, BOUNDARY),
    "current": (BOUNDARY, CURRENT_END),
}


def _session(
    agent: str,
    session_id: str,
    start: datetime,
    end: datetime,
) -> SessionStat:
    return SessionStat(
        project=f"{agent}-project",
        category="coding",
        session_id=session_id,
        start=start,
        end=end,
        active_sec=60,
        msg_count=2,
        user_msg_count=2,
        first_user_text=f"{agent} work",
        timestamps=[start.timestamp(), end.timestamp()],
        agent=agent,
    )


class _LegacyProvider(BaseAgentProvider):
    """Provider implementing only the pre-snapshot abstract contract."""

    def __init__(
        self,
        agent: str,
        sessions: list[SessionStat],
        usage_events: list[tuple[datetime, str, int, int, int, int]],
    ) -> None:
        self._agent = agent
        self._sessions = sessions
        self._usage_events = usage_events
        self.session_calls: list[tuple[datetime, datetime | None, bool]] = []
        self.usage_calls: list[tuple[datetime, datetime]] = []

    @property
    def agent_name(self) -> str:
        return self._agent

    def data_roots(self) -> tuple[Path, ...]:
        return (Path("/synthetic/provider"),)

    def extract_excerpt(self, path: Path) -> tuple[str, str]:
        return "synthetic", ""

    def collect_sessions(
        self,
        since: datetime,
        until: datetime | None = None,
        engaged_only: bool = True,
    ) -> list[SessionStat]:
        self.session_calls.append((since, until, engaged_only))
        return [
            session
            for session in self._sessions
            if session.end >= since
            and (until is None or session.start < until)
            and (not engaged_only or session.engaged)
        ]

    def parse_session(self, path: Path) -> SessionStat | None:
        return None

    def collect_usage(
        self,
        since: datetime,
        until: datetime,
        by_model: dict,
    ) -> int:
        self.usage_calls.append((since, until))
        turns = 0
        for timestamp, model, input_tokens, cache_creation, cache_read, output in (
            self._usage_events
        ):
            # Existing provider usage windows are inclusive at both ends.
            if not since <= timestamp <= until:
                continue
            usage = by_model.setdefault(model, ModelUsage(model=model))
            usage.turns += 1
            usage.input_tokens += input_tokens
            usage.cache_creation += cache_creation
            usage.cache_read += cache_read
            usage.output_tokens += output
            turns += 1
        return turns


class _WindowCountingProvider(_LegacyProvider):
    """Old provider with the already-supported multi-window usage override."""

    def __init__(self, *args, invocation_order: list[str], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.invocation_order = invocation_order
        self.snapshot_calls = 0
        self.usage_window_calls = 0

    def collect_snapshot(self, windows, *, engaged_only=True):
        self.snapshot_calls += 1
        self.invocation_order.append(self.agent_name)
        return super().collect_snapshot(windows, engaged_only=engaged_only)

    def collect_usage_for_windows(self, windows, by_model_by_window):
        self.usage_window_calls += 1
        turns = {key: 0 for key in windows}
        for timestamp, model, input_tokens, cache_creation, cache_read, output in (
            self._usage_events
        ):
            for key, (since, until) in windows.items():
                if not since <= timestamp <= until:
                    continue
                usage = by_model_by_window[key].setdefault(
                    model, ModelUsage(model=model)
                )
                usage.turns += 1
                usage.input_tokens += input_tokens
                usage.cache_creation += cache_creation
                usage.cache_read += cache_read
                usage.output_tokens += output
                turns[key] += 1
        return turns


def _install_specs(monkeypatch, specs: list[AgentProviderSpec]) -> None:
    monkeypatch.setattr(
        providers,
        "_PROVIDER_SPECS",
        {spec.name: spec for spec in specs},
    )


def test_legacy_adapter_scans_enclosing_sessions_and_preserves_identity():
    spanning = _session(
        "legacy",
        "spanning",
        BOUNDARY - timedelta(minutes=1),
        BOUNDARY + timedelta(minutes=1),
    )
    provider = _LegacyProvider(
        "legacy",
        [
            _session(
                "legacy",
                "previous",
                PREVIOUS_START + timedelta(minutes=1),
                PREVIOUS_START + timedelta(minutes=2),
            ),
            spanning,
            _session(
                "legacy",
                "current",
                BOUNDARY + timedelta(minutes=1),
                BOUNDARY + timedelta(minutes=2),
            ),
        ],
        [(BOUNDARY, "same-model", 1, 2, 3, 4)],
    )

    record = provider.collect_snapshot(WINDOWS)

    assert isinstance(record, ProviderRecord)
    assert provider.session_calls == [(PREVIOUS_START, CURRENT_END, True)]
    assert provider.usage_calls == [
        (PREVIOUS_START, BOUNDARY),
        (BOUNDARY, CURRENT_END),
    ]
    assert [s.session_id for s in record.sessions_by_window["previous"]] == [
        "previous",
        "spanning",
    ]
    assert [s.session_id for s in record.sessions_by_window["current"]] == [
        "spanning",
        "current",
    ]
    assert record.sessions_by_window["previous"][1] is (
        record.sessions_by_window["current"][0]
    )
    assert record.assistant_turns_by_window == {"previous": 1, "current": 1}


def test_aggregator_calls_each_selected_provider_once_in_registry_order(
    monkeypatch,
):
    invocation_order: list[str] = []
    instances: dict[str, list[_WindowCountingProvider]] = {
        "alpha": [],
        "beta": [],
    }

    def factory(agent: str):
        def make() -> _WindowCountingProvider:
            provider = _WindowCountingProvider(
                agent,
                [
                    _session(
                        agent,
                        f"{agent}-session",
                        BOUNDARY + timedelta(minutes=1),
                        BOUNDARY + timedelta(minutes=2),
                    )
                ],
                [],
                invocation_order=invocation_order,
            )
            instances[agent].append(provider)
            return provider

        return make

    _install_specs(
        monkeypatch,
        [
            AgentProviderSpec("alpha", "Alpha", factory("alpha"), "complete"),
            AgentProviderSpec("beta", "Beta", factory("beta"), "complete"),
        ],
    )

    snapshot = collect_provider_snapshot(WINDOWS)

    assert isinstance(snapshot, ProviderSnapshot)
    assert [record.agent for record in snapshot.records] == ["alpha", "beta"]
    assert invocation_order == ["alpha", "beta"]
    assert {name: len(value) for name, value in instances.items()} == {
        "alpha": 1,
        "beta": 1,
    }
    for provider_instances in instances.values():
        provider = provider_instances[0]
        assert provider.snapshot_calls == 1
        assert provider.session_calls == [(PREVIOUS_START, CURRENT_END, True)]
        assert provider.usage_window_calls == 1

    invocation_order.clear()
    filtered = collect_provider_snapshot(WINDOWS, agent="beta")
    assert [record.agent for record in filtered.records] == ["beta"]
    assert invocation_order == ["beta"]


def test_usage_boundary_same_model_merge_and_session_derived_coverage(
    monkeypatch,
):
    alpha = _WindowCountingProvider(
        "alpha",
        [
            _session(
                "alpha",
                "alpha-current",
                BOUNDARY + timedelta(minutes=1),
                BOUNDARY + timedelta(minutes=2),
            )
        ],
        [(BOUNDARY, "shared-model", 10, 20, 30, 40)],
        invocation_order=[],
    )
    beta = _WindowCountingProvider(
        "beta",
        [
            _session(
                "beta",
                "beta-previous",
                BOUNDARY - timedelta(minutes=2),
                BOUNDARY - timedelta(minutes=1),
            )
        ],
        [(BOUNDARY, "shared-model", 1, 2, 3, 4)],
        invocation_order=[],
    )
    _install_specs(
        monkeypatch,
        [
            AgentProviderSpec("alpha", "Alpha", lambda: alpha, "partial"),
            AgentProviderSpec("beta", "Beta", lambda: beta, "complete"),
        ],
    )

    snapshot = collect_provider_snapshot(WINDOWS)

    assert snapshot.windows == WINDOWS
    for key in ("previous", "current"):
        report = snapshot.usage_by_window[key]
        assert report.assistant_turns == 2
        assert report.by_model["shared-model"] == ModelUsage(
            model="shared-model",
            turns=2,
            input_tokens=11,
            cache_creation=22,
            cache_read=33,
            output_tokens=44,
        )
    # Both providers' single usage event sits exactly at BOUNDARY, which is
    # inclusive in both adjacent windows (see the boundary-merge assertion
    # above: each window's "shared-model" total is alpha's 10 + beta's 1).
    # alpha's only *session* is in "current" and beta's only session is in
    # "previous", but coverage must still name both providers in both
    # windows — a provider with real usage in a window must appear in that
    # window's provider_coverage even where it has no session at all, or the
    # totals above would show tokens spent by a provider named nowhere.
    assert snapshot.usage_by_window["previous"].provider_coverage == {
        "alpha": "partial", "beta": "complete",
    }
    assert snapshot.usage_by_window["current"].provider_coverage == {
        "alpha": "partial", "beta": "complete",
    }
    assert snapshot.usage_by_window["previous"].incomplete_agents == ["alpha"]
    assert snapshot.usage_by_window["current"].incomplete_agents == ["alpha"]
    assert (
        snapshot.usage_by_window["previous"].by_model["shared-model"]
        is not snapshot.usage_by_window["current"].by_model["shared-model"]
    )


def test_non_engaged_session_with_real_usage_still_names_its_provider(
    monkeypatch,
):
    """A provider's only session in a window can fail `SessionStat.engaged`
    (one quick exchange — a single user message under a minute) while a
    real, token-bearing turn from that same session still lands in the
    window through the provider's own usage scan, which never filters by
    engagement.

    Before the fix, `active_agents` was derived only from
    `sessions_by_window` (itself filtered to engaged sessions by
    `collect_snapshot`'s `engaged_only=True` default), so this provider
    disappeared from `provider_coverage` entirely while its tokens/turns
    were still merged into `by_model`/`assistant_turns` — real spend with
    no provider named for it, and `usage_complete` reading True by vacuous
    truth over an empty `provider_coverage` dict. Same bug class as
    `test_a_period_reached_only_by_a_crossing_session_reports_its_provider`
    in `tests/test_trend_windows.py` ("a period could show tokens spent by
    nobody"), but via engagement filtering rather than start-time bucketing.
    """
    quiet_session = replace(
        _session(
            "quiet",
            "quiet-quick-exchange",
            BOUNDARY + timedelta(minutes=1),
            BOUNDARY + timedelta(minutes=1, seconds=5),
        ),
        user_msg_count=1,
        active_sec=5,
    )
    assert quiet_session.engaged is False

    quiet = _WindowCountingProvider(
        "quiet",
        [quiet_session],
        [
            (
                BOUNDARY + timedelta(minutes=1, seconds=2),
                "quiet-model",
                100, 0, 0, 50,
            )
        ],
        invocation_order=[],
    )
    _install_specs(
        monkeypatch,
        [AgentProviderSpec("quiet", "Quiet", lambda: quiet, "partial")],
    )

    snapshot = collect_provider_snapshot(WINDOWS)

    # The session really did fail engagement and is absent from the window.
    assert snapshot.sessions_by_window["current"] == []
    # ...yet its provider's real usage is already in the aggregate totals.
    report = snapshot.usage_by_window["current"]
    assert report.assistant_turns == 1
    assert report.by_model["quiet-model"].output_tokens == 50

    # The fix: the provider must be named, and usage_complete must reflect
    # its non-complete coverage rather than defaulting to True over an
    # empty dict.
    assert report.provider_coverage == {"quiet": "partial"}
    assert report.usage_complete is False
    assert report.incomplete_agents == ["quiet"]

    # The "previous" window has none of this provider's usage — coverage
    # must stay scoped to the window usage actually landed in.
    assert snapshot.usage_by_window["previous"].provider_coverage == {}
    assert snapshot.usage_by_window["previous"].usage_complete is True


def test_default_metrics_fail_closed_and_carry_no_sensitive_identifiers():
    metrics = SnapshotMetrics()
    record = _LegacyProvider("legacy", [], []).collect_snapshot(WINDOWS)

    assert record.metrics == metrics
    assert asdict(metrics) == {
        "sources_enumerated": None,
        "source_opens": None,
        "records_parsed": None,
        "companion_reads": None,
        "record_inventory_complete": False,
    }
    serialized = json.dumps(asdict(metrics), sort_keys=True)
    for forbidden in (
        "transcript",
        "excerpt",
        "path",
        "session_id",
        "project",
    ):
        assert forbidden not in serialized


def test_claude_snapshot_single_pass_matches_legacy_facts(
    jsonl_factory,
    monkeypatch,
):
    """The snapshot parses every transcript once, plus one bounded reopen per
    boundary crossed by a boundary-spanning session.

    Window purity (#188/#200) requires bounded evidence for any session that
    crosses a report-window boundary, and that evidence can only come from
    rereading the transcript for that specific window. The #188 brief
    sanctions this exactly: "reuse the existing provider snapshot for
    ordinary sessions and reopen only boundary-crossing transcripts to build
    bounded evidence." So the single-pass guarantee below is now two
    guarantees, both load-bearing:

    * a transcript that never crosses a report-window boundary (every source
      here except ``spanning``) is still opened exactly once — the ordinary
      case must not regress into rereading;
    * ``spanning-session`` crosses both the "previous" and "current"
      boundaries, so it is opened once for the initial parse plus once more
      per window it crosses (2), for 3 total — never more than the windows it
      actually crosses.
    """

    def timestamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    previous = jsonl_factory(
        "previous-project",
        "previous-session",
        [
            make_user_msg(
                "Start previous work",
                timestamp(PREVIOUS_START + timedelta(hours=1)),
            ),
            make_assistant_msg(
                "Previous result",
                timestamp(PREVIOUS_START + timedelta(hours=1, minutes=1)),
                "previous-assistant",
                model="claude-sonnet-4-6",
                input_tokens=200,
                cache_creation=20,
                cache_read=40,
                output_tokens=60,
            ),
            make_user_msg(
                "Confirm previous result",
                timestamp(PREVIOUS_START + timedelta(hours=1, minutes=2)),
            ),
        ],
    )
    with previous.open("a", encoding="utf-8") as handle:
        handle.write("{malformed-json\n")

    boundary_assistant = make_assistant_msg(
        "Boundary result",
        timestamp(BOUNDARY),
        "boundary-assistant",
        model="claude-opus-4-7",
        input_tokens=100,
        cache_creation=10,
        cache_read=20,
        output_tokens=30,
    )
    spanning = jsonl_factory(
        "spanning-project",
        "spanning-session",
        [
            make_user_msg(
                "Start spanning work",
                timestamp(BOUNDARY - timedelta(minutes=1)),
            ),
            boundary_assistant,
            # Streaming duplicate: a session record, but one exact usage turn.
            boundary_assistant,
            make_user_msg(
                "Finish spanning work",
                timestamp(BOUNDARY + timedelta(minutes=1)),
            ),
        ],
    )
    unengaged = jsonl_factory(
        "current-project",
        "unengaged-session",
        [
            make_user_msg(
                "One short request",
                timestamp(BOUNDARY + timedelta(days=1)),
            ),
            make_assistant_msg(
                "Short reply",
                timestamp(BOUNDARY + timedelta(days=1, seconds=10)),
                "current-assistant",
                input_tokens=300,
                output_tokens=50,
            ),
        ],
    )
    short_subagent = jsonl_factory(
        "spanning-project/subagents",
        "short-subagent",
        [
            make_assistant_msg(
                "Delegated result",
                timestamp(BOUNDARY + timedelta(days=2)),
                "short-subagent-assistant",
                model="claude-sonnet-4-6",
                input_tokens=400,
                output_tokens=70,
            )
        ],
    )
    nested_subagent = jsonl_factory(
        "spanning-project/parent-session/subagents",
        "nested-subagent",
        [
            make_assistant_msg(
                "Nested delegated result",
                timestamp(BOUNDARY + timedelta(days=3)),
                "nested-subagent-assistant",
                model="claude-haiku-4-5-20251001",
                input_tokens=500,
                output_tokens=80,
            )
        ],
    )
    sources = [
        previous,
        spanning,
        unengaged,
        short_subagent,
        nested_subagent,
    ]
    def _record_count(path: Path) -> int:
        return sum(
            1
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    # Both counts are taken before the counting monkeypatches below, since
    # reading a transcript here would otherwise register as a source open.
    parse_attempts = sum(_record_count(path) for path in sources)
    spanning_records = _record_count(spanning)

    legacy_sessions = collect_sessions_for_windows(
        WINDOWS,
        agent="claude",
    )
    legacy_models = {key: {} for key in WINDOWS}
    legacy_turns = ClaudeCodeProvider().collect_usage_for_windows(
        WINDOWS,
        legacy_models,
    )
    legacy_usage = collect_usage_for_windows(
        WINDOWS,
        agent="claude",
        active_agents_by_window={
            key: {session.agent for session in sessions}
            for key, sessions in legacy_sessions.items()
        },
    )
    unfiltered_sessions = collect_sessions_for_windows(
        WINDOWS,
        engaged_only=False,
        agent="claude",
    )
    unfiltered_snapshot = ClaudeCodeProvider().collect_snapshot(
        WINDOWS,
        engaged_only=False,
    )
    assert unfiltered_snapshot.sessions_by_window == unfiltered_sessions
    assert {
        session.session_id
        for session in unfiltered_snapshot.sessions_by_window["current"]
    } == {"spanning-session", "unengaged-session"}

    opened: list[Path] = []
    parse_calls = 0
    glob_patterns: list[str] = []
    original_open = Path.open
    original_loads = claude_module.json.loads
    original_glob = claude_module.glob.glob

    def counted_open(path: Path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path in sources and "r" in mode:
            opened.append(path)
        return original_open(path, *args, **kwargs)

    def counted_loads(payload, *args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_loads(payload, *args, **kwargs)

    def counted_glob(pattern: str):
        glob_patterns.append(pattern)
        return original_glob(pattern)

    monkeypatch.setattr(Path, "open", counted_open)
    monkeypatch.setattr(claude_module.json, "loads", counted_loads)
    monkeypatch.setattr(claude_module.glob, "glob", counted_glob)

    provider = ClaudeCodeProvider()
    snapshot = collect_provider_snapshot(WINDOWS, agent="claude")
    record = snapshot.records[0]

    assert glob_patterns == [
        str(provider.projects_dir / "*" / "*.jsonl"),
        str(provider.projects_dir / "*" / "subagents" / "*.jsonl"),
        str(provider.projects_dir / "*" / "*" / "subagents" / "*.jsonl"),
    ]
    # "previous" and "current" both cut this session, so both ask it for
    # bounded evidence. Every other transcript is still opened exactly once.
    windows_crossed = 2
    assert Counter(opened) == Counter(
        {
            path: (1 + windows_crossed if path == spanning else 1)
            for path in sources
        }
    )
    assert (
        parse_calls == parse_attempts + windows_crossed * spanning_records
    )
    assert record.by_model_by_window == legacy_models
    assert record.assistant_turns_by_window == legacy_turns
    assert snapshot.usage_by_window == legacy_usage

    # The provider record stays raw: window purity is layered on top of it by
    # the registry, so no adapter has to know that slicing exists.
    assert record.sessions_by_window == legacy_sessions
    assert {
        session.session_id
        for session in record.sessions_by_window["previous"]
    } == {"previous-session", "spanning-session"}
    assert {
        session.session_id
        for session in record.sessions_by_window["current"]
    } == {"spanning-session"}

    # The registry snapshot covers the same physical sessions, but only the
    # crossing one is converted — a contained session is byte-identical to
    # what the raw overlap primitive returns.
    for key in WINDOWS:
        snapshot_sessions = snapshot.sessions_by_window[key]
        assert {
            public_session_id(session) for session in snapshot_sessions
        } == {session.session_id for session in legacy_sessions[key]}
        assert {
            session.physical_session_id
            for session in snapshot_sessions
            if isinstance(session, SessionSlice)
        } == {"spanning-session"}
        assert [
            session
            for session in snapshot_sessions
            if not isinstance(session, SessionSlice)
        ] == [
            session
            for session in legacy_sessions[key]
            if session.session_id != "spanning-session"
        ]
    assert record.assistant_turns_by_window == {
        "previous": 2,
        "current": 4,
    }
    assert record.metrics == SnapshotMetrics(
        sources_enumerated=5,
        source_opens=5,
        records_parsed=parse_attempts - 1,
        companion_reads=0,
        record_inventory_complete=False,
    )


def test_claude_snapshot_marks_clean_inventory_complete(jsonl_factory):
    jsonl_factory(
        "clean-project",
        "clean-session",
        [
            make_user_msg(
                "Start clean snapshot",
                (BOUNDARY + timedelta(minutes=1)).isoformat(),
            ),
            make_assistant_msg(
                "Clean result",
                (BOUNDARY + timedelta(minutes=2)).isoformat(),
                "clean-assistant",
            ),
            make_user_msg(
                "Confirm clean result",
                (BOUNDARY + timedelta(minutes=3)).isoformat(),
            ),
            {"type": "progress", "payload": {"status": "irrelevant"}},
        ],
    )

    record = ClaudeCodeProvider().collect_snapshot(WINDOWS)

    assert record.metrics == SnapshotMetrics(
        sources_enumerated=1,
        source_opens=1,
        records_parsed=4,
        companion_reads=0,
        record_inventory_complete=True,
    )


def test_claude_snapshot_relevant_schema_corruption_fails_closed(
    jsonl_factory,
):
    jsonl_factory(
        "clean-project/subagents",
        "corrupt-usage",
        [
            {
                "type": "assistant",
                # Exact usage is advertised, but cannot be event-windowed
                # without the missing timestamp.
                "message": {
                    "role": "assistant",
                    "id": "corrupt-assistant",
                    "model": "claude-opus-4-7",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                    },
                },
            }
        ],
    )

    record = ClaudeCodeProvider().collect_snapshot(WINDOWS)

    assert record.by_model_by_window == {"previous": {}, "current": {}}
    assert record.assistant_turns_by_window == {
        "previous": 0,
        "current": 0,
    }
    assert record.metrics == SnapshotMetrics(
        sources_enumerated=1,
        source_opens=1,
        records_parsed=1,
        companion_reads=0,
        record_inventory_complete=False,
    )


def test_claude_snapshot_deduplicates_paths_and_counts_failed_open_attempt(
    jsonl_factory,
    monkeypatch,
):
    source = jsonl_factory(
        "deduplicated-project",
        "deduplicated-session",
        [
            make_user_msg(
                "This source will become unreadable",
                (BOUNDARY + timedelta(minutes=1)).isoformat(),
            )
        ],
    )
    open_attempts = 0
    original_open = Path.open

    def duplicate_glob(_pattern: str):
        return [str(source)]

    def failed_open(path: Path, *args, **kwargs):
        nonlocal open_attempts
        if path == source:
            open_attempts += 1
            raise OSError("synthetic read failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(claude_module.glob, "glob", duplicate_glob)
    monkeypatch.setattr(Path, "open", failed_open)

    record = ClaudeCodeProvider().collect_snapshot(WINDOWS)

    assert open_attempts == 1
    assert record.sessions_by_window == {"previous": [], "current": []}
    assert record.metrics == SnapshotMetrics(
        sources_enumerated=1,
        source_opens=1,
        records_parsed=0,
        companion_reads=0,
        record_inventory_complete=False,
    )


def test_snapshot_preserves_codex_cumulative_branch_lineage(codex_factory):
    def token_count(
        timestamp: str,
        *,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
    ) -> dict:
        return {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached_input_tokens,
                        "cache_write_input_tokens": 0,
                        "output_tokens": output_tokens,
                    }
                },
            },
        }

    parent_id = "parent-branch"
    codex_factory(
        parent_id,
        [
            {
                "timestamp": "2026-07-22T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "session_id": parent_id,
                    "id": parent_id,
                    "cwd": "/synthetic/app",
                },
            },
            {
                "timestamp": "2026-07-22T12:01:00Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-terra"},
            },
            token_count(
                "2026-07-22T12:02:00Z",
                input_tokens=100,
                cached_input_tokens=20,
                output_tokens=10,
            ),
            token_count(
                "2026-07-22T12:03:00Z",
                input_tokens=300,
                cached_input_tokens=60,
                output_tokens=30,
            ),
        ],
    )
    codex_factory(
        "child-branch",
        [
            {
                "timestamp": "2026-07-22T12:04:00Z",
                "type": "session_meta",
                "payload": {
                    "session_id": parent_id,
                    "id": "child-branch",
                    "parent_thread_id": parent_id,
                    "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                    "cwd": "/synthetic/app",
                },
            },
            {
                "timestamp": "2026-07-22T12:04:01Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol"},
            },
            # Copied ancestor baseline: not child usage.
            token_count(
                "2026-07-22T12:04:02Z",
                input_tokens=100,
                cached_input_tokens=20,
                output_tokens=10,
            ),
            # Branch-local delta: 300 fresh + 100 cached + 60 output.
            token_count(
                "2026-07-22T12:05:00Z",
                input_tokens=500,
                cached_input_tokens=120,
                output_tokens=70,
            ),
        ],
    )

    snapshot = collect_provider_snapshot(
        {
            "window": (
                datetime(2026, 7, 1, tzinfo=timezone.utc),
                datetime(2026, 7, 31, tzinfo=timezone.utc),
            )
        },
        agent="codex",
    )
    usage = snapshot.usage_by_window["window"]

    assert usage.total_input == 540
    assert usage.total_cache_read == 160
    assert usage.total_output == 90
    assert usage.assistant_turns == 3
    assert usage.by_model["gpt-5.6-terra"].output_tokens == 30
    assert usage.by_model["gpt-5.6-sol"].output_tokens == 60


def test_build_recap_consumes_one_current_previous_snapshot(
    jsonl_factory,
    monkeypatch,
):
    now = datetime.now(timezone.utc)

    def timestamp(minutes_ago: int) -> str:
        return (now - timedelta(minutes=minutes_ago)).isoformat().replace(
            "+00:00", "Z"
        )

    jsonl_factory(
        "synthetic-project",
        "snapshot-recap",
        [
            make_user_msg("Start snapshot work", timestamp(4)),
            make_assistant_msg("Working", timestamp(3), "snapshot-a1"),
            make_user_msg("Finish the compatibility test", timestamp(2)),
            make_assistant_msg("Done", timestamp(1), "snapshot-a2"),
        ],
    )
    calls: list[dict[str, tuple[datetime, datetime]]] = []
    real_collect = providers.collect_provider_snapshot

    def counted_collect(windows, **kwargs):
        calls.append(dict(windows))
        return real_collect(windows, **kwargs)

    monkeypatch.setattr(recap, "collect_provider_snapshot", counted_collect)

    result = recap.build_recap(
        "week",
        minimal=True,
        artifacts=False,
        classify="folder",
        write_report=False,
    )

    assert result.sessions
    assert len(calls) == 1
    assert list(calls[0]) == ["current", "previous"]


def _public_projections(key: str, sessions, usage) -> tuple:
    """Render one window through every public surface at once.

    Markdown, JSON, and MCP are three independent projections of the same
    facts, and a divergence that shows up in only one of them is a bug. Any
    equivalence or divergence claim below is therefore made about all three.
    """

    rollups = rollup_by_category(sessions)
    markdown = render_report(
        key, *WINDOWS[key], sessions, rollups, usage, {}, agent="legacy",
    )
    payload = build_report_json(
        key, *WINDOWS[key], sessions, rollups, usage, {}, agent="legacy",
    )
    pytest.importorskip("mcp")
    from ccstory.mcp_server import _compact_recap

    compact = _compact_recap(
        SimpleNamespace(
            agent="legacy",
            label=key,
            since=WINDOWS[key][0],
            until=WINDOWS[key][1],
            sessions=sessions,
            rollups=rollups,
            summaries={},
            category_narratives={},
            overall_narrative=None,
            usage=usage,
            report_path=None,
            narrative_provenance={},
        )
    )
    return markdown, payload, compact


def _legacy_snapshot_and_primitive(monkeypatch, sessions: list[SessionStat]):
    """Drive both the raw overlap primitive and the registry snapshot."""

    def factory() -> _LegacyProvider:
        return _LegacyProvider(
            "legacy",
            sessions,
            [(BOUNDARY, "shared-model", 5, 6, 7, 8)],
        )

    _install_specs(
        monkeypatch,
        [AgentProviderSpec("legacy", "Legacy", factory, "complete")],
    )

    primitive_sessions = collect_sessions_for_windows(WINDOWS, agent="legacy")
    primitive_usage = collect_usage_for_windows(
        WINDOWS,
        agent="legacy",
        active_agents_by_window={
            key: {item.agent for item in window_sessions}
            for key, window_sessions in primitive_sessions.items()
        },
    )
    snapshot = collect_provider_snapshot(WINDOWS, agent="legacy")
    return primitive_sessions, primitive_usage, snapshot


def test_snapshot_matches_public_helpers_for_contained_sessions(monkeypatch):
    """A session fully inside its window renders identically either way.

    ``collect_sessions()`` / ``collect_sessions_for_windows()`` stay raw
    overlap primitives — that is their documented, semi-stable public
    contract (#110), and window purity (#188) is a property of the
    snapshot/reporting layer instead. The two therefore agree everywhere
    except on a genuinely boundary-crossing session, and the overwhelmingly
    common contained case must stay byte-identical on every public surface.
    """

    contained = [
        _session(
            "legacy",
            "contained-previous",
            BOUNDARY - timedelta(days=1),
            BOUNDARY - timedelta(days=1) + timedelta(minutes=1),
        ),
        _session(
            "legacy",
            "contained-current",
            BOUNDARY + timedelta(days=1),
            BOUNDARY + timedelta(days=1, minutes=1),
        ),
    ]
    primitive_sessions, primitive_usage, snapshot = (
        _legacy_snapshot_and_primitive(monkeypatch, contained)
    )

    for key in WINDOWS:
        snapshot_sessions = snapshot.sessions_by_window[key]
        # Nothing crossed, so nothing was converted — asserted directly so
        # this cannot pass vacuously on an empty window.
        assert len(snapshot_sessions) == 1
        assert not any(
            isinstance(session, SessionSlice) for session in snapshot_sessions
        )
        assert snapshot_sessions == primitive_sessions[key]

        assert _public_projections(
            key, primitive_sessions[key], primitive_usage[key]
        ) == _public_projections(
            key, snapshot_sessions, snapshot.usage_by_window[key]
        )


def test_snapshot_diverges_from_public_helpers_for_crossing_sessions(
    monkeypatch,
):
    """A crossing session is exactly where the two must stop agreeing.

    The primitive reports the whole session in both windows; the snapshot
    reports each window's own bounded facts. ``_LegacyProvider`` implements
    no bounded extraction — like any third-party adapter written before 0.8 —
    so the honest answer here is bounded time with an explicitly-unknown
    message count, never the full-session count republished twice.

    Public identity must survive the divergence: the slice carries an
    internal evidence id, but every public surface still shows the physical
    session id the primitive returns.
    """

    crossing = _session(
        "legacy",
        "crossing",
        BOUNDARY - timedelta(minutes=1),
        BOUNDARY + timedelta(minutes=1),
    )
    primitive_sessions, primitive_usage, snapshot = (
        _legacy_snapshot_and_primitive(monkeypatch, [crossing])
    )

    for key in WINDOWS:
        assert [
            session.session_id for session in primitive_sessions[key]
        ] == ["crossing"]

        snapshot_sessions = snapshot.sessions_by_window[key]
        assert len(snapshot_sessions) == 1
        sliced = snapshot_sessions[0]
        assert isinstance(sliced, SessionSlice)
        assert sliced.physical_session_id == "crossing"
        assert sliced.session_id != "crossing"
        assert sliced.msg_count == 0

        old_markdown, old_payload, old_compact = _public_projections(
            key, primitive_sessions[key], primitive_usage[key]
        )
        new_markdown, new_payload, new_compact = _public_projections(
            key, snapshot_sessions, snapshot.usage_by_window[key]
        )

        # The divergence is visible, and it is the message count — the fact
        # the primitive double-counts and the snapshot no longer does.
        assert old_payload["totals"]["messages"] == crossing.msg_count
        assert new_payload["totals"]["messages"] == 0
        assert old_markdown != new_markdown
        assert old_compact != new_compact

        # ... but identity does not diverge. No internal id is published.
        assert [item["id"] for item in new_payload["sessions"]] == ["crossing"]
        assert "slice-" not in json.dumps(new_payload, default=str)
        assert "slice-" not in new_markdown
        assert "slice-" not in json.dumps(new_compact, default=str)
