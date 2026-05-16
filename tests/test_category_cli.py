"""Tests for `ccstory category set/unset/list` + cache invalidation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ccstory import categorizer, session_summarizer
from ccstory.categorizer import (
    add_category_keywords,
    list_user_categories,
    remove_category_keywords,
)
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
