"""Tests for the two-layer resolver upgrade (#69).

Covers the additions layered on top of the unified resolver (test_resolver.py):
  - exact-membership tier that wins over an *earlier* area's fuzzy match
    (the ordering-hack fix), while staying byte-identical for token-needle
    configs that have no such shadowing;
  - the optional ``[projects]`` alias table + ``alias_fold`` / ``project_identity``;
  - the duplicate-membership load-time detection (first wins).
"""

from __future__ import annotations

from pathlib import Path

from ccstory.categorizer import (
    alias_fold,
    duplicate_memberships,
    load_project_aliases,
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
