from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ccstory.provider_metadata import bundled_provider_names
from ccstory.providers import provider_specs
from ccstory.providers.grok import GrokProvider


START = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
MODEL = "grok-4.6-build"


def _usage(
    *,
    input_tokens: int = 100,
    output_tokens: int = 15,
    cache_read: int = 20,
    cache_creation: int = 0,
) -> dict:
    model = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cachedReadTokens": cache_read,
        "cacheCreationTokens": cache_creation,
        "reasoningTokens": 3,
        "totalTokens": input_tokens + output_tokens,
        "costUsdTicks": 987654321,
    }
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cachedReadTokens": cache_read,
        "cacheCreationTokens": cache_creation,
        "reasoningTokens": 3,
        "totalTokens": input_tokens + output_tokens,
        "modelCalls": 1,
        "modelUsage": {MODEL: model},
        "costUsdTicks": 987654321,
    }


def _session(
    root: Path,
    session_id: str,
    *,
    created: datetime = START,
    active: datetime | None = None,
    kind: str = "headless",
    usage_events: list[tuple[str, str, datetime, dict]] | None = None,
) -> Path:
    active = active or created + timedelta(seconds=100)
    session_dir = root / "workspace-key" / session_id
    session_dir.mkdir(parents=True)
    summary = {
        "info": {"id": session_id, "cwd": "/work/project"},
        "session_kind": kind,
        "created_at": created.isoformat(),
        "last_active_at": active.isoformat(),
        "updated_at": active.isoformat(),
        "current_model_id": "grok-4.6",
        "generated_title": "fixture title",
        "num_chat_messages": 3,
    }
    (session_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    chat_rows = [
        {"type": "system", "content": "ignored system entry"},
        {"type": "user", "content": "first private user message"},
        {"type": "assistant", "content": "final assistant response"},
    ]
    (session_dir / "chat_history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in chat_rows),
        encoding="utf-8",
    )
    updates = [
        {
            "method": "session/update",
            "timestamp": int((created + timedelta(seconds=30)).timestamp()),
            "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "user_message_chunk"},
            },
        }
    ]
    for event_id, prompt_id, timestamp, payload in usage_events or []:
        updates.append(
            {
                "method": "_x.ai/session/update",
                "timestamp": int(timestamp.timestamp()),
                "params": {
                    "sessionId": session_id,
                    "_meta": {"eventId": event_id},
                    "update": {
                        "sessionUpdate": "turn_completed",
                        "prompt_id": prompt_id,
                        "usage": payload,
                    },
                },
            }
        )
    (session_dir / "updates.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in updates),
        encoding="utf-8",
    )
    return session_dir


def _window(until: datetime | None = None) -> dict[str, tuple[datetime, datetime]]:
    return {"all": (START - timedelta(days=1), until or START + timedelta(days=1))}


def test_grok_is_registered_as_partial_coverage() -> None:
    assert "grok" in bundled_provider_names()
    spec = next(spec for spec in provider_specs() if spec.name == "grok")
    assert spec.label == "Grok"
    assert spec.usage_coverage == "partial"


def test_grok_home_selects_the_native_session_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "custom-grok-home"))

    assert GrokProvider().data_roots() == (
        tmp_path / "custom-grok-home" / "sessions",
    )


