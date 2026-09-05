"""Codex session parsing: text extraction, project attribution, transcript lookup."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ccstory.providers import TranscriptResolver, collect_multi_agent_sessions
from ccstory.providers.codex import CodexProvider
from ccstory.providers.projects import encode_project_dir


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
        """`/codex` requests stay parseable but retain delegated provenance."""
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
        assert stat.is_delegated is True
        assert stat.delegation_source == "claude_code"

    def test_claude_code_originator_marks_delegated_session(self, codex_factory):
        records = [
            {
                "timestamp": _ts(0),
                "type": "session_meta",
                "payload": {
                    "id": SID,
                    "cwd": "/Users/x/demo",
                    "originator": "Claude Code",
                },
            },
            _user("Review the plan", 1),
            _assistant("Reviewed.", 5),
        ]

        stat = CodexProvider().parse_session(codex_factory(SID, records))

        assert stat is not None
        assert stat.is_delegated is True
        assert stat.delegation_source == "claude_code"

    def test_codex_delegation_wrapper_marks_delegated_session(
        self, codex_factory
    ):
        path = codex_factory(
            SID,
            [
                _meta(SID, "/Users/x/demo"),
                _user(
                    "<codex_delegation><input>Run QA</input>"
                    "</codex_delegation>",
                    1,
                ),
                _assistant("Done.", 5),
            ],
        )

        stat = CodexProvider().parse_session(path)

        assert stat is not None
        assert stat.is_delegated is True
        assert stat.delegation_source == "codex_delegation"

    def test_automation_thread_source_sets_the_automation_marker(
        self, codex_factory
    ):
        """Codex's own automation-trigger marker becomes SCHEDULED-mode
        provenance on a dedicated field — never on `is_scheduled`, which
        relaxes `SessionStat.engaged`'s admission threshold and was
        measured to admit +41 sessions (+13.7%) when this marker was wired
        into it during #240 (see #136)."""
        records = [
            {
                "timestamp": _ts(0),
                "type": "session_meta",
                "payload": {
                    "id": SID,
                    "cwd": "/Users/x/demo",
                    "thread_source": "automation",
                },
            },
            _user("run the nightly clip intake", 1),
            _assistant("Done.", 5),
        ]

        stat = CodexProvider().parse_session(codex_factory(SID, records))

        assert stat is not None
        assert stat.is_automation is True
        assert stat.is_scheduled is False
        assert stat.is_delegated is False
        assert stat.delegation_source == ""

    def test_non_automation_thread_source_leaves_the_marker_unset(
        self, codex_factory
    ):
        """Guards against a naive `"thread_source" in payload` check: only
        the exact `"automation"` value counts."""
        records = [
            {
                "timestamp": _ts(0),
                "type": "session_meta",
                "payload": {
                    "id": SID,
                    "cwd": "/Users/x/demo",
                    "thread_source": "interactive",
                },
            },
            _user("hi", 1),
        ]

        stat = CodexProvider().parse_session(codex_factory(SID, records))

        assert stat is not None
        assert stat.is_automation is False

    def test_missing_thread_source_leaves_the_marker_unset(self, codex_factory):
        path = codex_factory(SID, [_meta(SID, "/Users/x/demo"), _user("hi", 1)])
        stat = CodexProvider().parse_session(path)
        assert stat is not None
        assert stat.is_automation is False

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
            encode_project_dir("/Users/atomo/Side_project/investment_note")
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


