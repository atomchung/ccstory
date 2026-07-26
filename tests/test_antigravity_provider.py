"""Antigravity session parsing, attribution, usage, subagent filtering, and registry contracts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

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
        assert agent_label("antigravity") == "Google Antigravity"
        assert create_providers("antigravity")[0].agent_name == "antigravity"
        assert _agent_title("antigravity", "Recap") == "Google Antigravity Recap"

        data_roots = _agent_data_roots("antigravity")
        assert any(name == "antigravity" for name, _ in data_roots)
        usage = collect_usage(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 30, tzinfo=timezone.utc),
            agent="antigravity",
        )
        assert usage.incomplete_agents == ["antigravity"]
        assert not usage.usage_complete

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
    model: str, input_tokens: int, output_tokens: int, extra_fields: bytes = b""
) -> bytes:
    usage_msg = (
        encode_varint_field(2, input_tokens)
        + encode_varint_field(3, output_tokens)
        + extra_fields
    )
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
