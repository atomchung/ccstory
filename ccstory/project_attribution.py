"""Private project-attribution research primitives for issue #223.

This module deliberately does not participate in recap generation.  It gives
the issue #223 evaluation harness a small, deterministic rule model that can
be exercised against owner-labelled local fixtures before any public product
contract is proposed.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROFILE_SCHEMA_VERSION = 1
RULE_STATUSES = frozenset({"accepted", "suggested", "disabled"})
RULE_POLARITIES = frozenset({"positive", "negative"})
RULE_AUTHORITIES = frozenset({"authoritative", "suggestive"})
RULE_MATCHERS = frozenset({"exact", "prefix", "glob", "token"})
LABEL_STATUSES = frozenset({"unreviewed", "owner_reviewed", "adjudicated"})

# Exact matches on these provider-neutral metadata fields are candidates for
# the authoritative tier.  They still leave the miner as ``suggested`` until
# an owner accepts the rule in the local profile.
DEFAULT_EXACT_FIELDS = frozenset(
    {"workspace", "repo", "remote", "issue_namespace", "artifact"}
)
DEFAULT_TOKEN_FIELDS = frozenset(
    {"title", "summary", "entity", "command", "path"}
)

_TOKEN_RE = re.compile(r"[^\W_]+(?:[-_.][^\W_]+)*", re.UNICODE)
_DEFAULT_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "again",
        "agent",
        "an",
        "analysis",
        "analyze",
        "and",
        "answer",
        "are",
        "as",
        "assess",
        "at",
        "be",
        "been",
        "before",
        "build",
        "but",
        "by",
        "can",
        "change",
        "check",
        "code",
        "concise",
        "could",
        "create",
        "data",
        "did",
        "do",
        "does",
        "done",
        "each",
        "file",
        "for",
        "from",
        "had",
        "has",
        "have",
        "help",
        "here",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "issue",
        "it",
        "its",
        "latest",
        "may",
        "make",
        "me",
        "more",
        "most",
        "my",
        "no",
        "not",
        "of",
        "on",
        "one",
        "only",
        "or",
        "our",
        "out",
        "please",
        "project",
        "read",
        "review",
        "run",
        "session",
        "should",
        "so",
        "test",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "up",
        "update",
        "us",
        "use",
        "verify",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "write",
        "you",
        "your",
    }
)


class ProjectAttributionError(ValueError):
    """Raised when a private evaluation fixture violates its contract."""


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _tokens(value: str) -> frozenset[str]:
    return frozenset(_normalize(token) for token in _TOKEN_RE.findall(value))


def _require_text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectAttributionError(f"{location} must be a non-blank string")
    if any(ord(char) < 32 for char in value):
        raise ProjectAttributionError(
            f"{location} must not contain control characters"
        )
    return value.strip()


def _string_tuple(value: object, location: str) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = tuple(value)
    else:
        raise ProjectAttributionError(
            f"{location} must be a string or an array of strings"
        )
    cleaned = tuple(item.strip() for item in values if item.strip())
    if len(cleaned) != len(values):
        raise ProjectAttributionError(f"{location} must not contain blank values")
    return cleaned


@dataclass(frozen=True)
class ProjectRule:
    """One inspectable rule owned by a local Project Profile."""

    id: str
    project_id: str
    field: str
    matcher: str
    pattern: str
    polarity: str = "positive"
    authority: str = "suggestive"
    weight: float = 1.0
    status: str = "suggested"
    provenance: str = "owner"
    support: int | None = None
    precision: float | None = None

    def __post_init__(self) -> None:
        for name in ("id", "project_id", "field", "pattern", "provenance"):
            _require_text(getattr(self, name), f"rule.{name}")
        if self.matcher not in RULE_MATCHERS:
            raise ProjectAttributionError(
                f"rule {self.id!r} has unsupported matcher {self.matcher!r}"
            )
        if self.polarity not in RULE_POLARITIES:
            raise ProjectAttributionError(
                f"rule {self.id!r} has unsupported polarity {self.polarity!r}"
            )
        if self.authority not in RULE_AUTHORITIES:
            raise ProjectAttributionError(
                f"rule {self.id!r} has unsupported authority {self.authority!r}"
            )
        if self.status not in RULE_STATUSES:
            raise ProjectAttributionError(
                f"rule {self.id!r} has unsupported status {self.status!r}"
            )
        if not math.isfinite(self.weight) or self.weight <= 0:
            raise ProjectAttributionError(
                f"rule {self.id!r} weight must be finite and positive"
            )
        if self.support is not None and self.support < 1:
            raise ProjectAttributionError(
                f"rule {self.id!r} support must be positive"
            )
        if self.precision is not None and not 0 <= self.precision <= 1:
            raise ProjectAttributionError(
                f"rule {self.id!r} precision must be between 0 and 1"
            )

    def matches(self, values: Sequence[str]) -> bool:
        pattern = _normalize(self.pattern)
        for raw in values:
            value = _normalize(raw)
            if self.matcher == "exact" and value == pattern:
                return True
            if self.matcher == "prefix" and value.startswith(pattern):
                return True
            if self.matcher == "glob" and fnmatch.fnmatchcase(value, pattern):
                return True
            if self.matcher == "token" and pattern in _tokens(raw):
                return True
        return False


@dataclass(frozen=True)
class ProjectProfile:
    id: str
    rules: tuple[ProjectRule, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.id, "project.id")
        if any(rule.project_id != self.id for rule in self.rules):
            raise ProjectAttributionError(
                f"project {self.id!r} contains a rule for another project"
            )


@dataclass(frozen=True)
class ProfileSet:
    projects: tuple[ProjectProfile, ...]
    schema_version: int = PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROFILE_SCHEMA_VERSION:
            raise ProjectAttributionError(
                f"unsupported profile schema_version {self.schema_version!r}"
            )
        project_ids = [project.id for project in self.projects]
        duplicate_projects = sorted(
            project_id
            for project_id, count in Counter(project_ids).items()
            if count > 1
        )
        if duplicate_projects:
            raise ProjectAttributionError(
                f"duplicate project ids: {', '.join(duplicate_projects)}"
            )
        rule_ids = [rule.id for project in self.projects for rule in project.rules]
        duplicate_rules = sorted(
            rule_id for rule_id, count in Counter(rule_ids).items() if count > 1
        )
        if duplicate_rules:
            raise ProjectAttributionError(
                f"duplicate rule ids: {', '.join(duplicate_rules)}"
            )

    @property
    def rules(self) -> tuple[ProjectRule, ...]:
        return tuple(rule for project in self.projects for rule in project.rules)


@dataclass(frozen=True)
class SessionEvidence:
    """One private evaluation row.

    ``expected_projects`` is owner-authored ground truth.  Empty on an
    owner-reviewed row is an open-set negative ("no known project");
    ``label_status = "unreviewed"`` excludes a row from training and metrics.
    """

    session_id: str
    evidence: Mapping[str, tuple[str, ...]]
    expected_projects: tuple[str, ...] = ()
    split: str = "test"
    label_status: str = "owner_reviewed"
    candidate_projects: tuple[str, ...] = ()
    case_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.session_id, "session_id")
        _require_text(self.split, f"session {self.session_id!r} split")
        if self.label_status not in LABEL_STATUSES:
            raise ProjectAttributionError(
                f"session {self.session_id!r} has unsupported label_status "
                f"{self.label_status!r}"
            )
        for index, project_id in enumerate(self.expected_projects):
            _require_text(
                project_id,
                f"session {self.session_id!r} expected_projects[{index}]",
            )
        if len(set(self.expected_projects)) != len(self.expected_projects):
            raise ProjectAttributionError(
                f"session {self.session_id!r} has duplicate expected projects"
            )
        for field_name, values in (
            ("candidate_projects", self.candidate_projects),
            ("case_tags", self.case_tags),
        ):
            for index, value in enumerate(values):
                _require_text(
                    value,
                    f"session {self.session_id!r} {field_name}[{index}]",
                )
            if len(set(values)) != len(values):
                raise ProjectAttributionError(
                    f"session {self.session_id!r} has duplicate {field_name}"
                )
        for field_name, values in self.evidence.items():
            _require_text(field_name, f"session {self.session_id!r} evidence field")
            if not isinstance(values, tuple) or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise ProjectAttributionError(
                    f"session {self.session_id!r} evidence values must be "
                    "non-blank strings"
                )


@dataclass(frozen=True)
class RuleMatch:
    rule_id: str
    project_id: str
    field: str
    polarity: str
    authority: str
    weight: float
    status: str
    provenance: str


@dataclass(frozen=True)
class CandidateScore:
    project_id: str
    score: float


@dataclass(frozen=True)
class AttributionResult:
    session_id: str
    status: str
    projects: tuple[str, ...]
    source: str
    reason: str
    candidates: tuple[CandidateScore, ...]
    matches: tuple[RuleMatch, ...]


def _active_rule(rule: ProjectRule, include_suggested: bool) -> bool:
    return rule.status == "accepted" or (
        include_suggested and rule.status == "suggested"
    )


def attribute_session(
    profiles: ProfileSet,
    session: SessionEvidence,
    *,
    include_suggested: bool = False,
    min_score: float = 2.0,
    min_margin: float = 1.0,
) -> AttributionResult:
    """Apply staged authoritative-then-scored project attribution."""

    if not math.isfinite(min_score) or not math.isfinite(min_margin):
        raise ProjectAttributionError("score thresholds must be finite")
    if min_score <= 0 or min_margin < 0:
        raise ProjectAttributionError(
            "min_score must be positive and min_margin must be non-negative"
        )

    matched_rules = [
        rule
        for rule in profiles.rules
        if _active_rule(rule, include_suggested)
        and rule.matches(session.evidence.get(rule.field, ()))
    ]
    matched_rules.sort(key=lambda rule: (rule.project_id, rule.id))

    blocked = {
        rule.project_id
        for rule in matched_rules
        if rule.authority == "authoritative" and rule.polarity == "negative"
    }
    authoritative = sorted(
        {
            rule.project_id
            for rule in matched_rules
            if rule.authority == "authoritative"
            and rule.polarity == "positive"
            and rule.project_id not in blocked
        }
    )

    scores: defaultdict[str, float] = defaultdict(float)
    for rule in matched_rules:
        if rule.project_id in blocked:
            continue
        direction = 1.0 if rule.polarity == "positive" else -1.0
        scores[rule.project_id] += direction * rule.weight
    candidates = tuple(
        CandidateScore(project_id=project_id, score=round(score, 6))
        for project_id, score in sorted(
            scores.items(), key=lambda item: (-item[1], item[0])
        )
    )
    matches = tuple(
        RuleMatch(
            rule_id=rule.id,
            project_id=rule.project_id,
            field=rule.field,
            polarity=rule.polarity,
            authority=rule.authority,
            weight=rule.weight,
            status=rule.status,
            provenance=rule.provenance,
        )
        for rule in matched_rules
    )

    if len(authoritative) == 1:
        return AttributionResult(
            session_id=session.session_id,
            status="accepted",
            projects=(authoritative[0],),
            source="authoritative",
            reason="unique_authoritative_match",
            candidates=candidates,
            matches=matches,
        )
    if len(authoritative) > 1:
        return AttributionResult(
            session_id=session.session_id,
            status="conflict",
            projects=tuple(authoritative),
            source="authoritative",
            reason="multiple_authoritative_projects",
            candidates=candidates,
            matches=matches,
        )

    eligible = [candidate for candidate in candidates if candidate.score >= min_score]
    if not eligible:
        reason = "authoritative_negative_rule" if blocked else "below_score_threshold"
        return AttributionResult(
            session_id=session.session_id,
            status="abstained",
            projects=(),
            source="none",
            reason=reason,
            candidates=candidates,
            matches=matches,
        )

    top = eligible[0]
    close = tuple(
        candidate.project_id
        for candidate in eligible
        if top.score - candidate.score < min_margin
    )
    if len(close) > 1:
        return AttributionResult(
            session_id=session.session_id,
            status="conflict",
            projects=close,
            source="scored",
            reason="insufficient_score_margin",
            candidates=candidates,
            matches=matches,
        )
    return AttributionResult(
        session_id=session.session_id,
        status="accepted",
        projects=(top.project_id,),
        source="scored",
        reason="score_and_margin_satisfied",
        candidates=candidates,
        matches=matches,
    )


def _session_from_mapping(
    raw: Mapping[str, Any], *, location: str
) -> SessionEvidence:
    allowed = {
        "session_id",
        "split",
        "label_status",
        "expected_projects",
        "candidate_projects",
        "case_tags",
        "evidence",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ProjectAttributionError(
            f"{location} has unknown fields: {', '.join(unknown)}"
        )
    session_id = _require_text(raw.get("session_id"), f"{location}.session_id")
    split_raw = raw.get("split", "test")
    split = _require_text(split_raw, f"{location}.split")
    label_status = _require_text(
        raw.get("label_status", "owner_reviewed"), f"{location}.label_status"
    )
    expected = _string_tuple(
        raw.get("expected_projects", []), f"{location}.expected_projects"
    )
    candidates = _string_tuple(
        raw.get("candidate_projects", []), f"{location}.candidate_projects"
    )
    case_tags = _string_tuple(raw.get("case_tags", []), f"{location}.case_tags")
    evidence_raw = raw.get("evidence")
    if not isinstance(evidence_raw, dict):
        raise ProjectAttributionError(f"{location}.evidence must be an object")
    evidence = {
        _require_text(field_name, f"{location}.evidence field"): _string_tuple(
            values, f"{location}.evidence.{field_name}"
        )
        for field_name, values in evidence_raw.items()
    }
    return SessionEvidence(
        session_id=session_id,
        evidence=evidence,
        expected_projects=expected,
        split=split,
        label_status=label_status,
        candidate_projects=candidates,
        case_tags=case_tags,
    )


def load_evidence_jsonl(path: Path) -> list[SessionEvidence]:
    sessions: list[SessionEvidence] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProjectAttributionError(f"could not read evidence {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProjectAttributionError(
                f"{path}:{line_number} is not valid JSON: {exc.msg}"
            ) from exc
        if not isinstance(raw, dict):
            raise ProjectAttributionError(
                f"{path}:{line_number} must contain a JSON object"
            )
        session = _session_from_mapping(raw, location=f"{path}:{line_number}")
        if session.session_id in seen:
            raise ProjectAttributionError(
                f"{path}:{line_number} duplicates session_id {session.session_id!r}"
            )
        seen.add(session.session_id)
        sessions.append(session)
    return sessions


def _rule_from_mapping(
    raw: Mapping[str, Any], *, project_id: str, location: str
) -> ProjectRule:
    allowed = {
        "id",
        "field",
        "matcher",
        "pattern",
        "polarity",
        "authority",
        "weight",
        "status",
        "provenance",
        "support",
        "precision",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ProjectAttributionError(
            f"{location} has unknown fields: {', '.join(unknown)}"
        )
    try:
        weight = float(raw.get("weight", 1.0))
    except (TypeError, ValueError) as exc:
        raise ProjectAttributionError(f"{location}.weight must be numeric") from exc
    support_raw = raw.get("support")
    if support_raw is not None and (
        not isinstance(support_raw, int) or isinstance(support_raw, bool)
    ):
        raise ProjectAttributionError(f"{location}.support must be an integer")
    precision_raw = raw.get("precision")
    if precision_raw is not None:
        try:
            precision = float(precision_raw)
        except (TypeError, ValueError) as exc:
            raise ProjectAttributionError(
                f"{location}.precision must be numeric"
            ) from exc
    else:
        precision = None
    return ProjectRule(
        id=_require_text(raw.get("id"), f"{location}.id"),
        project_id=project_id,
        field=_require_text(raw.get("field"), f"{location}.field"),
        matcher=_require_text(raw.get("matcher"), f"{location}.matcher"),
        pattern=_require_text(raw.get("pattern"), f"{location}.pattern"),
        polarity=_require_text(
            raw.get("polarity", "positive"), f"{location}.polarity"
        ),
        authority=_require_text(
            raw.get("authority", "suggestive"), f"{location}.authority"
        ),
        weight=weight,
        status=_require_text(raw.get("status", "suggested"), f"{location}.status"),
        provenance=_require_text(
            raw.get("provenance", "owner"), f"{location}.provenance"
        ),
        support=support_raw,
        precision=precision,
    )


def load_profiles_toml(path: Path) -> ProfileSet:
    try:
        import tomllib
    except ImportError as exc:  # pragma: no cover - requires Python < 3.11
        raise ProjectAttributionError("TOML profiles require Python 3.11+") from exc
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProjectAttributionError(f"could not read profiles {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ProjectAttributionError(
            f"could not parse profiles {path}: {exc}"
        ) from exc
    allowed_root = {"schema_version", "projects"}
    unknown_root = sorted(set(raw) - allowed_root)
    if unknown_root:
        raise ProjectAttributionError(
            f"profile root has unknown fields: {', '.join(unknown_root)}"
        )
    version = raw.get("schema_version")
    if version != PROFILE_SCHEMA_VERSION:
        raise ProjectAttributionError(
            f"unsupported profile schema_version {version!r}"
        )
    projects_raw = raw.get("projects")
    if not isinstance(projects_raw, list):
        raise ProjectAttributionError("profiles.projects must be an array of tables")
    projects: list[ProjectProfile] = []
    for project_index, project_raw in enumerate(projects_raw):
        location = f"projects[{project_index}]"
        if not isinstance(project_raw, dict):
            raise ProjectAttributionError(f"{location} must be a table")
        unknown = sorted(set(project_raw) - {"id", "rules"})
        if unknown:
            raise ProjectAttributionError(
                f"{location} has unknown fields: {', '.join(unknown)}"
            )
        project_id = _require_text(project_raw.get("id"), f"{location}.id")
        rules_raw = project_raw.get("rules", [])
        if not isinstance(rules_raw, list):
            raise ProjectAttributionError(f"{location}.rules must be an array")
        rules = tuple(
            _rule_from_mapping(
                rule_raw,
                project_id=project_id,
                location=f"{location}.rules[{rule_index}]",
            )
            for rule_index, rule_raw in enumerate(rules_raw)
            if isinstance(rule_raw, dict)
        )
        if len(rules) != len(rules_raw):
            raise ProjectAttributionError(f"{location}.rules must contain tables")
        projects.append(ProjectProfile(id=project_id, rules=rules))
    return ProfileSet(projects=tuple(projects), schema_version=version)


def suggest_rules(
    sessions: Sequence[SessionEvidence],
    *,
    split: str = "train",
    min_support: int = 2,
    min_precision: float = 0.9,
    exact_fields: Iterable[str] = DEFAULT_EXACT_FIELDS,
    token_fields: Iterable[str] = DEFAULT_TOKEN_FIELDS,
    stopwords: Iterable[str] = _DEFAULT_STOPWORDS,
) -> ProfileSet:
    """Mine inspectable high-precision rules from owner-labelled training rows.

    The miner emits only ``suggested`` rules.  Multi-project labels,
    unreviewed rows, and non-training rows are skipped.  Owner-reviewed
    open-set negatives contribute evidence against a candidate rule.
    """

    if min_support < 2:
        raise ProjectAttributionError(
            "min_support must be at least 2; single-session rules overfit"
        )
    if not 0 < min_precision <= 1:
        raise ProjectAttributionError("min_precision must be in (0, 1]")
    exact = frozenset(_normalize(field) for field in exact_fields)
    token = frozenset(_normalize(field) for field in token_fields)
    ignored = frozenset(_normalize(word) for word in stopwords)
    training_rows = [
        session
        for session in sessions
        if session.split == split
        and session.label_status != "unreviewed"
        and len(session.expected_projects) <= 1
    ]
    fingerprint_payload = [
        {
            "session_id": session.session_id,
            "expected_projects": list(session.expected_projects),
            "evidence": {
                field_name: list(values)
                for field_name, values in sorted(session.evidence.items())
            },
        }
        for session in sorted(training_rows, key=lambda item: item.session_id)
    ]
    source_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]

    positive_counts: defaultdict[
        tuple[str, str, str], Counter[str]
    ] = defaultdict(Counter)
    occurrence_counts: Counter[tuple[str, str, str]] = Counter()
    project_ids: set[str] = set()
    for session in training_rows:
        project_id = (
            session.expected_projects[0] if session.expected_projects else None
        )
        if project_id is not None:
            project_ids.add(project_id)
        seen_in_session: set[tuple[str, str, str]] = set()
        for raw_field, values in session.evidence.items():
            field_name = _normalize(raw_field)
            if field_name in exact:
                for value in values:
                    key = (field_name, "exact", _normalize(value))
                    seen_in_session.add(key)
            if field_name in token:
                for value in values:
                    for candidate in _tokens(value):
                        if len(candidate) >= 3 and candidate not in ignored:
                            key = (field_name, "token", candidate)
                            seen_in_session.add(key)
        for key in seen_in_session:
            occurrence_counts[key] += 1
            if project_id is not None:
                positive_counts[key][project_id] += 1

    rules_by_project: defaultdict[str, list[ProjectRule]] = defaultdict(list)
    for (field_name, matcher, pattern), per_project in sorted(
        positive_counts.items()
    ):
        ranked = per_project.most_common()
        if not ranked:
            continue
        support = ranked[0][1]
        if len(ranked) > 1 and ranked[1][1] == support:
            continue
        total = occurrence_counts[(field_name, matcher, pattern)]
        precision = support / total
        if support < min_support or precision < min_precision:
            continue
        project_id = ranked[0][0]
        digest = hashlib.sha256(
            f"{project_id}\0{field_name}\0{matcher}\0{pattern}".encode()
        ).hexdigest()[:10]
        authority = (
            "authoritative"
            if matcher == "exact" and field_name in exact
            else "suggestive"
        )
        weight = 100.0 if authority == "authoritative" else round(
            max(1.0, precision * 10.0), 3
        )
        rules_by_project[project_id].append(
            ProjectRule(
                id=f"suggested-{project_id}-{field_name}-{digest}",
                project_id=project_id,
                field=field_name,
                matcher=matcher,
                pattern=pattern,
                polarity="positive",
                authority=authority,
                weight=weight,
                status="suggested",
                provenance=f"mined:{split}:{source_fingerprint}",
                support=support,
                precision=round(precision, 6),
            )
        )

    return ProfileSet(
        projects=tuple(
            ProjectProfile(
                id=project_id,
                rules=tuple(
                    sorted(
                        rules_by_project[project_id],
                        key=lambda rule: (rule.field, rule.matcher, rule.pattern),
                    )
                ),
            )
            for project_id in sorted(project_ids)
        )
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_profiles_toml(profiles: ProfileSet) -> str:
    lines = [f"schema_version = {profiles.schema_version}"]
    for project in sorted(profiles.projects, key=lambda item: item.id):
        lines.extend(["", "[[projects]]", f"id = {_toml_string(project.id)}"])
        for rule in sorted(project.rules, key=lambda item: item.id):
            lines.extend(
                [
                    "",
                    "[[projects.rules]]",
                    f"id = {_toml_string(rule.id)}",
                    f"field = {_toml_string(rule.field)}",
                    f"matcher = {_toml_string(rule.matcher)}",
                    f"pattern = {_toml_string(rule.pattern)}",
                    f"polarity = {_toml_string(rule.polarity)}",
                    f"authority = {_toml_string(rule.authority)}",
                    f"weight = {rule.weight}",
                    f"status = {_toml_string(rule.status)}",
                    f"provenance = {_toml_string(rule.provenance)}",
                ]
            )
            if rule.support is not None:
                lines.append(f"support = {rule.support}")
            if rule.precision is not None:
                lines.append(f"precision = {rule.precision}")
    return "\n".join(lines) + "\n"


def result_to_dict(
    result: AttributionResult,
    expected_projects: Sequence[str] = (),
    *,
    label_status: str = "owner_reviewed",
    candidate_projects: Sequence[str] = (),
    case_tags: Sequence[str] = (),
) -> dict[str, Any]:
    predicted = list(result.projects if result.status == "accepted" else ())
    expected = list(expected_projects)
    reviewed = label_status != "unreviewed"
    if not reviewed:
        correct: bool | None = None
        exact_match: bool | None = None
    elif result.status == "accepted":
        # A partial hit is not a correct attribution.  In particular, an
        # accepted single-project prediction must not inflate precision when
        # the owner labelled the session as genuinely multi-project.
        exact_match = set(predicted) == set(expected)
        correct = exact_match
    elif result.status == "abstained" and not expected:
        correct = True
        exact_match = True
    else:
        correct = False
        exact_match = False
    return {
        "kind": "result",
        "session_id": result.session_id,
        "label_status": label_status,
        "expected_projects": expected,
        "review_candidates": list(candidate_projects),
        "case_tags": list(case_tags),
        "status": result.status,
        "predicted_projects": predicted,
        "candidate_projects": list(result.projects),
        "source": result.source,
        "reason": result.reason,
        "correct": correct,
        "exact_match": exact_match,
        "scores": [
            {"project_id": candidate.project_id, "score": candidate.score}
            for candidate in result.candidates
        ],
        "matches": [
            {
                "rule_id": match.rule_id,
                "project_id": match.project_id,
                "field": match.field,
                "polarity": match.polarity,
                "authority": match.authority,
                "weight": match.weight,
                "status": match.status,
                "provenance": match.provenance,
            }
            for match in result.matches
        ],
    }


def evaluate_profiles(
    profiles: ProfileSet,
    sessions: Sequence[SessionEvidence],
    *,
    split: str = "test",
    include_suggested: bool = False,
    min_score: float = 2.0,
    min_margin: float = 1.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = [session for session in sessions if session.split == split]
    rows: list[dict[str, Any]] = []
    for session in selected:
        result = attribute_session(
            profiles,
            session,
            include_suggested=include_suggested,
            min_score=min_score,
            min_margin=min_margin,
        )
        rows.append(
            result_to_dict(
                result,
                session.expected_projects,
                label_status=session.label_status,
                candidate_projects=session.candidate_projects,
                case_tags=session.case_tags,
            )
        )

    labelled = [row for row in rows if row["label_status"] != "unreviewed"]
    known = [row for row in labelled if row["expected_projects"]]
    unknown = [row for row in labelled if not row["expected_projects"]]
    accepted = [row for row in labelled if row["status"] == "accepted"]
    known_accepted = [row for row in known if row["status"] == "accepted"]
    unknown_false_accepts = [
        row for row in unknown if row["status"] == "accepted"
    ]
    correct = [row for row in accepted if row["correct"]]
    exact = [row for row in accepted if row["exact_match"]]
    abstained = [row for row in labelled if row["status"] == "abstained"]
    conflicts = [row for row in labelled if row["status"] == "conflict"]
    denominator = len(labelled)
    accepted_denominator = len(accepted)
    summary = {
        "kind": "summary",
        "split": split,
        "policy": (
            "accepted_plus_suggested_experiment"
            if include_suggested
            else "accepted_only"
        ),
        "sessions": len(selected),
        "labelled_sessions": denominator,
        "known_sessions": len(known),
        "unknown_sessions": len(unknown),
        "accepted": accepted_denominator,
        "known_accepted": len(known_accepted),
        "correct": len(correct),
        "exact_matches": len(exact),
        "false_positives": accepted_denominator - len(correct),
        "unknown_false_accepts": len(unknown_false_accepts),
        "conflicts": len(conflicts),
        "abstentions": len(abstained),
        "precision": (
            round(len(correct) / accepted_denominator, 6)
            if accepted_denominator
            else None
        ),
        "exact_match_rate": (
            round(len(exact) / accepted_denominator, 6)
            if accepted_denominator
            else None
        ),
        "coverage": (
            round(accepted_denominator / denominator, 6) if denominator else None
        ),
        "known_coverage": (
            round(len(known_accepted) / len(known), 6) if known else None
        ),
        "unknown_false_accept_rate": (
            round(len(unknown_false_accepts) / len(unknown), 6)
            if unknown
            else None
        ),
        "conflict_rate": (
            round(len(conflicts) / denominator, 6) if denominator else None
        ),
        "abstention_rate": (
            round(len(abstained) / denominator, 6) if denominator else None
        ),
        "model_calls": 0,
        "model_tokens": 0,
    }
    return rows, summary
