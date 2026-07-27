"""Antigravity session parsing, attribution, usage, subagent filtering, and registry contracts."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import ccstory.providers.antigravity as antigravity_module
from ccstory import cli
from ccstory.providers import (
    TranscriptResolver,
    agent_label,
    collect_multi_agent_sessions,
    create_providers,
    list_providers,
)
from ccstory.providers.antigravity import (
    AntigravityProvider,
    extract_cwd_from_db,
    extract_user_request_text,
)
from ccstory.recap import _agent_data_roots
from ccstory.report import _agent_title
from ccstory.token_usage import collect_usage


def _ts(minute: int) -> str:
    return datetime(2026, 7, 22, 12, minute, tzinfo=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _user(text: str, minute: int) -> dict:
    return {
        "step_index": minute,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": _ts(minute),
        "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>",
    }


def _planner(text: str, minute: int, thinking: str = "") -> dict:
    return {
        "step_index": minute,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": _ts(minute),
        "content": text,
        "thinking": thinking,
    }


@pytest.fixture
def antigravity_factory(tmp_home: Path):
    """Write an Antigravity transcript and companion DB into the fake home."""

    def _make(
        session_id: str, records: list[dict], cwd: str | None = None
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
        with transcript_path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        if cwd:
            conv_dir = tmp_home / ".gemini" / "antigravity" / "conversations"
            conv_dir.mkdir(parents=True, exist_ok=True)
            db_path = conv_dir / f"{session_id}.db"
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute(
                "CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB);"
            )
            blob = f"\x0a(file://{cwd}".encode("utf-8")
            c.execute(
                "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (blob,)
            )
            conn.commit()
            conn.close()

        return transcript_path

    return _make


SID = "076447e4-3f6c-42e0-ada8-e1ade38b7706"


class TestAntigravityParsing:
    def test_reads_user_turns_and_unwraps_user_request(
        self, antigravity_factory, tmp_home
    ):
        path = antigravity_factory(
            SID,
            [
                _user("Add unit tests for Antigravity provider", 1),
                _planner("I will write the test file.", 2),
                _user("Run pytest to verify", 6),
            ],
            cwd="/Users/x/Side_project/ccstory",
        )
        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        stat = provider.parse_session(path)

        assert stat is not None
        assert stat.agent == "antigravity"
        assert stat.user_msg_count == 2
        assert stat.first_user_text == "Add unit tests for Antigravity provider"
        assert stat.project == "-Users-x-Side-project-ccstory"

    def test_unwraps_user_request_and_strips_metadata(self):
        raw = (
            "<USER_REQUEST>\nFix bug in parser\n</USER_REQUEST>\n"
            "<ADDITIONAL_METADATA>\ntime: 12:00\n</ADDITIONAL_METADATA>\n"
            "<USER_SETTINGS_CHANGE>\nmodel changed\n</USER_SETTINGS_CHANGE>"
        )
        assert extract_user_request_text(raw) == "Fix bug in parser"

    def test_subagent_transcript_is_filtered(
        self, antigravity_factory, tmp_home
    ):
        path = antigravity_factory(
            "subagent-session",
            [
                {
                    "step_index": 1,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "created_at": _ts(1),
                    "content": "<identity>\nYou are a research subagent.\n</identity>",
                },
                _planner("Subagent finished analysis.", 2),
            ],
            cwd="/Users/x/demo",
        )
        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        assert provider.parse_session(path) is None

    def test_default_project_when_no_cwd(self, antigravity_factory, tmp_home):
        path = antigravity_factory(
            SID,
            [_user("Hello", 1), _planner("Hi there", 2)],
        )
        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        stat = provider.parse_session(path)
        assert stat is not None
        assert stat.project == "antigravity"

    def test_gap_capping_for_active_time(self, antigravity_factory, tmp_home):
        path = antigravity_factory(
            SID,
            [_user("start", 0), _planner("working", 10)],
            cwd="/Users/x/demo",
        )
        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        stat = provider.parse_session(path)
        assert stat is not None
        assert stat.active_sec == 300

    def test_reads_percent_encoded_cwd_from_db_in_read_only_mode(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB);")
        c.execute(
            "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
            (
                b"prefix file:///Users/test/"
                b"%E9%A1%B9%E7%9B%AE%20With%20Space\x12",
            ),
        )
        conn.commit()
        conn.close()
        real_connect = sqlite3.connect
        calls: list[tuple[object, dict]] = []

        def tracking_connect(database, *args, **kwargs):
            calls.append((database, kwargs))
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(
            "ccstory.providers.antigravity.sqlite3.connect",
            tracking_connect,
        )

        extracted = extract_cwd_from_db(db_path)

        assert extracted == "/Users/test/项目 With Space"
        assert "mode=ro" in str(calls[0][0])
        assert calls[0][1]["uri"] is True

    def test_non_string_content_is_ignored(self, antigravity_factory, tmp_home):
        malformed = _user("ignored", 1)
        malformed["content"] = {"text": "unsupported shape"}
        path = antigravity_factory(
            SID,
            [malformed, _planner("valid response", 2)],
        )
        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )

        stat = provider.parse_session(path)

        assert stat is not None
        assert stat.user_msg_count == 0


class TestAntigravityUsageCollection:
    def test_returns_zero_when_no_explicit_exact_usage(
        self, antigravity_factory, tmp_home
    ):
        antigravity_factory(
            SID,
            [
                _user("A" * 400, 1),
                _planner("B" * 200, 2, thinking="C" * 100),
            ],
            cwd="/Users/x/demo",
        )
        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 7, 30, tzinfo=timezone.utc)

        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        by_model: dict = {}
        turns = provider.collect_usage(since, until, by_model)
        assert turns == 0
        assert by_model == {}

    @pytest.mark.parametrize(
        "cache_key,cache_val,expected_cache",
        [
            ("cache_read_input_tokens", 80, 80),
            ("cached_input_tokens", 45, 45),
            ("cached_content_token_count", 30, 30),
            ("cache_read_input_tokens", 0, 0),
        ],
    )
    def test_collects_exact_tokens_and_cache_variants(
        self, antigravity_factory, tmp_home, cache_key, cache_val, expected_cache
    ):
        rec = _planner("Hello", 2)
        rec["usage"] = {
            "model": "gemini-3.6-pro",
            "input_tokens": 0,
            "output_tokens": 0,
            cache_key: cache_val,
        }
        antigravity_factory(SID, [_user("Hi", 1), rec], cwd="/Users/x/demo")

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 7, 30, tzinfo=timezone.utc)

        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        by_model: dict = {}
        turns = provider.collect_usage(since, until, by_model)
        assert turns == 1
        assert "gemini-3.6-pro" in by_model
        assert by_model["gemini-3.6-pro"].input_tokens == 0
        assert by_model["gemini-3.6-pro"].output_tokens == 0
        assert by_model["gemini-3.6-pro"].cache_read == expected_cache

    @pytest.mark.parametrize(
        "bad_usage",
        [
            {"model": "gemini-3.6-flash", "input_tokens": "100", "output_tokens": 50},
            {"model": "gemini-3.6-flash", "input_tokens": -1, "output_tokens": 50},
            {"model": "gemini-3.6-flash", "input_tokens": 100, "output_tokens": -5},
            {"model": "  ", "input_tokens": 100, "output_tokens": 50},
            {"model": {}, "input_tokens": 100, "output_tokens": 50},
            {"input_tokens": 100, "output_tokens": 50},
            {"model": "gemini-3.6-flash", "output_tokens": 50},
            {"model": "gemini-3.6-flash", "input_tokens": 100},
        ],
    )
    def test_invalid_or_missing_usage_fields_ignores_turn(
        self, antigravity_factory, tmp_home, bad_usage
    ):
        rec = _planner("Hello", 2)
        rec["usage"] = bad_usage
        antigravity_factory(SID, [_user("Hi", 1), rec])
        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )

        by_model: dict = {}
        turns = provider.collect_usage(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 30, tzinfo=timezone.utc),
            by_model,
        )

        assert turns == 0
        assert by_model == {}

    def test_subagent_invoke_discovery_and_filtering(
        self, antigravity_factory, tmp_home
    ):
        parent_sid = "11111111-1111-1111-1111-111111111111"
        child_sid = "22222222-2222-2222-2222-222222222222"

        parent_invoke_rec = {
            "step_index": 2,
            "source": "MODEL",
            "type": "INVOKE_SUBAGENT",
            "status": "DONE",
            "created_at": _ts(2),
            "content": f"Spawning subagent task conversation id: {child_sid}",
        }
        parent_resp = _planner("Parent response", 3)
        parent_resp["usage"] = {
            "model": "gemini-3.6-flash",
            "input_tokens": 100,
            "output_tokens": 50,
        }

        parent_path = antigravity_factory(
            parent_sid,
            [_user("Parent prompt", 1), parent_invoke_rec, parent_resp],
            cwd="/Users/x/demo",
        )

        child_resp = _planner("Child response", 3)
        child_resp["usage"] = {
            "model": "gemini-3.6-flash",
            "input_tokens": 999,
            "output_tokens": 888,
        }

        child_path = antigravity_factory(
            child_sid,
            [_user("Child task", 1), child_resp],
            cwd="/Users/x/demo",
        )

        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )

        assert provider.parse_session(parent_path) is not None
        assert provider.parse_session(child_path) is None

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 7, 30, tzinfo=timezone.utc)
        stats = provider.collect_sessions(since, until)
        session_ids = [s.session_id for s in stats]
        assert parent_sid in session_ids
        assert child_sid not in session_ids

        by_model: dict = {}
        turns = provider.collect_usage(since, until, by_model)
        assert turns == 1
        assert "gemini-3.6-flash" in by_model
        assert by_model["gemini-3.6-flash"].input_tokens == 100
        assert by_model["gemini-3.6-flash"].output_tokens == 50

    def test_child_discovery_is_cached_and_reads_structured_id_once(
        self, antigravity_factory, tmp_home, monkeypatch
    ):
        parent_sid = "77777777-7777-7777-7777-777777777777"
        child_sid = "88888888-8888-8888-8888-888888888888"
        antigravity_factory(
            parent_sid,
            [
                _user("delegate this", 1),
                {
                    "type": "INVOKE_SUBAGENT",
                    "tool_calls": [{
                        "arguments": {"Subagents": [{"conversationId": child_sid}]},
                    }],
                },
                _planner("delegated", 2),
            ],
        )
        antigravity_factory(child_sid, [_user("child task", 1), _planner("done", 2)])
        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        real_glob = antigravity_module.glob.glob
        calls: list[str] = []

        def recording_glob(pattern: str):
            calls.append(pattern)
            return real_glob(pattern)

        monkeypatch.setattr(antigravity_module.glob, "glob", recording_glob)

        assert provider._get_child_session_ids() == {child_sid}
        assert provider._get_child_session_ids() == {child_sid}
        assert len(calls) == 1

    def test_user_request_containing_conversation_id_is_not_misidentified(
        self, antigravity_factory, tmp_home
    ):
        parent_sid = "33333333-3333-3333-3333-333333333333"
        target_sid = "44444444-4444-4444-4444-444444444444"

        parent_path = antigravity_factory(
            parent_sid,
            [
                _user(f"Please check conversation id: {target_sid}", 1),
                _planner("Understood", 2),
            ],
            cwd="/Users/x/demo",
        )
        target_path = antigravity_factory(
            target_sid,
            [_user("Target user request", 1), _planner("Target response", 2)],
            cwd="/Users/x/demo",
        )

        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        assert provider.parse_session(parent_path) is not None
        assert provider.parse_session(target_path) is not None

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 7, 30, tzinfo=timezone.utc)
        stats = provider.collect_sessions(since, until)
        session_ids = [s.session_id for s in stats]
        assert parent_sid in session_ids
        assert target_sid in session_ids

    def test_collects_tokens_from_top_level_step_fields(
        self, antigravity_factory, tmp_home
    ):
        rec = _planner("Hello", 2)
        rec["input_tokens"] = 150
        rec["output_tokens"] = 60
        rec["model"] = "gemini-3.6-pro"
        rec["cache_read_input_tokens"] = 25
        antigravity_factory(SID, [_user("Hi", 1), rec], cwd="/Users/x/demo")

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 7, 30, tzinfo=timezone.utc)

        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        by_model: dict = {}
        turns = provider.collect_usage(since, until, by_model)
        assert turns == 1
        assert "gemini-3.6-pro" in by_model
        assert by_model["gemini-3.6-pro"].input_tokens == 150
        assert by_model["gemini-3.6-pro"].output_tokens == 60
        assert by_model["gemini-3.6-pro"].cache_read == 25

    def test_planner_response_containing_invoke_subagent_text_is_not_misidentified(
        self, antigravity_factory, tmp_home
    ):
        parent_sid = "55555555-5555-5555-5555-555555555555"
        target_sid = "66666666-6666-6666-6666-666666666666"

        planner_mention = _planner(
            f"I completed task for INVOKE_SUBAGENT conversation id: {target_sid}", 2
        )
        planner_mention["usage"] = {
            "model": "gemini-3.6-flash",
            "input_tokens": 100,
            "output_tokens": 50,
        }

        parent_path = antigravity_factory(
            parent_sid,
            [_user("Check subagent status", 1), planner_mention],
            cwd="/Users/x/demo",
        )
        target_path = antigravity_factory(
            target_sid,
            [_user("Target user request", 1), _planner("Target response", 2)],
            cwd="/Users/x/demo",
        )

        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        assert provider.parse_session(parent_path) is not None
        assert provider.parse_session(target_path) is not None

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 7, 30, tzinfo=timezone.utc)
        stats = provider.collect_sessions(since, until)
        session_ids = [s.session_id for s in stats]
        assert parent_sid in session_ids
        assert target_sid in session_ids

    def test_window_aggregation_only_counts_turns_within_window(
        self, antigravity_factory, tmp_home
    ):
        rec_in = _planner("In window", 10)
        rec_in["usage"] = {
            "model": "gemini-3.6-flash",
            "input_tokens": 100,
            "output_tokens": 20,
        }
        rec_out = _planner("Out of window", 50)
        rec_out["created_at"] = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc).isoformat()
        rec_out["usage"] = {
            "model": "gemini-3.6-flash",
            "input_tokens": 500,
            "output_tokens": 200,
        }

        antigravity_factory(
            SID,
            [_user("Hi", 1), rec_in, rec_out],
            cwd="/Users/x/demo",
        )

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 7, 30, tzinfo=timezone.utc)

        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        by_model: dict = {}
        turns = provider.collect_usage(since, until, by_model)

        assert turns == 1
        assert by_model["gemini-3.6-flash"].input_tokens == 100
        assert by_model["gemini-3.6-flash"].output_tokens == 20


class TestAntigravityMultiAgentCollection:
    def test_collects_antigravity_and_multi_agent(
        self, antigravity_factory, tmp_home
    ):
        antigravity_factory(
            SID,
            [_user("test multi agent", 1), _planner("ok", 3)],
            cwd="/Users/x/demo",
        )

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        stats = collect_multi_agent_sessions(since, agent="antigravity")
        assert len(stats) == 1
        assert stats[0].agent == "antigravity"

        all_stats = collect_multi_agent_sessions(since, agent="all")
        assert any(s.agent == "antigravity" for s in all_stats)


class TestAntigravityTranscriptResolution:
    def test_resolves_transcript_path(self, antigravity_factory, tmp_home):
        path = antigravity_factory(
            SID,
            [_user("query", 1), _planner("ans", 2)],
            cwd="/Users/x/demo",
        )
        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        stat = provider.parse_session(path)
        assert stat is not None
        resolver = TranscriptResolver()
        assert resolver.path_for(stat) == path


class TestAntigravityExcerptExtraction:
    def test_extract_excerpt(self, antigravity_factory, tmp_home):
        path = antigravity_factory(
            SID,
            [
                _user("First user request", 1),
                _planner("First planner response", 2),
            ],
            cwd="/Users/x/Side_project/ccstory",
        )
        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        project, excerpt = provider.extract_excerpt(path)
        assert project == "-Users-x-Side-project-ccstory"
        assert "[USER 1]\nFirst user request" in excerpt
        assert "[ASSISTANT END]\nFirst planner response" in excerpt

    def test_parse_and_excerpt_share_one_cwd_db_lookup(
        self, antigravity_factory, tmp_home, monkeypatch
    ):
        path = antigravity_factory(
            SID,
            [_user("First user request", 1), _planner("First planner response", 2)],
        )
        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        lookups: list[Path] = []

        def fake_extract(db_path: Path) -> str:
            lookups.append(db_path)
            return "/Users/x/Side_project/ccstory"

        monkeypatch.setattr(antigravity_module, "extract_cwd_from_db", fake_extract)

        assert provider.parse_session(path) is not None
        project, _ = provider.extract_excerpt(path)

        assert project == "-Users-x-Side-project-ccstory"
        assert lookups == [
            tmp_home / ".gemini" / "antigravity" / "conversations" / f"{SID}.db"
        ]

    def test_model_tool_events_are_not_narrative_assistant_text(
        self, antigravity_factory, tmp_home
    ):
        tool_event = {
            "source": "MODEL",
            "type": "VIEW_FILE",
            "created_at": _ts(2),
            "content": "SECRET_FILE_CONTENT",
        }
        system_input = {
            "source": "SYSTEM",
            "type": "USER_INPUT",
            "created_at": _ts(3),
            "content": "SYSTEM_INJECTED_SECRET",
        }
        path = antigravity_factory(
            SID,
            [
                _user("Review the provider", 1),
                _planner("Safe final response", 2),
                tool_event,
                system_input,
            ],
        )
        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )

        _, excerpt = provider.extract_excerpt(path)
        stat = provider.parse_session(path)

        assert "Safe final response" in excerpt
        assert "SECRET_FILE_CONTENT" not in excerpt
        assert "SYSTEM_INJECTED_SECRET" not in excerpt
        assert stat is not None
        assert stat.user_msg_count == 1


class TestAntigravityRegistryContracts:
    def test_registry_integration(self, tmp_home, capsys):
        assert "antigravity" in list_providers()
        assert agent_label("antigravity") == "Antigravity"
        assert create_providers("antigravity")[0].agent_name == "antigravity"
        assert _agent_title("antigravity", "Recap") == "Antigravity Recap"

        data_roots = _agent_data_roots("antigravity")
        assert any(name == "antigravity" for name, _ in data_roots)
        usage = collect_usage(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 30, tzinfo=timezone.utc),
            agent="antigravity",
        )
        assert usage.incomplete_agents == []
        assert usage.usage_complete is True

        with pytest.raises(SystemExit) as excinfo:
            cli.main(["week", "--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "antigravity" in out


def encode_varint(val: int) -> bytes:
    res = bytearray()
    while val >= 0x80:
        res.append((val & 0x7F) | 0x80)
        val >>= 7
    res.append(val & 0x7F)
    return bytes(res)


def encode_tag(field_num: int, wire_type: int) -> bytes:
    return encode_varint((field_num << 3) | wire_type)


def encode_string(field_num: int, text: str) -> bytes:
    b = text.encode("utf-8")
    return encode_tag(field_num, 2) + encode_varint(len(b)) + b


def encode_bytes(field_num: int, b: bytes) -> bytes:
    return encode_tag(field_num, 2) + encode_varint(len(b)) + b


def encode_varint_field(field_num: int, val: int) -> bytes:
    return encode_tag(field_num, 0) + encode_varint(val)


def make_title_proto(entries: list[tuple[str, str]]) -> bytes:
    root = bytearray()
    for sid, title in entries:
        summary_msg = encode_string(1, title)
        entry_msg = encode_string(1, sid) + encode_bytes(2, summary_msg)
        root.extend(encode_bytes(1, entry_msg))
    return bytes(root)


def make_gen_metadata_blob(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    extra_fields: bytes = b"",
) -> bytes:
    usage_parts = [
        encode_varint_field(2, input_tokens),
        encode_varint_field(3, output_tokens),
    ]
    if cached_tokens > 0:
        usage_parts.append(encode_varint_field(5, cached_tokens))
    usage_msg = b"".join(usage_parts) + extra_fields
    gen_msg = encode_string(19, model) + encode_bytes(4, usage_msg)
    return encode_bytes(1, gen_msg)


class TestAntigravityNativeTitle:
    def test_reads_native_title_map_and_preserves_first_user_text(
        self, antigravity_factory, tmp_home
    ):
        sid = "title-test-session-123"
        path = antigravity_factory(
            sid,
            [
                _user("Add unit tests for Antigravity native title", 1),
                _planner("Writing tests.", 2),
            ],
            cwd="/Users/x/demo",
        )
        pb_path = tmp_home / ".gemini" / "antigravity" / "agyhub_summaries_proto.pb"
        pb_path.parent.mkdir(parents=True, exist_ok=True)
        pb_path.write_bytes(
            make_title_proto([(sid, "Refactor Antigravity metadata parser")])
        )

        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        stat = provider.parse_session(path)

        assert stat is not None
        assert stat.native_title == "Refactor Antigravity metadata parser"
        assert stat.first_user_text == "Add unit tests for Antigravity native title"


class TestAntigravityDBMetadataUsage:
    def test_exact_db_usage_window_filtering_dedup_and_fallback(
        self, antigravity_factory, tmp_home
    ):
        sid = "db-usage-session-456"
        rec2 = _planner("Turn 2", 2)
        rec2["usage"] = {
            "model": "gemini-3.6-flash",
            "input_tokens": 999,
            "output_tokens": 999,
        }
        rec4 = _planner("Turn 4", 4)
        rec4["usage"] = {
            "model": "gemini-3.1-pro-low",
            "input_tokens": 200,
            "output_tokens": 80,
            "cache_read_input_tokens": 50,
        }
        rec6 = _planner("Turn 6", 6)
        rec6["created_at"] = (
            datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

        antigravity_factory(
            sid,
            [_user("Prompt", 1), rec2, _user("Next", 3), rec4, rec6],
            cwd="/Users/x/demo",
        )

        conv_dir = tmp_home / ".gemini" / "antigravity" / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        db_path = conv_dir / f"{sid}.db"
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute(
            "CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER);"
        )
        blob2 = make_gen_metadata_blob(
            "gemini-3.6-flash",
            100,
            40,
            extra_fields=encode_varint_field(99, 777),
        )
        c.execute(
            "INSERT INTO gen_metadata VALUES (2, ?, ?);", (blob2, len(blob2))
        )
        bad_blob = encode_bytes(1, b"corrupt data")
        c.execute(
            "INSERT INTO gen_metadata VALUES (4, ?, ?);",
            (bad_blob, len(bad_blob)),
        )
        blob6 = make_gen_metadata_blob("gemini-3.6-flash", 500, 300)
        c.execute(
            "INSERT INTO gen_metadata VALUES (6, ?, ?);", (blob6, len(blob6))
        )
        conn.commit()
        conn.close()

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 7, 30, tzinfo=timezone.utc)

        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        by_model: dict = {}
        turns = provider.collect_usage(since, until, by_model)

        assert turns == 2
        assert "gemini-3.6-flash" in by_model
        assert by_model["gemini-3.6-flash"].turns == 1
        assert by_model["gemini-3.6-flash"].input_tokens == 100
        assert by_model["gemini-3.6-flash"].output_tokens == 40
        assert by_model["gemini-3.6-flash"].cache_read == 0

        assert "gemini-3.1-pro-low" in by_model
        assert by_model["gemini-3.1-pro-low"].turns == 1
        assert by_model["gemini-3.1-pro-low"].input_tokens == 200
        assert by_model["gemini-3.1-pro-low"].output_tokens == 80
        assert by_model["gemini-3.1-pro-low"].cache_read == 50


class TestSessionStatPositionalCompatibility:
    def test_positional_constructor_backward_compatibility(self):
        from ccstory.time_tracking import SessionStat

        start = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 22, 12, 10, tzinfo=timezone.utc)
        path = Path("/tmp/transcript.jsonl")
        ts_list = [start.timestamp(), end.timestamp()]

        stat = SessionStat(
            "proj-a",
            "coding",
            "sid-123",
            start,
            end,
            600,
            10,
            2,
            "first prompt",
            True,
            "/Users/me/proj-a",
            ts_list,
            "user_rule",
            "antigravity",
            path,
        )

        assert stat.project == "proj-a"
        assert stat.category == "coding"
        assert stat.session_id == "sid-123"
        assert stat.start == start
        assert stat.end == end
        assert stat.active_sec == 600
        assert stat.msg_count == 10
        assert stat.user_msg_count == 2
        assert stat.first_user_text == "first prompt"
        assert stat.is_scheduled is True
        assert stat.cwd == "/Users/me/proj-a"
        assert stat.timestamps == ts_list
        assert stat.category_source == "user_rule"
        assert stat.agent == "antigravity"
        assert stat.path == path
        assert stat.native_title == ""


class TestStrictProtobufFailClosed:
    @pytest.mark.parametrize(
        "trailing_bytes",
        [
            b"\x12\x50\x01\x02",
            b"\x0f",
            b"\x80" * 11,
            b"\x00",
        ],
    )
    def test_parse_gen_metadata_blob_fail_closed_on_trailing_corrupt_data(
        self, trailing_bytes
    ):
        from ccstory.providers.antigravity import parse_gen_metadata_blob

        valid_blob = make_gen_metadata_blob("gemini-3.6-flash", 100, 40)
        corrupt_blob = valid_blob + trailing_bytes
        assert parse_gen_metadata_blob(corrupt_blob) is None

    def test_title_proto_bad_entry_does_not_corrupt_other_entries(
        self, tmp_path
    ):
        from ccstory.providers.antigravity import _read_title_map

        pb_path = tmp_path / "agyhub_summaries_proto.pb"
        bad_entry = encode_bytes(
            1, encode_tag(1, 2) + encode_varint(50) + b"truncated"
        )
        valid_entry = encode_bytes(
            1,
            encode_string(1, "sid-valid")
            + encode_bytes(2, encode_string(1, "Valid Native Title")),
        )
        pb_path.write_bytes(bad_entry + valid_entry)

        title_map = _read_title_map(pb_path)
        assert "sid-valid" in title_map
        assert title_map["sid-valid"] == "Valid Native Title"

    @pytest.mark.parametrize(
        "varint_bytes",
        [
            b"\x80" * 9 + b"\x02",
            b"\x80" * 10,
        ],
    )
    def test_decode_varint_rejects_uint64_overflow(self, varint_bytes):
        from ccstory.providers.antigravity import decode_varint

        with pytest.raises(ValueError, match="Malformed varint"):
            decode_varint(varint_bytes, 0)

    def test_parse_gen_metadata_blob_fails_closed_on_invalid_utf8_model(self):
        from ccstory.providers.antigravity import parse_gen_metadata_blob

        invalid_utf8_blob = encode_bytes(
            1,
            encode_bytes(19, b"\xff\xfe\xfd")
            + encode_bytes(
                4, encode_varint_field(2, 100) + encode_varint_field(3, 40)
            ),
        )
        assert parse_gen_metadata_blob(invalid_utf8_blob) is None

    def test_title_proto_skips_invalid_utf8_entry(self, tmp_path):
        from ccstory.providers.antigravity import _read_title_map

        pb_path = tmp_path / "agyhub_summaries_proto.pb"
        invalid_entry = encode_bytes(
            1,
            encode_string(1, "sid-invalid")
            + encode_bytes(2, encode_bytes(1, b"\xff\xfe\xfd")),
        )
        valid_entry = encode_bytes(
            1,
            encode_string(1, "sid-valid")
            + encode_bytes(2, encode_string(1, "Valid Native Title")),
        )
        pb_path.write_bytes(invalid_entry + valid_entry)

        title_map = _read_title_map(pb_path)
        assert "sid-invalid" not in title_map
        assert "sid-valid" in title_map
        assert title_map["sid-valid"] == "Valid Native Title"


class TestDuplicateIdxDefense:
    def test_gen_metadata_duplicate_idx_counted_only_once(
        self, antigravity_factory, tmp_home
    ):
        sid = "dup-idx-session-789"
        rec2 = _planner("Turn 2", 2)
        antigravity_factory(
            sid,
            [_user("Prompt", 1), rec2],
            cwd="/Users/x/demo",
        )

        conv_dir = tmp_home / ".gemini" / "antigravity" / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        db_path = conv_dir / f"{sid}.db"
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute(
            "CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER);"
        )
        blob1 = make_gen_metadata_blob("gemini-3.6-flash", 100, 40)
        blob2 = make_gen_metadata_blob("gemini-3.6-flash", 999, 999)
        c.execute(
            "INSERT INTO gen_metadata VALUES (2, ?, ?);", (blob1, len(blob1))
        )
        c.execute(
            "INSERT INTO gen_metadata VALUES (2, ?, ?);", (blob2, len(blob2))
        )
        conn.commit()
        conn.close()

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        until = datetime(2026, 7, 30, tzinfo=timezone.utc)

        provider = AntigravityProvider(
            antigravity_dir=tmp_home / ".gemini" / "antigravity"
        )
        by_model: dict = {}
        turns = provider.collect_usage(since, until, by_model)

        assert turns == 1
        assert "gemini-3.6-flash" in by_model
        assert by_model["gemini-3.6-flash"].turns == 1
        assert by_model["gemini-3.6-flash"].input_tokens == 100
        assert by_model["gemini-3.6-flash"].output_tokens == 40


class TestAntigravityDBConnectionFallback:
    def test_falls_back_when_primary_probe_raises_operational_error(
        self, tmp_path, monkeypatch
    ):
        from ccstory.providers.antigravity import _connect_readonly_db

        db_path = tmp_path / "lazy_restricted.db"
        conn_real = sqlite3.connect(db_path)
        conn_real.execute("CREATE TABLE test (id INT);")
        conn_real.commit()
        conn_real.close()

        real_connect = sqlite3.connect
        connect_calls: list[str] = []
        primary_closed = False

        class PrimaryProbeFailingConnection:
            def __init__(self, real_conn):
                self._real = real_conn

            def execute(self, sql, *args, **kwargs):
                if "schema_version" in sql:
                    raise sqlite3.OperationalError("unable to open database file")
                return self._real.execute(sql, *args, **kwargs)

            def cursor(self):
                return self._real.cursor()

            def close(self):
                nonlocal primary_closed
                primary_closed = True
                return self._real.close()

        def custom_connect(database, *args, **kwargs):
            db_str = str(database)
            connect_calls.append(db_str)
            conn = real_connect(database, *args, **kwargs)
            if "immutable=1" not in db_str:
                return PrimaryProbeFailingConnection(conn)
            return conn

        monkeypatch.setattr(
            "ccstory.providers.antigravity.sqlite3.connect",
            custom_connect,
        )

        with contextlib.closing(_connect_readonly_db(db_path)) as conn:
            c = conn.cursor()
            rows = c.execute("SELECT name FROM sqlite_master;").fetchall()
            assert ("test",) in rows

        assert primary_closed is True
        assert len(connect_calls) == 2
        assert "mode=ro" in connect_calls[0]
        assert "immutable=1" not in connect_calls[0]
        assert "mode=ro" in connect_calls[1]
        assert "immutable=1" in connect_calls[1]

    def test_falls_back_to_immutable_uri_on_connect_operational_error(
        self, tmp_path, monkeypatch
    ):
        from ccstory.providers.antigravity import _connect_readonly_db

        db_path = tmp_path / "restricted.db"
        conn_real = sqlite3.connect(db_path)
        conn_real.execute("CREATE TABLE test (id INT);")
        conn_real.commit()
        conn_real.close()

        real_connect = sqlite3.connect
        connect_calls: list[tuple[str, dict]] = []

        def failing_primary_connect(database, *args, **kwargs):
            connect_calls.append((str(database), kwargs))
            if "immutable=1" not in str(database):
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(
            "ccstory.providers.antigravity.sqlite3.connect",
            failing_primary_connect,
        )

        with contextlib.closing(_connect_readonly_db(db_path)) as conn:
            c = conn.cursor()
            rows = c.execute("SELECT name FROM sqlite_master;").fetchall()
            assert ("test",) in rows

        assert len(connect_calls) == 2
        assert "mode=ro" in connect_calls[0][0]
        assert "immutable=1" not in connect_calls[0][0]
        assert "mode=ro" in connect_calls[1][0]
        assert "immutable=1" in connect_calls[1][0]

    def test_normal_connection_does_not_trigger_fallback(
        self, tmp_path, monkeypatch
    ):
        from ccstory.providers.antigravity import _connect_readonly_db

        db_path = tmp_path / "normal.db"
        conn_real = sqlite3.connect(db_path)
        conn_real.execute("CREATE TABLE test (id INT);")
        conn_real.commit()
        conn_real.close()

        real_connect = sqlite3.connect
        connect_calls: list[tuple[str, dict]] = []

        def normal_connect(database, *args, **kwargs):
            connect_calls.append((str(database), kwargs))
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(
            "ccstory.providers.antigravity.sqlite3.connect",
            normal_connect,
        )

        with contextlib.closing(_connect_readonly_db(db_path)) as conn:
            c = conn.cursor()
            rows = c.execute("SELECT name FROM sqlite_master;").fetchall()
            assert ("test",) in rows

        assert len(connect_calls) == 1
        assert "mode=ro" in connect_calls[0][0]
        assert "immutable=1" not in connect_calls[0][0]


class TestAntigravityCacheTokens:
    def test_parse_gen_metadata_blob_field_5_cache_tokens(self):
        from ccstory.providers.antigravity import parse_gen_metadata_blob

        blob = make_gen_metadata_blob(
            "gemini-3.6-flash", 1000, 200, cached_tokens=500
        )
        parsed = parse_gen_metadata_blob(blob)
        assert parsed is not None
        model, inp, out, cache_read = parsed
        assert model == "gemini-3.6-flash"
        assert inp == 1000
        assert out == 200
        assert cache_read == 500

    def test_parse_gen_metadata_blob_absent_field_5_defaults_to_zero(self):
        from ccstory.providers.antigravity import parse_gen_metadata_blob

        blob = make_gen_metadata_blob("gemini-3.6-flash", 1000, 200)
        parsed = parse_gen_metadata_blob(blob)
        assert parsed is not None
        model, inp, out, cache_read = parsed
        assert cache_read == 0

    def test_parse_gen_metadata_blob_fails_closed_on_corrupt_or_missing_fields(self):
        from ccstory.providers.antigravity import parse_gen_metadata_blob

        # Trailing corrupt protobuf bytes
        valid_blob = make_gen_metadata_blob("gemini-3.6-flash", 1000, 200)
        assert parse_gen_metadata_blob(valid_blob + b"\x80" * 11) is None

        # Missing input tokens (field 2)
        gen_msg_no_inp = encode_string(19, "gemini-3.6-flash") + encode_bytes(
            4, encode_varint_field(3, 200)
        )
        blob_no_inp = encode_bytes(1, gen_msg_no_inp)
        assert parse_gen_metadata_blob(blob_no_inp) is None


class TestCompactedStepAttribution:
    def test_unmatched_db_row_included_when_whole_session_in_window(
        self, tmp_path, monkeypatch
    ):
        from ccstory.providers.antigravity import AntigravityProvider

        antigravity_dir = tmp_path / "antigravity"
        antigravity_dir.mkdir()
        monkeypatch.setattr(AntigravityProvider, "antigravity_dir", antigravity_dir)

        sid = "compacted-session-in-window"
        session_dir = antigravity_dir / "brain" / sid / ".system_generated" / "logs"
        session_dir.mkdir(parents=True)
        transcript = session_dir / "transcript.jsonl"

        ts1 = "2026-07-26T10:00:00Z"
        ts2 = "2026-07-26T10:30:00Z"
        transcript.write_text(
            json.dumps({
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "step_index": 1,
                "created_at": ts1,
                "content": "Hi",
            })
            + "\n"
            + json.dumps({
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "step_index": 5,
                "created_at": ts2,
                "content": "Hello",
            })
            + "\n",
            encoding="utf-8",
        )

        db_dir = antigravity_dir / "conversations"
        db_dir.mkdir()
        db_path = db_dir / f"{sid}.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE gen_metadata (idx INT PRIMARY KEY, data BLOB);")
        blob3 = make_gen_metadata_blob("gemini-3.6-flash", 400, 100, cached_tokens=50)
        conn.execute("INSERT INTO gen_metadata VALUES (?, ?);", (3, blob3))
        conn.commit()
        conn.close()

        since = datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)
        until = datetime(2026, 7, 26, 11, 0, tzinfo=timezone.utc)
        provider = AntigravityProvider(antigravity_dir=antigravity_dir)
        by_model = {}
        turns = provider.collect_usage(since, until, by_model)

        assert turns == 1
        assert "gemini-3.6-flash" in by_model
        mu = by_model["gemini-3.6-flash"]
        assert mu.input_tokens == 400
        assert mu.output_tokens == 100
        assert mu.cache_read == 50

    def test_unmatched_db_row_excluded_when_whole_session_outside_window(
        self, tmp_path, monkeypatch
    ):
        from ccstory.providers.antigravity import AntigravityProvider

        antigravity_dir = tmp_path / "antigravity"
        antigravity_dir.mkdir()

        sid = "compacted-session-outside-window"
        session_dir = antigravity_dir / "brain" / sid / ".system_generated" / "logs"
        session_dir.mkdir(parents=True)
        transcript = session_dir / "transcript.jsonl"

        ts1 = "2026-07-26T08:00:00Z"
        ts2 = "2026-07-26T08:30:00Z"
        transcript.write_text(
            json.dumps({
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "step_index": 1,
                "created_at": ts1,
                "content": "Hi",
            })
            + "\n"
            + json.dumps({
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "step_index": 5,
                "created_at": ts2,
                "content": "Hello",
            })
            + "\n",
            encoding="utf-8",
        )

        db_dir = antigravity_dir / "conversations"
        db_dir.mkdir()
        db_path = db_dir / f"{sid}.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE gen_metadata (idx INT PRIMARY KEY, data BLOB);")
        blob3 = make_gen_metadata_blob("gemini-3.6-flash", 400, 100)
        conn.execute("INSERT INTO gen_metadata VALUES (?, ?);", (3, blob3))
        conn.commit()
        conn.close()

        since = datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)
        until = datetime(2026, 7, 26, 11, 0, tzinfo=timezone.utc)
        provider = AntigravityProvider(antigravity_dir=antigravity_dir)
        by_model = {}
        turns = provider.collect_usage(since, until, by_model)

        assert turns == 0
        assert not by_model

    def test_boundary_crossing_linear_interpolation(self):
        from ccstory.providers.antigravity import _is_db_step_in_window

        step_timestamps = {
            1: datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc),
            10: datetime(2026, 7, 26, 10, 30, tzinfo=timezone.utc),
        }
        since = datetime(2026, 7, 26, 10, 15, tzinfo=timezone.utc)
        until = datetime(2026, 7, 26, 10, 45, tzinfo=timezone.utc)

        assert _is_db_step_in_window(3, step_timestamps, since, until) is False
        assert _is_db_step_in_window(7, step_timestamps, since, until) is True

    def test_boundary_crossing_single_sided_nearest_step(self):
        from ccstory.providers.antigravity import _is_db_step_in_window

        since = datetime(2026, 7, 26, 10, 15, tzinfo=timezone.utc)
        until = datetime(2026, 7, 26, 10, 45, tzinfo=timezone.utc)

        # Missing idx beyond higher end (only lower candidates exist)
        step_timestamps_high = {
            1: datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc),
            10: datetime(2026, 7, 26, 10, 30, tzinfo=timezone.utc),
            20: datetime(2026, 7, 26, 11, 30, tzinfo=timezone.utc),
        }
        assert _is_db_step_in_window(25, step_timestamps_high, since, until) is False

        step_timestamps_lower_in = {
            1: datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc),
            10: datetime(2026, 7, 26, 10, 30, tzinfo=timezone.utc),
        }
        assert _is_db_step_in_window(15, step_timestamps_lower_in, since, until) is True

        # Missing idx before lower end (only higher candidates exist)
        step_timestamps_higher_out = {
            10: datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc),
            20: datetime(2026, 7, 26, 11, 0, tzinfo=timezone.utc),
        }
        assert _is_db_step_in_window(5, step_timestamps_higher_out, since, until) is False

        step_timestamps_higher_in = {
            10: datetime(2026, 7, 26, 10, 30, tzinfo=timezone.utc),
            20: datetime(2026, 7, 26, 11, 0, tzinfo=timezone.utc),
        }
        assert _is_db_step_in_window(5, step_timestamps_higher_in, since, until) is True


class TestDbPriorityAndDedup:
    def test_db_row_takes_priority_over_transcript_fallback(self, tmp_path):
        from ccstory.providers.antigravity import AntigravityProvider

        antigravity_dir = tmp_path / "antigravity"
        antigravity_dir.mkdir()

        sid = "db-priority-session"
        session_dir = antigravity_dir / "brain" / sid / ".system_generated" / "logs"
        session_dir.mkdir(parents=True)
        transcript = session_dir / "transcript.jsonl"

        ts1 = "2026-07-26T10:00:00Z"
        ts2 = "2026-07-26T10:05:00Z"

        transcript.write_text(
            json.dumps({
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "step_index": 1,
                "created_at": ts1,
                "model": "gemini-3.6-flash",
                "usage": {"input_tokens": 999, "output_tokens": 999},
            })
            + "\n"
            + json.dumps({
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "step_index": 2,
                "created_at": ts2,
                "model": "gemini-3.6-flash",
                "usage": {"input_tokens": 200, "output_tokens": 50},
            })
            + "\n",
            encoding="utf-8",
        )

        db_dir = antigravity_dir / "conversations"
        db_dir.mkdir()
        db_path = db_dir / f"{sid}.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE gen_metadata (idx INT PRIMARY KEY, data BLOB);")
        blob1 = make_gen_metadata_blob("gemini-3.6-flash", 500, 100, cached_tokens=25)
        conn.execute("INSERT INTO gen_metadata VALUES (?, ?);", (1, blob1))
        conn.commit()
        conn.close()

        since = datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)
        until = datetime(2026, 7, 26, 11, 0, tzinfo=timezone.utc)
        provider = AntigravityProvider(antigravity_dir=antigravity_dir)
        by_model = {}
        turns = provider.collect_usage(since, until, by_model)

        assert turns == 2
        mu = by_model["gemini-3.6-flash"]
        assert mu.input_tokens == 700
        assert mu.output_tokens == 150
        assert mu.cache_read == 25
