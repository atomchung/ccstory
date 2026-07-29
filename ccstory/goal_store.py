"""Managed GoalContext storage and single-source recap selection."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .categorizer import load_goal_context_path
from .goals import (
    GOAL_CONTEXT_SCHEMA_VERSION,
    GoalContext,
    GoalContextError,
    load_goal_context,
    parse_goal_context,
)


def managed_goal_context_path() -> Path:
    """Return the only GoalContext path mutated by ``ccstory goal``."""

    return Path.home() / ".ccstory" / "goals.toml"


def _empty_context(
    aliases: Mapping[str, str] | None = None,
) -> GoalContext:
    return parse_goal_context(
        {"schema_version": GOAL_CONTEXT_SCHEMA_VERSION, "goals": []},
        aliases=aliases,
    )


def load_managed_goal_context(
    path: Path | None = None,
    *,
    aliases: Mapping[str, str] | None = None,
) -> GoalContext:
    """Load the managed store, representing an absent store as empty v1."""

    source = path if path is not None else managed_goal_context_path()
    if not source.exists():
        return _empty_context(aliases)
    context = load_goal_context(source, aliases=aliases)
    if context is None:  # An explicit path never uses the core's no-file case.
        return _empty_context(aliases)  # pragma: no cover
    return context


def render_goal_context_toml(context: GoalContext) -> bytes:
    """Serialize validated v1 data in one deterministic strict TOML form."""

    lines = [f"schema_version = {GOAL_CONTEXT_SCHEMA_VERSION}", ""]
    for goal in context.goals:
        lines.extend(
            (
                "[[goals]]",
                f"id = {json.dumps(goal.id, ensure_ascii=False)}",
                f"title = {json.dumps(goal.title, ensure_ascii=False)}",
                "projects = ["
                + ", ".join(
                    json.dumps(project, ensure_ascii=False)
                    for project in goal.projects
                )
                + "]",
            )
        )
        if goal.valid_from is not None:
            lines.append(f"valid_from = {goal.valid_from.isoformat()}")
        if goal.valid_until is not None:
            lines.append(f"valid_until = {goal.valid_until.isoformat()}")
        lines.append("")
    try:
        return ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GoalContextError(
            "goal context text must contain valid Unicode characters"
        ) from exc


def _atomic_write_private(path: Path, payload: bytes) -> None:
    """Atomically replace ``path`` from a private same-directory temp file."""

    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise GoalContextError(
            f"could not create goal context directory {path.parent}: {exc}"
        ) from exc

    fd = -1
    temp_path: Path | None = None
    try:
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(raw_temp_path)
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            # mkstemp is already private on POSIX. fchmod is best-effort on
            # platforms that do not expose POSIX permissions.
            pass
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise GoalContextError(f"could not write goal context {path}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def set_managed_goal(
    goal_id: str,
    *,
    title: str,
    projects: list[str],
    valid_from: str | None = None,
    valid_until: str | None = None,
    path: Path | None = None,
    aliases: Mapping[str, str] | None = None,
) -> tuple[GoalContext, bool]:
    """Create or replace one goal by stable id, preserving all other goals."""

    destination = path if path is not None else managed_goal_context_path()
    existing = load_managed_goal_context(destination, aliases=aliases)
    raw_goal: dict[str, object] = {
        "id": goal_id,
        "title": title,
        "projects": list(projects),
    }
    if valid_from is not None:
        raw_goal["valid_from"] = valid_from
    if valid_until is not None:
        raw_goal["valid_until"] = valid_until
    parsed_new = parse_goal_context(
        {
            "schema_version": GOAL_CONTEXT_SCHEMA_VERSION,
            "goals": [raw_goal],
        },
        aliases=aliases,
    )
    canonical_goal_id = parsed_new.goals[0].id
    existed = any(goal.id == canonical_goal_id for goal in existing.goals)
    prospective = GoalContext(
        schema_version=GOAL_CONTEXT_SCHEMA_VERSION,
        goals=tuple(
            goal for goal in existing.goals if goal.id != canonical_goal_id
        )
        + parsed_new.goals,
    )
    payload = render_goal_context_toml(prospective)
    _atomic_write_private(destination, payload)
    return prospective, existed


def _validated_goal_id(goal_id: str) -> str:
    """Validate an unset id through the canonical GoalContext parser."""

    validation = parse_goal_context(
        {
            "schema_version": GOAL_CONTEXT_SCHEMA_VERSION,
            "goals": [
                {
                    "id": goal_id,
                    "title": "Goal id validation",
                    "projects": ["ccstory-validation"],
                }
            ],
        },
        aliases={},
    )
    return validation.goals[0].id


def unset_managed_goal(
    goal_id: str,
    *,
    path: Path | None = None,
    aliases: Mapping[str, str] | None = None,
) -> bool:
    """Remove one managed goal; a missing id is a byte-preserving no-op."""

    destination = path if path is not None else managed_goal_context_path()
    canonical_goal_id = _validated_goal_id(goal_id)
    existing = load_managed_goal_context(destination, aliases=aliases)
    if not any(goal.id == canonical_goal_id for goal in existing.goals):
        return False
    prospective = GoalContext(
        schema_version=GOAL_CONTEXT_SCHEMA_VERSION,
        goals=tuple(
            goal for goal in existing.goals if goal.id != canonical_goal_id
        ),
    )
    _atomic_write_private(destination, render_goal_context_toml(prospective))
    return True


def _resolve_user_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _with_source_kind(context: GoalContext, source_kind: str) -> GoalContext:
    return GoalContext(
        schema_version=context.schema_version,
        goals=context.goals,
        source_metadata={
            **context.source_metadata,
            "source_kind": source_kind,
        },
        source_fingerprint=context.source_fingerprint,
    )


def resolve_goal_context_source(
    goals_file: str | Path | None = None,
    *,
    config_path: Path,
    cwd: Path | None = None,
    managed_path: Path | None = None,
    aliases: Mapping[str, str] | None = None,
) -> GoalContext | None:
    """Load exactly one GoalContext by explicit/configured/managed precedence."""

    source_kind: str
    if goals_file is not None:
        source_kind = "explicit"
        source = _resolve_user_path(goals_file, base=cwd or Path.cwd())
    else:
        try:
            configured = load_goal_context_path(config_path)
        except ValueError as exc:
            raise GoalContextError(str(exc)) from exc
        if configured is not None:
            source_kind = "configured"
            source = _resolve_user_path(
                configured,
                base=config_path.expanduser().resolve().parent,
            )
        else:
            source_kind = "managed"
            source = (
                managed_path
                if managed_path is not None
                else managed_goal_context_path()
            ).expanduser()
            if not source.exists():
                return None
            source = source.resolve()

    if not source.exists():
        raise GoalContextError(
            f"{source_kind} goal context file does not exist: {source}"
        )
    context = load_goal_context(source, aliases=aliases)
    if context is None:  # All selected paths are explicit to the core loader.
        return None  # pragma: no cover
    return _with_source_kind(context, source_kind)
