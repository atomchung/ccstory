"""Private deterministic rule-evaluation contracts for issue #223."""

from __future__ import annotations

from pathlib import Path

import pytest

from ccstory.project_attribution import (
    ProfileSet,
    ProjectProfile,
    ProjectRule,
    SessionEvidence,
    attribute_session,
    evaluate_profiles,
    load_profiles_toml,
    render_profiles_toml,
    suggest_rules,
)


def _rule(
    rule_id: str,
    project_id: str,
    *,
    field: str = "repo",
    matcher: str = "exact",
    pattern: str | None = None,
    polarity: str = "positive",
    authority: str = "authoritative",
    weight: float = 100.0,
    status: str = "accepted",
) -> ProjectRule:
    return ProjectRule(
        id=rule_id,
        project_id=project_id,
        field=field,
        matcher=matcher,
        pattern=pattern or f"github.com/example/{project_id}",
        polarity=polarity,
        authority=authority,
        weight=weight,
        status=status,
        provenance="synthetic-owner",
    )


def _profiles(*rules: ProjectRule) -> ProfileSet:
    project_ids = sorted({rule.project_id for rule in rules})
    return ProfileSet(
        projects=tuple(
            ProjectProfile(
                id=project_id,
                rules=tuple(rule for rule in rules if rule.project_id == project_id),
            )
            for project_id in project_ids
        )
    )


def _session(
    session_id: str,
    evidence: dict[str, tuple[str, ...]],
    *,
    expected: tuple[str, ...] = (),
    split: str = "test",
    label_status: str = "owner_reviewed",
    candidate_projects: tuple[str, ...] = (),
    case_tags: tuple[str, ...] = (),
) -> SessionEvidence:
    return SessionEvidence(
        session_id=session_id,
        evidence=evidence,
        expected_projects=expected,
        split=split,
        label_status=label_status,
        candidate_projects=candidate_projects,
        case_tags=case_tags,
    )


def test_unique_authoritative_metadata_stops_the_engine():
    profiles = _profiles(
        _rule("repo-alpha", "alpha"),
        _rule(
            "summary-beta",
            "beta",
            field="summary",
            matcher="token",
            pattern="sharedword",
            authority="suggestive",
            weight=10,
        ),
    )
    result = attribute_session(
        profiles,
        _session(
            "s1",
            {
                "repo": ("github.com/example/alpha",),
                "summary": ("A sharedword appeared here",),
            },
        ),
    )
    assert result.status == "accepted"
    assert result.projects == ("alpha",)
    assert result.source == "authoritative"
    assert {match.rule_id for match in result.matches} == {
        "repo-alpha",
        "summary-beta",
    }


def test_conflicting_authoritative_metadata_is_visible_not_first_match_wins():
    profiles = _profiles(
        _rule("workspace-alpha", "alpha", field="workspace", pattern="shared"),
        _rule("repo-beta", "beta", pattern="github.com/example/beta"),
    )
    result = attribute_session(
        profiles,
        _session(
            "s1",
            {
                "workspace": ("shared",),
                "repo": ("github.com/example/beta",),
            },
        ),
    )
    assert result.status == "conflict"
    assert result.projects == ("alpha", "beta")
    assert result.reason == "multiple_authoritative_projects"


def test_negative_authoritative_rule_blocks_a_positive_match():
    profiles = _profiles(
        _rule("repo-alpha", "alpha"),
        _rule(
            "exclude-archive",
            "alpha",
            field="path",
            matcher="glob",
            pattern="*/archive/*",
            polarity="negative",
        ),
    )
    result = attribute_session(
        profiles,
        _session(
            "s1",
            {
                "repo": ("github.com/example/alpha",),
                "path": ("/tmp/archive/old",),
            },
        ),
    )
    assert result.status == "abstained"
    assert result.reason == "authoritative_negative_rule"


def test_scored_rules_need_threshold_and_margin():
    profiles = _profiles(
        _rule(
            "alpha-token",
            "alpha",
            field="summary",
            matcher="token",
            pattern="alphasymbol",
            authority="suggestive",
            weight=4,
        ),
        _rule(
            "beta-token",
            "beta",
            field="summary",
            matcher="token",
            pattern="betasymbol",
            authority="suggestive",
            weight=3.5,
        ),
    )
    result = attribute_session(
        profiles,
        _session(
            "s1",
            {"summary": ("alphasymbol and betasymbol",)},
        ),
        min_score=2,
        min_margin=1,
    )
    assert result.status == "conflict"
    assert result.projects == ("alpha", "beta")

    accepted = attribute_session(
        profiles,
        _session("s2", {"summary": ("alphasymbol only",)}),
        min_score=2,
        min_margin=1,
    )
    assert accepted.status == "accepted"
    assert accepted.projects == ("alpha",)


