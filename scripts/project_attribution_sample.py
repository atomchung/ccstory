#!/usr/bin/env python3
"""Build a private owner-review pilot set from local ccstory sessions (#223).

The output contains suggestions only.  Every row is ``label_status =
"unreviewed"`` with an empty ``expected_projects`` array, so it cannot
accidentally train the rule miner or enter evaluation metrics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

# Prefer the checkout that owns this script over an older installed ccstory.
# This keeps the documented direct invocation reproducible during research.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ccstory.artifacts import github_slug, repo_root_for_cwd
from ccstory.categorizer import project_identity
from ccstory.session_identity import public_session_id
from ccstory.session_summarizer import DB_PATH, SOURCE_NO_EVIDENCE
from ccstory.time_tracking import (
    SessionStat,
    _extract_first_user_text,
    collect_sessions,
)
from ccstory.providers.antigravity import (
    _content_text as _antigravity_content_text,
    _is_user_step as _is_antigravity_user_step,
    extract_user_request_text,
)
from ccstory.providers.codex import _codex_text, strip_task_wrapper


_WORKTREE_PARTS = frozenset({"worktrees", ".claude", ".codex"})
_GENERIC_CWD_NAMES = frozenset(
    {
        "src",
        "app",
        "repo",
        "project",
        "workspace",
        "work",
        "tmp",
        "private-tmp",
        "side-project",
        "atomo",
        "users",
        "codex",
    }
)
_SPLITS = ("train", "validation", "test")
_STRATA = (
    "top_level",
    "no_repo",
    "worktree",
    "nested_workspace",
    "workspace_repo_disagree",
    "clear_repo",
)
_ROUTINE_MIN_CLUSTER = 3
_ROUTINE_MIN_TEXT_CHARS = 160
_ROUTINE_SIGNATURE_CHARS = 320
_ROUTINE_LEAD_CHARS = 120
_SYNTHETIC_USER_PREFIXES = (
    "<command-message>",
    "<command-name>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<system-reminder>",
    "<task-notification>",
    "this session is being continued from a previous conversation",
    "the user just ran /insights",
)


@dataclass(frozen=True)
class _RepoEvidence:
    root: Path | None
    github: str
    name: str
    relative_cwd: str
    is_worktree: bool


@dataclass(frozen=True)
class _PilotRow:
    payload: dict[str, Any]
    split: str
    stratum: str
    project_key: str


def _slug(value: str) -> str:
    text = value.strip().casefold().replace("_", "-").replace(" ", "-")
    text = re.sub(r"[^a-z0-9.-]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-.")


def _canonical_project(
    value: str,
    aliases: Mapping[str, str] | None = None,
) -> str:
    if value.strip() == "(top-level)":
        return "(top-level)"
    project = _slug(value)
    return (aliases or {}).get(project, project)


def _weak_project_name(value: str) -> bool:
    return (
        not value
        or value == "(top-level)"
        or value in _GENERIC_CWD_NAMES
        or value.startswith("private-tmp-")
        or value.startswith("users-")
        or re.fullmatch(r"codex-\d{4}-\d{2}-\d{2}(?:-codex)?", value) is not None
    )


def _bounded_text(value: str, limit: int = 360) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _private_eval_id(session: SessionStat) -> str:
    material = f"{session.agent}\0{public_session_id(session)}"
    return "eval-" + hashlib.sha256(material.encode()).hexdigest()[:16]


def _read_cached_summaries(session_ids: Sequence[str]) -> dict[str, tuple[str, str]]:
    """Read existing summaries without creating or migrating the cache."""

    if not session_ids or not DB_PATH.is_file():
        return {}
    uri = f"file:{DB_PATH.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return {}
    rows: dict[str, tuple[str, str]] = {}
    try:
        for offset in range(0, len(session_ids), 500):
            chunk = session_ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            try:
                fetched = conn.execute(
                    f"""SELECT session_id, summary, source
                        FROM session_summaries
                        WHERE session_id IN ({placeholders})""",
                    chunk,
                ).fetchall()
            except sqlite3.Error:
                return {}
            for session_id, summary, source in fetched:
                if summary and source != SOURCE_NO_EVIDENCE:
                    rows[str(session_id)] = (str(summary), str(source))
    finally:
        conn.close()
    return rows


@lru_cache(maxsize=None)
def _repo_evidence(cwd: str) -> _RepoEvidence:
    if not cwd:
        return _RepoEvidence(None, "", "", "", False)
    cwd_path = Path(cwd)
    root = repo_root_for_cwd(cwd)
    path_parts = {part.casefold() for part in cwd_path.parts}
    is_worktree = (
        "worktrees" in path_parts
        and bool(path_parts.intersection(_WORKTREE_PARTS))
    )
    if root is None:
        return _RepoEvidence(None, "", "", "", is_worktree)
    github = github_slug(root) or ""
    name = _slug(github.rsplit("/", 1)[-1] if github else root.name)
    try:
        relative = str(cwd_path.resolve().relative_to(root.resolve()))
    except (OSError, ValueError):
        relative = ""
    return _RepoEvidence(root, github, name, relative, is_worktree)


def _candidate_projects(
    session: SessionStat,
    workspace: str,
    repo: _RepoEvidence,
    *,
    aliases: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    candidates: list[str] = []
    cwd_leaf = (
        _canonical_project(Path(session.cwd).name, aliases)
        if session.cwd
        else ""
    )
    repo_name = _canonical_project(repo.name, aliases)
    workspace_name = _canonical_project(workspace, aliases)
    nested = (
        repo.root is not None
        and repo.relative_cwd not in ("", ".")
        and not repo.is_worktree
    )
    if nested and not _weak_project_name(cwd_leaf):
        candidates.append(cwd_leaf)
    if repo_name and not _weak_project_name(repo_name):
        candidates.append(repo_name)
    if (
        not _weak_project_name(cwd_leaf)
        and not repo.is_worktree
        and cwd_leaf not in candidates
    ):
        candidates.append(cwd_leaf)
    if (
        not _weak_project_name(workspace_name)
        and workspace_name not in candidates
    ):
        candidates.append(workspace_name)
    return tuple(candidates[:3])


def _case_tags(
    session: SessionStat,
    workspace: str,
    repo: _RepoEvidence,
    candidates: Sequence[str],
    text: str,
) -> tuple[str, ...]:
    tags: list[str] = []
    if workspace == "(top-level)":
        tags.append("top_level")
    if repo.root is None:
        tags.append("no_repo")
    if repo.is_worktree:
        tags.append("worktree")
    if (
        repo.root is not None
        and repo.relative_cwd not in ("", ".")
        and not repo.is_worktree
    ):
        tags.append("nested_workspace")
    if repo.name and workspace not in ("", "(top-level)", repo.name):
        tags.append("workspace_repo_disagree")
    if (
        repo.root is not None
        and not repo.is_worktree
        and repo.relative_cwd in ("", ".")
        and workspace in (repo.name, "")
    ):
        tags.append("clear_repo")
    if len(candidates) > 1:
        tags.append("candidate_conflict")
    if not text:
        tags.append("weak_text")
    if session.is_scheduled:
        tags.append("scheduled")
    return tuple(dict.fromkeys(tags)) or ("other",)


def _primary_stratum(tags: Sequence[str]) -> str:
    return next((tag for tag in _STRATA if tag in tags), "other")


def _split_for(start: datetime, since: datetime, until: datetime) -> str:
    elapsed = (start - since).total_seconds()
    span = max((until - since).total_seconds(), 1)
    ratio = min(max(elapsed / span, 0), 1)
    if ratio < 0.5:
        return "train"
    if ratio < 0.75:
        return "validation"
    return "test"


def _partition_owner_intent_sessions(
    sessions: Sequence[SessionStat],
) -> tuple[list[SessionStat], list[SessionStat], list[SessionStat]]:
    eligible: list[SessionStat] = []
    delegated: list[SessionStat] = []
    scheduled: list[SessionStat] = []
    for session in sessions:
        if session.is_delegated:
            delegated.append(session)
        elif session.is_scheduled:
            scheduled.append(session)
        else:
            eligible.append(session)
    return eligible, delegated, scheduled


def _user_turn_texts(session: SessionStat) -> list[str]:
    """Read real user-turn payloads while dropping provider control messages."""

    if session.path is None:
        return []
    messages: list[str] = []
    try:
        with session.path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                text = ""
                if session.agent == "claude" and record.get("type") == "user":
                    message = record.get("message")
                    if isinstance(message, dict):
                        text = _extract_first_user_text(
                            message.get("content", "")
                        )
                elif (
                    session.agent == "codex"
                    and record.get("type") == "event_msg"
                ):
                    payload = record.get("payload")
                    if (
                        isinstance(payload, dict)
                        and payload.get("type") == "user_message"
                    ):
                        text = strip_task_wrapper(
                            _codex_text(payload.get("message", ""))
                        )
                elif session.agent == "antigravity":
                    step_type = record.get("type")
                    source = record.get("source")
                    if _is_antigravity_user_step(step_type, source):
                        text = extract_user_request_text(
                            _antigravity_content_text(record.get("content"))
                        )
                cleaned = text.strip()
                if (
                    cleaned
                    and "tool_use_id" not in cleaned
                    and not cleaned.casefold().startswith(
                        _SYNTHETIC_USER_PREFIXES
                    )
                ):
                    messages.append(cleaned)
    except OSError:
        return []
    return messages


def _full_user_text(session: SessionStat) -> str:
    """Read every user turn locally for high-confidence routine detection."""

    return "\n".join(_user_turn_texts(session))


def _has_manual_command_wrapper(session: SessionStat) -> bool:
    """Return whether the transcript records an explicit slash-command action."""

    if session.agent != "claude" or session.path is None:
        return False
    try:
        with session.path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "user":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                text = _extract_first_user_text(message.get("content", ""))
                if "<command-message>" in text or "<command-name>" in text:
                    return True
    except OSError:
        return False
    return False


def _routine_signature(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith(("<command-message>", "<command-name>")):
        return ""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    if re.match(
        r"^(?:reply|respond)\s+with\s+(?:exactly|just)\b|^say\s+\S+\s*$",
        normalized.strip(),
    ):
        return "probe:fixed-reply"
    normalized = re.sub(r"https?://\S+", " <url> ", normalized)
    normalized = re.sub(
        r"\b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b",
        " <date> ",
        normalized,
    )
    normalized = re.sub(r"\d+", "#", normalized)
    normalized = re.sub(r"[`*_#>-]+", " ", normalized)
    normalized = " ".join(normalized.split())
    if len(normalized) < _ROUTINE_MIN_TEXT_CHARS:
        return ""
    lead = re.split(r"[（(:：\n]", normalized, maxsplit=1)[0].strip()
    if len(lead) >= 10:
        return "lead:" + lead[:_ROUTINE_LEAD_CHARS]
    return "body:" + normalized[:_ROUTINE_SIGNATURE_CHARS]


def _looks_like_explicit_template(signature: str) -> bool:
    return (
        signature.startswith(
            (
                "lead:base directory for this skill",
                "lead:你是",
                "lead:you are",
            )
        )
        or "掃描驗證" in signature
        or "扫描验证" in signature
    )


def _partition_repetitive_routines(
    sessions: Sequence[SessionStat],
) -> tuple[list[SessionStat], list[SessionStat], list[dict[str, Any]]]:
    """Quarantine obvious repeated prompt families across full transcripts."""

    by_signature: defaultdict[str, list[SessionStat]] = defaultdict(list)
    for session in sessions:
        if _has_manual_command_wrapper(session):
            continue
        one_shot = (
            session.user_msg_count == 1
            and session.msg_count <= 4
            and session.active_sec <= 600
        )
        signature = _routine_signature(_full_user_text(session))
        if signature and (one_shot or _looks_like_explicit_template(signature)):
            by_signature[signature].append(session)

    repetitive_ids: set[tuple[str, str]] = set()
    clusters: list[dict[str, Any]] = []
    for signature, members in sorted(by_signature.items()):
        if len(members) < _ROUTINE_MIN_CLUSTER:
            continue
        cluster_id = hashlib.sha256(signature.encode()).hexdigest()[:12]
        repetitive_ids.update(
            (session.agent, public_session_id(session)) for session in members
        )
        clusters.append(
            {
                "cluster_id": cluster_id,
                "sessions": len(members),
                "by_provider": dict(
                    sorted(Counter(session.agent for session in members).items())
                ),
                "by_workspace": dict(
                    sorted(
                        Counter(
                            project_identity(session.project)
                            for session in members
                        ).items()
                    )
                ),
            }
        )

    eligible: list[SessionStat] = []
    repetitive: list[SessionStat] = []
    for session in sessions:
        identity = (session.agent, public_session_id(session))
        if identity in repetitive_ids:
            repetitive.append(session)
        else:
            eligible.append(session)
    return eligible, repetitive, clusters


def _row_for_session(
    session: SessionStat,
    *,
    since: datetime,
    until: datetime,
    cached: dict[str, tuple[str, str]],
    aliases: Mapping[str, str] | None = None,
) -> _PilotRow:
    public_id = public_session_id(session)
    cached_summary, summary_source = cached.get(public_id, ("", ""))
    user_turns = _user_turn_texts(session)
    if user_turns:
        text = user_turns[0]
        summary_source = "first_user"
    elif session.first_user_text:
        text = session.first_user_text
        summary_source = "first_user"
    elif session.native_title:
        text = session.native_title
        summary_source = "native_title"
    else:
        text = cached_summary
    text = _bounded_text(text)
    workspace = project_identity(session.project)
    repo = _repo_evidence(session.cwd)
    candidates = _candidate_projects(
        session,
        workspace,
        repo,
        aliases=aliases,
    )
    tags = _case_tags(session, workspace, repo, candidates, text)
    split = _split_for(session.start, since, until)
    evidence: dict[str, list[str]] = {
        "provider": [session.agent],
        "workspace": [workspace],
        "local_date": [session.start.astimezone().date().isoformat()],
    }
    if repo.github:
        evidence["repo"] = [repo.github]
    elif repo.name:
        evidence["repo"] = [repo.name]
    if repo.relative_cwd not in ("", "."):
        evidence["path"] = [_bounded_text(repo.relative_cwd, 160)]
    if session.native_title:
        evidence["title"] = [_bounded_text(session.native_title, 180)]
    if text:
        evidence["summary"] = [text]
    if summary_source:
        evidence["summary_source"] = [summary_source]
    payload = {
        "session_id": _private_eval_id(session),
        "split": split,
        "label_status": "unreviewed",
        "expected_projects": [],
        "candidate_projects": list(candidates),
        "case_tags": list(tags),
        "evidence": evidence,
    }
    return _PilotRow(
        payload=payload,
        split=split,
        stratum=_primary_stratum(tags),
        project_key=candidates[0] if candidates else "(none)",
    )


def _diverse_order(rows: Iterable[_PilotRow]) -> list[_PilotRow]:
    by_project: defaultdict[str, list[_PilotRow]] = defaultdict(list)
    for row in rows:
        by_project[row.project_key].append(row)
    for project_rows in by_project.values():
        project_rows.sort(key=lambda row: row.payload["session_id"])
    ordered: list[_PilotRow] = []
    project_keys = sorted(by_project)
    while project_keys:
        next_keys: list[str] = []
        for project_key in project_keys:
            project_rows = by_project[project_key]
            ordered.append(project_rows.pop(0))
            if project_rows:
                next_keys.append(project_key)
        project_keys = next_keys
    return ordered


def _select_split(rows: Sequence[_PilotRow], target: int) -> list[_PilotRow]:
    by_stratum: dict[str, list[_PilotRow]] = {
        stratum: _diverse_order(row for row in rows if row.stratum == stratum)
        for stratum in (*_STRATA, "other")
    }
    selected: list[_PilotRow] = []
    strata = [stratum for stratum, items in by_stratum.items() if items]
    while len(selected) < target and strata:
        remaining: list[str] = []
        for stratum in strata:
            if len(selected) >= target:
                break
            items = by_stratum[stratum]
            selected.append(items.pop(0))
            if items:
                remaining.append(stratum)
        strata = remaining
    return selected


def _select(rows: Sequence[_PilotRow], sample_size: int) -> list[_PilotRow]:
    targets = {
        "train": sample_size // 2,
        "validation": sample_size // 4,
    }
    targets["test"] = sample_size - sum(targets.values())
    selected: list[_PilotRow] = []
    for split in _SPLITS:
        split_rows = [row for row in rows if row.split == split]
        selected.extend(_select_split(split_rows, targets[split]))
    return sorted(
        selected,
        key=lambda row: (
            _SPLITS.index(row.split),
            row.stratum,
            row.payload["session_id"],
        ),
    )


def _exclude_prior_rows(
    rows: Sequence[_PilotRow],
    excluded_session_ids: Iterable[str],
) -> tuple[list[_PilotRow], int]:
    excluded = frozenset(excluded_session_ids)
    retained = [
        row for row in rows if row.payload["session_id"] not in excluded
    ]
    return retained, len(rows) - len(retained)


def _markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _render_review(rows: Sequence[_PilotRow]) -> str:
    lines = [
        "# Issue #223 project-attribution pilot review",
        "",
        "Every row is unreviewed. Candidate projects are suggestions, not labels.",
        "For each row, decide `expected_projects`: one project, several projects,",
        "or `[]` for genuinely unattributable work.",
        "",
        "| id | split | case | candidates | workspace/repo | bounded evidence |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        evidence = row.payload["evidence"]
        workspace = ", ".join(evidence.get("workspace", []))
        repo = ", ".join(evidence.get("repo", []))
        context = " / ".join(part for part in (workspace, repo) if part)
        text = (evidence.get("summary") or evidence.get("title") or [""])[0]
        lines.append(
            "| {id} | {split} | {case} | {candidates} | {context} | {text} |".format(
                id=row.payload["session_id"],
                split=row.split,
                case=_markdown_escape(", ".join(row.payload["case_tags"])),
                candidates=_markdown_escape(
                    ", ".join(row.payload["candidate_projects"]) or "(none)"
                ),
                context=_markdown_escape(context),
                text=_markdown_escape(_bounded_text(text, 140)),
            )
        )
    return "\n".join(lines) + "\n"


def _render_proposed_review(
    rows: Sequence[_PilotRow],
    proposals: dict[str, Any],
) -> str:
    proposal_rows = proposals.get("proposals")
    if not isinstance(proposal_rows, dict):
        raise ValueError("proposal file must contain a proposals object")
    lines = [
        "# Issue #223 proposed labels",
        "",
        "These are Codex suggestions only. They are not owner-reviewed labels and",
        "have not been copied into `pilot.jsonl`.",
        "",
        "| id | split | proposed | confidence | current candidates | reason | evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        proposal = proposal_rows.get(row.payload["session_id"], {})
        if not isinstance(proposal, dict):
            proposal = {}
        projects = proposal.get("projects")
        proposed = (
            ", ".join(str(project) for project in projects)
            if isinstance(projects, list) and projects
            else "[]"
        )
        evidence = row.payload["evidence"]
        text = (evidence.get("summary") or evidence.get("title") or [""])[0]
        lines.append(
            "| {id} | {split} | {proposed} | {confidence} | {candidates} | "
            "{reason} | {evidence} |".format(
                id=row.payload["session_id"],
                split=row.split,
                proposed=_markdown_escape(proposed),
                confidence=_markdown_escape(str(proposal.get("confidence", ""))),
                candidates=_markdown_escape(
                    ", ".join(row.payload["candidate_projects"]) or "(none)"
                ),
                reason=_markdown_escape(str(proposal.get("reason", ""))),
                evidence=_markdown_escape(_bounded_text(text, 110)),
            )
        )
    return "\n".join(lines) + "\n"


def build_pilot(
    *,
    days: int,
    sample_size: int,
    agent: str,
    aliases: Mapping[str, str] | None = None,
    excluded_session_ids: Iterable[str] = (),
) -> tuple[list[_PilotRow], dict[str, Any]]:
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    collected_sessions = collect_sessions(
        since,
        until,
        engaged_only=False,
        agent=agent,
    )
    sessions, delegated_sessions, scheduled_sessions = (
        _partition_owner_intent_sessions(collected_sessions)
    )
    sessions, repetitive_sessions, routine_clusters = (
        _partition_repetitive_routines(sessions)
    )
    public_ids = [public_session_id(session) for session in sessions]
    cached = _read_cached_summaries(public_ids)
    population_before_prior_exclusions = [
        _row_for_session(
            session,
            since=since,
            until=until,
            cached=cached,
            aliases=aliases,
        )
        for session in sessions
    ]
    population, excluded_prior_sessions = _exclude_prior_rows(
        population_before_prior_exclusions,
        excluded_session_ids,
    )
    selected = _select(population, min(sample_size, len(population)))
    manifest = {
        "schema_version": 1,
        "generated_at": until.isoformat(),
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "days": days,
        },
        "population_sessions_before_prior_exclusions": len(
            population_before_prior_exclusions
        ),
        "excluded_prior_sessions": excluded_prior_sessions,
        "population_sessions": len(population),
        "excluded_delegated_sessions": len(delegated_sessions),
        "excluded_delegated_by_source": dict(
            sorted(
                Counter(
                    session.delegation_source or "provider_delegated"
                    for session in delegated_sessions
                ).items()
            )
        ),
        "excluded_scheduled_sessions": len(scheduled_sessions),
        "excluded_scheduled_by_provider": dict(
            sorted(Counter(session.agent for session in scheduled_sessions).items())
        ),
        "excluded_repetitive_sessions": len(repetitive_sessions),
        "excluded_repetitive_by_provider": dict(
            sorted(Counter(session.agent for session in repetitive_sessions).items())
        ),
        "repetitive_clusters": routine_clusters,
        "selected_sessions": len(selected),
        "summary_cache_hits": len(cached),
        "population_by_provider": dict(
            sorted(
                Counter(
                    row.payload["evidence"]["provider"][0] for row in population
                ).items()
            )
        ),
        "selected_by_split": dict(
            sorted(Counter(row.split for row in selected).items())
        ),
        "selected_by_stratum": dict(
            sorted(Counter(row.stratum for row in selected).items())
        ),
        "privacy": {
            "network_calls": 0,
            "model_calls": 0,
            "full_transcripts_read": True,
            "absolute_cwd_exported": False,
            "raw_session_id_exported": False,
            "delegated_sessions_exported": False,
            "scheduled_sessions_exported": False,
            "repetitive_sessions_exported": False,
            "output_label_status": "unreviewed",
        },
        "project_aliases": len(aliases or {}),
    }
    return selected, manifest


def _load_aliases(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"--aliases must be a readable JSON object: {exc}")
    if not isinstance(raw, dict) or not all(
        isinstance(alias, str)
        and alias.strip()
        and isinstance(project, str)
        and project.strip()
        for alias, project in raw.items()
    ):
        raise SystemExit("--aliases must map non-empty strings to non-empty strings")
    return {
        _slug(alias): _slug(project)
        for alias, project in raw.items()
    }


def _load_excluded_session_ids(paths: Sequence[Path]) -> set[str]:
    session_ids: set[str] = set()
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SystemExit(
                f"--exclude-evidence must be readable JSONL: {exc}"
            ) from exc
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"{path}:{line_number} is not valid JSON: {exc.msg}"
                ) from exc
            session_id = raw.get("session_id") if isinstance(raw, dict) else None
            if not isinstance(session_id, str) or not session_id.strip():
                raise SystemExit(
                    f"{path}:{line_number} must contain a non-blank session_id"
                )
            session_ids.add(session_id.strip())
    return session_ids


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a private unreviewed project-attribution pilot set."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".local-eval") / "issue-223",
    )
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--sample-size", type=int, default=36)
    parser.add_argument("--agent", default="all")
    parser.add_argument(
        "--aliases",
        type=Path,
        help="optional private JSON map of directory aliases to canonical projects",
    )
    parser.add_argument(
        "--proposals",
        type=Path,
        help="optional Codex-suggestion JSON used only to render proposed-review.md",
    )
    parser.add_argument(
        "--exclude-evidence",
        action="append",
        default=[],
        type=Path,
        metavar="JSONL",
        help=(
            "exclude stable session IDs found in a prior evidence JSONL; "
            "repeat for multiple discovery/formal sets"
        ),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.days < 1 or args.sample_size < 1:
        raise SystemExit("--days and --sample-size must be positive")
    rows, manifest = build_pilot(
        days=args.days,
        sample_size=args.sample_size,
        agent=args.agent,
        aliases=_load_aliases(args.aliases),
        excluded_session_ids=_load_excluded_session_ids(args.exclude_evidence),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = args.output_dir / "pilot.jsonl"
    evidence_path.write_text(
        "".join(
            json.dumps(row.payload, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    (args.output_dir / "review.md").write_text(
        _render_review(rows),
        encoding="utf-8",
    )
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.proposals is not None:
        proposals = json.loads(args.proposals.read_text(encoding="utf-8"))
        if not isinstance(proposals, dict):
            raise SystemExit("--proposals must contain a JSON object")
        (args.output_dir / "proposed-review.md").write_text(
            _render_proposed_review(rows, proposals),
            encoding="utf-8",
        )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
