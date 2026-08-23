"""Observed workspace/project discovery contracts (#222).

Covers the provider-neutral core in `ccstory.project_discovery` (grouping,
canonicalization, bounding, deterministic ordering, close-spelling
candidates) and the `ccstory project list` CLI surface built on top of it.
`ccstory goal set`'s observed/unobserved guidance is covered alongside the
rest of the managed goal CLI in `tests/test_goal_cli.py`.
"""

from __future__ import annotations

import io
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from ccstory import cli as cli_module
from ccstory import project_discovery
from ccstory import session_summarizer
from ccstory.project_discovery import (
    DEFAULT_DISPLAY_CAP,
    JSON_HARD_MAX,
    ObservedProject,
    bounded_observed_projects,
    collect_observed_projects,
    find_close_project_candidates,
    observed_project_ids,
)
from ccstory.time_tracking import SessionStat

from tests.conftest import _ts, make_assistant_msg, make_user_msg

UTC = timezone.utc
SINCE = datetime(2026, 1, 1, tzinfo=UTC)
UNTIL = datetime(2026, 9, 1, tzinfo=UTC)


def _console() -> tuple[Console, io.StringIO]:
    stream = io.StringIO()
    return Console(file=stream, width=120), stream


def _session(
    project: str,
    session_id: str,
    start: datetime,
    end: datetime,
    *,
    agent: str = "claude",
    path: Path | None = None,
) -> SessionStat:
    return SessionStat(
        project=project,
        category="coding",
        session_id=session_id,
        start=start,
        end=end,
        active_sec=max(1, int((end - start).total_seconds())),
        msg_count=2,
        user_msg_count=2,
        first_user_text="private prompt text must not escape",
        cwd="/private/must-not-escape",
        timestamps=[start.timestamp(), end.timestamp()],
        agent=agent,
        path=path if path is not None else Path("/private/must-not-escape.jsonl"),
    )


def _stub_snapshot(monkeypatch: pytest.MonkeyPatch, sessions: list[SessionStat]) -> None:
    """Replace the provider seam with fixed sessions for one call.

    Mirrors `tests/test_goal_history.py`'s `fake_snapshot` pattern: the core
    under test never touches real transcripts, so grouping/ordering/bounding
    logic can be exercised precisely and fast.
    """

    def _fake(windows, *, engaged_only: bool = True, agent: str = "all"):
        assert set(windows) == {project_discovery._WINDOW_KEY}
        return SimpleNamespace(
            sessions_by_window={project_discovery._WINDOW_KEY: list(sessions)}
        )

    monkeypatch.setattr(project_discovery, "collect_provider_snapshot", _fake)