def test_suggested_rules_are_inert_unless_evaluation_opts_in():
    profiles = _profiles(_rule("repo-alpha", "alpha", status="suggested"))
    session = _session("s1", {"repo": ("github.com/example/alpha",)})

    assert attribute_session(profiles, session).status == "abstained"
    result = attribute_session(profiles, session, include_suggested=True)
    assert result.status == "accepted"
    assert result.matches[0].status == "suggested"


def test_rule_miner_uses_only_single_labelled_train_rows_and_skips_ambiguity():
    sessions = [
        _session(
            "train-a1",
            {
                "repo": ("github.com/example/alpha",),
                "summary": ("alphasymbol sharedword",),
            },
            expected=("alpha",),
            split="train",
        ),
        _session(
            "train-a2",
            {
                "repo": ("github.com/example/alpha",),
                "summary": ("alphasymbol sharedword",),
            },
            expected=("alpha",),
            split="train",
        ),
        _session(
            "train-b1",
            {
                "repo": ("github.com/example/beta",),
                "summary": ("betasymbol sharedword",),
            },
            expected=("beta",),
            split="train",
        ),
        _session(
            "train-b2",
            {
                "repo": ("github.com/example/beta",),
                "summary": ("betasymbol sharedword",),
            },
            expected=("beta",),
            split="train",
        ),
        _session(
            "multi",
            {"repo": ("github.com/example/both",)},
            expected=("alpha", "beta"),
            split="train",
        ),
        _session(
            "held-out",
            {"summary": ("testonlytoken",)},
            expected=("alpha",),
            split="test",
        ),
    ]
    suggested = suggest_rules(
        sessions,
        min_support=2,
        min_precision=1.0,
    )
    rules = suggested.rules
    patterns = {rule.pattern for rule in rules}

    assert "github.com/example/alpha" in patterns
    assert "github.com/example/beta" in patterns
    assert "alphasymbol" in patterns
    assert "betasymbol" in patterns
    assert "sharedword" not in patterns
    assert "github.com/example/both" not in patterns
    assert "testonlytoken" not in patterns
    assert all(rule.status == "suggested" for rule in rules)
    provenances = {rule.provenance for rule in rules}
    assert len(provenances) == 1
    assert next(iter(provenances)).startswith("mined:train:")


def test_rule_miner_rejects_single_session_support():
    session = _session(
        "train-1",
        {"summary": ("unique alpha signal",)},
        expected=("alpha",),
        split="train",
    )

    with pytest.raises(
        ValueError, match="min_support must be at least 2"
    ):
        suggest_rules([session], min_support=1)


def test_rule_miner_filters_prompt_boilerplate_stopwords():
    sessions = [
        _session(
            f"train-{index}",
            {"summary": ("You are the analyst for alphasymbol",)},
            expected=("alpha",),
            split="train",
        )
        for index in range(2)
    ]

    patterns = {rule.pattern for rule in suggest_rules(sessions).rules}

    assert "alphasymbol" in patterns
    assert not {"you", "are", "the", "for"}.intersection(patterns)


def test_open_set_negatives_count_against_mined_rule_precision():
    sessions = [
        _session(
            "positive-1",
            {"summary": ("alphasymbol sharedword",)},
            expected=("alpha",),
            split="train",
        ),
        _session(
            "positive-2",
            {"summary": ("alphasymbol sharedword",)},
            expected=("alpha",),
            split="train",
        ),
        _session(
            "unknown",
            {"summary": ("sharedword unrelated",)},
            expected=(),
            split="train",
        ),
    ]

    patterns = {
        rule.pattern
        for rule in suggest_rules(
            sessions,
            min_support=2,
            min_precision=0.9,
        ).rules
    }

    assert "alphasymbol" in patterns
    assert "sharedword" not in patterns


def test_profile_render_round_trips_rule_provenance(tmp_path: Path):
    profiles = ProfileSet(
        projects=(
            ProjectProfile(
                id="alpha",
                rules=(
                    ProjectRule(
                        id="alpha-rule",
                        project_id="alpha",
                        field="summary",
                        matcher="token",
                        pattern="符号",
                        status="suggested",
                        provenance="mined:train",
                        support=3,
                        precision=0.75,
                    ),
                ),
            ),
        )
    )
    path = tmp_path / "profiles.toml"
    path.write_text(render_profiles_toml(profiles), encoding="utf-8")
    assert load_profiles_toml(path) == profiles


