"""Provider-neutral GoalContext v1 and deterministic goal attribution."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .categorizer import load_project_aliases, project_identity

if TYPE_CHECKING:
    from .time_tracking import SessionSlice, SessionStat


GOAL_CONTEXT_SCHEMA_VERSION = 1
GOAL_CONTRIBUTION_UNIT = "seconds"
DEFAULT_GOAL_CONTEXT_PATH = Path.home() / ".ccstory" / "goals.toml"
PER_GOAL_SHARED_SEMANTICS = "overlapping_non_additive"

_ROOT_FIELDS = {"schema_version", "goals"}
_GOAL_FIELDS = {"id", "title", "projects", "valid_from", "valid_until"}


class GoalContextError(ValueError):
    """A goal context failed strict validation."""


def _text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoalContextError(f"{location} must be a non-blank string")
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise GoalContextError(f"{location} must not contain control characters")
    return value


def _parse_date(value: Any, location: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise GoalContextError(f"{location} must be an ISO date (YYYY-MM-DD)")


def _alias_map(aliases: Mapping[str, str] | None) -> dict[str, str]:
    return load_project_aliases() if aliases is None else dict(aliases)


def _project(value: Any, aliases: Mapping[str, str], location: str) -> str:
    # This is the existing, canonical project-identity lane. Do not add a
    # competing normalizer here.
    canonical = project_identity(_text(value, location), aliases=dict(aliases))
    if not canonical or any(
        ord(char) < 32 or ord(char) == 127 for char in canonical
    ):
        raise GoalContextError(f"{location} is not a valid project reference")
    return canonical


def _check_unknown(
    value: Mapping[str, Any], allowed: set[str], location: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise GoalContextError(
            f"{location} has unknown field(s): "
            + ", ".join(repr(name) for name in unknown)
        )


def _contribution(value: Any, location: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise GoalContextError(
            f"{location} must be a finite, non-negative number"
        )
    return float(value)


@dataclass(frozen=True)
class GoalDefinition:
    """A stable goal linked only to canonical physical project identities."""

    id: str
    title: str
    projects: tuple[str, ...]
    valid_from: date | None = None
    valid_until: date | None = None

    def __post_init__(self) -> None:
        goal_id = _text(self.id, "goal id")
        title = _text(self.title, f"goal {goal_id!r} title")
        if isinstance(self.projects, (str, bytes)):
            raise GoalContextError(
                f"goal {goal_id!r} projects must be a non-empty sequence"
            )
        try:
            raw_projects = tuple(self.projects)
        except TypeError as exc:
            raise GoalContextError(
                f"goal {goal_id!r} projects must be a non-empty sequence"
            ) from exc
        if not raw_projects:
            raise GoalContextError(f"goal {goal_id!r} projects must not be empty")
        projects = tuple(
            sorted(
                {
                    _project(
                        project, {}, f"goal {goal_id!r} projects[{index}]"
                    )
                    for index, project in enumerate(raw_projects)
                }
            )
        )
        valid_from = _parse_date(
            self.valid_from, f"goal {goal_id!r} valid_from"
        )
        valid_until = _parse_date(
            self.valid_until, f"goal {goal_id!r} valid_until"
        )
        if (
            valid_from is not None
            and valid_until is not None
            and valid_until < valid_from
        ):
            raise GoalContextError(
                f"goal {goal_id!r} valid_until must be on or after valid_from"
            )
        object.__setattr__(self, "id", goal_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "projects", projects)
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_until", valid_until)

    def matches(self, project: str, effective_date: date) -> bool:
        return (
            project in self.projects
            and (self.valid_from is None or effective_date >= self.valid_from)
            and (self.valid_until is None or effective_date <= self.valid_until)
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "projects": list(self.projects),
        }
        if self.valid_from is not None:
            result["valid_from"] = self.valid_from.isoformat()
        if self.valid_until is not None:
            result["valid_until"] = self.valid_until.isoformat()
        return result


@dataclass(frozen=True)
class GoalContext:
    """Validated goals plus source provenance."""

    schema_version: int
    goals: tuple[GoalDefinition, ...]
    source_metadata: Mapping[str, str] = field(default_factory=dict)
    source_fingerprint: str = ""

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != GOAL_CONTEXT_SCHEMA_VERSION
        ):
            raise GoalContextError(
                f"unsupported goal context schema_version "
                f"{self.schema_version!r}; expected "
                f"{GOAL_CONTEXT_SCHEMA_VERSION}"
            )
        goals = tuple(self.goals)
        if not all(isinstance(goal, GoalDefinition) for goal in goals):
            raise GoalContextError("goals must contain GoalDefinition values")
        ids = [goal.id for goal in goals]
        if len(ids) != len(set(ids)):
            duplicate = next(item for item in ids if ids.count(item) > 1)
            raise GoalContextError(f"duplicate goal id {duplicate!r}")
        metadata = {
            _text(key, "source metadata key"): _text(
                value, f"source metadata {key!r}"
            )
            for key, value in self.source_metadata.items()
        }
        if not isinstance(self.source_fingerprint, str):
            raise GoalContextError("source_fingerprint must be a string")
        object.__setattr__(self, "goals", tuple(sorted(goals, key=lambda g: g.id)))
        object.__setattr__(self, "source_metadata", dict(sorted(metadata.items())))

    def to_schema_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goals": [goal.to_dict() for goal in self.goals],
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.to_schema_dict()
        result["source_metadata"] = dict(self.source_metadata)
        result["source_fingerprint"] = self.source_fingerprint
        return result


@dataclass(frozen=True)
class GoalAttributionInput:
    """One already-scaled active-seconds contribution on one local date."""

    project: str
    effective_date: date
    contribution: float

    def __post_init__(self) -> None:
        effective_date = _parse_date(
            self.effective_date, "goal attribution effective_date"
        )
        if effective_date is None:
            raise GoalContextError(
                "goal attribution effective_date must be an ISO date"
            )
        object.__setattr__(
            self, "project", _project(self.project, {}, "goal attribution project")
        )
        object.__setattr__(
            self,
            "effective_date",
            effective_date,
        )
        object.__setattr__(
            self,
            "contribution",
            _contribution(self.contribution, "goal attribution contribution"),
        )


@dataclass(frozen=True)
class GoalContribution:
    """Contribution and minimal deterministic evidence for one goal."""

    goal_id: str
    title: str
    exclusive_contribution: float
    shared_contribution: float
    projects_touched: tuple[str, ...] = ()
    latest_activity: date | None = None
    shared_contribution_is_non_additive: bool = field(
        default=True, init=False
    )

    @property
    def total_contribution(self) -> float:
        return math.fsum(
            (self.exclusive_contribution, self.shared_contribution)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "title": self.title,
            "exclusive_contribution": self.exclusive_contribution,
            "shared_contribution": self.shared_contribution,
            "shared_contribution_is_non_additive": True,
            "total_contribution": self.total_contribution,
            "projects_touched": list(self.projects_touched),
            "latest_activity": (
                self.latest_activity.isoformat()
                if self.latest_activity is not None
                else None
            ),
        }


@dataclass(frozen=True)
class GoalBreakdown:
    """Per-goal active seconds and additive global coverage buckets."""

    goals: tuple[GoalContribution, ...]
    covered_contribution: float
    exclusive_contribution: float
    shared_contribution: float
    unattributed_contribution: float
    contribution_unit: str = field(
        default=GOAL_CONTRIBUTION_UNIT, init=False
    )
    per_goal_shared_semantics: str = field(
        default=PER_GOAL_SHARED_SEMANTICS, init=False
    )

    def __post_init__(self) -> None:
        bucket_total = math.fsum(
            (
                self.exclusive_contribution,
                self.shared_contribution,
                self.unattributed_contribution,
            )
        )
        if not math.isclose(
            bucket_total, self.covered_contribution, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise GoalContextError(
                "global exclusive/shared/unattributed contribution must "
                "reconcile to covered_contribution"
            )
        object.__setattr__(
            self, "goals", tuple(sorted(self.goals, key=lambda g: g.goal_id))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contribution_unit": self.contribution_unit,
            "goals": [goal.to_dict() for goal in self.goals],
            "coverage": {
                "covered_contribution": self.covered_contribution,
                "exclusive_contribution": self.exclusive_contribution,
                "shared_contribution": self.shared_contribution,
                "unattributed_contribution": self.unattributed_contribution,
            },
            "per_goal_shared_semantics": self.per_goal_shared_semantics,
        }


def parse_goal_context(
    data: Mapping[str, Any],
    *,
    aliases: Mapping[str, str] | None = None,
    source_metadata: Mapping[str, str] | None = None,
    source_fingerprint: str = "",
) -> GoalContext:
    """Strictly validate an already-decoded GoalContext v1 mapping."""

    if not isinstance(data, Mapping):
        raise GoalContextError("goal context root must be a table")
    _check_unknown(data, _ROOT_FIELDS, "goal context root")
    if "schema_version" not in data:
        raise GoalContextError("goal context is missing schema_version")
    if "goals" not in data:
        raise GoalContextError("goal context is missing goals")
    version = data["schema_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != GOAL_CONTEXT_SCHEMA_VERSION
    ):
        raise GoalContextError(
            f"unsupported goal context schema_version {version!r}; "
            f"expected {GOAL_CONTEXT_SCHEMA_VERSION}"
        )
    raw_goals = data["goals"]
    if not isinstance(raw_goals, list):
        raise GoalContextError("goal context goals must be an array of tables")

    project_aliases = _alias_map(aliases)
    definitions: list[GoalDefinition] = []
    for index, raw_goal in enumerate(raw_goals):
        location = f"goals[{index}]"
        if not isinstance(raw_goal, Mapping):
            raise GoalContextError(f"{location} must be a table")
        _check_unknown(raw_goal, _GOAL_FIELDS, location)
        if "id" not in raw_goal:
            raise GoalContextError(f"{location} is missing id")
        goal_id = _text(raw_goal["id"], f"{location}.id")
        if "title" not in raw_goal:
            raise GoalContextError(f"goal {goal_id!r} is missing title")
        if "projects" not in raw_goal:
            raise GoalContextError(f"goal {goal_id!r} is missing projects")
        raw_projects = raw_goal["projects"]
        if not isinstance(raw_projects, list) or not raw_projects:
            raise GoalContextError(
                f"goal {goal_id!r} projects must be a non-empty array"
            )
        definitions.append(
            GoalDefinition(
                id=goal_id,
                title=_text(raw_goal["title"], f"goal {goal_id!r} title"),
                projects=tuple(
                    _project(
                        project,
                        project_aliases,
                        f"goal {goal_id!r} projects[{project_index}]",
                    )
                    for project_index, project in enumerate(raw_projects)
                ),
                valid_from=_parse_date(
                    raw_goal.get("valid_from"),
                    f"goal {goal_id!r} valid_from",
                ),
                valid_until=_parse_date(
                    raw_goal.get("valid_until"),
                    f"goal {goal_id!r} valid_until",
                ),
            )
        )
    return GoalContext(
        schema_version=version,
        goals=tuple(definitions),
        source_metadata=source_metadata or {},
        source_fingerprint=source_fingerprint,
    )


def load_goal_context(
    path: str | Path | None = None,
    *,
    aliases: Mapping[str, str] | None = None,
) -> GoalContext | None:
    """Load strict TOML; an absent default file means goal support is off."""

    explicit = path is not None
    source = (
        Path(path).expanduser()
        if explicit
        else DEFAULT_GOAL_CONTEXT_PATH.expanduser()
    )
    try:
        raw = source.read_bytes()
    except FileNotFoundError as exc:
        if not explicit:
            return None
        raise GoalContextError(f"goal context file does not exist: {source}") from exc
    except OSError as exc:
        raise GoalContextError(f"could not read goal context file {source}: {exc}") from exc
    try:
        import tomllib

        decoded = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise GoalContextError(
            f"could not parse goal context TOML {source}: {exc}"
        ) from exc
    return parse_goal_context(
        decoded,
        aliases=aliases,
        source_metadata={"format": "toml", "path": str(source.resolve())},
        source_fingerprint=f"sha256:{hashlib.sha256(raw).hexdigest()}",
    )


def attribute_goals(
    items: Iterable[GoalAttributionInput],
    context: GoalContext | None,
    *,
    aliases: Mapping[str, str] | None = None,
) -> GoalBreakdown | None:
    """Apply the 0/1/>1 unmatched/exclusive/shared attribution rule."""

    if context is None:
        return None
    project_aliases = _alias_map(aliases)
    goals = context.goals
    normalized = sorted(
        (
            item.effective_date,
            _project(item.project, project_aliases, "goal attribution project"),
            item.contribution,
        )
        for item in items
    )
    exclusive = {goal.id: [] for goal in goals}
    shared = {goal.id: [] for goal in goals}
    touched = {goal.id: set() for goal in goals}
    latest: dict[str, date | None] = {goal.id: None for goal in goals}
    global_exclusive: list[float] = []
    global_shared: list[float] = []
    global_unattributed: list[float] = []

    for effective_date, project, contribution in normalized:
        matches = [
            goal for goal in goals if goal.matches(project, effective_date)
        ]
        if not matches:
            global_unattributed.append(contribution)
            continue
        target = exclusive if len(matches) == 1 else shared
        (global_exclusive if len(matches) == 1 else global_shared).append(
            contribution
        )
        for goal in matches:
            target[goal.id].append(contribution)
            touched[goal.id].add(project)
            if latest[goal.id] is None or effective_date > latest[goal.id]:
                latest[goal.id] = effective_date

    per_goal = tuple(
        GoalContribution(
            goal_id=goal.id,
            title=goal.title,
            exclusive_contribution=math.fsum(exclusive[goal.id]),
            shared_contribution=math.fsum(shared[goal.id]),
            projects_touched=tuple(sorted(touched[goal.id])),
            latest_activity=latest[goal.id],
        )
        for goal in goals
    )
    exclusive_total = math.fsum(global_exclusive)
    shared_total = math.fsum(global_shared)
    unattributed_total = math.fsum(global_unattributed)
    return GoalBreakdown(
        goals=per_goal,
        covered_contribution=math.fsum(
            (exclusive_total, shared_total, unattributed_total)
        ),
        exclusive_contribution=exclusive_total,
        shared_contribution=shared_total,
        unattributed_contribution=unattributed_total,
    )


def _date_segments(
    start: float, end: float, report_timezone: tzinfo
) -> Iterable[tuple[date, float]]:
    cursor = start
    while cursor < end:
        local_date = datetime.fromtimestamp(cursor, tz=report_timezone).date()
        next_midnight = datetime.combine(
            local_date + timedelta(days=1), time.min, tzinfo=report_timezone
        ).timestamp()
        segment_end = min(end, next_midnight)
        if segment_end <= cursor:
            raise GoalContextError("report timezone has a non-advancing midnight")
        yield local_date, segment_end - cursor
        cursor = segment_end


def build_goal_breakdown(
    sessions: Sequence[SessionStat | SessionSlice],
    context: GoalContext | None,
    *,
    aliases: Mapping[str, str] | None = None,
    timezone: tzinfo | None = None,
) -> GoalBreakdown | None:
    """Attribute existing session evidence using the existing wall-clock scale.

    Validity dates use the report calendar: pass ``since.tzinfo`` from the
    report window.  The default matches ``parse_window()``'s system-local
    timezone. Inclusive ``valid_until`` ends at the next local midnight.
    """

    if context is None:
        return None
    from .time_tracking import (
        SessionSlice,
        active_intervals_for_timestamps,
        wall_clock_active_sec,
    )

    report_timezone = timezone or datetime.now().astimezone().tzinfo
    if report_timezone is None:  # pragma: no cover
        raise GoalContextError("could not determine the system local timezone")
    sessions = tuple(sessions)
    raw_total = math.fsum(session.active_sec for session in sessions)
    scale = wall_clock_active_sec(sessions) / raw_total if raw_total else 1.0
    items: list[GoalAttributionInput] = []
    for session in sessions:
        if session.active_sec <= 0:
            continue
        intervals = (
            session.active_intervals
            if isinstance(session, SessionSlice)
            else active_intervals_for_timestamps(session.timestamps)
        )
        interval_total = math.fsum(i.end - i.start for i in intervals)
        if not interval_total:
            raise GoalContextError(
                f"session {session.session_id!r} has positive contribution "
                "without active-interval evidence"
            )
        for interval in intervals:
            for effective_date, duration in _date_segments(
                interval.start, interval.end, report_timezone
            ):
                items.append(
                    GoalAttributionInput(
                        session.project,
                        effective_date,
                        session.active_sec * duration / interval_total * scale,
                    )
                )
    return attribute_goals(items, context, aliases=aliases)