class TestCollectObservedProjects:
    def test_groups_by_canonical_identity_including_worktree_suffix(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """A Claude Code worktree folder folds onto its parent project (#222:
        "alias/nested workspace canonicalization")."""

        _stub_snapshot(
            monkeypatch,
            [
                _session("atlas-app", "s1", SINCE, SINCE + timedelta(minutes=10)),
                _session(
                    "atlas-app--claude-worktrees-lucid-turing-9f8e7d",
                    "s2",
                    SINCE + timedelta(days=1),
                    SINCE + timedelta(days=1, minutes=5),
                ),
            ],
        )
        observed = collect_observed_projects(SINCE, UNTIL)
        assert [p.project_id for p in observed] == ["atlas-app"]
        assert observed[0].session_count == 2
        assert observed[0].agents == ("claude",)
        assert observed[0].last_seen == SINCE + timedelta(days=1, minutes=5)

    def test_duplicate_aliases_collapse_to_one_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        _stub_snapshot(
            monkeypatch,
            [
                _session("borealis-legacy", "s1", SINCE, SINCE + timedelta(minutes=5)),
                _session(
                    "borealis-old",
                    "s2",
                    SINCE + timedelta(days=2),
                    SINCE + timedelta(days=2, minutes=5),
                ),
            ],
        )
        aliases = {"borealis-legacy": "borealis-app", "borealis-old": "borealis-app"}
        observed = collect_observed_projects(SINCE, UNTIL, aliases=aliases)
        assert [p.project_id for p in observed] == ["borealis-app"]
        assert observed[0].session_count == 2
        assert observed[0].last_seen == SINCE + timedelta(days=2, minutes=5)

    def test_multi_provider_same_project_dedupes_with_every_agent_listed(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        _stub_snapshot(
            monkeypatch,
            [
                _session(
                    "borealis-app", "s1", SINCE, SINCE + timedelta(minutes=5),
                    agent="claude",
                ),
                _session(
                    "-Users-synthetic-borealis-app",
                    "s2",
                    SINCE + timedelta(days=1),
                    SINCE + timedelta(days=1, minutes=5),
                    agent="codex",
                ),
            ],
        )
        observed = collect_observed_projects(SINCE, UNTIL)
        assert [p.project_id for p in observed] == ["borealis-app"]
        assert observed[0].agents == ("claude", "codex")
        assert observed[0].session_count == 2

    def test_order_is_deterministic_regardless_of_input_permutation(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        sessions = [
            _session("alpha", "a1", SINCE, SINCE + timedelta(minutes=5)),
            _session(
                "beta", "b1",
                SINCE + timedelta(days=5), SINCE + timedelta(days=5, minutes=5),
            ),
            # Ties `beta` on last_seen exactly — the canonical-id tie-break
            # must decide, not insertion order.
            _session(
                "gamma", "g1",
                SINCE + timedelta(days=5), SINCE + timedelta(days=5, minutes=5),
            ),
            _session(
                "delta", "d1",
                SINCE + timedelta(days=10), SINCE + timedelta(days=10, minutes=5),
            ),
        ]
        rng = random.Random(1234)
        orders = []
        for _ in range(6):
            shuffled = sessions[:]
            rng.shuffle(shuffled)
            _stub_snapshot(monkeypatch, shuffled)
            observed = collect_observed_projects(SINCE, UNTIL)
            orders.append([p.project_id for p in observed])

        assert all(order == orders[0] for order in orders)
        # Most-recently-observed first; a same-instant tie breaks on the
        # canonical project id (issue #222).
        assert orders[0] == ["delta", "beta", "gamma", "alpha"]

    def test_empty_history_returns_empty_tuple(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        _stub_snapshot(monkeypatch, [])
        assert collect_observed_projects(SINCE, UNTIL) == ()

    def test_never_exposes_transcript_path_or_session_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        marker_path = Path("/private/must-not-escape/marker-session-id.jsonl")
        _stub_snapshot(
            monkeypatch,
            [
                _session(
                    "atlas-app",
                    "marker-session-id",
                    SINCE,
                    SINCE + timedelta(minutes=5),
                    path=marker_path,
                )
            ],
        )
        observed = collect_observed_projects(SINCE, UNTIL)
        rendered = json.dumps([p.to_dict() for p in observed])
        assert "marker-session-id" not in rendered
        assert "must-not-escape" not in rendered
        assert set(observed[0].to_dict()) == {
            "project_id", "last_seen", "session_count", "agents",
        }


class TestBoundedObservedProjects:
    def test_caps_without_reordering_and_reports_true_total(self):
        projects = tuple(
            ObservedProject(
                project_id=f"project-{i:03d}",
                last_seen=SINCE + timedelta(days=i),
                session_count=1,
                agents=("claude",),
            )
            for i in range(JSON_HARD_MAX + 50)
        )
        capped, total = bounded_observed_projects(projects, JSON_HARD_MAX)
        assert total == JSON_HARD_MAX + 50
        assert len(capped) == JSON_HARD_MAX
        assert capped == projects[:JSON_HARD_MAX]

    def test_cap_larger_than_population_returns_everything(self):
        projects = tuple(
            ObservedProject(
                project_id=f"project-{i}",
                last_seen=SINCE,
                session_count=1,
                agents=("claude",),
            )
            for i in range(3)
        )
        capped, total = bounded_observed_projects(projects, 100)
        assert capped == projects
        assert total == 3


class TestFindCloseProjectCandidates:
    _POOL = ["atlas-app", "borealis-api", "borealis-app"]

    def test_ambiguous_candidates_include_every_close_match(self):
        # Verified empirically: difflib.get_close_matches("boreal-ap", pool)
        # returns both similarly-named observed projects, highest ratio
        # first — this is guidance, and the caller must never auto-pick one.
        assert find_close_project_candidates("boreal-ap", self._POOL) == (
            "borealis-app",
            "borealis-api",
        )

    def test_no_match_below_cutoff_returns_empty(self):
        assert find_close_project_candidates("future-app", self._POOL) == ()
        assert find_close_project_candidates("not-yet-seen-xyz", self._POOL) == ()

    def test_result_is_independent_of_pool_iteration_order(self):
        forward = find_close_project_candidates("boreal-ap", self._POOL)
        backward = find_close_project_candidates(
            "boreal-ap", list(reversed(self._POOL)),
        )
        assert forward == backward

    def test_never_suggests_the_exact_candidate_itself(self):
        assert find_close_project_candidates("ccstory", ["ccstory"]) == ()


class TestObservedProjectIds:
    def test_preserves_order(self):
        projects = (
            ObservedProject("zeta", SINCE, 1, ("claude",)),
            ObservedProject("alpha", SINCE, 1, ("claude",)),
        )
        assert observed_project_ids(projects) == ("zeta", "alpha")


class TestProjectListCLI:
    def test_lists_observed_project_and_json_matches_table(
        self, jsonl_factory, capsys: pytest.CaptureFixture[str],
    ):
        jsonl_factory(
            "atlas-app",
            "s1",
            [
                make_user_msg(
                    "first acceptance message long enough to engage",
                    _ts(2026, 3, 1),
                ),
                make_assistant_msg("ack", _ts(2026, 3, 1, 0, 1), "a1"),
                make_user_msg(
                    "second acceptance message keeps this engaged",
                    _ts(2026, 3, 1, 0, 5),
                ),
            ],
        )

        console, stream = _console()
        assert cli_module._run_project(["list"], console) == 0
        rendered = stream.getvalue()
        assert "atlas-app" in rendered
        assert "2026-03-01" in rendered
        assert "claude" in rendered

        json_console, _ = _console()
        assert cli_module._run_project(["list", "--json"], json_console) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["window"] == "all"
        assert payload["agent"] == "all"
        assert payload["truncated"] is False
        assert payload["hard_max"] == JSON_HARD_MAX
        assert payload["count"] == payload["total_count"] == 1
        assert payload["projects"] == [
            {
                "project_id": "atlas-app",
                "last_seen": payload["projects"][0]["last_seen"],
                "session_count": 1,
                "agents": ["claude"],
            }
        ]

    def test_empty_window_prints_a_clear_message_not_an_error(self):
        console, stream = _console()
        assert cli_module._run_project(["list"], console) == 0
        assert "No observed projects" in stream.getvalue()

    def test_empty_window_json_is_a_valid_empty_envelope(
        self, capsys: pytest.CaptureFixture[str],
    ):
        console, stream = _console()
        assert cli_module._run_project(["list", "--json"], console) == 0
        assert stream.getvalue() == ""
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["count"] == payload["total_count"] == 0
        assert payload["truncated"] is False
        assert payload["projects"] == []

    def test_window_flag_narrows_population(self, jsonl_factory):
        jsonl_factory(
            "old-app",
            "s1",
            [
                make_user_msg(
                    "an old message well outside the recent window",
                    _ts(2020, 1, 1),
                ),
                make_user_msg(
                    "second old message keeps this session engaged",
                    _ts(2020, 1, 1, 0, 5),
                ),
            ],
        )
        console, stream = _console()
        assert cli_module._run_project(["list", "--window", "month"], console) == 0
        rendered = stream.getvalue()
        assert "old-app" not in rendered
        assert "No observed projects" in rendered

        all_console, all_stream = _console()
        assert cli_module._run_project(["list", "--window", "all"], all_console) == 0
        assert "old-app" in all_stream.getvalue()

    def test_bad_window_is_a_clean_error_not_a_traceback(
        self, capsys: pytest.CaptureFixture[str],
    ):
        console, stream = _console()
        assert cli_module._run_project(["list", "--window", "bogus"], console) == 1
        assert stream.getvalue() == ""
        assert "unrecognized window" in capsys.readouterr().err

    def test_agent_filter_narrows_to_one_provider(
        self, jsonl_factory, codex_factory,
    ):
        jsonl_factory(
            "claude-app",
            "s1",
            [
                make_user_msg(
                    "claude first engaging message for this session",
                    _ts(2026, 3, 1),
                ),
                make_user_msg(
                    "claude second engaging message for this session",
                    _ts(2026, 3, 1, 0, 5),
                ),
            ],
        )
        codex_factory(
            "codexsess",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "codexsess",
                        "cwd": "/Users/synthetic/codex-app",
                    },
                },
                {
                    "timestamp": _ts(2026, 3, 2),
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "codex first engaging message here",
                    },
                },
                {
                    "timestamp": _ts(2026, 3, 2, 0, 2),
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "codex reply text",
                    },
                },
                {
                    "timestamp": _ts(2026, 3, 2, 0, 5),
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "codex second engaging message here",
                    },
                },
            ],
        )

        console, stream = _console()
        assert cli_module._run_project(["list", "--agent", "codex"], console) == 0
        rendered = stream.getvalue()
        assert "claude-app" not in rendered
        assert "codex-app" in rendered

    def test_large_population_is_capped_in_terminal_and_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ):
        sessions = [
            _session(
                f"project-{i:04d}", f"s{i}",
                SINCE + timedelta(days=i), SINCE + timedelta(days=i, minutes=5),
            )
            for i in range(DEFAULT_DISPLAY_CAP + 5)
        ]
        _stub_snapshot(monkeypatch, sessions)

        console, stream = _console()
        assert cli_module._run_project(["list"], console) == 0
        rendered = stream.getvalue()
        assert f"Showing {DEFAULT_DISPLAY_CAP} of {DEFAULT_DISPLAY_CAP + 5}" in rendered

        json_console, _ = _console()
        assert cli_module._run_project(["list", "--json"], json_console) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == min(DEFAULT_DISPLAY_CAP + 5, JSON_HARD_MAX)
        assert payload["total_count"] == DEFAULT_DISPLAY_CAP + 5

    def test_never_calls_the_narrator(
        self, jsonl_factory, monkeypatch: pytest.MonkeyPatch,
    ):
        jsonl_factory(
            "atlas-app",
            "s1",
            [
                make_user_msg("engaging enough first message text", _ts(2026, 3, 1)),
                make_user_msg("engaging enough second message text", _ts(2026, 3, 1, 0, 5)),
            ],
        )

        def _forbidden() -> bool:
            raise AssertionError("ccstory project list must make zero model calls")

        monkeypatch.setattr(session_summarizer, "llm_available", _forbidden)

        console, _ = _console()
        assert cli_module._run_project(["list"], console) == 0
        json_console, _ = _console()
        assert cli_module._run_project(["list", "--json"], json_console) == 0


