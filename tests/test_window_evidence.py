"""Provider transcript evidence must remain inside the requested window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ccstory.providers.claude import ClaudeCodeProvider
from ccstory.time_tracking import (
    active_intervals_for_timestamps,
    clip_active_intervals,
    session_slice_for_window,
)

from tests.conftest import write_jsonl


BASE = datetime(2026, 7, 28, 10, tzinfo=timezone.utc)


def _timestamp(minutes: int) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


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


def test_adapters_without_bounded_extraction_fail_closed(tmp_path: Path) -> None:
    """Codex and Antigravity keep the base default until 0.8-D3/D4.

    The default must return ``None`` rather than something derived from a full
    transcript. An adapter that cannot prove which records fall inside the
    window has no safe answer, and a wrong one would be published as that
    window's story.
    """
    from ccstory.providers.antigravity import AntigravityProvider
    from ccstory.providers.codex import CodexProvider

    transcript = tmp_path / "projects" / "demo" / "any.jsonl"
    write_jsonl(
        transcript,
        [
            {
                "type": "user",
                "timestamp": _timestamp(0),
                "message": {"content": "some request"},
            },
        ],
    )
    session = ClaudeCodeProvider(tmp_path / "projects").parse_session(transcript)
    assert session is not None

    for provider in (CodexProvider(), AntigravityProvider()):
        assert (
            provider.extract_window_evidence(
                session, BASE, BASE + timedelta(minutes=1),
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
