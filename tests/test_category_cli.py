"""Tests for `ccstory category set/unset/list` + cache invalidation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from rich.console import Console

from ccstory import categorizer, session_summarizer
from ccstory.categorizer import (
    add_category_keywords,
    list_user_categories,
    remove_category_keywords,
)
from ccstory.cli import _run_category
from ccstory.session_summarizer import (
    _connect,
    invalidate_comparison_narratives,
    invalidate_content_buckets,
    invalidate_period_aggregates,
)


class TestAddCategoryKeywords:
    def test_creates_config_when_missing(self, tmp_home: Path):
        assert not categorizer.CONFIG_PATH.exists()
        cats, moved = add_category_keywords("research", ["ai-project-research"])
        assert categorizer.CONFIG_PATH.exists()
        assert cats == {"research": ["ai-project-research"]}
        assert moved == []
        # Defaults preserved in the rendered file
        txt = categorizer.CONFIG_PATH.read_text()
        assert 'default_bucket = "coding"' in txt
        assert "monthly_quota_usd" in txt
        assert '"research" = ["ai-project-research"]' in txt

    def test_appends_to_existing_bucket(self, tmp_home: Path):
        add_category_keywords("writing", ["blog"])
        cats, moved = add_category_keywords("writing", ["newsletter", "essay"])
        assert cats["writing"] == ["blog", "newsletter", "essay"]
        assert moved == []

    def test_dedupes_keywords(self, tmp_home: Path):
        add_category_keywords("writing", ["blog"])
        cats, _ = add_category_keywords("writing", ["blog", "BLOG", "Blog"])
        # Case-folded + deduped
        assert cats["writing"] == ["blog"]

    def test_keyword_collision_moves_across_buckets(self, tmp_home: Path):
        add_category_keywords("writing", ["xhs"])
        cats, moved = add_category_keywords("research", ["xhs"])
        # `xhs` should be lifted out of `writing` into `research`
        assert "xhs" in cats["research"]
        assert "writing" not in cats  # bucket emptied → dropped
        assert moved == [("xhs", "writing")]

    def test_lowercases_keywords(self, tmp_home: Path):
        cats, _ = add_category_keywords("Research", ["AI-Project", "RESEARCH"])
        # bucket name preserves case, keywords are lowered for the matcher
        assert cats["Research"] == ["ai-project", "research"]

    def test_rejects_empty_bucket(self, tmp_home: Path):
        with pytest.raises(ValueError):
            add_category_keywords("   ", ["foo"])

    def test_rejects_empty_keywords(self, tmp_home: Path):
        with pytest.raises(ValueError):
            add_category_keywords("writing", ["   ", ""])


class TestRemoveCategoryKeywords:
    def test_removes_keyword(self, tmp_home: Path):
        add_category_keywords("writing", ["blog", "newsletter"])
        cats, missing = remove_category_keywords("writing", ["blog"])
        assert cats == {"writing": ["newsletter"]}
        assert missing == []

    def test_drops_empty_bucket(self, tmp_home: Path):
        add_category_keywords("writing", ["blog"])
        cats, _ = remove_category_keywords("writing", ["blog"])
        assert "writing" not in cats

    def test_reports_missing_keywords(self, tmp_home: Path):
        add_category_keywords("writing", ["blog"])
        cats, missing = remove_category_keywords("writing", ["blog", "ghost"])
        assert missing == ["ghost"]

    def test_remove_from_nonexistent_bucket_is_noop(self, tmp_home: Path):
        cats, missing = remove_category_keywords("ghost", ["x"])
        assert cats == {}
        assert missing == ["x"]


class TestListUserCategories:
    def test_empty_when_no_config(self, tmp_home: Path):
        assert list_user_categories() == {}

    def test_returns_current_state(self, tmp_home: Path):
        add_category_keywords("writing", ["blog"])
        add_category_keywords("research", ["ai-research"])
        assert list_user_categories() == {
            "writing": ["blog"],
            "research": ["ai-research"],
        }


class TestRoundTripWithLoadRules:
    def test_added_rule_takes_effect_via_classify(self, tmp_home: Path):
        # Round-trip: write rule via add_category_keywords, then verify
        # categorizer.load_rules() reads it back. Pass the config path
        # explicitly because load_rules binds its default arg at def-time.
        add_category_keywords("research", ["myresearch"])
        rules = categorizer.load_rules(categorizer.CONFIG_PATH)
        assert categorizer.classify(
            "-Users-alice-code-myresearch", rules=rules,
        ) == "research"

    def test_default_bucket_preserved_across_writes(self, tmp_home: Path):
        add_category_keywords("writing", ["blog"])
        add_category_keywords("writing", ["newsletter"])
        # load_settings also binds default arg at def-time; read raw text
        # to verify our renderer wrote the scalar back through.
        txt = categorizer.CONFIG_PATH.read_text()
        assert 'default_bucket = "coding"' in txt
        assert "monthly_quota_usd = 3500" in txt

    def test_language_preserved_across_writes(self, tmp_home: Path):
        # Hand-set `language = "..."` must survive subsequent
        # `category set/unset` rewrites — losing it would silently
        # demote the user's chosen narrative language back to English.
        categorizer.CONFIG_PATH.write_text(
            'default_bucket = "coding"\n'
            "monthly_quota_usd = 3500\n"
            'language = "Traditional Chinese"\n'
            "[categories]\n"
            '"writing" = ["blog"]\n',
            encoding="utf-8",
        )
        add_category_keywords("writing", ["newsletter"])
        txt = categorizer.CONFIG_PATH.read_text()
        assert 'language = "Traditional Chinese"' in txt


def _seed_caches(period_keys: list[str]) -> None:
    """Drop a row into each cache table so we can verify invalidation."""
    _connect().close()  # ensure schema
    conn = sqlite3.connect(str(session_summarizer.DB_PATH))
    try:
        for key in period_keys:
            conn.execute(
                """INSERT OR REPLACE INTO period_aggregates
                   (period_key, category, summary, session_ids, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (key, "coding", "stale aggregate", "s1,s2", 1.0),
            )
        conn.execute(
            """INSERT OR REPLACE INTO comparison_narratives
               (current_key, previous_key, signature, narrative, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("k_cur", "k_prev", "sig123", "stale comparison", 1.0),
        )
        conn.execute(
            """INSERT OR REPLACE INTO session_content_buckets
               (session_id, bucket, created_at) VALUES (?, ?, ?)""",
            ("sess-1", "coding", 1.0),
        )
        conn.execute(
            """INSERT OR REPLACE INTO session_content_buckets
               (session_id, bucket, created_at) VALUES (?, ?, ?)""",
            ("sess-2", "writing", 1.0),
        )
        conn.commit()
    finally:
        conn.close()


class TestInvalidatePeriodAggregates:
    def test_global_clear(self, tmp_home: Path):
        _seed_caches(["2026-W19", "2026-05"])
        deleted = invalidate_period_aggregates(None)
        assert deleted == 2
        conn = sqlite3.connect(str(session_summarizer.DB_PATH))
        try:
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM period_aggregates"
            ).fetchone()
            assert n == 0
        finally:
            conn.close()

    def test_scoped_clear(self, tmp_home: Path):
        _seed_caches(["2026-W19", "2026-05"])
        deleted = invalidate_period_aggregates("2026-W19")
        assert deleted == 1
        conn = sqlite3.connect(str(session_summarizer.DB_PATH))
        try:
            keys = {
                r[0]
                for r in conn.execute(
                    "SELECT period_key FROM period_aggregates"
                ).fetchall()
            }
            assert keys == {"2026-05"}
        finally:
            conn.close()


class TestInvalidateContentBuckets:
    def test_global_clear(self, tmp_home: Path):
        _seed_caches([])
        deleted = invalidate_content_buckets(None)
        assert deleted == 2

    def test_scoped_clear(self, tmp_home: Path):
        _seed_caches([])
        deleted = invalidate_content_buckets(["sess-1"])
        assert deleted == 1
        conn = sqlite3.connect(str(session_summarizer.DB_PATH))
        try:
            sids = {
                r[0]
                for r in conn.execute(
                    "SELECT session_id FROM session_content_buckets"
                ).fetchall()
            }
            assert sids == {"sess-2"}
        finally:
            conn.close()

    def test_empty_list_is_noop(self, tmp_home: Path):
        _seed_caches([])
        deleted = invalidate_content_buckets([])
        assert deleted == 0


class TestInvalidateComparisonNarratives:
    def test_clears_all(self, tmp_home: Path):
        _seed_caches([])
        deleted = invalidate_comparison_narratives()
        assert deleted == 1


class TestRunCategoryColorConsistency:
    """`_run_category`'s set/unset console output colors every bucket it
    prints via colors_for(), matching the `list` table instead of the old
    per-bucket color_for() (which could color the same bucket differently
    depending on which subcommand printed it).
    """

    def test_set_does_not_crash_when_moved_from_bucket_empties_out(
        self, tmp_home: Path,
    ):
        # add_category_keywords deletes a moved-from bucket from its
        # returned `categories` dict once it has no keywords left — the
        # console still needs to print a color for that now-gone bucket
        # name via `moved`, so colors_for()'s input must union it back in.
        add_category_keywords("old-bucket", ["only-keyword"])
        console = Console(record=True, width=80)
        rc = _run_category(["set", "new-bucket", "only-keyword"], console)
        assert rc == 0
        out = console.export_text()
        assert "old-bucket" not in categorizer.list_user_categories()
        assert "moved" in out
        assert "old-bucket" in out

    def test_unset_does_not_crash_when_bucket_empties_out(self, tmp_home: Path):
        # remove_category_keywords drops the bucket itself once its last
        # keyword is removed — args.bucket must still resolve to a color.
        add_category_keywords("solo-bucket", ["only-keyword"])
        console = Console(record=True, width=80)
        rc = _run_category(["unset", "solo-bucket", "only-keyword"], console)
        assert rc == 0
        out = console.export_text()
        assert "solo-bucket" not in categorizer.list_user_categories()
        assert "Removed" in out
        assert "solo-bucket" in out

    def test_same_bucket_same_color_across_list_and_set(self, tmp_home: Path):
        import re

        # Buckets picked to collide under the old per-bucket color_for()
        # hash (custom names, none matching a BUCKET_COLORS key).
        add_category_keywords("輸出", ["kw-a"])
        add_category_keywords("投資", ["kw-b"])
        add_category_keywords("學習", ["kw-c"])

        list_console = Console(record=True, width=80)
        _run_category(["list"], list_console)
        list_ansi = list_console.export_text(styles=True)

        set_console = Console(record=True, width=80)
        _run_category(["set", "輸出", "kw-d"], set_console)
        set_ansi = set_console.export_text(styles=True)

        # `list` bolds the whole Bucket column (table style) while `set`'s
        # confirmation line doesn't — compare the color digit, not weight.
        list_code = re.search(r"\x1b\[([\d;]+)m輸出", list_ansi).group(1).split(";")[-1]
        set_code = re.search(r"\x1b\[([\d;]+)m輸出", set_ansi).group(1).split(";")[-1]
        assert list_code == set_code