def test_mined_rule_provenance_changes_with_training_snapshot():
    first = [
        _session(
            "train-1",
            {"repo": ("github.com/example/alpha",)},
            expected=("alpha",),
            split="train",
        ),
        _session(
            "train-2",
            {"repo": ("github.com/example/alpha",)},
            expected=("alpha",),
            split="train",
        ),
    ]
    changed = [
        _session(
            "train-1",
            {
                "repo": ("github.com/example/alpha",),
                "summary": ("new evidence",),
            },
            expected=("alpha",),
            split="train",
        ),
        _session(
            "train-2",
            {"repo": ("github.com/example/alpha",)},
            expected=("alpha",),
            split="train",
        ),
    ]
    first_profile = suggest_rules(first, min_support=2, min_precision=1)
    changed_profile = suggest_rules(changed, min_support=2, min_precision=1)

    assert first_profile.rules[0].provenance != changed_profile.rules[0].provenance


def test_unreviewed_candidates_never_train_or_enter_labelled_metrics():
    unreviewed = _session(
        "candidate",
        {"repo": ("github.com/example/alpha",)},
        expected=("alpha",),
        split="train",
        label_status="unreviewed",
        candidate_projects=("alpha",),
        case_tags=("clear_repo",),
    )
    assert suggest_rules([unreviewed], min_support=2).rules == ()

    profiles = _profiles(_rule("repo-alpha", "alpha"))
    test_candidate = _session(
        "candidate-test",
        {"repo": ("github.com/example/alpha",)},
        expected=("alpha",),
        label_status="unreviewed",
        candidate_projects=("alpha",),
        case_tags=("clear_repo",),
    )
    rows, summary = evaluate_profiles(profiles, [test_candidate])
    assert rows[0]["review_candidates"] == ["alpha"]
    assert rows[0]["case_tags"] == ["clear_repo"]
    assert summary["labelled_sessions"] == 0
    assert summary["precision"] is None
    assert summary["coverage"] is None


def test_owner_reviewed_empty_label_measures_unknown_false_acceptance():
    profiles = _profiles(_rule("repo-alpha", "alpha"))
    sessions = [
        _session(
            "forced-project",
            {"repo": ("github.com/example/alpha",)},
            expected=(),
        ),
        _session("safe-abstain", {"summary": ("unrelated",)}, expected=()),
    ]
    rows, summary = evaluate_profiles(profiles, sessions)

    forced = next(row for row in rows if row["session_id"] == "forced-project")
    safe = next(row for row in rows if row["session_id"] == "safe-abstain")
    assert forced["correct"] is False
    assert safe["correct"] is True
    assert safe["exact_match"] is True
    assert summary["unknown_sessions"] == 2
    assert summary["unknown_false_accepts"] == 1
    assert summary["unknown_false_accept_rate"] == 0.5
    assert summary["false_positives"] == 1


def test_partial_multi_project_prediction_is_not_counted_as_correct():
    profiles = _profiles(_rule("repo-alpha", "alpha"))
    sessions = [
        _session(
            "multi-project",
            {"repo": ("github.com/example/alpha",)},
            expected=("alpha", "beta"),
        )
    ]

    rows, summary = evaluate_profiles(profiles, sessions)

    assert rows[0]["predicted_projects"] == ["alpha"]
    assert rows[0]["correct"] is False
    assert rows[0]["exact_match"] is False
    assert summary["precision"] == 0.0
    assert summary["false_positives"] == 1


def test_evaluation_reports_precision_coverage_conflicts_and_abstention():
    profiles = _profiles(
        _rule("repo-alpha", "alpha"),
        _rule("repo-beta", "beta"),
    )
    sessions = [
        _session(
            "correct",
            {"repo": ("github.com/example/alpha",)},
            expected=("alpha",),
        ),
        _session(
            "false-positive",
            {"repo": ("github.com/example/beta",)},
            expected=("alpha",),
        ),
        _session(
            "conflict",
            {
                "repo": (
                    "github.com/example/alpha",
                    "github.com/example/beta",
                )
            },
            expected=("alpha",),
        ),
        _session("abstain", {"summary": ("unknown",)}, expected=("beta",)),
    ]
    rows, summary = evaluate_profiles(profiles, sessions)

    assert len(rows) == 4
    assert summary == {
        "kind": "summary",
        "split": "test",
        "policy": "accepted_only",
        "sessions": 4,
        "labelled_sessions": 4,
        "known_sessions": 4,
        "unknown_sessions": 0,
        "accepted": 2,
        "known_accepted": 2,
        "correct": 1,
        "exact_matches": 1,
        "false_positives": 1,
        "unknown_false_accepts": 0,
        "conflicts": 1,
        "abstentions": 1,
        "precision": 0.5,
        "exact_match_rate": 0.5,
        "coverage": 0.5,
        "known_coverage": 0.5,
        "unknown_false_accept_rate": None,
        "conflict_rate": 0.25,
        "abstention_rate": 0.25,
        "model_calls": 0,
        "model_tokens": 0,
    }
    false_positive = next(row for row in rows if row["session_id"] == "false-positive")
    assert false_positive["matches"][0]["rule_id"] == "repo-beta"
