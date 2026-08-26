"""Tests for the two-layer resolver upgrade (#69).

Covers the additions layered on top of the unified resolver (test_resolver.py):
  - exact-membership tier that wins over an *earlier* area's fuzzy match
    (the ordering-hack fix), while staying byte-identical for token-needle
    configs that have no such shadowing;
  - the optional ``[projects]`` alias table + ``alias_fold`` / ``project_identity``;
  - the duplicate-membership load-time detection (first wins).
  - `classify()`, the declared integration API, resolving a config the same
    way the resolver does (#262).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ccstory.categorizer import (
    CategoryRule,
    alias_fold,
    classify,
    duplicate_memberships,
    load_project_aliases,
    load_rules,
    project_identity,
    resolve_session_bucket,
    user_rule_match,
)


def _cfg(tmp_home: Path, body: str) -> Path:
    p = tmp_home / ".ccstory" / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


class TestExactMembershipTier:
    def test_exact_membership_beats_earlier_fuzzy(self, tmp_home: Path):
        # "coding" is listed first with the fuzzy token "mcp"; "investment"
        # lists the project's exact leaf. Old first-match-wins token matching
        # would pick coding — exact membership must now pick investment.
        p = _cfg(
            tmp_home,
            '[categories]\n'
            '"coding" = ["mcp"]\n'
            '"investment" = ["stock-mcp"]\n',
        )
        bucket, source = resolve_session_bucket(
            "-Users-a-code-stock-mcp", None,
            mode="folder", fallback="other", config_path=p,
        )
        assert (bucket, source) == ("investment", "user_rule")

    def test_exact_single_token_reports_user_rule(self, tmp_home: Path):
        p = _cfg(tmp_home, '[categories]\n"investment" = ["stock"]\n')
        bucket, source = resolve_session_bucket(
            "-Users-a-code-stock", None,
            mode="folder", fallback="other", config_path=p,
        )
        assert (bucket, source) == ("investment", "user_rule")

    def test_token_needle_compat_unchanged(self, tmp_home: Path):
        # "stock-dashboard" != "stock" so no exact hit; tier-2 token match on
        # "stock" still lands it in investment — existing configs unaffected.
        p = _cfg(tmp_home, '[categories]\n"investment" = ["stock"]\n')
        bucket, source = resolve_session_bucket(
            "-Users-a-code-stock-dashboard", None,
            mode="folder", fallback="other", config_path=p,
        )
        assert (bucket, source) == ("investment", "user_rule")

    def test_no_match_falls_through(self, tmp_home: Path):
        p = _cfg(tmp_home, '[categories]\n"investment" = ["stock"]\n')
        assert user_rule_match("-Users-a-code-unrelated-thing", p) is None

    def test_multi_token_exact_membership(self, tmp_home: Path):
        # A hyphenated needle equal to the whole leaf is an exact member too.
        p = _cfg(
            tmp_home,
            '[categories]\n'
            '"coding" = ["kernel"]\n'
            '"output" = ["fomo-kernel"]\n',
        )
        assert user_rule_match("-Users-a-code-fomo-kernel", p) == "output"


class TestAliasFold:
    def test_alias_fold_identity_when_empty(self):
        assert alias_fold("stock", {}) == "stock"
        assert alias_fold("stock", None) == "stock"

    def test_alias_fold_maps_variant(self):
        assert alias_fold("ic", {"ic": "info-collector"}) == "info-collector"

    def test_load_project_aliases_lowercases(self, tmp_home: Path):
        p = _cfg(
            tmp_home,
            '[projects]\n'
            '"infocollector" = "info-collector"\n'
            '"IC-Tool" = "ictool"\n',
        )
        assert load_project_aliases(p) == {
            "infocollector": "info-collector",
            "ic-tool": "ictool",
        }

    def test_load_project_aliases_absent(self, tmp_home: Path):
        p = _cfg(tmp_home, '[categories]\n"coding" = ["app"]\n')
        assert load_project_aliases(p) == {}

    def test_project_identity_folds_and_strips_worktree(self, tmp_home: Path):
        p = _cfg(tmp_home, '[projects]\n"infocollector" = "info-collector"\n')
        assert project_identity("-Users-a-code-infocollector", config_path=p) == (
            "info-collector"
        )
        # Worktree suffix is stripped before folding.
        wt = "-Users-a-code-infocollector--claude-worktrees-zesty-yang-9f"
        assert project_identity(wt, config_path=p) == "info-collector"

    def test_alias_folded_leaf_matches_membership(self, tmp_home: Path):
        # Folder leaf "infocollector" folds to canonical "info-collector",
        # which is an exact member of "learning".
        p = _cfg(
            tmp_home,
            '[projects]\n"infocollector" = "info-collector"\n'
            '[categories]\n"learning" = ["info-collector"]\n',
        )
        bucket, source = resolve_session_bucket(
            "-Users-a-code-infocollector", None,
            mode="folder", fallback="other", config_path=p,
        )
        assert (bucket, source) == ("learning", "user_rule")


class TestDuplicateMemberships:
    def test_detects_project_under_two_areas(self, tmp_home: Path):
        p = _cfg(
            tmp_home,
            '[categories]\n'
            '"investment" = ["stock"]\n'
            '"learning" = ["stock"]\n',
        )
        assert duplicate_memberships(p) == [("stock", ["investment", "learning"])]

    def test_resolver_keeps_first_area(self, tmp_home: Path):
        p = _cfg(
            tmp_home,
            '[categories]\n'
            '"investment" = ["stock"]\n'
            '"learning" = ["stock"]\n',
        )
        bucket, source = resolve_session_bucket(
            "-Users-x-code-stock", None,
            mode="folder", fallback="other", config_path=p,
        )
        assert (bucket, source) == ("investment", "user_rule")

    def test_no_duplicates_returns_empty(self, tmp_home: Path):
        p = _cfg(
            tmp_home,
            '[categories]\n'
            '"investment" = ["stock"]\n'
            '"coding" = ["app"]\n',
        )
        assert duplicate_memberships(p) == []

    def test_absent_categories_returns_empty(self, tmp_home: Path):
        p = _cfg(tmp_home, 'default_bucket = "other"\n')
        assert duplicate_memberships(p) == []


class TestAliasPreservation:
    def test_category_set_preserves_projects_table(self, tmp_home: Path):
        # `category set` re-renders config from scratch — it must not drop the
        # user's [projects] aliases.
        from ccstory.categorizer import add_category_keywords

        p = _cfg(
            tmp_home,
            '[projects]\n"infocollector" = "info-collector"\n'
            '[categories]\n"learning" = ["info-collector"]\n',
        )
        add_category_keywords("coding", ["myapp"], path=p)
        assert load_project_aliases(p) == {"infocollector": "info-collector"}
        assert "[projects]" in p.read_text(encoding="utf-8")

    def test_category_unset_preserves_projects_table(self, tmp_home: Path):
        from ccstory.categorizer import remove_category_keywords

        p = _cfg(
            tmp_home,
            '[projects]\n"infocollector" = "info-collector"\n'
            '[categories]\n"learning" = ["info-collector", "app"]\n',
        )
        remove_category_keywords("learning", ["app"], path=p)
        assert load_project_aliases(p) == {"infocollector": "info-collector"}


class TestClassifyAgreesWithResolver:
    """`classify()` is the entry point external consumers are told to call.

    It ran the pre-#69 single-tier matcher until #262, so a project pinned
    with `ccstory category set` bound for the CLI and silently did not bind
    for a library consumer reading the same config.
    """

    NESTED = "-Users-a-Side-project-kol-collector-fomo-kernel"
    PARENT = "-Users-a-Side-project-kol-collector"
    PINNED_CONFIG = (
        'default_bucket = "other"\n'
        '[categories]\n'
        '"investing" = ["kol-collector"]\n'
        '"building" = ["kol-collector-fomo-kernel"]\n'
    )

    def test_pin_beats_an_earlier_buckets_fuzzy_keyword(self, tmp_home: Path):
        p = _cfg(tmp_home, self.PINNED_CONFIG)
        assert classify(self.NESTED, fallback="other", config_path=p) == "building"

    def test_parent_folder_keeps_its_own_keyword(self, tmp_home: Path):
        p = _cfg(tmp_home, self.PINNED_CONFIG)
        assert classify(self.PARENT, fallback="other", config_path=p) == "investing"

    def test_worktree_suffix_still_resolves_to_the_pin(self, tmp_home: Path):
        p = _cfg(tmp_home, self.PINNED_CONFIG)
        worktree = self.NESTED + "--claude-worktrees-bold-khorana-138046"
        assert classify(worktree, fallback="other", config_path=p) == "building"

    def test_matches_the_resolver_for_every_tier(self, tmp_home: Path):
        p = _cfg(tmp_home, self.PINNED_CONFIG)
        for project in (self.NESTED, self.PARENT, "-Users-a-Side-project-unruled"):
            resolved, _source = resolve_session_bucket(
                project, None, mode="folder", fallback="other", config_path=p,
            )
            assert classify(
                project,
                rules=load_rules(p),
                fallback="other",
                config_path=p,
            ) == resolved, project

    def test_folds_project_aliases_before_matching(self, tmp_home: Path):
        p = _cfg(
            tmp_home,
            'default_bucket = "other"\n'
            '[projects]\n'
            '"cc-story" = "ccstory"\n'
            '[categories]\n'
            '"building" = ["ccstory"]\n',
        )
        assert classify(
            "-Users-a-Side-project-cc-story", fallback="other", config_path=p,
        ) == "building"

    def test_token_needle_config_is_unchanged(self, tmp_home: Path):
        # No leaf is listed verbatim, so tier 1 never fires and every
        # pre-#262 config keeps its exact behavior.
        p = _cfg(tmp_home, '[categories]\n"investing" = ["stock"]\n')
        assert classify(
            "-Users-a-code-stock-dashboard", fallback="other", config_path=p,
        ) == "investing"

    def test_reads_the_module_config_path_when_none_is_passed(self, tmp_home: Path):
        _cfg(tmp_home, self.PINNED_CONFIG)
        assert classify(self.NESTED, fallback="other") == "building"

    def test_user_keyword_outranks_a_built_in_exact_member(self, tmp_home: Path):
        # `load_rules()` appends DEFAULT_RULES, whose "test-bed" needle is
        # hyphenated and therefore an exact member of this leaf. Membership
        # must stay a user-rule tier: the user's fuzzy "test" keyword wins,
        # the same way builtin_rule_match() only runs after user_rule_match().
        p = _cfg(tmp_home, '[categories]\n"custom" = ["test"]\n')
        assert classify("-Users-a-code-test-bed", config_path=p) == "custom"

    def test_aliases_do_not_leak_into_the_built_in_tier(self, tmp_home: Path):
        # The alias canonicalizes into the user's vocabulary only. The leaf
        # itself is still "alias", which matches no built-in needle, so both
        # entry points fall through instead of resolving the canonical name's
        # built-in keyword.
        p = _cfg(
            tmp_home,
            '[projects]\n"alias" = "stock"\n'
            '[categories]\n"custom" = ["nothing"]\n',
        )
        assert classify("-Users-a-code-alias", config_path=p) == "coding"

    def test_hand_built_rules_keep_pre_membership_behavior(self, tmp_home: Path):
        # A caller that constructs CategoryRule itself gets no membership
        # tier unless it opts in, so no existing consumer changes silently.
        rules = [
            CategoryRule(name="first", needles=["test"]),
            CategoryRule(name="second", needles=["test-bed"]),
        ]
        assert classify("-Users-a-code-test-bed", rules=rules) == "first"
        opted_in = [
            CategoryRule(name="first", needles=["test"], user_defined=True),
            CategoryRule(name="second", needles=["test-bed"], user_defined=True),
        ]
        assert classify("-Users-a-code-test-bed", rules=opted_in) == "second"


@pytest.mark.parametrize(
    "config,project",
    [
        # exact pin under a later bucket vs an earlier bucket's keyword
        ('[categories]\n"investing" = ["kol-collector"]\n'
         '"building" = ["kol-collector-fomo-kernel"]\n',
         "-Users-a-Side-project-kol-collector-fomo-kernel"),
        # parent folder, no pin
        ('[categories]\n"investing" = ["kol-collector"]\n'
         '"building" = ["kol-collector-fomo-kernel"]\n',
         "-Users-a-Side-project-kol-collector"),
        # user keyword vs a hyphenated built-in needle
        ('[categories]\n"custom" = ["test"]\n', "-Users-a-code-test-bed"),
        # alias folding, canonical name carries a built-in needle
        ('[projects]\n"alias" = "stock"\n[categories]\n"custom" = ["x"]\n',
         "-Users-a-code-alias"),
        # alias folding onto a real user membership
        ('[projects]\n"cc-story" = "ccstory"\n'
         '[categories]\n"building" = ["ccstory"]\n',
         "-Users-a-Side-project-cc-story"),
        # nothing matches anywhere
        ('[categories]\n"custom" = ["x"]\n', "-Users-a-code-unruled-thing"),
        # built-in tier only
        ('[categories]\n"custom" = ["x"]\n', "-Users-a-code-playground"),
    ],
)
def test_classify_matches_the_folder_resolver(config, project, tmp_home: Path):
    """One config, two documented entry points, one answer.

    The regression this pins is not any single case but the drift itself:
    classify() sat on an older matcher for two releases because no internal
    caller exercised it (#262).
    """
    p = _cfg(tmp_home, config)
    resolved, _source = resolve_session_bucket(
        project, None, mode="folder", config_path=p,
    )
    assert classify(project, rules=load_rules(p), config_path=p) == resolved
    assert classify(project, config_path=p) == resolved