class TestDirectAcceptanceFlow:
    """Issue #222's four-step direct acceptance, run against real transcripts.

    Fresh isolated HOME (the autouse `tmp_home` fixture) with one synthetic
    session: `project list` makes the exact string discoverable, `goal set`
    confirms it as observed, a second `goal set` for a not-yet-seen project
    succeeds and is clearly labeled unobserved, and a subsequent recap's
    GoalContext selection behaves exactly as the existing contract.
    """

    def test_discover_observed_goal_unobserved_goal_recap_unchanged(
        self, jsonl_factory, capsys: pytest.CaptureFixture[str],
    ):
        jsonl_factory(
            "acceptance-app",
            "s1",
            [
                make_user_msg(
                    "first acceptance message long enough to engage",
                    _ts(2026, 3, 1),
                ),
                make_user_msg(
                    "second acceptance message also engaging enough",
                    _ts(2026, 3, 1, 0, 5),
                ),
            ],
        )

        # Step 1: `ccstory project list` makes the exact string discoverable.
        json_console, _ = _console()
        assert cli_module._run_project(["list", "--json"], json_console) == 0
        list_payload = json.loads(capsys.readouterr().out)
        assert list_payload["projects"][0]["project_id"] == "acceptance-app"
        observed_id = list_payload["projects"][0]["project_id"]

        # Step 2: `goal set ... --project <observed>` confirms an observed
        # match.
        console, stream = _console()
        assert cli_module._run_goal(
            [
                "set", "acceptance-goal",
                "--title", "Acceptance goal",
                "--project", observed_id,
            ],
            console,
        ) == 0
        rendered = stream.getvalue()
        assert "observed:" in rendered
        assert f"`{observed_id}`" in rendered

        # Step 3: `goal set ... --project <future>` succeeds but clearly
        # reports it is currently unobserved; the value is preserved.
        future_console, future_stream = _console()
        assert cli_module._run_goal(
            [
                "set", "future-goal",
                "--title", "Future goal",
                "--project", "not-yet-seen-xyz",
            ],
            future_console,
        ) == 0
        assert "unobserved:" in future_stream.getvalue()

        from ccstory.goal_store import (
            load_managed_goal_context,
            managed_goal_context_path,
            resolve_goal_context_source,
        )
        from ccstory.categorizer import load_project_aliases

        path = managed_goal_context_path()
        stored = load_managed_goal_context(path, aliases={})
        future_goal = next(g for g in stored.goals if g.id == "future-goal")
        assert future_goal.projects == ("not-yet-seen-xyz",)

        # Step 4: a subsequent recap's GoalContext selection behaves exactly
        # as the existing contract — both goals load, unchanged shape.
        ctx = resolve_goal_context_source(
            config_path=cli_module.CONFIG_PATH,
            aliases=load_project_aliases(cli_module.CONFIG_PATH),
        )
        assert ctx is not None
        assert {g.id for g in ctx.goals} == {"acceptance-goal", "future-goal"}
        acceptance_goal = next(
            g for g in ctx.goals if g.id == "acceptance-goal"
        )
        assert acceptance_goal.projects == ("acceptance-app",)
