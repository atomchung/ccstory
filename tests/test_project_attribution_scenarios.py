"""Synthetic scenario matrix for the issue #223 attribution strategy.

These cases deliberately use invented projects, paths, and transcript text.
They exercise the public rule contract without copying private evaluation data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccstory.project_attribution import (
    ProfileSet,
    ProjectProfile,
    ProjectRule,
    SessionEvidence,
    attribute_session,
)
from ccstory.time_tracking import SessionStat
from scripts.project_attribution_sample import (
    _RepoEvidence,
    _candidate_projects,
    _full_user_text,
    _partition_owner_intent_sessions,
)


def _session(cwd: str, *, agent: str = "codex") -> SessionStat:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    return SessionStat(
        project="-workspace-synthetic",
        category="",
        session_id="synthetic-session",
        start=now,
        end=now,
        active_sec=60,
        msg_count=2,
        cwd=cwd,
        agent=agent,
    )


def _repo(
    root: str | None,
    *,
    name: str = "",
    relative_cwd: str = "",
    is_worktree: bool = False,
) -> _RepoEvidence:
    return _RepoEvidence(
        root=Path(root) if root else None,
        github=f"example/{name}" if name else "",
        name=name,
        relative_cwd=relative_cwd,
        is_worktree=is_worktree,
    )


@pytest.mark.parametrize(
    ("cwd", "workspace", "repo", "aliases", "expected"),
    [
        (
            "/workspace/atlas-app",
            "atlas-app",
            _repo("/workspace/atlas-app", name="atlas-app"),
            {},
            ("atlas-app",),
        ),
        (
            "/workspace/atlas-app-gtm",
            "atlas-app-gtm",
            _repo("/workspace/atlas-app-gtm", name="atlas-app"),
            {"atlas-app-gtm": "atlas-app"},
            ("atlas-app",),
        ),
        (
            "/workspace/legacy-atlas",
            "legacy-atlas",
            _repo(None),
            {"legacy-atlas": "atlas-app"},
            ("atlas-app",),
        ),
        (
            "/workspace/atlas-app/mobile",
            "atlas-app",
            _repo(
                "/workspace/atlas-app",
                name="atlas-app",
                relative_cwd="mobile",
            ),
            {},
            ("mobile", "atlas-app"),
        ),
        (
            "/workspace/.codex/worktrees/atlas-app-fix",
            "atlas-app",
            _repo(
                "/workspace/.codex/worktrees/atlas-app-fix",
                name="atlas-app",
                is_worktree=True,
            ),
            {},
            ("atlas-app",),
        ),
        (
            "/workspace/atlas-app/research-lab",
            "research-lab",
            _repo(
                "/workspace/atlas-app",
                name="atlas-app",
                relative_cwd="research-lab",
            ),
            {},
            ("research-lab", "atlas-app"),
        ),
        (
            "/workspace/research-lab",
            "(top-level)",
            _repo(None),
            {},
            ("research-lab",),
        ),
        (
            "/workspace",
            "(top-level)",
            _repo(None),
            {},
            (),
        ),
    ],
)
def test_folder_candidate_matrix(
    cwd: str,
    workspace: str,
    repo: _RepoEvidence,
    aliases: dict[str, str],
    expected: tuple[str, ...],
):
    assert _candidate_projects(
        _session(cwd),
        workspace,
        repo,
        aliases=aliases,
    ) == expected


def _rule(
    rule_id: str,
    project_id: str,
    *,
    field: str,
    pattern: str,
    authority: str = "suggestive",
    polarity: str = "positive",
    weight: float = 1.25,
) -> ProjectRule:
    return ProjectRule(
        id=rule_id,
        project_id=project_id,
        field=field,
        matcher="exact" if field == "repo" else "token",
        pattern=pattern,
        authority=authority,
        polarity=polarity,
        weight=weight,
        status="accepted",
        provenance="synthetic-owner",
    )


@pytest.fixture
def synthetic_profiles() -> ProfileSet:
    rules = (
        _rule(
            "atlas-repo",
            "atlas-app",
            field="repo",
            pattern="example/atlas-app",
            authority="authoritative",
            weight=100,
        ),
        _rule(
            "atlas-feature",
            "atlas-app",
            field="summary",
            pattern="atlasfeature",
        ),
        _rule(
            "atlas-launch",
            "atlas-app",
            field="summary",
            pattern="launchplan",
        ),
        _rule(
            "atlas-archive",
            "atlas-app",
            field="path",
            pattern="archived",
            authority="authoritative",
            polarity="negative",
            weight=100,
        ),
        _rule(
            "research-repo",
            "research-lab",
            field="repo",
            pattern="example/research-lab",
            authority="authoritative",
            weight=100,
        ),
        _rule(
            "research-experiment",
            "research-lab",
            field="summary",
            pattern="experiment",
        ),
        _rule(
            "research-dataset",
            "research-lab",
            field="summary",
            pattern="dataset",
        ),
    )
    return ProfileSet(
        projects=tuple(
            ProjectProfile(
                id=project_id,
                rules=tuple(
                    rule for rule in rules if rule.project_id == project_id
                ),
            )
            for project_id in ("atlas-app", "research-lab")
        )
    )


@pytest.mark.parametrize(
    ("evidence", "status", "projects", "reason"),
    [
        (
            {"repo": ("example/atlas-app",)},
            "accepted",
            ("atlas-app",),
            "unique_authoritative_match",
        ),
        (
            {"repo": ("example/unknown-tool",)},
            "abstained",
            (),
            "below_score_threshold",
        ),
        (
            {
                "repo": (
                    "example/atlas-app",
                    "example/research-lab",
                )
            },
            "conflict",
            ("atlas-app", "research-lab"),
            "multiple_authoritative_projects",
        ),
        (
            {"summary": ("atlasfeature launchplan",)},
            "accepted",
            ("atlas-app",),
            "score_and_margin_satisfied",
        ),
        (
            {"summary": ("atlasfeature",)},
            "abstained",
            (),
            "below_score_threshold",
        ),
        (
            {
                "summary": (
                    "atlasfeature launchplan experiment dataset",
                )
            },
            "conflict",
            ("atlas-app", "research-lab"),
            "insufficient_score_margin",
        ),
        (
            {
                "repo": ("example/atlas-app",),
                "path": ("archived",),
            },
            "abstained",
            (),
            "authoritative_negative_rule",
        ),
    ],
)
def test_rule_decision_matrix(
    synthetic_profiles: ProfileSet,
    evidence: dict[str, tuple[str, ...]],
    status: str,
    projects: tuple[str, ...],
    reason: str,
):
    result = attribute_session(
        synthetic_profiles,
        SessionEvidence(
            session_id="synthetic-evidence",
            evidence=evidence,
        ),
    )

    assert result.status == status
    assert result.projects == projects
    assert result.reason == reason


@pytest.mark.parametrize(
    ("agent", "records"),
    [
        (
            "claude",
            [
                {
                    "type": "user",
                    "message": {
                        "content": (
                            "<local-command-caveat>generated</local-command-caveat>"
                        )
                    },
                },
                {
                    "type": "user",
                    "message": {"content": "Plan the atlasfeature release"},
                },
            ],
        ),
        (
            "codex",
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "<task-notification>done</task-notification>",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "Plan the atlasfeature release",
                    },
                },
            ],
        ),
        (
            "antigravity",
            [
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "AI summary about research-lab",
                },
                {
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "content": (
                        "<USER_REQUEST>Plan the atlasfeature release"
                        "</USER_REQUEST>"
                    ),
                },
            ],
        ),
    ],
)
def test_provider_control_payloads_do_not_replace_owner_input(
    tmp_path: Path,
    agent: str,
    records: list[dict[str, object]],
):
    path = tmp_path / f"{agent}.jsonl"
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    session = _session("/workspace/atlas-app", agent=agent)
    session.path = path

    assert _full_user_text(session) == "Plan the atlasfeature release"


def test_owner_population_excludes_delegated_and_scheduled_sessions():
    direct = _session("/workspace/atlas-app")
    delegated = _session("/workspace/atlas-app")
    delegated.is_delegated = True
    delegated.delegation_source = "synthetic-parent-agent"
    scheduled = _session("/workspace/research-lab")
    scheduled.is_scheduled = True

    eligible, excluded_delegated, excluded_scheduled = (
        _partition_owner_intent_sessions([direct, delegated, scheduled])
    )

    assert eligible == [direct]
    assert excluded_delegated == [delegated]
    assert excluded_scheduled == [scheduled]