def _token_count(
    ts: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> dict:
    return {
        "timestamp": ts,
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


class TestCodexUsageCorrectness:
    def test_concurrent_branches_without_rollout_id_do_not_interleave(
        self, codex_factory,
    ):
        """When rollout `id` is absent, rollouts must not merge under shared `session_id`.

        Two physical files sharing `session_id` (parent and concurrent subagent)
        must not have their cumulative snapshots interleaved into one branch
        timeline. Doing so previously caused child deltas to be diffed against
        parent cumulative totals and vanish.
        """
        shared_thread_id = "019f0000-1111-7000-8000-000000000001"
        # Parent rollout: has session_id, but NO 'id'
        parent_path = codex_factory(
            "parent-no-id",
            [
                {
                    "timestamp": "2026-07-22T12:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": shared_thread_id,
                        "cwd": "/Users/x/demo",
                    },
                },
                {
                    "timestamp": "2026-07-22T12:00:01Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-terra"},
                },
                _user("parent task", 1),
                _token_count("2026-07-22T12:01:00Z", 500, 100, 50),
                _token_count("2026-07-22T12:03:00Z", 1000, 200, 100),
            ],
        )

        # Child subagent rollout: has session_id, parent_thread_id, but NO 'id'
        child_path = codex_factory(
            "child-no-id",
            [
                {
                    "timestamp": "2026-07-22T12:01:30Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": shared_thread_id,
                        "parent_thread_id": shared_thread_id,
                        "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                        "cwd": "/Users/x/demo",
                    },
                },
                {
                    "timestamp": "2026-07-22T12:01:31Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-sol"},
                },
                # Copied ancestor snapshot:
                _token_count("2026-07-22T12:01:32Z", 500, 100, 50),
                # Child's own growth (interleaved in time between parent's snapshots):
                _token_count("2026-07-22T12:02:00Z", 1300, 300, 250),
            ],
        )

        provider = CodexProvider()
        usage_by_window = {"w": {}}
        turns = provider.collect_usage_for_windows(
            {
                "w": (
                    datetime(2026, 7, 22, 11, tzinfo=timezone.utc),
                    datetime(2026, 7, 22, 13, tzinfo=timezone.utc),
                )
            },
            usage_by_window,
        )

        # Parent branch:
        # Snapshot 1: 500 in (100 cached), 50 out => 400 uncached in, 100 cached, 50 out
        # Snapshot 2: 1000 in (200 cached), 100 out => 400 uncached in, 100 cached, 50 out
        # Parent total: 800 uncached in, 200 cached, 100 out, 2 turns.
        #
        # Child branch:
        # Copied prefix: 500 in (100 cached), 50 out (matches parent)
        # Snapshot: 1300 in (300 cached), 250 out => delta: 800 in (200 cached), 200 out
        # => 600 uncached in, 200 cached, 200 out, 1 turn.
        #
        # Combined totals across distinct branches:
        terra = usage_by_window["w"]["gpt-5.6-terra"]
        sol = usage_by_window["w"]["gpt-5.6-sol"]

        assert terra.input_tokens == 800
        assert terra.cache_read == 200
        assert terra.output_tokens == 100
        assert terra.turns == 2

        assert sol.input_tokens == 600
        assert sol.cache_read == 200
        assert sol.output_tokens == 200
        assert sol.turns == 1

        assert turns["w"] == 3

    def test_decreasing_cumulative_counter_treated_as_reset(self, codex_factory):
        """When cumulative token counter drops mid-branch, treat as new baseline.

        A drop in total_token_usage within one branch must not be clipped to 0
        via max(0, delta) (silent token loss). It must be treated as a counter
        reset with the new value as the fresh delta.
        """
        codex_factory(
            "counter-reset",
            [
                {
                    "timestamp": "2026-07-22T12:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "reset-branch",
                        "session_id": "reset-branch",
                        "cwd": "/Users/x/demo",
                    },
                },
                {
                    "timestamp": "2026-07-22T12:00:01Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-sol"},
                },
                _user("task turn 1", 1),
                _token_count("2026-07-22T12:01:00Z", 1000, 200, 100),
                # Mid-branch counter reset:
                _token_count("2026-07-22T12:02:00Z", 300, 50, 50),
                # Normal growth after reset:
                _token_count("2026-07-22T12:03:00Z", 500, 100, 90),
            ],
        )

        provider = CodexProvider()
        usage_by_window = {"w": {}}
        turns = provider.collect_usage_for_windows(
            {
                "w": (
                    datetime(2026, 7, 22, 11, tzinfo=timezone.utc),
                    datetime(2026, 7, 22, 13, tzinfo=timezone.utc),
                )
            },
            usage_by_window,
        )

        sol = usage_by_window["w"]["gpt-5.6-sol"]
        # Turn 1: 1000 in (200 cached), 100 out => uncached in 800, cache 200, out 100
        # Turn 2 (reset): 300 in (50 cached), 50 out => uncached in 250, cache 50, out 50
        # Turn 3: delta: 200 in (50 cached), 40 out => uncached in 150, cache 50, out 40
        # Total uncached input: 800 + 250 + 150 = 1200
        # Total cached input: 200 + 50 + 50 = 300
        # Total output: 100 + 50 + 40 = 190
        assert sol.input_tokens == 1200
        assert sol.cache_read == 300
        assert sol.output_tokens == 190
        assert sol.turns == 3
        assert turns["w"] == 3
