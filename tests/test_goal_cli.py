"""Managed goal CLI and single-source selection contracts (#217)."""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from ccstory import categorizer
from ccstory import cli as cli_module
from ccstory import goal_store
from ccstory import recap
from ccstory import session_summarizer
from ccstory.cli import _run_goal
from ccstory.goal_store import (
    GoalSourceOperation,
    ManagedTomlGoalSource,
    ReadOnlyTomlGoalSource,
    load_managed_goal_context,
    managed_goal_context_path,
    resolve_goal_context_source,
    select_goal_source,
)
from ccstory.goals import GoalContextError

from tests.conftest import _ts, make_user_msg


def _console() -> tuple[Console, io.StringIO]:
    stream = io.StringIO()
    return Console(file=stream, width=120), stream


def _goal_file(path: Path, goal_id: str, title: str, project: str) -> bytes:
    body = (
        "schema_version = 1\n"
        "\n"
        "[[goals]]\n"
        f'id = "{goal_id}"\n'
        f'title = "{title}"\n'
        f'projects = ["{project}"]\n'
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return body


class TestManagedGoalCLI:
    def test_set_list_update_unset_round_trip(
        self, tmp_home: Path, capsys: pytest.CaptureFixture[str]
    ):
        console, stream = _console()
        assert _run_goal(
            [
                "set",
                "goal-b",
                "--title",
                "Ship beta",
                "--project",
                "beta-app",
                "--project",
                "alpha-app",
                "--valid-from",
                "2026-07-01",
                "--valid-until",
                "2026-07-31",
            ],
            console,
        ) == 0
        assert _run_goal(
            [
                "set",
                "goal-a",
                "--title",
                "First goal",
                "--project",
                "first-app",
            ],
            console,
        ) == 0

        path = managed_goal_context_path()
        assert path == tmp_home / ".ccstory" / "goals.toml"
        context = load_managed_goal_context(path, aliases={})
        assert [goal.id for goal in context.goals] == ["goal-a", "goal-b"]
        goal_b = context.goals[1]
        assert goal_b.projects == ("alpha-app", "beta-app")
        assert goal_b.valid_from.isoformat() == "2026-07-01"
        assert goal_b.valid_until.isoformat() == "2026-07-31"

        # Stable-id set replaces that goal's full declared fields and leaves
        # unrelated goals alone. Omitted optional dates are cleared.
        assert _run_goal(
            [
                "set",
                "goal-b",
                "--title",
                "Ship stable",
                "--project",
                "stable-app",
            ],
            console,
        ) == 0
        context = load_managed_goal_context(path, aliases={})
        assert [goal.id for goal in context.goals] == ["goal-a", "goal-b"]
        assert context.goals[1].title == "Ship stable"
        assert context.goals[1].projects == ("stable-app",)
        assert context.goals[1].valid_from is None
        assert context.goals[1].valid_until is None
        assert "Updated goal `goal-b`" in stream.getvalue()

        capsys.readouterr()
        json_console, _ = _console()
        assert _run_goal(["list", "--json"], json_console) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "schema_version": 1,
            "goals": [
                {
                    "id": "goal-a",
                    "title": "First goal",
                    "projects": ["first-app"],
                },
                {
                    "id": "goal-b",
                    "title": "Ship stable",
                    "projects": ["stable-app"],
                },
            ],
        }

        list_console, list_stream = _console()
        assert _run_goal(["list"], list_console) == 0
        assert "goal-a" in list_stream.getvalue()
        assert "First goal" in list_stream.getvalue()

        assert _run_goal(["unset", "goal-a"], console) == 0
        assert [
            goal.id
            for goal in load_managed_goal_context(path, aliases={}).goals
        ] == ["goal-b"]

    def test_missing_unset_is_idempotent_and_byte_preserving(
        self, tmp_home: Path
    ):
        console, stream = _console()
        path = managed_goal_context_path()
        assert not path.exists()
        assert _run_goal(["unset", "absent"], console) == 0
        assert not path.exists()
        assert "not found" in stream.getvalue()

        _goal_file(path, "kept", "Keep me", "kept-app")
        before = path.read_bytes()
        assert _run_goal(["unset", "absent"], console) == 0
        assert path.read_bytes() == before

    def test_json_list_of_missing_store_is_empty_versioned_schema(
        self, capsys: pytest.CaptureFixture[str]
    ):
        console, _ = _console()
        assert _run_goal(["list", "--json"], console) == 0
        assert json.loads(capsys.readouterr().out) == {
            "schema_version": 1,
            "goals": [],
        }

    def test_unset_rejects_invalid_goal_id_without_creating_store(self):
        console, stream = _console()
        path = managed_goal_context_path()
        assert _run_goal(["unset", "   "], console) == 1
        assert "must be a non-blank string" in stream.getvalue()
        assert not path.exists()

    def test_invalid_input_and_invalid_existing_store_never_mutate_bytes(
        self, tmp_home: Path
    ):
        console, stream = _console()
        path = managed_goal_context_path()
        before = _goal_file(path, "kept", "Keep me", "kept-app")

        assert _run_goal(
            [
                "set",
                "bad",
                "--title",
                "Bad dates",
                "--project",
                "bad-app",
                "--valid-from",
                "2026-08-02",
                "--valid-until",
                "2026-08-01",
            ],
            console,
        ) == 1
        assert path.read_bytes() == before
        assert "valid_until must be on or after valid_from" in stream.getvalue()

        malformed = b"schema_version = 1\ngoals = [not valid"
        path.write_bytes(malformed)
        assert _run_goal(
            [
                "set",
                "new",
                "--title",
                "New",
                "--project",
                "new-app",
            ],
            console,
        ) == 1
        assert path.read_bytes() == malformed

    def test_output_is_deterministic_and_private(self, tmp_home: Path):
        console, _ = _console()
        assert _run_goal(
            [
                "set",
                "zeta",
                "--title",
                "Zeta",
                "--project",
                "zeta-app",
            ],
            console,
        ) == 0
        assert _run_goal(
            [
                "set",
                "alpha",
                "--title",
                "Alpha",
                "--project",
                "alpha-app",
            ],
            console,
        ) == 0
        path = managed_goal_context_path()
        text = path.read_text()
        assert text.index('id = "alpha"') < text.index('id = "zeta"')
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_atomic_replace_failure_preserves_original_and_cleans_temp(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        console, _ = _console()
        path = managed_goal_context_path()
        before = _goal_file(path, "kept", "Keep me", "kept-app")
        replace_paths: list[tuple[Path, Path]] = []

        def fail_replace(source, destination):
            replace_paths.append((Path(source), Path(destination)))
            raise OSError("simulated replace failure")

        monkeypatch.setattr(goal_store.os, "replace", fail_replace)
        assert _run_goal(
            [
                "set",
                "new",
                "--title",
                "New",
                "--project",
                "new-app",
            ],
            console,
        ) == 1
        assert path.read_bytes() == before
        assert len(replace_paths) == 1
        temp, destination = replace_paths[0]
        assert temp.parent == destination.parent == path.parent
        assert not temp.exists()


def _engaged_messages(seed: str, day: int) -> list[dict]:
    """Two real user turns — enough for `SessionStat.engaged` — on a fixed day."""

    return [
        make_user_msg(f"{seed} first engaging message here", _ts(2026, 3, day)),
        make_user_msg(f"{seed} second engaging message here", _ts(2026, 3, day, 0, 5)),
    ]


class TestGoalSetObservedGuidance:
    """Issue #222's goal-set guidance: observed/unobserved feedback only.

    `_goal_set_project_feedback` scans the full local history through the
    same read-only seam as `ccstory project list` — these tests write real
    synthetic transcripts (via the `jsonl_factory`/`tmp_home` fixtures) so
    the observed check exercises the real provider parse path, not a stub.
    """

    def test_observed_project_is_reported_and_json_matches(
        self, jsonl_factory, capsys: pytest.CaptureFixture[str],
    ):
        jsonl_factory("demo-app", "s1", _engaged_messages("demo", 1))

        console, stream = _console()
        assert _run_goal(
            ["set", "demo", "--title", "Demo goal", "--project", "demo-app"],
            console,
        ) == 0
        rendered = stream.getvalue()
        assert "Set goal `demo`" in rendered
        assert "observed:" in rendered
        assert "`demo-app`" in rendered
        assert "unobserved" not in rendered

        json_console, _ = _console()
        assert _run_goal(
            [
                "set", "demo", "--title", "Demo goal",
                "--project", "demo-app", "--json",
            ],
            json_console,
        ) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["replaced"] is True  # second `set` for the same id
        assert payload["goal"] == {
            "id": "demo", "title": "Demo goal", "projects": ["demo-app"],
        }
        assert payload["projects"] == [{"project": "demo-app", "observed": True}]

    def test_unobserved_future_project_preserves_value_and_is_labeled(
        self, tmp_home: Path,
    ):
        console, stream = _console()
        assert _run_goal(
            [
                "set", "future", "--title", "Future goal",
                "--project", "not-yet-seen-xyz",
            ],
            console,
        ) == 0
        rendered = stream.getvalue()
        assert "Set goal `future`" in rendered
        assert "unobserved:" in rendered
        assert "`not-yet-seen-xyz`" in rendered
        assert "value unchanged" in rendered

        path = managed_goal_context_path()
        context = load_managed_goal_context(path, aliases={})
        goal = next(g for g in context.goals if g.id == "future")
        # A valid future project is preserved exactly, never rewritten —
        # matches the pre-existing GoalContext contract (docstring in
        # ccstory/goals.py, README "Goal activity").
        assert goal.projects == ("not-yet-seen-xyz",)

    def test_ambiguous_candidates_are_suggested_but_never_auto_correct(
        self, jsonl_factory, capsys: pytest.CaptureFixture[str],
    ):
        jsonl_factory("borealis-app", "s1", _engaged_messages("app", 1))
        jsonl_factory("borealis-api", "s2", _engaged_messages("api", 2))

        json_console, _ = _console()
        assert _run_goal(
            [
                "set", "typo-goal", "--title", "Typo goal",
                "--project", "boreal-ap", "--json",
            ],
            json_console,
        ) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["goal"]["projects"] == ["boreal-ap"]
        assert payload["projects"] == [
            {
                "project": "boreal-ap",
                "observed": False,
                "candidates": ["borealis-app", "borealis-api"],
            }
        ]

        # Guidance only: the stored value is exactly the caller's input,
        # never silently corrected to one of the suggested candidates.
        path = managed_goal_context_path()
        context = load_managed_goal_context(path, aliases={})
        goal = next(g for g in context.goals if g.id == "typo-goal")
        assert goal.projects == ("boreal-ap",)

    def test_alias_folded_project_is_reported_observed(
        self, tmp_home: Path, jsonl_factory,
    ):
        config_path = tmp_home / ".ccstory" / "config.toml"
        config_path.write_text(
            '[projects]\n"borealis-legacy" = "borealis-app"\n',
            encoding="utf-8",
        )
        jsonl_factory("borealis-legacy", "s1", _engaged_messages("legacy", 1))

        console, stream = _console()
        assert _run_goal(
            [
                "set", "aliased", "--title", "Aliased goal",
                "--project", "borealis-app",
            ],
            console,
        ) == 0
        rendered = stream.getvalue()
        assert "observed:" in rendered
        assert "`borealis-app`" in rendered
        assert "unobserved" not in rendered

    def test_duplicate_alias_projects_collapse_to_one_feedback_row(
        self, tmp_home: Path, jsonl_factory, capsys: pytest.CaptureFixture[str],
    ):
        config_path = tmp_home / ".ccstory" / "config.toml"
        config_path.write_text(
            '[projects]\n'
            '"borealis-legacy" = "borealis-app"\n'
            '"borealis-old" = "borealis-app"\n',
            encoding="utf-8",
        )
        jsonl_factory("borealis-legacy", "s1", _engaged_messages("legacy", 1))

        json_console, _ = _console()
        assert _run_goal(
            [
                "set", "dup-goal", "--title", "Dup goal",
                "--project", "borealis-legacy",
                "--project", "borealis-old",
                "--json",
            ],
            json_console,
        ) == 0
        payload = json.loads(capsys.readouterr().out)
        # goals.py already folds both spellings to one canonical entry.
        assert payload["goal"]["projects"] == ["borealis-app"]
        assert payload["projects"] == [
            {"project": "borealis-app", "observed": True},
        ]

    def test_mixed_observed_and_unobserved_projects_are_each_reported(
        self, jsonl_factory, capsys: pytest.CaptureFixture[str],
    ):
        jsonl_factory("live-app", "s1", _engaged_messages("live", 1))

        json_console, _ = _console()
        assert _run_goal(
            [
                "set", "mixed-goal", "--title", "Mixed goal",
                "--project", "live-app",
                "--project", "not-yet-seen-xyz",
                "--json",
            ],
            json_console,
        ) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["goal"]["projects"] == ["live-app", "not-yet-seen-xyz"]
        assert payload["projects"] == [
            {"project": "live-app", "observed": True},
            {"project": "not-yet-seen-xyz", "observed": False},
        ]

        console, stream = _console()
        assert _run_goal(
            [
                "set", "mixed-goal", "--title", "Mixed goal",
                "--project", "live-app",
                "--project", "not-yet-seen-xyz",
            ],
            console,
        ) == 0
        rendered = stream.getvalue()
        assert "observed:" in rendered and "`live-app`" in rendered
        assert "unobserved:" in rendered and "`not-yet-seen-xyz`" in rendered

    def test_relevance_filter_does_not_shrink_the_observed_set(
        self, jsonl_factory, capsys: pytest.CaptureFixture[str],
    ):
        """`ccstory project list` hides ephemeral and provider-synthesized
        identities by default (#254). The observed check must keep judging
        against the *complete* population: a user who names one of those
        identities explicitly has still observed it, and calling it
        unobserved would be false.
        """

        jsonl_factory(
            "-private-tmp-screen-20260817-axis2-audit-ppcpug",
            "s1",
            _engaged_messages("scratch", 1),
        )
        jsonl_factory(
            "-Users-alice-Documents-Codex-2026-08-21-new-chat",
            "s2",
            _engaged_messages("dated", 2),
        )

        # Both identities are hidden from the default discovery view …
        list_console, _ = _console()
        assert cli_module._run_project(["list", "--json"], list_console) == 0
        list_payload = json.loads(capsys.readouterr().out)
        assert list_payload["projects"] == []
        assert list_payload["filtered_count"] == 2

        # … and both are still reported as observed by `goal set`.
        json_console, _ = _console()
        assert _run_goal(
            [
                "set", "scratch-goal", "--title", "Scratch goal",
                "--project", "private-tmp-screen-20260817-axis2-audit-ppcpug",
                "--project", "codex-2026-08-21-new-chat",
                "--json",
            ],
            json_console,
        ) == 0
        payload = json.loads(capsys.readouterr().out)
        # `goals.py` already normalizes/sorts the stored project tuple.
        assert payload["projects"] == [
            {"project": "codex-2026-08-21-new-chat", "observed": True},
            {
                "project": "private-tmp-screen-20260817-axis2-audit-ppcpug",
                "observed": True,
            },
        ]

        console, stream = _console()
        assert _run_goal(
            [
                "set", "scratch-goal", "--title", "Scratch goal",
                "--project", "private-tmp-screen-20260817-axis2-audit-ppcpug",
            ],
            console,
        ) == 0
        assert "unobserved" not in stream.getvalue()

    def test_never_calls_the_narrator(self, monkeypatch: pytest.MonkeyPatch):
        def _forbidden() -> bool:
            raise AssertionError("goal set must make zero model calls")

        monkeypatch.setattr(session_summarizer, "llm_available", _forbidden)

        console, _ = _console()
        assert _run_goal(
            ["set", "quiet", "--title", "Quiet", "--project", "quiet-app"],
            console,
        ) == 0

    def test_observed_feedback_never_changes_stored_bytes_or_schema(
        self, tmp_home: Path,
    ):
        """The new console/JSON feedback is purely additive output — the
        managed store and the core `GoalContext` schema stay byte/field
        compatible with the pre-#222 contract."""

        console, _ = _console()
        assert _run_goal(
            [
                "set", "byte-check", "--title", "Byte check",
                "--project", "byte-app",
            ],
            console,
        ) == 0
        path = managed_goal_context_path()
        context = load_managed_goal_context(path, aliases={})
        assert context.to_schema_dict() == {
            "schema_version": 1,
            "goals": [
                {
                    "id": "byte-check",
                    "title": "Byte check",
                    "projects": ["byte-app"],
                }
            ],
        }


class TestGoalSourceSelection:
    def test_adapter_capabilities_and_managed_round_trip(self, tmp_path: Path):
        managed_path = tmp_path / "managed.toml"
        managed = ManagedTomlGoalSource(managed_path, aliases={})
        readonly = ReadOnlyTomlGoalSource(
            tmp_path / "external.toml", kind="explicit", aliases={}
        )

        assert managed.capabilities == {
            GoalSourceOperation.READ,
            GoalSourceOperation.UPSERT,
            GoalSourceOperation.DELETE,
        }
        assert readonly.capabilities == {GoalSourceOperation.READ}
        assert managed.read().goals == ()
        managed.upsert(
            "owned",
            title="Owned path",
            projects=["owned-app"],
        )
        assert [goal.id for goal in managed.read().goals] == ["owned"]
        assert managed.delete("owned") is True
        assert managed.read().goals == ()

    def test_readonly_mutation_rejects_before_changing_bytes(
        self, tmp_path: Path
    ):
        external = tmp_path / "external.toml"
        before = _goal_file(external, "external", "External", "external-app")
        source = ReadOnlyTomlGoalSource(external, kind="configured", aliases={})

        with pytest.raises(GoalContextError, match="read-only"):
            source.upsert("new", title="New", projects=["new-app"])
        with pytest.raises(GoalContextError, match="read-only"):
            source.delete("external")
        assert external.read_bytes() == before

    def test_managed_adapter_owns_only_its_injected_path(self, tmp_path: Path):
        first = ManagedTomlGoalSource(tmp_path / "first.toml", aliases={})
        second_path = tmp_path / "second.toml"
        second_before = _goal_file(
            second_path, "second", "Second", "second-app"
        )

        first.upsert("first", title="First", projects=["first-app"])
        assert [goal.id for goal in first.read().goals] == ["first"]
        assert second_path.read_bytes() == second_before

    def test_precedence_is_explicit_then_configured_then_managed(
        self, tmp_home: Path, tmp_path: Path
    ):
        config = tmp_path / "settings" / "config.toml"
        configured = config.parent / "sources" / "configured.toml"
        explicit = tmp_path / "cwd" / "explicit.toml"
        managed = tmp_home / ".ccstory" / "goals.toml"
        _goal_file(configured, "configured", "Configured", "configured-app")
        _goal_file(explicit, "explicit", "Explicit", "explicit-app")
        _goal_file(managed, "managed", "Managed", "managed-app")
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            '[goal_context]\npath = "sources/configured.toml"\n',
            encoding="utf-8",
        )

        selected = resolve_goal_context_source(
            "explicit.toml",
            config_path=config,
            cwd=explicit.parent,
            managed_path=managed,
            aliases={},
        )
        assert [goal.id for goal in selected.goals] == ["explicit"]
        assert selected.source_metadata["source_kind"] == "explicit"

        source = select_goal_source(
            "explicit.toml",
            config_path=config,
            cwd=explicit.parent,
            managed_path=managed,
            aliases={},
        )
        assert isinstance(source, ReadOnlyTomlGoalSource)
        assert source.source_kind == "explicit"

        selected = resolve_goal_context_source(
            config_path=config,
            managed_path=managed,
            aliases={},
        )
        assert [goal.id for goal in selected.goals] == ["configured"]
        assert selected.source_metadata["source_kind"] == "configured"

        source = select_goal_source(
            config_path=config, managed_path=managed, aliases={}
        )
        assert isinstance(source, ReadOnlyTomlGoalSource)
        assert source.source_kind == "configured"

        config.unlink()
        selected = resolve_goal_context_source(
            config_path=config,
            managed_path=managed,
            aliases={},
        )
        assert [goal.id for goal in selected.goals] == ["managed"]
        assert selected.source_metadata["source_kind"] == "managed"

        source = select_goal_source(
            config_path=config, managed_path=managed, aliases={}
        )
        assert isinstance(source, ManagedTomlGoalSource)
        assert source.source_kind == "managed"

    def test_tilde_expands_and_explicit_does_not_read_invalid_config(
        self, tmp_home: Path
    ):
        explicit = tmp_home / "external.toml"
        _goal_file(explicit, "external", "External", "external-app")
        config = tmp_home / ".ccstory" / "config.toml"
        config.write_text("[goal_context]\npath = 42\n", encoding="utf-8")

        selected = resolve_goal_context_source(
            "~/external.toml",
            config_path=config,
            aliases={},
        )
        assert [goal.id for goal in selected.goals] == ["external"]

    def test_missing_default_is_none_but_requested_sources_are_errors(
        self, tmp_home: Path
    ):
        config = tmp_home / ".ccstory" / "config.toml"
        managed = tmp_home / ".ccstory" / "missing-managed.toml"
        assert (
            resolve_goal_context_source(
                config_path=config,
                managed_path=managed,
                aliases={},
            )
            is None
        )

        with pytest.raises(GoalContextError, match="explicit.*does not exist"):
            resolve_goal_context_source(
                "missing-explicit.toml",
                config_path=config,
                cwd=tmp_home,
                managed_path=managed,
                aliases={},
            )

        config.write_text(
            '[goal_context]\npath = "missing-configured.toml"\n',
            encoding="utf-8",
        )
        with pytest.raises(GoalContextError, match="configured.*does not exist"):
            resolve_goal_context_source(
                config_path=config,
                managed_path=managed,
                aliases={},
            )

    def test_selected_external_file_is_re_read_and_never_mutated(
        self, tmp_home: Path
    ):
        config = tmp_home / ".ccstory" / "config.toml"
        external = tmp_home / "external.toml"
        first_bytes = _goal_file(
            external, "external", "First version", "external-app"
        )
        config.write_text(
            f"[goal_context]\npath = {json.dumps(str(external))}\n",
            encoding="utf-8",
        )

        first = resolve_goal_context_source(config_path=config, aliases={})
        assert external.read_bytes() == first_bytes

        second_bytes = _goal_file(
            external, "external", "Second version", "external-app"
        )
        second = resolve_goal_context_source(config_path=config, aliases={})
        assert second.goals[0].title == "Second version"
        assert second.source_fingerprint != first.source_fingerprint
        assert external.read_bytes() == second_bytes

        # Goal CLI remains hard-wired to the managed default and cannot write
        # the configured external source.
        console, _ = _console()
        assert _run_goal(
            [
                "set",
                "managed",
                "--title",
                "Managed",
                "--project",
                "managed-app",
            ],
            console,
        ) == 0
        assert external.read_bytes() == second_bytes
        assert categorizer.load_goal_context_path(config) == str(external)

    def test_recap_cli_passes_the_selected_context_to_build_recap(
        self,
        tmp_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        explicit = tmp_home / "selected.toml"
        _goal_file(explicit, "selected", "Selected", "selected-app")
        captured: dict[str, object] = {}

        def fake_build_recap(*args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                report_path=tmp_home / "recap.md",
                markdown="",
                to_json=lambda: {"schema_version": 1, "kind": "recap"},
            )

        # `_dispatch` now imports `build_recap` / `_agent_data_roots` locally
        # (inside the function, at call time) instead of once at module top,
        # so the effective patch target is their true home (`ccstory.recap`)
        # rather than `cli`'s former re-export of the same names.
        monkeypatch.setattr(recap, "build_recap", fake_build_recap)
        monkeypatch.setattr(
            recap,
            "_agent_data_roots",
            lambda agent: [
                ("claude", tmp_home / ".claude" / "projects"),
            ],
        )
        monkeypatch.setattr(
            cli_module, "_print_first_run_preview", lambda console: None
        )

        assert cli_module._dispatch(
            [
                "week",
                "--goals-file",
                str(explicit),
                "--format",
                "json",
                "--classify",
                "folder",
            ]
        ) == 0
        selected = captured["goal_context"]
        assert [goal.id for goal in selected.goals] == ["selected"]
        assert selected.source_metadata["source_kind"] == "explicit"
        assert json.loads(capsys.readouterr().out)["kind"] == "recap"

    @pytest.mark.parametrize(
        "body",
        [
            "[goal_context]\npath = 42\n",
            "[goal_context]\npath = \"\"\n",
            "[goal_context]\npath = \"goals.toml\"\nextra = true\n",
        ],
    )
    def test_malformed_goal_context_config_is_actionable(
        self, tmp_home: Path, body: str
    ):
        config = tmp_home / ".ccstory" / "config.toml"
        config.write_text(body, encoding="utf-8")
        with pytest.raises(GoalContextError, match=r"\[goal_context\]"):
            resolve_goal_context_source(config_path=config, aliases={})
