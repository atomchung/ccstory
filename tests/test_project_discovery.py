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
    FILTERED_RELEVANCE_REASONS,
    JSON_HARD_MAX,
    RELEVANCE_EPHEMERAL_ROOT,
    RELEVANCE_RELEVANT,
    RELEVANCE_SYNTHETIC_DATED_ID,
    ObservedProject,
    bounded_observed_projects,
    collect_observed_projects,
    find_close_project_candidates,
    is_synthetic_dated_project_id,
    observed_project_ids,
    partition_by_relevance,
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
    cwd: str = "/private/must-not-escape",
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
        cwd=cwd,
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
            # Additive (#254): decided facts about the identity, never a new
            # evidence source.
            "category", "category_source", "relevance",
        }


class TestEphemeralRootRelevance:
    """Rule 1 (#254): a workspace whose root is operating-system scratch space.

    Decided from path evidence — the recorded `cwd`, or the encoded project
    folder when a pre-cwd-era transcript left `cwd` empty — never from the
    canonical id's spelling.
    """

    @pytest.mark.parametrize(
        "cwd",
        [
            "/tmp/scratch-run",
            "/private/tmp/screen-20260817-axis2-audit-ppcpug",
            "/var/tmp/lane-runs",
            "/private/var/tmp/lane-runs",
            "/var/folders/qx/T/pytest-of-alice/pytest-3/scratch",
            "file:///private/tmp/editor-supplied-uri",
        ],
    )
    def test_every_temp_root_spelling_is_filtered(
        self, monkeypatch: pytest.MonkeyPatch, cwd: str,
    ):
        _stub_snapshot(
            monkeypatch,
            [
                _session(
                    "scratch-workspace", "s1", SINCE, SINCE + timedelta(minutes=5),
                    cwd=cwd,
                )
            ],
        )
        observed = collect_observed_projects(SINCE, UNTIL)
        assert observed[0].relevance == RELEVANCE_EPHEMERAL_ROOT

    def test_encoded_project_is_the_fallback_when_cwd_is_empty(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Pre-cwd-era transcripts still carry the temp root in the folder name."""

        _stub_snapshot(
            monkeypatch,
            [
                _session(
                    "-private-tmp-screen-20260817-axis2-audit-ppcpug",
                    "s1", SINCE, SINCE + timedelta(minutes=5),
                    cwd="",
                )
            ],
        )
        observed = collect_observed_projects(SINCE, UNTIL)
        assert observed[0].project_id == (
            "private-tmp-screen-20260817-axis2-audit-ppcpug"
        )
        assert observed[0].relevance == RELEVANCE_EPHEMERAL_ROOT

    @pytest.mark.parametrize(
        ("project", "cwd"),
        [
            # Leaf merely *contains* "tmp" — a real repository, not scratch.
            ("-Users-alice-code-tmp-runner", "/Users/alice/code/tmp-runner"),
            ("-Users-alice-code-tmpdata", "/Users/alice/code/tmpdata"),
            # A sibling of /tmp, not a child of it.
            ("-tmpfs-live-app", "/tmpfs/live-app"),
            # `var`/`private` alone are not temp roots.
            ("-var-www-storefront", "/var/www/storefront"),
            ("-private-repos-storefront", "/private/repos/storefront"),
        ],
    )
    def test_tmp_lookalikes_are_never_filtered(
        self, monkeypatch: pytest.MonkeyPatch, project: str, cwd: str,
    ):
        _stub_snapshot(
            monkeypatch,
            [_session(project, "s1", SINCE, SINCE + timedelta(minutes=5), cwd=cwd)],
        )
        observed = collect_observed_projects(SINCE, UNTIL)
        assert observed[0].relevance == RELEVANCE_RELEVANT

    def test_encoded_fallback_does_not_match_a_tmp_lookalike(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        _stub_snapshot(
            monkeypatch,
            [
                _session(
                    "-Users-alice-code-tmp-runner", "s1",
                    SINCE, SINCE + timedelta(minutes=5), cwd="",
                )
            ],
        )
        observed = collect_observed_projects(SINCE, UNTIL)
        assert observed[0].relevance == RELEVANCE_RELEVANT

    def test_one_durable_session_keeps_the_whole_project_visible(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """An alias that folds a scratch checkout onto a real project must not
        hide the real project — the verdict needs *every* member ephemeral."""

        _stub_snapshot(
            monkeypatch,
            [
                _session(
                    "-private-tmp-atlas-app-copy", "s1",
                    SINCE, SINCE + timedelta(minutes=5),
                    cwd="/private/tmp/atlas-app-copy",
                ),
                _session(
                    "-Users-alice-code-atlas-app", "s2",
                    SINCE + timedelta(days=1), SINCE + timedelta(days=1, minutes=5),
                    cwd="/Users/alice/code/atlas-app",
                ),
            ],
        )
        aliases = {"private-tmp-atlas-app-copy": "atlas-app"}
        observed = collect_observed_projects(SINCE, UNTIL, aliases=aliases)
        assert [p.project_id for p in observed] == ["atlas-app"]
        assert observed[0].session_count == 2
        assert observed[0].relevance == RELEVANCE_RELEVANT


class TestSyntheticDatedIdRelevance:
    """Rule 2 (#254): the per-day workspace an agent synthesizes for a
    folderless chat.

    Verified shape on real local history: the Codex desktop app parks such a
    chat in `~/Documents/Codex/<YYYY-MM-DD>/<leaf>`, which normalizes to
    `codex-<YYYY-MM-DD>-<leaf>` once the `Documents` path stem is dropped.
    """

    @pytest.mark.parametrize(
        "project_id",
        [
            "codex-2026-08-21-new-chat",
            "codex-2026-07-24-codex",
            "claude-2026-08-21-new-chat",
            "antigravity-2026-08-21-new-chat",
        ],
    )
    def test_agent_dated_placeholder_ids_match(self, project_id: str):
        assert is_synthetic_dated_project_id(project_id) is True

    @pytest.mark.parametrize(
        "project_id",
        [
            # A real project whose name happens to carry a release date —
            # no agent token, so never touched.
            "release-2026-08-21-notes",
            "sprint-2026-08-21",
            # Agent token, but no date.
            "codex-plugin",
            "codex-new-chat",
            # Agent token and a date-shaped run, but not a real calendar date.
            "codex-2026-13-05-new-chat",
            "codex-2026-08-32-new-chat",
            # Structurally short of the shape: no leaf after the date.
            "codex-2026-08-21",
            # Date not directly after the agent token.
            "codex-app-2026-08-21-notes",
            # Not a registered agent.
            "cursor-2026-08-21-new-chat",
        ],
    )
    def test_lookalike_project_names_never_match(self, project_id: str):
        assert is_synthetic_dated_project_id(project_id) is False

    def test_dated_id_is_reported_through_collection(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        _stub_snapshot(
            monkeypatch,
            [
                _session(
                    "-Users-alice-Documents-Codex-2026-08-21-new-chat", "s1",
                    SINCE, SINCE + timedelta(minutes=5),
                    cwd="/Users/alice/Documents/Codex/2026-08-21/new-chat",
                    agent="codex",
                ),
                _session(
                    "-Users-alice-code-release-2026-08-21-notes", "s2",
                    SINCE + timedelta(days=1), SINCE + timedelta(days=1, minutes=5),
                    cwd="/Users/alice/code/release-2026-08-21-notes",
                ),
            ],
        )
        observed = collect_observed_projects(SINCE, UNTIL)
        verdicts = {p.project_id: p.relevance for p in observed}
        assert verdicts == {
            "codex-2026-08-21-new-chat": RELEVANCE_SYNTHETIC_DATED_ID,
            "release-2026-08-21-notes": RELEVANCE_RELEVANT,
        }


class TestPartitionByRelevance:
    def test_vocabulary_is_closed_and_default_is_relevant(self):
        assert RELEVANCE_RELEVANT not in FILTERED_RELEVANCE_REASONS
        assert set(FILTERED_RELEVANCE_REASONS) == {
            RELEVANCE_EPHEMERAL_ROOT, RELEVANCE_SYNTHETIC_DATED_ID,
        }
        assert ObservedProject(
            "atlas-app", SINCE, 1, ("claude",),
        ).relevance == RELEVANCE_RELEVANT

    def test_split_preserves_order_and_loses_nothing(self):
        projects = (
            ObservedProject("atlas-app", SINCE, 1, ("claude",)),
            ObservedProject(
                "codex-2026-08-21-new-chat", SINCE, 1, ("codex",),
                relevance=RELEVANCE_SYNTHETIC_DATED_ID,
            ),
            ObservedProject("borealis-api", SINCE, 1, ("claude",)),
            ObservedProject(
                "private-tmp-scratch", SINCE, 1, ("claude",),
                relevance=RELEVANCE_EPHEMERAL_ROOT,
            ),
        )
        relevant, filtered = partition_by_relevance(projects)
        assert [p.project_id for p in relevant] == ["atlas-app", "borealis-api"]
        assert [p.project_id for p in filtered] == [
            "codex-2026-08-21-new-chat", "private-tmp-scratch",
        ]
        assert len(relevant) + len(filtered) == len(projects)

    def test_relevance_and_order_are_permutation_independent(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        sessions = [
            _session(
                "-Users-alice-code-atlas-app", "a1",
                SINCE + timedelta(days=3), SINCE + timedelta(days=3, minutes=5),
                cwd="/Users/alice/code/atlas-app",
            ),
            _session(
                "-private-tmp-scratch-run", "t1",
                SINCE + timedelta(days=2), SINCE + timedelta(days=2, minutes=5),
                cwd="/private/tmp/scratch-run",
            ),
            _session(
                "-Users-alice-Documents-Codex-2026-08-21-new-chat", "c1",
                SINCE + timedelta(days=1), SINCE + timedelta(days=1, minutes=5),
                cwd="/Users/alice/Documents/Codex/2026-08-21/new-chat",
                agent="codex",
            ),
            _session(
                "-Users-alice-code-tmp-runner", "r1",
                SINCE, SINCE + timedelta(minutes=5),
                cwd="/Users/alice/code/tmp-runner",
            ),
        ]
        rng = random.Random(9876)
        runs = []
        for _ in range(6):
            shuffled = sessions[:]
            rng.shuffle(shuffled)
            _stub_snapshot(monkeypatch, shuffled)
            observed = collect_observed_projects(SINCE, UNTIL)
            relevant, filtered = partition_by_relevance(observed)
            runs.append(
                (
                    [p.project_id for p in relevant],
                    [(p.project_id, p.relevance) for p in filtered],
                )
            )

        assert all(run == runs[0] for run in runs)
        assert runs[0] == (
            ["atlas-app", "tmp-runner"],
            [
                ("private-tmp-scratch-run", RELEVANCE_EPHEMERAL_ROOT),
                ("codex-2026-08-21-new-chat", RELEVANCE_SYNTHETIC_DATED_ID),
            ],
        )


class TestResolvedCategoryColumn:
    """Issue #254's second ask: show how a project relates to report
    categories, using only the deterministic folder layer (#214's
    `user_rule > builtin_rule > fallback`)."""

    def _write_config(self, body: str) -> None:
        cli_module.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cli_module.CONFIG_PATH.write_text(body, encoding="utf-8")

    def test_user_rule_wins_over_the_builtin_match(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # `atlas-app` matches the built-in `coding` needle `app`; an explicit
        # user rule must still take precedence.
        self._write_config(
            "[categories]\nresearch = [\"atlas-app\"]\n",
        )
        _stub_snapshot(
            monkeypatch,
            [
                _session(
                    "-Users-alice-code-atlas-app", "s1",
                    SINCE, SINCE + timedelta(minutes=5),
                    cwd="/Users/alice/code/atlas-app",
                )
            ],
        )
        observed = collect_observed_projects(
            SINCE, UNTIL, config_path=cli_module.CONFIG_PATH,
        )
        assert observed[0].category == "research"
        assert observed[0].category_source == "user_rule"

    def test_builtin_rule_when_the_user_expressed_no_opinion(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        _stub_snapshot(
            monkeypatch,
            [
                _session(
                    "-Users-alice-code-atlas-app", "s1",
                    SINCE, SINCE + timedelta(minutes=5),
                    cwd="/Users/alice/code/atlas-app",
                )
            ],
        )
        observed = collect_observed_projects(
            SINCE, UNTIL, config_path=cli_module.CONFIG_PATH,
        )
        assert observed[0].category == "coding"
        assert observed[0].category_source == "builtin_rule"

    def test_fallback_when_no_rule_matches_at_all(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # A distinct `default_bucket` proves the value came from the
        # configured fallback rather than from the built-in default.
        self._write_config("default_bucket = \"writing\"\n")
        _stub_snapshot(
            monkeypatch,
            [
                _session(
                    "-Users-alice-code-zzqqxx", "s1",
                    SINCE, SINCE + timedelta(minutes=5),
                    cwd="/Users/alice/code/zzqqxx",
                )
            ],
        )
        observed = collect_observed_projects(
            SINCE, UNTIL, config_path=cli_module.CONFIG_PATH,
        )
        assert observed[0].category == "writing"
        assert observed[0].category_source == "fallback"

    def test_never_reads_the_content_classification_cache(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        def _forbidden(*args, **kwargs):
            raise AssertionError(
                "project discovery must resolve categories folder-only"
            )

        monkeypatch.setattr(
            session_summarizer, "_classify_cache_get_many", _forbidden,
        )
        _stub_snapshot(
            monkeypatch,
            [
                _session(
                    "-Users-alice-code-atlas-app", "s1",
                    SINCE, SINCE + timedelta(minutes=5),
                    cwd="/Users/alice/code/atlas-app",
                )
            ],
        )
        observed = collect_observed_projects(
            SINCE, UNTIL, config_path=cli_module.CONFIG_PATH,
        )
        assert observed[0].category_source == "builtin_rule"

    def test_representative_folder_choice_is_input_order_independent(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Two folder variants folded by one alias resolve to one stable
        category regardless of which session the provider yields first."""

        sessions = [
            _session(
                "-Users-alice-code-borealis-api", "s1",
                SINCE, SINCE + timedelta(minutes=5),
                cwd="/Users/alice/code/borealis-api",
            ),
            _session(
                "-Users-alice-code-borealis-notes", "s2",
                SINCE + timedelta(days=1), SINCE + timedelta(days=1, minutes=5),
                cwd="/Users/alice/code/borealis-notes",
            ),
        ]
        aliases = {
            "borealis-api": "borealis",
            "borealis-notes": "borealis",
        }
        results = []
        for ordered in (sessions, list(reversed(sessions))):
            _stub_snapshot(monkeypatch, ordered)
            observed = collect_observed_projects(
                SINCE, UNTIL, aliases=aliases, config_path=cli_module.CONFIG_PATH,
            )
            results.append(
                (observed[0].category, observed[0].category_source)
            )
        assert results[0] == results[1]


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
        assert payload["all"] is False
        assert payload["filtered_count"] == 0
        assert payload["projects"] == [
            {
                "project_id": "atlas-app",
                "last_seen": payload["projects"][0]["last_seen"],
                "session_count": 1,
                "agents": ["claude"],
                "category": "coding",
                "category_source": "builtin_rule",
                "relevance": "relevant",
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


class TestProjectListRelevanceView:
    """Issue #254's default view, over real synthetic transcripts.

    Four workspaces cover both filter rules and both near-miss boundaries:
    a durable project, a `/private/tmp` scratch run, a Codex per-day
    folderless-chat placeholder, and a real repository whose leaf merely
    starts with `tmp`.
    """

    _WORKSPACES = {
        "atlas-app": "-Users-alice-code-atlas-app",
        "tmp-runner": "-Users-alice-code-tmp-runner",
        "private-tmp-screen-20260817-axis2-audit-ppcpug": (
            "-private-tmp-screen-20260817-axis2-audit-ppcpug"
        ),
        "codex-2026-08-21-new-chat": (
            "-Users-alice-Documents-Codex-2026-08-21-new-chat"
        ),
    }

    def _populate(self, jsonl_factory) -> None:
        for index, folder in enumerate(self._WORKSPACES.values()):
            jsonl_factory(
                folder,
                f"s{index}",
                [
                    make_user_msg(
                        "first engaging message for this workspace",
                        _ts(2026, 3, 1 + index),
                    ),
                    make_user_msg(
                        "second engaging message for this workspace",
                        _ts(2026, 3, 1 + index, 0, 5),
                    ),
                ],
            )

    def test_default_hides_both_noise_classes_and_says_how_many(
        self, jsonl_factory,
    ):
        self._populate(jsonl_factory)

        console, stream = _console()
        assert cli_module._run_project(["list"], console) == 0
        rendered = stream.getvalue()

        assert "atlas-app" in rendered
        assert "tmp-runner" in rendered
        assert "private-tmp-screen" not in rendered
        assert "new-chat" not in rendered
        assert "Filtered 2 ephemeral/synthetic identities" in rendered
        assert "--all to show" in rendered

    def test_all_restores_the_complete_listing(self, jsonl_factory):
        self._populate(jsonl_factory)

        console, stream = _console()
        assert cli_module._run_project(["list", "--all"], console) == 0
        rendered = stream.getvalue()

        for project_id in self._WORKSPACES:
            assert project_id.split("-")[0] in rendered
        assert "private-tmp-screen" in rendered
        assert "new-chat" in rendered
        assert "Filtered" not in rendered
        assert "hidden by default" in rendered

    def test_json_carries_filtered_count_and_per_row_relevance(
        self, capsys: pytest.CaptureFixture[str],
        jsonl_factory,
    ):
        self._populate(jsonl_factory)

        console, _ = _console()
        assert cli_module._run_project(["list", "--json"], console) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["all"] is False
        assert payload["filtered_count"] == 2
        assert payload["count"] == payload["total_count"] == 2
        assert {row["project_id"] for row in payload["projects"]} == {
            "atlas-app", "tmp-runner",
        }
        assert {row["relevance"] for row in payload["projects"]} == {"relevant"}

        all_console, _ = _console()
        assert cli_module._run_project(["list", "--json", "--all"], all_console) == 0
        all_payload = json.loads(capsys.readouterr().out)
        assert all_payload["all"] is True
        assert all_payload["filtered_count"] == 0
        assert all_payload["count"] == all_payload["total_count"] == 4
        assert {
            row["project_id"]: row["relevance"] for row in all_payload["projects"]
        } == {
            "atlas-app": "relevant",
            "tmp-runner": "relevant",
            "private-tmp-screen-20260817-axis2-audit-ppcpug": "ephemeral_root",
            "codex-2026-08-21-new-chat": "synthetic_dated_id",
        }

    def test_json_keeps_every_pre_existing_envelope_field(
        self, capsys: pytest.CaptureFixture[str],
        jsonl_factory,
    ):
        """The #254 additions are purely additive — an existing machine
        consumer reading the #222 envelope keeps working unchanged."""

        self._populate(jsonl_factory)

        console, _ = _console()
        assert cli_module._run_project(["list", "--json"], console) == 0
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {
            "ok", "window", "agent", "count", "total_count", "truncated",
            "hard_max", "projects",
            "all", "filtered_count",
        }
        assert payload["ok"] is True
        assert payload["window"] == "all"
        assert payload["agent"] == "all"
        assert payload["truncated"] is False
        assert payload["hard_max"] == JSON_HARD_MAX
        assert set(payload["projects"][0]) == {
            "project_id", "last_seen", "session_count", "agents",
            "category", "category_source", "relevance",
        }

    def test_ordering_and_display_cap_survive_the_filter(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Filtering removes rows; it never reorders them or changes the cap."""

        sessions = [
            _session(
                f"atlas-{index:04d}", f"s{index}",
                SINCE + timedelta(days=index),
                SINCE + timedelta(days=index, minutes=5),
            )
            for index in range(DEFAULT_DISPLAY_CAP + 5)
        ]
        sessions.append(
            _session(
                "-private-tmp-scratch-run", "noise",
                SINCE + timedelta(days=500), SINCE + timedelta(days=500, minutes=5),
                cwd="/private/tmp/scratch-run",
            )
        )
        _stub_snapshot(monkeypatch, sessions)

        console, stream = _console()
        assert cli_module._run_project(["list"], console) == 0
        rendered = stream.getvalue()
        assert f"Showing {DEFAULT_DISPLAY_CAP} of {DEFAULT_DISPLAY_CAP + 5}" in rendered
        assert "Filtered 1 ephemeral/synthetic identities" in rendered
        # Most-recently-observed first, exactly as before the filter existed.
        assert rendered.index(f"atlas-{DEFAULT_DISPLAY_CAP + 4:04d}") < rendered.index(
            f"atlas-{DEFAULT_DISPLAY_CAP + 3:04d}"
        )

    def test_all_noise_window_explains_the_filter_instead_of_looking_empty(
        self, jsonl_factory,
    ):
        jsonl_factory(
            "-private-tmp-screen-20260817-axis2-audit-ppcpug",
            "s1",
            [
                make_user_msg("first engaging scratch message", _ts(2026, 3, 1)),
                make_user_msg(
                    "second engaging scratch message", _ts(2026, 3, 1, 0, 5),
                ),
            ],
        )

        console, stream = _console()
        assert cli_module._run_project(["list"], console) == 0
        rendered = stream.getvalue()
        assert "No configuration-relevant projects" in rendered
        assert "filtered 1 ephemeral/synthetic identities" in rendered
        assert "--all to show" in rendered

        all_console, all_stream = _console()
        assert cli_module._run_project(["list", "--all"], all_console) == 0
        assert "private-tmp-screen" in all_stream.getvalue()

    def test_category_column_is_rendered_from_the_folder_layer(
        self, jsonl_factory,
    ):
        cli_module.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cli_module.CONFIG_PATH.write_text(
            "[categories]\nresearch = [\"atlas-app\"]\n", encoding="utf-8",
        )
        jsonl_factory(
            "-Users-alice-code-atlas-app",
            "s1",
            [
                make_user_msg("first engaging message here", _ts(2026, 3, 1)),
                make_user_msg("second engaging message here", _ts(2026, 3, 1, 0, 5)),
            ],
        )

        console, stream = _console()
        assert cli_module._run_project(["list"], console) == 0
        rendered = stream.getvalue()
        assert "Category" in rendered
        assert "research" in rendered


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
