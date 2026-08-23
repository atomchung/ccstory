"""Private pilot-set sampling contracts for issue #223."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ccstory.project_attribution import load_evidence_jsonl
from ccstory.session_identity import public_session_id
from ccstory.time_tracking import SessionStat
from scripts.project_attribution_sample import (
    _PilotRow,
    _RepoEvidence,
    _candidate_projects,
    _exclude_prior_rows,
    _full_user_text,
    _load_aliases,
    _load_excluded_session_ids,
    _partition_owner_intent_sessions,
    _partition_repetitive_routines,
    _render_proposed_review,
    _render_review,
    _row_for_session,
    _select,
)


def _session(cwd: str) -> SessionStat:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    return SessionStat(
        project="-Users-demo-Side-project-monorepo-subapp",
        category="",
        session_id="private-source-id",
        start=now,
        end=now,
        active_sec=60,
        msg_count=2,
        cwd=cwd,
    )


def _row(index: int, split: str, stratum: str, project: str) -> _PilotRow:
    return _PilotRow(
        payload={
            "session_id": f"eval-{index:03d}",
            "split": split,
            "label_status": "unreviewed",
            "expected_projects": [],
            "candidate_projects": [project],
            "case_tags": [stratum],
            "evidence": {
                "provider": ["codex"],
                "workspace": [project],
                "summary": [f"bounded evidence {index}"],
            },
        },
        split=split,
        stratum=stratum,
        project_key=project,
    )


def test_nested_repo_candidates_keep_subproject_and_repo_separate():
    session = _session("/workspace/monorepo/subapp")
    repo = _RepoEvidence(
        # ``root`` is what proves nesting. Use an inert path; this helper does
        # not touch the filesystem.
        root=Path("/workspace/monorepo"),
        github="example/monorepo",
        name="monorepo",
        relative_cwd="subapp",
        is_worktree=False,
    )
    assert _candidate_projects(
        session,
        "monorepo-subapp",
        repo,
    ) == ("subapp", "monorepo", "monorepo-subapp")


def test_container_paths_are_not_suggested_as_durable_projects():
    session = _session("/Users/demo/Side_project")
    repo = _RepoEvidence(
        root=None,
        github="",
        name="",
        relative_cwd="",
        is_worktree=False,
    )
    assert _candidate_projects(session, "(top-level)", repo) == ()


def test_workspace_aliases_share_one_canonical_project():
    session = _session("/workspace/example-app-gtm")
    repo = _RepoEvidence(
        root=Path("/workspace/example-app-gtm"),
        github="example/example-app",
        name="example-app",
        relative_cwd="",
        is_worktree=False,
    )

    assert _candidate_projects(
        session,
        "example-app-gtm",
        repo,
        aliases={"example-app-gtm": "example-app"},
    ) == ("example-app",)


def test_private_alias_file_is_normalized(tmp_path: Path):
    path = tmp_path / "aliases.json"
    path.write_text(
        json.dumps({"Example_App_GTM": "Example App"}),
        encoding="utf-8",
    )

    assert _load_aliases(path) == {"example-app-gtm": "example-app"}


def test_non_owner_sessions_are_excluded_from_owner_review_population():
    direct = _session("/workspace/direct")
    delegated = _session("/workspace/delegated")
    delegated.is_delegated = True
    delegated.delegation_source = "claude_code"
    scheduled = _session("/workspace/scheduled")
    scheduled.is_scheduled = True

    eligible, excluded_delegated, excluded_scheduled = (
        _partition_owner_intent_sessions([direct, delegated, scheduled])
    )

    assert eligible == [direct]
    assert excluded_delegated == [delegated]
    assert excluded_scheduled == [scheduled]


def test_repeated_one_shot_prompt_family_is_quarantined(tmp_path: Path):
    sessions = []
    template = (
        "你是一個 AI 領域的資深資訊摘要助理。請幫下面 5 篇文章產出"
        "結構化繁體中文摘要，包含標題、摘要、重點與意義。"
    )
    for index, date in enumerate(("2026-07-01", "2026-07-02", "2026-07-03")):
        path = tmp_path / f"routine-{index}.jsonl"
        prompt = f"{template} 批次日期 {date}。" + ("內容 " * 50)
        path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": prompt},
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        session = _session("/workspace/info-collector")
        session.session_id = f"routine-{index}"
        session.path = path
        session.agent = "claude"
        session.user_msg_count = 1
        session.msg_count = 3
        session.active_sec = 90
        sessions.append(session)

    direct = _session("/workspace/direct")
    direct.session_id = "direct"
    direct.path = tmp_path / "direct.jsonl"
    direct.path.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": "请评估这个项目的架构取舍。" + ("独特上下文 " * 50)
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    direct.agent = "claude"
    direct.user_msg_count = 1
    direct.msg_count = 3
    direct.active_sec = 90

    eligible, repetitive, clusters = _partition_repetitive_routines(
        [*sessions, direct]
    )

    assert eligible == [direct]
    assert repetitive == sessions
    assert len(clusters) == 1
    assert clusters[0]["sessions"] == 3
    assert "cluster_id" in clusters[0]


def test_repeated_manual_command_wrappers_are_not_quarantined(tmp_path: Path):
    sessions = []
    for index in range(3):
        path = tmp_path / f"command-{index}.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": (
                            "<command-message>review-prs</command-message>\n"
                            "<command-name>/review-prs</command-name>\n"
                            + ("Review the current pull requests. " * 20)
                        )
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        session = _session("/workspace/investment-note")
        session.session_id = f"command-{index}"
        session.path = path
        session.agent = "claude"
        session.user_msg_count = 1
        session.msg_count = 3
        session.active_sec = 90
        sessions.append(session)

    eligible, repetitive, clusters = _partition_repetitive_routines(sessions)

    assert eligible == sessions
    assert repetitive == []
    assert clusters == []


def test_repeated_explicit_templates_are_found_in_long_sessions(tmp_path: Path):
    sessions = []
    for index, project in enumerate(("ccstory", "info-collector", "xhs-skills")):
        path = tmp_path / f"scan-{index}.jsonl"
        prompt = (
            f"2026-07-{index + 10:02d} 掃描驗證（side-project-sweep §{index}）："
            f"请检查 {project} 的具体问题。" + ("验证材料 " * 80)
        )
        path.write_text(
            json.dumps(
                {"type": "user", "message": {"content": prompt}},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        session = _session(f"/workspace/{project}")
        session.session_id = f"scan-{index}"
        session.path = path
        session.agent = "claude"
        session.user_msg_count = 2
        session.msg_count = 40
        session.active_sec = 1_200
        sessions.append(session)

    eligible, repetitive, clusters = _partition_repetitive_routines(sessions)

    assert eligible == []
    assert repetitive == sessions
    assert len(clusters) == 1
    assert clusters[0]["sessions"] == 3


def test_synthetic_command_payloads_do_not_replace_the_first_real_input(
    tmp_path: Path,
):
    path = tmp_path / "commands.jsonl"
    records = [
        {
            "type": "user",
            "message": {
                "content": (
                    "<local-command-caveat>generated locally</local-command-caveat>"
                    "\n<command-name>/model</command-name>"
                )
            },
        },
        {
            "type": "user",
            "message": {"content": "介紹一下這個專案"},
        },
        {
            "type": "user",
            "message": {
                "content": (
                    "<task-notification><status>completed</status>"
                    "</task-notification>"
                )
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    session = _session("/workspace/example")
    session.path = path
    session.agent = "claude"

    assert _full_user_text(session) == "介紹一下這個專案"


def test_repeated_exact_reply_health_probes_are_quarantined(tmp_path: Path):
    sessions = []
    for index, prompt in enumerate(
        ("reply with exactly: ok", "Reply with exactly: OK", "Reply with exactly: OK.")
    ):
        path = tmp_path / f"probe-{index}.jsonl"
        path.write_text(
            json.dumps({"type": "user", "message": {"content": prompt}}) + "\n",
            encoding="utf-8",
        )
        session = _session("/workspace/info-collector")
        session.session_id = f"probe-{index}"
        session.path = path
        session.agent = "claude"
        session.user_msg_count = 1
        session.msg_count = 2
        session.active_sec = 1
        sessions.append(session)

    eligible, repetitive, clusters = _partition_repetitive_routines(sessions)

    assert eligible == []
    assert repetitive == sessions
    assert len(clusters) == 1


def test_export_prefers_first_user_input_over_cached_ai_summary(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"content": "理解一下这个项目最初要解决什么问题"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    session = _session("")
    session.path = path
    session.agent = "claude"
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    row = _row_for_session(
        session,
        since=now,
        until=now,
        cached={
            public_session_id(session): (
                "AI 最后修改了另一个项目，这不是用户最初的意图",
                "generated",
            )
        },
    )

    assert row.payload["evidence"]["summary"] == [
        "理解一下这个项目最初要解决什么问题"
    ]
    assert row.payload["evidence"]["summary_source"] == ["first_user"]


def test_selection_keeps_temporal_ratio_and_case_diversity():
    rows = []
    index = 0
    for split in ("train", "validation", "test"):
        for stratum in ("clear_repo", "top_level", "nested_workspace"):
            for project in ("alpha", "beta", "gamma"):
                rows.append(_row(index, split, stratum, project))
                index += 1

    selected = _select(rows, 12)
    assert len(selected) == 12
    assert sum(row.split == "train" for row in selected) == 6
    assert sum(row.split == "validation" for row in selected) == 3
    assert sum(row.split == "test" for row in selected) == 3
    assert {row.stratum for row in selected} == {
        "clear_repo",
        "top_level",
        "nested_workspace",
    }


def test_prior_evidence_ids_are_excluded_before_sampling(tmp_path: Path):
    prior = tmp_path / "prior.jsonl"
    prior.write_text(
        "\n".join(
            (
                json.dumps({"session_id": "eval-001"}),
                json.dumps({"session_id": "eval-003"}),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    excluded_ids = _load_excluded_session_ids([prior])

    retained, excluded_count = _exclude_prior_rows(
        [_row(index, "test", "clear_repo", "alpha") for index in range(5)],
        excluded_ids,
    )

    assert excluded_count == 2
    assert [row.payload["session_id"] for row in retained] == [
        "eval-000",
        "eval-002",
        "eval-004",
    ]


def test_prior_evidence_requires_a_session_id(tmp_path: Path):
    prior = tmp_path / "invalid.jsonl"
    prior.write_text(json.dumps({"split": "test"}) + "\n", encoding="utf-8")

    try:
        _load_excluded_session_ids([prior])
    except SystemExit as exc:
        assert "must contain a non-blank session_id" in str(exc)
    else:  # pragma: no cover - failure path assertion
        raise AssertionError("invalid prior evidence must fail closed")


def test_review_output_marks_candidates_as_suggestions():
    rendered = _render_review([_row(1, "train", "clear_repo", "alpha")])
    assert "Candidate projects are suggestions, not labels." in rendered
    assert "private-source-id" not in rendered
    assert "alpha" in rendered


def test_proposed_review_does_not_promote_suggestion_to_owner_label():
    row = _row(1, "train", "clear_repo", "alpha")
    rendered = _render_proposed_review(
        [row],
        {
            "proposals": {
                "eval-001": {
                    "projects": ["alpha"],
                    "confidence": "high",
                    "reason": "synthetic evidence",
                }
            }
        },
    )
    assert "Codex suggestions only" in rendered
    assert "have not been copied into `pilot.jsonl`" in rendered
    assert "| alpha | high |" in rendered


def test_export_shape_loads_as_unreviewed_and_cannot_be_labelled(tmp_path):
    row = _row(1, "train", "clear_repo", "alpha")
    path = tmp_path / "pilot.jsonl"
    path.write_text(json.dumps(row.payload) + "\n", encoding="utf-8")

    loaded = load_evidence_jsonl(path)
    assert loaded[0].label_status == "unreviewed"
    assert loaded[0].expected_projects == ()
    assert loaded[0].candidate_projects == ("alpha",)
