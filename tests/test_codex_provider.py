"""Codex session parsing: text extraction, project attribution, transcript lookup."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ccstory.providers import TranscriptResolver, collect_multi_agent_sessions
from ccstory.providers.codex import CodexProvider, _encode_project_dir


def _ts(minute: int) -> str:
    return datetime(2026, 7, 22, 12, minute, tzinfo=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _meta(session_id: str, cwd: str, minute: int = 0) -> dict:
    return {
        "timestamp": _ts(minute),
        "type": "session_meta",
        "payload": {"session_id": session_id, "cwd": cwd},
    }


def _user(text: str, minute: int) -> dict:
    return {
        "timestamp": _ts(minute),
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    }


def _injected_user(text: str, minute: int) -> dict:
    """A `response_item` user record — the harness-injected twin of a turn."""
    return {
        "timestamp": _ts(minute),
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _assistant(text: str, minute: int) -> dict:
    return {
        "timestamp": _ts(minute),
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


@pytest.fixture
def codex_factory(tmp_home: Path):
    """Write a Codex rollout transcript into the fake home. Returns its path."""

    def _make(session_id: str, records: list[dict], archived: bool = False) -> Path:
        root = tmp_home / ".codex" / (
            "archived_sessions" if archived else "sessions"
        ) / "2026" / "07" / "22"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"rollout-2026-07-22T12-00-00-{session_id}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return path

    return _make


SID = "019f8a2c-2df3-7f01-b55d-b8dcae9f2516"


class TestCodexParsing:
    def test_reads_user_turns_and_ignores_injected_context(self, codex_factory):
        path = codex_factory(
            SID,
            [
                _meta(SID, "/Users/x/Side_project/demo"),
                _injected_user("<recommended_plugins>\nAtlassian Rovo\n", 1),
                _user("fix the flaky retry test", 2),
                _assistant("Patched the retry helper.", 6),
                _user("now add a regression test", 9),
            ],
        )
        stat = CodexProvider().parse_session(path)

        assert stat is not None
        assert stat.agent == "codex"
        assert stat.user_msg_count == 2
        assert stat.first_user_text == "fix the flaky retry test"
        assert stat.path == path

    def test_session_id_comes_from_the_record_not_the_filename(self, codex_factory):
        path = codex_factory(SID, [_meta(SID, "/Users/x/demo"), _user("hi", 1)])
        stat = CodexProvider().parse_session(path)
        assert stat.session_id == SID

    def test_rollout_id_wins_over_the_shared_thread_id(self, codex_factory):
        """`session_id` is the thread root — every resumed rollout of a thread
        repeats it, so keying the summary cache on it makes them overwrite each
        other. `id` identifies this rollout."""
        thread = "019f6b8b-9c58-75d2-8417-b08b44af753e"
        records = [
            {
                "timestamp": _ts(0),
                "type": "session_meta",
                "payload": {"session_id": thread, "id": SID, "cwd": "/Users/x/demo"},
            },
            _user("resume the migration", 1),
            _assistant("done", 5),
        ]
        stat = CodexProvider().parse_session(codex_factory(SID, records))
        assert stat.session_id == SID

    def test_subagent_threads_are_skipped(self, codex_factory):
        """A spawned subagent's turns already count toward its parent's wall
        clock; a second SessionStat for them is a double count."""
        records = [
            {
                "timestamp": _ts(0),
                "type": "session_meta",
                "payload": {
                    "session_id": "019f6b8b-9c58-75d2-8417-b08b44af753e",
                    "id": SID,
                    "cwd": "/Users/x/demo",
                    "parent_thread_id": "019f6b8b-9c58-75d2-8417-b08b44af753e",
                    "source": {"subagent": {"thread_spawn": {}}},
                },
            },
            # Subagents do record user turns, so `engaged` will not filter them.
            _user("run the sweep", 1),
            _assistant("swept", 8),
        ]
        assert CodexProvider().parse_session(codex_factory(SID, records)) is None

    def test_task_wrapper_is_unwrapped_not_dropped(self, codex_factory):
        """`/codex` dispatches wrap the request in <task>…</task> — a real turn."""
        path = codex_factory(
            SID,
            [
                _meta(SID, "/Users/x/demo"),
                _user("<task>\nReview PR #169 in investment_note\n</task>", 1),
                _assistant("Reviewed.", 5),
            ],
        )
        stat = CodexProvider().parse_session(path)
        assert stat.first_user_text == "Review PR #169 in investment_note"
        assert stat.user_msg_count == 1

    def test_transcript_without_timestamps_is_skipped(self, codex_factory):
        path = codex_factory(SID, [{"type": "session_meta", "payload": {"cwd": "/x"}}])
        assert CodexProvider().parse_session(path) is None

    def test_bookkeeping_events_do_not_inflate_active_time(self, codex_factory):
        """`token_count` fires between turns; counting it would shrink the gaps
        the 5-minute idle cap is supposed to discard."""
        dense = [
            {
                "timestamp": _ts(m),
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {}},
            }
            for m in range(1, 20)
        ]
        records = [_meta(SID, "/Users/x/demo"), _user("go", 0)] + dense + [
            _assistant("done", 20)
        ]
        stat = CodexProvider().parse_session(codex_factory(SID, records))
        # One 20-minute gap, capped at 5 minutes.
        assert stat.active_sec == 300


class TestCodexProjectAttribution:
    def test_cwd_is_encoded_like_a_claude_project_dir(self):
        assert (
            _encode_project_dir("/Users/atomo/Side_project/investment_note")
            == "-Users-atomo-Side-project-investment-note"
        )

    def test_in_repo_worktree_folds_back_to_the_parent_project(self, codex_factory):
        """A `.claude/worktrees/<name>` cwd must not mint a throwaway project."""
        from ccstory.categorizer import normalize_project_name

        cwd = (
            "/Users/atomo/Side_project/investment_note"
            "/.claude/worktrees/mk-podcast-update-4ce60a"
        )
        path = codex_factory(SID, [_meta(SID, cwd), _user("go", 1), _assistant("k", 5)])
        stat = CodexProvider().parse_session(path)
        assert normalize_project_name(stat.project) == "investment-note"

    def test_out_of_tree_worktree_resolves_through_the_git_pointer(
        self, codex_factory, tmp_home
    ):
        """Codex parks worktrees outside the repo, so only git knows the origin."""
        from ccstory.categorizer import normalize_project_name

        repo = tmp_home / "Side_project" / "kol_collector" / "fomo-kernel"
        (repo / ".git" / "worktrees" / "fomo-kernel3").mkdir(parents=True)
        wt = tmp_home / ".codex" / "worktrees" / "6ffd" / "fomo-kernel"
        wt.mkdir(parents=True)
        (wt / ".git").write_text(
            f"gitdir: {repo / '.git' / 'worktrees' / 'fomo-kernel3'}\n"
        )

        path = codex_factory(
            SID, [_meta(SID, str(wt)), _user("go", 1), _assistant("k", 5)]
        )
        stat = CodexProvider().parse_session(path)
        leaf = normalize_project_name(stat.project)
        # tmp_path lives under /private/var/..., which no stem hint strips, so
        # assert on the tail: the origin repo, with no worktree hash in sight.
        assert leaf.endswith("kol-collector-fomo-kernel")
        assert "6ffd" not in leaf and "codex-worktrees" not in leaf

    def test_pruned_worktree_degrades_to_the_recorded_path(self, codex_factory):
        """The checkout is gone; attribution falls back rather than crashing."""
        path = codex_factory(
            SID,
            [
                _meta(SID, "/Users/atomo/.codex/worktrees/dead/fomo-kernel"),
                _user("go", 1),
                _assistant("k", 5),
            ],
        )
        stat = CodexProvider().parse_session(path)
        assert stat.project.endswith("fomo-kernel")


class TestCodexCollection:
    def test_collects_active_and_archived_transcripts(self, codex_factory):
        codex_factory(SID, [_meta(SID, "/Users/x/demo"), _user("a", 1),
                            _assistant("b", 5)])
        other = "019f0000-0000-7000-8000-000000000001"
        codex_factory(other, [_meta(other, "/Users/x/demo"), _user("c", 1),
                              _assistant("d", 5)], archived=True)

        since = datetime(2026, 7, 1, tzinfo=timezone.utc)
        stats = collect_multi_agent_sessions(since, agent="codex")
        assert {s.session_id for s in stats} == {SID, other}

    def test_agent_filter_rejects_unknown_names(self):
        with pytest.raises(ValueError, match="Unsupported agent filter"):
            collect_multi_agent_sessions(
                datetime.now(timezone.utc) - timedelta(days=1), agent="nope"
            )


class TestTranscriptResolution:
    def test_resolves_by_session_id_when_the_stat_has_no_path(self, codex_factory):
        """Cache-rebuilt stats carry an id but no path — the id embeds in the
        filename, so this must not require re-walking the tree per session."""
        from ccstory.time_tracking import SessionStat

        path = codex_factory(SID, [_meta(SID, "/Users/x/demo"), _user("a", 1)])
        stat = SessionStat(
            project="-Users-x-demo", category="", session_id=SID,
            start=datetime.now(timezone.utc), end=datetime.now(timezone.utc),
            active_sec=60, msg_count=2, agent="codex",
        )
        assert TranscriptResolver().path_for(stat) == path

    def test_missing_transcript_resolves_to_none(self):
        from ccstory.time_tracking import SessionStat

        stat = SessionStat(
            project="-Users-x-demo", category="", session_id="gone",
            start=datetime.now(timezone.utc), end=datetime.now(timezone.utc),
            active_sec=60, msg_count=2, agent="codex",
        )
        assert TranscriptResolver().path_for(stat) is None
