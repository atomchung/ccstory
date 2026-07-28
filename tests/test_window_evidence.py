"""Provider transcript evidence must remain inside the requested window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ccstory.providers.antigravity import AntigravityProvider
from ccstory.providers.claude import ClaudeCodeProvider
from ccstory.providers.codex import CodexProvider
from ccstory.providers.excerpts import ASSISTANT_EVIDENCE_CHARS
from ccstory.time_tracking import (
    active_intervals_for_timestamps,
    clip_active_intervals,
    session_slice_for_window,
)

from tests.conftest import write_jsonl


BASE = datetime(2026, 7, 28, 10, tzinfo=timezone.utc)


def _timestamp(minutes: int) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


# Antigravity step-log record shapes, adapted from the local `_ts`/`_user`/
# `_planner`/`antigravity_factory` helpers in tests/test_antigravity_provider.py
# to this file's plain `tmp_path`/`BASE` fixtures (that file's `antigravity_factory`
# fixture is local to it and anchors to a different base timestamp).
def _antigravity_ts(minutes: int) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


def _antigravity_user(text: str, minutes: int) -> dict:
    return {
        "step_index": minutes,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": _antigravity_ts(minutes),
        "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>",
    }


def _antigravity_planner(text: str, minutes: int) -> dict:
    return {
        "step_index": minutes,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": _antigravity_ts(minutes),
        "content": text,
    }


def _antigravity_transcript(
    tmp_path: Path, session_id: str, records: list[dict],
) -> Path:
    path = (
        tmp_path
        / "antigravity"
        / "brain"
        / session_id
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    return write_jsonl(path, records)


def test_claude_window_evidence_uses_only_bounded_authoritative_records(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "projects" / "demo" / "claude-window.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "user",
                "timestamp": _timestamp(-1),
                "message": {"content": "outside before"},
            },
            {
                "type": "user",
                "timestamp": _timestamp(0),
                "message": {"content": "window request"},
            },
            {
                "type": "assistant",
                "timestamp": _timestamp(1),
                "message": {"content": "window outcome"},
            },
            {
                "type": "user",
                "timestamp": _timestamp(2),
                "message": {"content": "<scheduled-task>noise</scheduled-task>"},
            },
            {
                "type": "user",
                "timestamp": _timestamp(3),
                "message": {"content": "tool_use_id injected noise"},
            },
            {
                "type": "assistant",
                "timestamp": _timestamp(4),
                "message": {"content": "outside until"},
            },
        ],
    )
    provider = ClaudeCodeProvider(tmp_path / "projects")
    session = provider.parse_session(transcript)
    assert session is not None

    evidence = provider.extract_window_evidence(
        session,
        BASE,
        BASE + timedelta(minutes=4),
    )

    assert evidence is not None
    assert evidence.timestamps == tuple(
        (BASE + timedelta(minutes=minute)).timestamp()
        for minute in range(4)
    )
    # All top-level user/assistant records count; only genuine user turns do.
    assert evidence.msg_count == 4
    assert evidence.user_msg_count == 1
    assert evidence.first_user_text == "window request"
    assert evidence.latest_user_text == "window request"
    assert evidence.final_assistant_text == "window outcome"
    assert "window request" in evidence.excerpt
    assert "window outcome" in evidence.excerpt
    assert "outside before" not in evidence.excerpt
    assert "outside until" not in evidence.excerpt
    assert "scheduled-task" not in evidence.excerpt
    assert "tool_use_id" not in evidence.excerpt
    assert evidence.active_intervals == clip_active_intervals(
        active_intervals_for_timestamps(session.timestamps),
        BASE,
        BASE + timedelta(minutes=4),
    )
    # This is the hand-off validation used by the integration layer.
    assert session_slice_for_window(
        session,
        BASE,
        BASE + timedelta(minutes=4),
        evidence=evidence,
    ) is not None


def test_provider_window_evidence_fails_closed_without_transcript(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "projects" / "demo" / "missing.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "user",
                "timestamp": _timestamp(0),
                "message": {"content": "cannot fall back"},
            },
        ],
    )
    provider = ClaudeCodeProvider(tmp_path / "projects")
    session = provider.parse_session(transcript)
    assert session is not None
    transcript.unlink()

    assert (
        provider.extract_window_evidence(
            session,
            BASE,
            BASE + timedelta(minutes=1),
        )
        is None
    )


def test_bundled_providers_fail_closed_without_a_backing_transcript(
    tmp_path: Path,
) -> None:
    """Every bundled adapter fails closed when its transcript disappears.

    Antigravity implemented its own bounded extraction in 0.8-D4 (mirroring
    Codex's 0.8-D3 adapter), so no bundled provider still relies on
    ``BaseAgentProvider.extract_window_evidence``'s ``None``-returning
    compatibility default. This replaces
    ``test_adapters_without_bounded_extraction_fail_closed``, which asserted
    Antigravity's fail-closed behavior only through that now-unused default.
    Each adapter must still fail closed, but now because its own extraction
    logic cannot prove which records belong to a window it can no longer
    read, not because it never implemented the hook.
    """
    claude_transcript = tmp_path / "claude" / "demo" / "missing.jsonl"
    write_jsonl(
        claude_transcript,
        [
            {
                "type": "user",
                "timestamp": _timestamp(0),
                "message": {"content": "cannot fall back"},
            },
        ],
    )
    claude_provider = ClaudeCodeProvider(tmp_path / "claude")
    claude_session = claude_provider.parse_session(claude_transcript)
    assert claude_session is not None
    claude_transcript.unlink()
    assert (
        claude_provider.extract_window_evidence(
            claude_session, BASE, BASE + timedelta(minutes=1),
        )
        is None
    )

    codex_session_id = "019f8a2c-2df3-7f01-b55d-b8dcae9f2599"
    codex_transcript = (
        tmp_path
        / "codex"
        / "sessions"
        / "2026"
        / "07"
        / "28"
        / f"rollout-2026-07-28T10-00-00-{codex_session_id}.jsonl"
    )
    write_jsonl(
        codex_transcript,
        [
            {
                "type": "session_meta",
                "payload": {"id": codex_session_id, "cwd": "/tmp/demo"},
            },
            {
                "timestamp": _timestamp(0),
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "cannot fall back"},
            },
        ],
    )
    codex_provider = CodexProvider(tmp_path / "codex")
    codex_session = codex_provider.parse_session(codex_transcript)
    assert codex_session is not None
    codex_transcript.unlink()
    assert (
        codex_provider.extract_window_evidence(
            codex_session, BASE, BASE + timedelta(minutes=1),
        )
        is None
    )

    antigravity_session_id = "076447e4-3f6c-42e0-ada8-e1ade38b7799"
    antigravity_transcript = _antigravity_transcript(
        tmp_path,
        antigravity_session_id,
        [_antigravity_user("cannot fall back", 0)],
    )
    antigravity_provider = AntigravityProvider(
        antigravity_dir=tmp_path / "antigravity"
    )
    antigravity_session = antigravity_provider.parse_session(antigravity_transcript)
    assert antigravity_session is not None
    antigravity_transcript.unlink()
    assert (
        antigravity_provider.extract_window_evidence(
            antigravity_session, BASE, BASE + timedelta(minutes=1),
        )
        is None
    )


def test_claude_window_evidence_preserves_final_assistant_tail(tmp_path: Path) -> None:
    transcript = tmp_path / "projects" / "demo" / "claude-tail.jsonl"
    final_outcome = "head " + ("x" * 700) + " FINAL_TEST_OUTCOME"
    write_jsonl(
        transcript,
        [
            {
                "type": "user",
                "timestamp": _timestamp(0),
                "message": {"content": "run the test"},
            },
            {
                "type": "assistant",
                "timestamp": _timestamp(1),
                "message": {"content": final_outcome},
            },
        ],
    )
    provider = ClaudeCodeProvider(tmp_path / "projects")
    session = provider.parse_session(transcript)
    assert session is not None

    evidence = provider.extract_window_evidence(
        session, BASE, BASE + timedelta(minutes=2)
    )

    assert evidence is not None
    assert evidence.final_assistant_text.endswith("FINAL_TEST_OUTCOME")
    assert "FINAL_TEST_OUTCOME" in evidence.excerpt


def test_claude_window_evidence_accepted_by_session_slice_for_boundary_crossing_session(
    tmp_path: Path,
) -> None:
    """A boundary-crossing session's evidence must hand off cleanly to the slice.

    This is the adapter-side half of the 0.8-D contract: the evidence built by
    `ClaudeCodeProvider.extract_window_evidence` for a session whose physical
    transcript extends before/after the requested window must pass
    `session_slice_for_window`'s validation (matching clipped physical
    intervals, a self-consistent fingerprint) and the resulting slice's
    bounded facts must come from the evidence, not the full session.
    """
    transcript = tmp_path / "projects" / "demo" / "claude-boundary.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "user",
                "timestamp": _timestamp(-10),
                "message": {"content": "before the window, must not leak"},
            },
            {
                "type": "assistant",
                "timestamp": _timestamp(-9),
                "message": {"content": "outside outcome, must not leak"},
            },
            {
                "type": "user",
                "timestamp": _timestamp(0),
                "message": {"content": "in-window request"},
            },
            {
                "type": "assistant",
                "timestamp": _timestamp(1),
                "message": {"content": "in-window outcome"},
            },
            {
                "type": "user",
                "timestamp": _timestamp(20),
                "message": {"content": "after the window, must not leak"},
            },
        ],
    )
    provider = ClaudeCodeProvider(tmp_path / "projects")
    session = provider.parse_session(transcript)
    assert session is not None
    # The physical session spans well outside the window we are about to ask
    # for, so the resulting slice must be boundary-clipped.
    since, until = BASE, BASE + timedelta(minutes=2)
    assert session.start < since
    assert session.end >= until

    evidence = provider.extract_window_evidence(session, since, until)
    assert evidence is not None

    expected_intervals = clip_active_intervals(
        active_intervals_for_timestamps(session.timestamps),
        since,
        until,
    )
    assert evidence.active_intervals == expected_intervals

    slice_ = session_slice_for_window(session, since, until, evidence=evidence)

    assert slice_ is not None
    assert slice_.boundary_clipped is True
    assert slice_.msg_count == evidence.msg_count == 2
    assert slice_.user_msg_count == evidence.user_msg_count == 1
    assert slice_.evidence_fingerprint == evidence.evidence_fingerprint
    assert slice_.active_intervals == expected_intervals
    assert "in-window request" in slice_.evidence_excerpt
    assert "in-window outcome" in slice_.evidence_excerpt
    assert "before the window" not in slice_.evidence_excerpt
    assert "outside outcome" not in slice_.evidence_excerpt
    assert "after the window" not in slice_.evidence_excerpt


def test_codex_window_evidence_preserves_activity_and_message_filters(
    tmp_path: Path,
) -> None:
    transcript = (
        tmp_path
        / "sessions"
        / "2026"
        / "07"
        / "28"
        / "rollout-2026-07-28T10-00-00-window.jsonl"
    )
    write_jsonl(
        transcript,
        [
            {
                "type": "session_meta",
                "payload": {"id": "window", "cwd": "/tmp/demo"},
            },
            {
                "timestamp": _timestamp(-1),
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "outside before"},
            },
            {
                "timestamp": _timestamp(0),
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "<task>window request</task>",
                },
            },
            {
                "timestamp": _timestamp(1),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "window outcome",
                },
            },
            {
                "timestamp": _timestamp(2),
                "type": "response_item",
                "payload": {"type": "function_call", "name": "pytest"},
            },
            {
                "timestamp": _timestamp(3),
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "activity only"},
            },
            {
                "timestamp": _timestamp(4),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "outside until",
                },
            },
        ],
    )
    provider = CodexProvider(tmp_path)
    session = provider.parse_session(transcript)
    assert session is not None

    evidence = provider.extract_window_evidence(
        session,
        BASE,
        BASE + timedelta(minutes=4),
    )

    assert evidence is not None
    assert evidence.timestamps == tuple(
        (BASE + timedelta(minutes=minute)).timestamp()
        for minute in range(4)
    )
    # The function call is a counted response item; agent_message is activity
    # only, exactly as in CodexProvider.parse_session().
    assert evidence.msg_count == 3
    assert evidence.user_msg_count == 1
    assert evidence.first_user_text == "window request"
    assert evidence.latest_user_text == "window request"
    assert evidence.final_assistant_text == "window outcome"
    assert "window request" in evidence.excerpt
    assert "window outcome" in evidence.excerpt
    assert "outside before" not in evidence.excerpt
    assert "outside until" not in evidence.excerpt
    assert evidence.active_intervals == clip_active_intervals(
        active_intervals_for_timestamps(session.timestamps),
        BASE,
        BASE + timedelta(minutes=4),
    )
    assert session_slice_for_window(
        session,
        BASE,
        BASE + timedelta(minutes=4),
        evidence=evidence,
    ) is not None


def test_codex_window_evidence_accepted_by_session_slice_for_boundary_crossing_session(
    codex_factory,
) -> None:
    """A boundary-crossing Codex session's evidence must hand off cleanly to the slice.

    Mirrors
    ``test_claude_window_evidence_accepted_by_session_slice_for_boundary_crossing_session``
    for the Codex adapter: a session whose physical transcript extends
    before/after the requested window must produce evidence that
    ``session_slice_for_window`` accepts (matching clipped physical
    intervals, a self-consistent fingerprint), and the resulting slice's
    bounded facts must come from the evidence, not the full session.
    """
    session_id = "019f8a2c-2df3-7f01-b55d-b8dcae9f2516"
    path = codex_factory(
        session_id,
        [
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": "/tmp/demo"},
            },
            {
                "timestamp": _timestamp(-10),
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "before the window, must not leak",
                },
            },
            {
                "timestamp": _timestamp(-9),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "outside outcome, must not leak",
                },
            },
            {
                "timestamp": _timestamp(0),
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "in-window request",
                },
            },
            {
                "timestamp": _timestamp(1),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "in-window outcome",
                },
            },
            {
                "timestamp": _timestamp(20),
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "after the window, must not leak",
                },
            },
        ],
    )
    provider = CodexProvider()
    session = provider.parse_session(path)
    assert session is not None
    # The physical session spans well outside the window we are about to ask
    # for, so the resulting slice must be boundary-clipped.
    since, until = BASE, BASE + timedelta(minutes=2)
    assert session.start < since
    assert session.end >= until

    evidence = provider.extract_window_evidence(session, since, until)
    assert evidence is not None

    expected_intervals = clip_active_intervals(
        active_intervals_for_timestamps(session.timestamps),
        since,
        until,
    )
    assert evidence.active_intervals == expected_intervals

    slice_ = session_slice_for_window(session, since, until, evidence=evidence)

    assert slice_ is not None
    assert slice_.boundary_clipped is True
    assert slice_.msg_count == evidence.msg_count == 2
    assert slice_.user_msg_count == evidence.user_msg_count == 1
    assert slice_.evidence_fingerprint == evidence.evidence_fingerprint
    assert slice_.active_intervals == expected_intervals
    assert "in-window request" in slice_.evidence_excerpt
    assert "in-window outcome" in slice_.evidence_excerpt
    assert "before the window" not in slice_.evidence_excerpt
    assert "outside outcome" not in slice_.evidence_excerpt
    assert "after the window" not in slice_.evidence_excerpt


def test_codex_window_evidence_excludes_injected_user_mirror_from_user_turns(
    codex_factory,
) -> None:
    """A ``response_item`` user record is the harness-injected twin of a real
    turn (see ``CodexProvider``'s module docstring): it duplicates the
    genuine ``event_msg``/``user_message`` text plus injected harness
    material such as ``<environment_context>``. It must contribute to
    ``msg_count`` the same way ``parse_session`` counts it, but never to
    ``user_msg_count``, ``first_user_text``/``latest_user_text``, or the
    excerpt — otherwise injected harness noise would be read back as a real
    user turn.
    """
    session_id = "019f8a2c-2df3-7f01-b55d-b8dcae9f2517"
    path = codex_factory(
        session_id,
        [
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": "/tmp/demo"},
            },
            {
                "timestamp": _timestamp(0),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "<environment_context>injected mirror, "
                                "must not count</environment_context>"
                            ),
                        }
                    ],
                },
            },
            {
                "timestamp": _timestamp(1),
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "genuine request"},
            },
            {
                "timestamp": _timestamp(2),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "outcome",
                },
            },
        ],
    )
    provider = CodexProvider()
    session = provider.parse_session(path)
    assert session is not None

    evidence = provider.extract_window_evidence(
        session, BASE, BASE + timedelta(minutes=3),
    )

    assert evidence is not None
    assert evidence.timestamps == tuple(
        (BASE + timedelta(minutes=minute)).timestamp() for minute in range(3)
    )
    # The injected mirror and the genuine turn both count toward msg_count,
    # mirroring parse_session's response_item/event_msg parity, but only the
    # genuine event_msg turn is a real user turn.
    assert evidence.msg_count == 3
    assert evidence.user_msg_count == 1
    assert evidence.first_user_text == "genuine request"
    assert evidence.latest_user_text == "genuine request"
    assert "genuine request" in evidence.excerpt
    assert "injected mirror" not in evidence.excerpt
    assert "environment_context" not in evidence.excerpt


def test_antigravity_window_evidence_preserves_role_gated_evidence_and_bounds(
    tmp_path: Path,
) -> None:
    """Only in-window, role-gated Antigravity evidence survives extraction.

    Ported from ``TestAntigravityWindowEvidence
    .test_extracts_only_window_bounded_role_gated_evidence`` in prior-art
    commit d46a082 (unpushed local branch, ``tests/test_antigravity_provider.py``),
    adapted from that file's local ``antigravity_factory``/``tmp_home``/``_user``/
    ``_planner`` fixtures to this file's plain ``tmp_path``/``BASE`` convention.
    A tool/model event (``VIEW_FILE``) and a system-injected input
    (``source=SYSTEM``) both fall inside the window but must not surface as
    evidence, mirroring the role predicates ``parse_session`` itself uses.
    """
    session_id = "076447e4-3f6c-42e0-ada8-e1ade38b7601"
    long_request = "request-head " + ("x" * 600) + " REQUEST-TAIL"
    long_response = "response-head " + ("y" * 400) + " RESPONSE-TAIL"
    path = _antigravity_transcript(
        tmp_path,
        session_id,
        [
            _antigravity_user("before window", -1),
            _antigravity_user(long_request, 0),
            _antigravity_planner(long_response, 1),
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "VIEW_FILE",
                "created_at": _timestamp(1),
                "content": "tool content must not become evidence",
            },
            {
                "step_index": 1,
                "source": "SYSTEM",
                "type": "USER_INPUT",
                "created_at": _timestamp(1),
                "content": "system content must not become evidence",
            },
            _antigravity_planner("at the exclusive window end", 2),
        ],
    )
    provider = AntigravityProvider(antigravity_dir=tmp_path / "antigravity")
    session = provider.parse_session(path)
    assert session is not None

    since, until = BASE, BASE + timedelta(minutes=2)
    evidence = provider.extract_window_evidence(session, since, until)

    assert evidence is not None
    assert evidence.timestamps == (
        BASE.timestamp(),
        (BASE + timedelta(minutes=1)).timestamp(),
        (BASE + timedelta(minutes=1)).timestamp(),
        (BASE + timedelta(minutes=1)).timestamp(),
    )
    assert evidence.active_intervals == clip_active_intervals(
        active_intervals_for_timestamps(session.timestamps),
        since,
        until,
    )
    assert evidence.msg_count == 2
    assert evidence.user_msg_count == 1
    # Bounded exactly like the Claude and Codex adapters: the user-text fields
    # mirror `SessionStat.first_user_text`'s 200-character cap, and the final
    # assistant field is head+tail compacted so the outcome at the response
    # tail survives. A slice with no cached summary renders `first_user_text`
    # in the detailed session list, so an unbounded value here would print a
    # full raw prompt for one provider only.
    assert evidence.first_user_text == long_request[:200]
    assert evidence.latest_user_text == long_request[:200]
    assert len(evidence.final_assistant_text) <= ASSISTANT_EVIDENCE_CHARS
    assert evidence.final_assistant_text.startswith("response-head")
    assert evidence.final_assistant_text.endswith("RESPONSE-TAIL")
    # The excerpt lane keeps its own head+tail compaction, so both ends of the
    # long turns still reach a narrator.
    assert "REQUEST-TAIL" in evidence.excerpt
    assert "RESPONSE-TAIL" in evidence.excerpt
    assert "before window" not in evidence.excerpt
    assert "at the exclusive window end" not in evidence.excerpt
    assert "tool content must not become evidence" not in evidence.excerpt
    assert "system content must not become evidence" not in evidence.excerpt
    # This is the hand-off validation used by the integration layer.
    assert session_slice_for_window(session, since, until, evidence=evidence) is not None


def test_antigravity_window_evidence_accepted_by_session_slice_for_boundary_crossing_session(
    tmp_path: Path,
) -> None:
    """A boundary-crossing Antigravity session's evidence must hand off cleanly to the slice.

    Mirrors
    ``test_claude_window_evidence_accepted_by_session_slice_for_boundary_crossing_session``
    and
    ``test_codex_window_evidence_accepted_by_session_slice_for_boundary_crossing_session``
    for the Antigravity adapter: a session whose physical transcript extends
    before/after the requested window must produce evidence that
    ``session_slice_for_window`` accepts (matching clipped physical
    intervals, a self-consistent fingerprint), and the resulting slice's
    bounded facts must come from the evidence, not the full session.
    """
    session_id = "076447e4-3f6c-42e0-ada8-e1ade38b7602"
    path = _antigravity_transcript(
        tmp_path,
        session_id,
        [
            _antigravity_user("before the window, must not leak", -10),
            _antigravity_planner("outside outcome, must not leak", -9),
            _antigravity_user("in-window request", 0),
            _antigravity_planner("in-window outcome", 1),
            _antigravity_user("after the window, must not leak", 20),
        ],
    )
    provider = AntigravityProvider(antigravity_dir=tmp_path / "antigravity")
    session = provider.parse_session(path)
    assert session is not None
    # The physical session spans well outside the window we are about to ask
    # for, so the resulting slice must be boundary-clipped.
    since, until = BASE, BASE + timedelta(minutes=2)
    assert session.start < since
    assert session.end >= until

    evidence = provider.extract_window_evidence(session, since, until)
    assert evidence is not None

    expected_intervals = clip_active_intervals(
        active_intervals_for_timestamps(session.timestamps),
        since,
        until,
    )
    assert evidence.active_intervals == expected_intervals

    slice_ = session_slice_for_window(session, since, until, evidence=evidence)

    assert slice_ is not None
    assert slice_.boundary_clipped is True
    assert slice_.msg_count == evidence.msg_count == 2
    assert slice_.user_msg_count == evidence.user_msg_count == 1
    assert slice_.evidence_fingerprint == evidence.evidence_fingerprint
    assert slice_.active_intervals == expected_intervals
    assert "in-window request" in slice_.evidence_excerpt
    assert "in-window outcome" in slice_.evidence_excerpt
    assert "before the window" not in slice_.evidence_excerpt
    assert "outside outcome" not in slice_.evidence_excerpt
    assert "after the window" not in slice_.evidence_excerpt