def test_native_turn_usage_uses_actual_model_and_separates_cached_tokens(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    timestamp = START + timedelta(seconds=70)
    session_dir = _session(
        root,
        "session-1",
        usage_events=[("event-1", "prompt-1", timestamp, _usage())],
    )
    provider = GrokProvider(root)

    record = provider.collect_snapshot(_window(), engaged_only=False)

    assert record.agent == "grok"
    assert record.assistant_turns_by_window["all"] == 1
    usage = record.by_model_by_window["all"][MODEL]
    assert usage.turns == 1
    assert usage.input_tokens == 80
    assert usage.cache_read == 20
    assert usage.cache_creation == 0
    assert usage.output_tokens == 15
    assert usage.total_tokens == 115
    assert usage.model == MODEL
    assert "grok-4.6" not in record.by_model_by_window["all"]
    assert len(record.sessions_by_window["all"]) == 1
    session = record.sessions_by_window["all"][0]
    assert session.path == session_dir / "summary.json"
    assert session.native_title == "fixture title"
    assert session.agent == "grok"
    assert record.metrics.record_inventory_complete


def test_session_excerpt_uses_chat_history_and_workspace_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    session_dir = _session(root, "session-1")

    project, excerpt = GrokProvider(root).extract_excerpt(session_dir / "summary.json")

    assert project == "-work-project"
    assert "[USER 1]" in excerpt
    assert "first private user message" in excerpt
    assert "[ASSISTANT END]" in excerpt


def test_native_usage_is_windowed_by_final_event_timestamp(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    timestamp = START + timedelta(seconds=70)
    _session(
        root,
        "session-1",
        usage_events=[("event-1", "prompt-1", timestamp, _usage())],
    )
    provider = GrokProvider(root)
    windows = {
        "before": (START, START + timedelta(seconds=65)),
        "after": (START + timedelta(seconds=65), START + timedelta(seconds=120)),
    }
    by_model = {key: {} for key in windows}

    turns = provider.collect_usage_for_windows(windows, by_model)

    assert turns == {"before": 0, "after": 1}
    assert by_model["before"] == {}
    assert by_model["after"][MODEL].total_tokens == 115


def test_identical_fork_and_resume_copies_count_once_at_first_timestamp(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    event_at = START + timedelta(seconds=70)
    payload = _usage()
    _session(
        root,
        "session-1",
        usage_events=[("shared-event", "shared-prompt", event_at, payload)],
    )
    _session(
        root,
        "session-2",
        created=START + timedelta(seconds=120),
        active=START + timedelta(seconds=180),
        kind="headless",
        usage_events=[
            ("shared-event", "shared-prompt", START + timedelta(seconds=150), payload)
        ],
    )
    _session(
        root,
        "session-3",
        created=START + timedelta(seconds=120),
        active=START + timedelta(seconds=180),
        kind="subagent_resume",
        usage_events=[
            ("shared-event", "shared-prompt", START + timedelta(seconds=150), payload)
        ],
    )

    record = GrokProvider(root).collect_snapshot(_window(), engaged_only=False)

    assert record.assistant_turns_by_window["all"] == 1
    assert record.by_model_by_window["all"][MODEL].turns == 1
    assert record.by_model_by_window["all"][MODEL].total_tokens == 115
    # The independent headless fork remains visible; the resumed subagent does not.
    assert len(record.sessions_by_window["all"]) == 2
    assert record.metrics.record_inventory_complete


def test_replayed_event_in_resumed_session_is_counted_once(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    payload = _usage()
    _session(
        root,
        "session-1",
        usage_events=[
            ("replayed-event", "prompt-1", START + timedelta(seconds=70), payload),
            ("replayed-event", "prompt-1", START + timedelta(seconds=80), payload),
        ],
    )

    record = GrokProvider(root).collect_snapshot(_window(), engaged_only=False)

    assert record.assistant_turns_by_window["all"] == 1
    assert record.by_model_by_window["all"][MODEL].turns == 1
    assert record.metrics.record_inventory_complete


def test_subagent_usage_is_counted_without_a_duplicate_attended_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    _session(
        root,
        "child-session",
        kind="subagent",
        usage_events=[
            ("child-event", "child-prompt", START + timedelta(seconds=70), _usage())
        ],
    )

    record = GrokProvider(root).collect_snapshot(_window())

    assert record.assistant_turns_by_window["all"] == 1
    assert record.by_model_by_window["all"][MODEL].total_tokens == 115
    assert record.sessions_by_window["all"] == []


def test_conflicting_duplicate_event_identity_is_skipped_and_marks_partial(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    first = _usage()
    changed = _usage(input_tokens=101)
    _session(
        root,
        "session-1",
        usage_events=[("same-event", "prompt-1", START + timedelta(seconds=70), first)],
    )
    _session(
        root,
        "session-2",
        usage_events=[("same-event", "prompt-1", START + timedelta(seconds=80), changed)],
    )

    record = GrokProvider(root).collect_snapshot(_window(), engaged_only=False)

    assert record.assistant_turns_by_window["all"] == 0
    assert record.by_model_by_window["all"] == {}
    assert not record.metrics.record_inventory_complete


def test_malformed_cache_breakdown_is_not_counted(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    malformed = _usage(input_tokens=10, cache_read=11)
    _session(
        root,
        "session-1",
        usage_events=[("event-1", "prompt-1", START + timedelta(seconds=70), malformed)],
    )

    record = GrokProvider(root).collect_snapshot(_window(), engaged_only=False)

    assert record.assistant_turns_by_window["all"] == 0
    assert record.by_model_by_window["all"] == {}
    assert not record.metrics.record_inventory_complete


def test_unverified_nonzero_cache_creation_is_left_unpriced_for_usage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    _session(
        root,
        "session-1",
        usage_events=[
            (
                "event-1",
                "prompt-1",
                START + timedelta(seconds=70),
                _usage(cache_creation=1),
            )
        ],
    )

    record = GrokProvider(root).collect_snapshot(_window(), engaged_only=False)

    assert record.assistant_turns_by_window["all"] == 0
    assert record.by_model_by_window["all"] == {}
    assert not record.metrics.record_inventory_complete


def test_final_usage_missing_stays_missing_instead_of_becoming_zero_tokens(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    session_dir = _session(root, "session-1")
    updates_path = session_dir / "updates.jsonl"
    row = {
        "method": "_x.ai/session/update",
        "timestamp": int((START + timedelta(seconds=70)).timestamp()),
        "params": {
            "sessionId": "session-1",
            "_meta": {"eventId": "no-usage"},
            "update": {"sessionUpdate": "turn_completed", "prompt_id": "prompt-1"},
        },
    }
    with updates_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")

    record = GrokProvider(root).collect_snapshot(_window(), engaged_only=False)

    assert record.assistant_turns_by_window["all"] == 0
    assert record.by_model_by_window["all"] == {}
    assert not record.metrics.record_inventory_complete
