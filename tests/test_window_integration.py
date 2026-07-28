"""End-to-end integration tests for window-pure snapshots (#188, 0.8 wrap-up).

``tests/test_session_identity.py`` and ``tests/test_session_slice.py`` prove
the ``SessionSlice`` contract against hand-built ``SessionStat``/``WindowEvidence``
objects, and ``tests/test_window_evidence.py`` proves each bundled provider's
``extract_window_evidence`` in isolation. None of those exercise the actual
glue in ``ccstory.providers._window_bounded_sessions`` that
``collect_provider_snapshot()`` runs in production: real transcripts, parsed
by the real bundled providers, reopened for real bounded evidence, and handed
back through the public snapshot/recap surfaces.

These tests fill that gap. They write synthetic transcripts under the
autouse ``tmp_home`` fixture (see ``tests/conftest.py``) and drive everything
through ``collect_provider_snapshot`` / ``recap.build_recap`` — never by
constructing a ``SessionSlice`` by hand.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ccstory import recap, session_summarizer
from ccstory.providers import collect_provider_snapshot
from ccstory.providers.claude import ClaudeCodeProvider
from ccstory.session_identity import resolve_session_summaries
from ccstory.session_summarizer import SOURCE_GENERATED, SOURCE_PROVIDED
from ccstory.time_tracking import (
    SessionSlice,
    SessionStat,
    active_intervals_for_timestamps,
    clip_active_intervals,
    collect_sessions_for_windows,
)
from ccstory.token_usage import collect_usage_for_windows
from tests.conftest import make_assistant_msg, make_user_msg

BOUNDARY = datetime(2026, 7, 20, tzinfo=timezone.utc)
PREVIOUS_START = BOUNDARY - timedelta(days=7)
CURRENT_END = BOUNDARY + timedelta(days=7)
WINDOWS = {
    "previous": (PREVIOUS_START, BOUNDARY),
    "current": (BOUNDARY, CURRENT_END),
}


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


# Antigravity step-log record shapes. `tests/test_antigravity_provider.py`
# keeps equivalent helpers, but they are local to that file and anchor to a
# different base timestamp, so they cannot be reused around this BOUNDARY.
def _antigravity_user(text: str, offset: timedelta) -> dict:
    return {
        "step_index": int(offset.total_seconds()),
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": _iso(BOUNDARY + offset),
        "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>",
    }


def _antigravity_planner(text: str, offset: timedelta) -> dict:
    return {
        "step_index": int(offset.total_seconds()),
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": _iso(BOUNDARY + offset),
        "content": text,
    }


@pytest.fixture
def antigravity_factory(tmp_home: Path):
    """Write an Antigravity transcript (and its cwd companion DB) into $HOME."""

    def _make(
        session_id: str, records: list[dict], cwd: str | None = None,
    ) -> Path:
        brain_dir = (
            tmp_home
            / ".gemini"
            / "antigravity"
            / "brain"
            / session_id
            / ".system_generated"
            / "logs"
        )
        brain_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = brain_dir / "transcript.jsonl"
        with transcript_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

        if cwd:
            conv_dir = tmp_home / ".gemini" / "antigravity" / "conversations"
            conv_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(conv_dir / f"{session_id}.db")
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB);"
            )
            cursor.execute(
                "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
                (f"\x0a(file://{cwd}".encode("utf-8"),),
            )
            conn.commit()
            conn.close()

        return transcript_path

    return _make


# ----- 1. Per-provider boundary split ---------------------------------------


def test_claude_boundary_split_isolates_each_windows_evidence(jsonl_factory):
    """A Claude session that crosses the boundary must split cleanly.

    One request before the boundary, one outcome after it. Each window's
    slice must show only the message/evidence/excerpt that actually happened
    inside its own half-open range.
    """
    jsonl_factory(
        "claude-split-project",
        "claude-split-session",
        [
            make_user_msg(
                "CLAUDE_BEFORE_BOUNDARY_REQUEST",
                _iso(BOUNDARY - timedelta(minutes=1)),
            ),
            make_assistant_msg(
                "CLAUDE_AFTER_BOUNDARY_OUTCOME",
                _iso(BOUNDARY + timedelta(minutes=1)),
                "claude-split-a1",
            ),
        ],
    )

    snapshot = collect_provider_snapshot(WINDOWS, agent="claude")
    previous_sessions = snapshot.sessions_by_window["previous"]
    current_sessions = snapshot.sessions_by_window["current"]
    assert len(previous_sessions) == 1
    assert len(current_sessions) == 1
    previous_slice = previous_sessions[0]
    current_slice = current_sessions[0]
    assert isinstance(previous_slice, SessionSlice)
    assert isinstance(current_slice, SessionSlice)

    assert previous_slice.msg_count == 1
    assert current_slice.msg_count == 1
    assert "CLAUDE_BEFORE_BOUNDARY_REQUEST" in previous_slice.evidence_excerpt
    assert "CLAUDE_AFTER_BOUNDARY_OUTCOME" in current_slice.evidence_excerpt
    # Neither window's excerpt contains the other's text.
    assert "CLAUDE_AFTER_BOUNDARY_OUTCOME" not in previous_slice.evidence_excerpt
    assert "CLAUDE_BEFORE_BOUNDARY_REQUEST" not in current_slice.evidence_excerpt


def test_codex_boundary_split_isolates_each_windows_evidence(codex_factory):
    """Mirrors the Claude case above for the Codex adapter."""
    session_id = "019f8a2c-2df3-7f01-b55d-b8dcae9f9101"
    codex_factory(
        session_id,
        [
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": "/tmp/codex-split-demo"},
            },
            {
                "timestamp": _iso(BOUNDARY - timedelta(minutes=1)),
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "CODEX_BEFORE_BOUNDARY_REQUEST",
                },
            },
            {
                "timestamp": _iso(BOUNDARY + timedelta(minutes=1)),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "CODEX_AFTER_BOUNDARY_OUTCOME",
                },
            },
        ],
    )

    snapshot = collect_provider_snapshot(WINDOWS, agent="codex")
    previous_sessions = snapshot.sessions_by_window["previous"]
    current_sessions = snapshot.sessions_by_window["current"]
    assert len(previous_sessions) == 1
    assert len(current_sessions) == 1
    previous_slice = previous_sessions[0]
    current_slice = current_sessions[0]
    assert isinstance(previous_slice, SessionSlice)
    assert isinstance(current_slice, SessionSlice)

    assert "CODEX_BEFORE_BOUNDARY_REQUEST" in previous_slice.evidence_excerpt
    assert "CODEX_AFTER_BOUNDARY_OUTCOME" in current_slice.evidence_excerpt
    assert "CODEX_AFTER_BOUNDARY_OUTCOME" not in previous_slice.evidence_excerpt
    assert "CODEX_BEFORE_BOUNDARY_REQUEST" not in current_slice.evidence_excerpt


def test_antigravity_boundary_split_isolates_each_windows_evidence(
    antigravity_factory,
):
    """Mirrors the Claude case above for the Antigravity adapter.

    Completes the bundled-provider set the #188 acceptance list asks for: one
    synthetic boundary-crossing session per provider, each window seeing only
    its own evidence.
    """
    antigravity_factory(
        "076447e4-3f6c-42e0-ada8-e1ade38b7710",
        [
            _antigravity_user(
                "ANTIGRAVITY_BEFORE_BOUNDARY_REQUEST", -timedelta(minutes=1),
            ),
            _antigravity_planner(
                "ANTIGRAVITY_AFTER_BOUNDARY_OUTCOME", timedelta(minutes=1),
            ),
        ],
        cwd="/Users/x/antigravity-split-demo",
    )

    snapshot = collect_provider_snapshot(WINDOWS, agent="antigravity")
    previous_sessions = snapshot.sessions_by_window["previous"]
    current_sessions = snapshot.sessions_by_window["current"]
    assert len(previous_sessions) == 1
    assert len(current_sessions) == 1
    previous_slice = previous_sessions[0]
    current_slice = current_sessions[0]
    assert isinstance(previous_slice, SessionSlice)
    assert isinstance(current_slice, SessionSlice)

    assert previous_slice.msg_count == 1
    assert current_slice.msg_count == 1
    assert (
        "ANTIGRAVITY_BEFORE_BOUNDARY_REQUEST" in previous_slice.evidence_excerpt
    )
    assert (
        "ANTIGRAVITY_AFTER_BOUNDARY_OUTCOME" in current_slice.evidence_excerpt
    )
    assert (
        "ANTIGRAVITY_AFTER_BOUNDARY_OUTCOME"
        not in previous_slice.evidence_excerpt
    )
    assert (
        "ANTIGRAVITY_BEFORE_BOUNDARY_REQUEST"
        not in current_slice.evidence_excerpt
    )


# ----- 2. Time conservation ---------------------------------------------------


def test_active_seconds_conserved_across_adjacent_windows(jsonl_factory):
    """Splitting active time at a boundary must not create or lose seconds.

    ``[first request .. first outcome]`` closes into one active interval that
    starts before the boundary; ``[second request .. second outcome]`` closes
    into another that starts after it — and the gap between "first outcome"
    and "second request" is short enough (4 min, under the 5 min gap cap) to
    build one *third* interval that straddles the boundary itself, so this
    also proves a straddling interval gets split without double-counting or
    losing time. The expectation is derived from the same
    ``clip_active_intervals``/``active_intervals_for_timestamps`` helpers
    production code uses, not hardcoded.
    """
    path = jsonl_factory(
        "conservation-project",
        "conservation-session",
        [
            make_user_msg(
                "first request", _iso(BOUNDARY - timedelta(minutes=10)),
            ),
            make_assistant_msg(
                "first outcome",
                _iso(BOUNDARY - timedelta(minutes=2)),
                "conservation-a1",
            ),
            make_user_msg(
                "second request", _iso(BOUNDARY + timedelta(minutes=2)),
            ),
            make_assistant_msg(
                "second outcome",
                _iso(BOUNDARY + timedelta(minutes=10)),
                "conservation-a2",
            ),
        ],
    )
    provider = ClaudeCodeProvider()
    physical = provider.parse_session(path)
    assert physical is not None

    expected_intervals = clip_active_intervals(
        active_intervals_for_timestamps(physical.timestamps),
        WINDOWS["previous"][0],
        WINDOWS["current"][1],
    )
    expected_union_active_sec = int(
        sum(interval.end - interval.start for interval in expected_intervals)
    )

    snapshot = collect_provider_snapshot(WINDOWS, agent="claude")
    previous_slice = snapshot.sessions_by_window["previous"][0]
    current_slice = snapshot.sessions_by_window["current"][0]
    assert isinstance(previous_slice, SessionSlice)
    assert isinstance(current_slice, SessionSlice)

    # Non-trivial split: both windows actually carry a piece of the activity.
    assert previous_slice.active_sec > 0
    assert current_slice.active_sec > 0
    assert (
        previous_slice.active_sec + current_slice.active_sec
        == expected_union_active_sec
    )


# ----- 3. Contained sessions are untouched ------------------------------------


def test_contained_session_is_untouched_by_window_bounding(jsonl_factory):
    """A session fully inside its window keeps its plain ``SessionStat`` shape.

    ``_window_bounded_sessions`` only converts a boundary-crossing session; a
    contained one must come back with the exact facts a direct
    ``parse_session`` call would produce — proving the common case is not
    reshaped or reopened for no reason.
    """
    path = jsonl_factory(
        "contained-project",
        "contained-session",
        [
            make_user_msg(
                "only request", _iso(BOUNDARY + timedelta(days=1)),
            ),
            make_assistant_msg(
                "only outcome",
                _iso(BOUNDARY + timedelta(days=1, minutes=1)),
                "contained-a1",
            ),
            make_user_msg(
                "confirm outcome",
                _iso(BOUNDARY + timedelta(days=1, minutes=2)),
            ),
        ],
    )
    provider = ClaudeCodeProvider()
    independently_parsed = provider.parse_session(path)
    assert independently_parsed is not None

    snapshot = collect_provider_snapshot(WINDOWS, agent="claude")
    current_sessions = snapshot.sessions_by_window["current"]

    assert snapshot.sessions_by_window["previous"] == []
    assert len(current_sessions) == 1
    contained = current_sessions[0]
    assert isinstance(contained, SessionStat)
    assert not isinstance(contained, SessionSlice)
    assert contained == independently_parsed


# ----- 4. No public identity leak --------------------------------------------


def test_build_recap_never_leaks_an_internal_slice_id(jsonl_factory):
    """A full, unmocked ``build_recap()`` run must never publish a slice id.

    No monkeypatching of ``collect_provider_snapshot`` here — this exercises
    the real pipeline end to end. The session crosses the "week" boundary, so
    it becomes a ``SessionSlice`` internally; the public JSON/Markdown must
    still show only the physical session id.
    """
    now = datetime.now(timezone.utc)
    boundary = now - timedelta(days=7)

    jsonl_factory(
        "leak-check-project",
        "leak-check-session",
        [
            make_user_msg(
                "request before the boundary",
                _iso(boundary - timedelta(minutes=2)),
            ),
            make_assistant_msg(
                "outcome after the boundary",
                _iso(boundary + timedelta(minutes=2)),
                "leak-check-a1",
            ),
            make_user_msg(
                "confirm outcome", _iso(boundary + timedelta(minutes=3)),
            ),
        ],
    )

    result = recap.build_recap(
        "week",
        minimal=True,
        artifacts=False,
        classify="folder",
        write_report=False,
    )

    assert len(result.sessions) == 1
    sliced = result.sessions[0]
    assert isinstance(sliced, SessionSlice)
    assert sliced.physical_session_id == "leak-check-session"
    assert sliced.session_id.startswith("slice-")

    payload = result.to_json()
    assert [s["id"] for s in payload["sessions"]] == ["leak-check-session"]
    serialized = json.dumps(payload, default=str)
    assert "slice-" not in serialized
    assert "slice-" not in result.markdown


# ----- 5. Human record precedence survives integration -----------------------


def test_provided_summary_precedence_survives_the_real_snapshot_pipeline(
    jsonl_factory,
):
    """An externally ``provided`` summary about the physical session must
    outrank generated prose for every window slice
    ``collect_provider_snapshot`` derives from it — proven against real
    Claude-provider slices (parsed from a real transcript), not the hand-built
    ones in ``tests/test_session_identity.py``.
    """
    physical_id = "provided-precedence-session"
    jsonl_factory(
        "provided-precedence-project",
        physical_id,
        [
            make_user_msg(
                "request before boundary",
                _iso(BOUNDARY - timedelta(minutes=1)),
            ),
            make_assistant_msg(
                "outcome after boundary",
                _iso(BOUNDARY + timedelta(minutes=1)),
                "provided-precedence-a1",
            ),
            make_user_msg(
                "confirm outcome", _iso(BOUNDARY + timedelta(minutes=2)),
            ),
        ],
    )
    session_summarizer.upsert(physical_id, "a human wrote this", SOURCE_PROVIDED)

    snapshot = collect_provider_snapshot(WINDOWS, agent="claude")

    slice_ids: set[str] = set()
    for key in ("previous", "current"):
        sessions = snapshot.sessions_by_window[key]
        assert len(sessions) == 1
        sliced = sessions[0]
        assert isinstance(sliced, SessionSlice)
        assert sliced.physical_session_id == physical_id
        slice_ids.add(sliced.session_id)

        resolved = resolve_session_summaries([sliced])

        assert resolved[sliced.session_id].source == SOURCE_PROVIDED
        assert resolved[sliced.session_id].summary == "a human wrote this"

    # The two windows produced genuinely different bounded evidence (and
    # therefore different evidence/cache ids) — precedence is keyed off the
    # shared physical id, not an accidental id collision between windows.
    assert len(slice_ids) == 2


# ----- 6. Identity stability ---------------------------------------------------


def test_slice_identity_is_stable_unless_new_evidence_enters_the_window(
    jsonl_factory,
):
    """A slice id names its bounded facts, not the raw window endpoints.

    Rebuilding the identical window must reproduce the identical slice id.
    Widening ``until`` without capturing any new event must not rotate the id
    either (a moving ``until=now`` boundary across successive recap runs must
    not invalidate an unrelated cached slice) — but widening it far enough to
    capture a genuinely new in-window event must rotate it.
    """
    since = BOUNDARY - timedelta(minutes=30)
    jsonl_factory(
        "identity-stability-project",
        "identity-stability-session",
        [
            make_user_msg("first request", _iso(BOUNDARY)),
            make_assistant_msg(
                "first outcome",
                _iso(BOUNDARY + timedelta(minutes=1)),
                "identity-stability-a1",
            ),
            make_user_msg(
                "second request", _iso(BOUNDARY + timedelta(minutes=30)),
            ),
            make_assistant_msg(
                "second outcome",
                _iso(BOUNDARY + timedelta(minutes=60)),
                "identity-stability-a2",
            ),
        ],
    )

    def slice_id_for(until: datetime) -> str:
        snapshot = collect_provider_snapshot(
            {"window": (since, until)}, agent="claude",
        )
        sessions = snapshot.sessions_by_window["window"]
        assert len(sessions) == 1
        sliced = sessions[0]
        assert isinstance(sliced, SessionSlice)
        return sliced.session_id

    until_a = BOUNDARY + timedelta(minutes=7)
    until_b = BOUNDARY + timedelta(minutes=20)  # widened: no new event captured
    until_c = BOUNDARY + timedelta(minutes=35)  # widened: captures "second request"

    id_first_build = slice_id_for(until_a)
    id_rebuild_same_window = slice_id_for(until_a)
    assert id_rebuild_same_window == id_first_build

    id_widened_without_new_events = slice_id_for(until_b)
    assert id_widened_without_new_events == id_first_build

    id_widened_with_new_event = slice_id_for(until_c)
    assert id_widened_with_new_event != id_first_build


# ----- 7. Generated prose never crosses a window ------------------------------


def test_generated_prose_is_never_reused_across_a_boundary(jsonl_factory):
    """The CHANGELOG's central promise, asserted end to end.

    Two rows exist that a pre-0.8 pipeline would happily have shown in both
    windows: a full-session summary cached at the physical id, and one
    window's own generated summary. Neither may describe the other window.
    An externally ``provided`` row is the documented exception and is covered
    by the precedence test above.
    """
    physical_id = "prose-isolation-session"
    jsonl_factory(
        "prose-isolation-project",
        physical_id,
        [
            make_user_msg(
                "request before boundary", _iso(BOUNDARY - timedelta(minutes=1)),
            ),
            make_assistant_msg(
                "outcome after boundary",
                _iso(BOUNDARY + timedelta(minutes=1)),
                "prose-isolation-a1",
            ),
            make_user_msg(
                "confirm outcome", _iso(BOUNDARY + timedelta(minutes=2)),
            ),
        ],
    )
    # A full-session summary left behind by a pre-0.8 run, keyed by physical id.
    session_summarizer.upsert(
        physical_id, "prose about the whole session", SOURCE_GENERATED,
    )

    snapshot = collect_provider_snapshot(WINDOWS, agent="claude")
    previous_slice = snapshot.sessions_by_window["previous"][0]
    current_slice = snapshot.sessions_by_window["current"][0]
    assert isinstance(previous_slice, SessionSlice)
    assert isinstance(current_slice, SessionSlice)
    assert previous_slice.session_id != current_slice.session_id

    # Neither slice inherits the full-session prose: it describes facts that
    # partly lie outside whichever window asks for it.
    resolved = resolve_session_summaries([previous_slice, current_slice])
    assert resolved == {}

    # Now give exactly one window its own generated summary. It must stay
    # inside that window.
    session_summarizer.upsert(
        previous_slice.session_id,
        "prose about the previous window only",
        SOURCE_GENERATED,
    )
    resolved = resolve_session_summaries([previous_slice, current_slice])
    assert (
        resolved[previous_slice.session_id].summary
        == "prose about the previous window only"
    )
    assert current_slice.session_id not in resolved


# ----- 8. Usage attribution is untouched --------------------------------------


def test_window_bounding_does_not_change_token_or_cost_attribution(
    jsonl_factory,
):
    """Decision 5 of the #188 integration brief, asserted.

    Token and cost figures come from exact per-record usage events, never from
    session boundaries. Slicing a boundary-crossing session must therefore
    leave every usage number — including the per-provider coverage lane, which
    *is* derived from the (now sliced) session list — exactly as it was.
    """
    jsonl_factory(
        "usage-invariance-project",
        "usage-invariance-session",
        [
            make_user_msg(
                "request before boundary", _iso(BOUNDARY - timedelta(minutes=1)),
            ),
            make_assistant_msg(
                "outcome before boundary",
                _iso(BOUNDARY - timedelta(seconds=30)),
                "usage-invariance-a1",
                model="claude-sonnet-4-6",
                input_tokens=200,
                cache_creation=20,
                cache_read=40,
                output_tokens=60,
            ),
            make_assistant_msg(
                "outcome after boundary",
                _iso(BOUNDARY + timedelta(minutes=1)),
                "usage-invariance-a2",
                model="claude-opus-4-7",
                input_tokens=100,
                cache_creation=10,
                cache_read=20,
                output_tokens=30,
            ),
        ],
    )

    legacy_sessions = collect_sessions_for_windows(WINDOWS, agent="claude")
    legacy_usage = collect_usage_for_windows(
        WINDOWS,
        agent="claude",
        active_agents_by_window={
            key: {session.agent for session in sessions}
            for key, sessions in legacy_sessions.items()
        },
    )

    snapshot = collect_provider_snapshot(WINDOWS, agent="claude")

    # The session really is sliced in both windows — otherwise this asserts
    # invariance over a case the change never touched.
    for key in WINDOWS:
        assert isinstance(snapshot.sessions_by_window[key][0], SessionSlice)

    assert snapshot.usage_by_window == legacy_usage


# ----- 9. Honest degradation when bounded evidence is unavailable -------------


@pytest.mark.parametrize(
    "failure",
    ["returns_none", "raises"],
    ids=["unreadable_transcript", "third_party_adapter_raises"],
)
def test_unavailable_bounded_evidence_keeps_time_and_drops_prose(
    jsonl_factory, monkeypatch, caplog, failure,
):
    """An adapter that cannot prove bounded evidence must degrade honestly.

    Of the three candidate behaviors only one is defensible: dropping the
    session loses real active time, keeping the full ``SessionStat``
    republishes exactly the out-of-window facts this release removes, so the
    slice keeps bounded *time* (provable from the session's own timestamps)
    and reports ``msg_count == 0``, which #200 defines as explicitly unknown
    rather than a fabricated count.

    A third-party adapter is arbitrary code, so a raised exception must
    degrade the same way rather than abort the recap — but it must not be
    silent.
    """
    jsonl_factory(
        "degraded-project",
        "degraded-session",
        [
            make_user_msg(
                "request before boundary", _iso(BOUNDARY - timedelta(minutes=1)),
            ),
            make_assistant_msg(
                "outcome after boundary",
                _iso(BOUNDARY + timedelta(minutes=1)),
                "degraded-a1",
            ),
        ],
    )

    def unavailable(self, session, since, until):
        if failure == "raises":
            raise RuntimeError("third-party adapter blew up")
        return None

    monkeypatch.setattr(
        ClaudeCodeProvider, "extract_window_evidence", unavailable,
    )

    with caplog.at_level("WARNING", logger="ccstory.providers"):
        snapshot = collect_provider_snapshot(WINDOWS, agent="claude")

    for key in WINDOWS:
        sessions = snapshot.sessions_by_window[key]
        assert len(sessions) == 1
        degraded = sessions[0]
        assert isinstance(degraded, SessionSlice)
        # Time survives: it is derivable from the session's own timestamps.
        assert degraded.active_sec > 0
        # Prose does not: an unproven count or excerpt would be a fabrication.
        assert degraded.msg_count == 0
        assert degraded.user_msg_count == 0
        assert degraded.evidence_excerpt == ""

    if failure == "raises":
        assert "failed to extract window evidence" in caplog.text
    else:
        assert caplog.text == ""
