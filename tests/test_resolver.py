"""Tests for `categorizer.resolve_session_bucket` — the unified priority chain
introduced in PR-A to fix bug #61.

Priority chain (high → low):
  user_pin > user_rule > llm_cache > llm_fresh (caller-batched) > fallback
"""

from __future__ import annotations

from pathlib import Path

from ccstory.categorizer import resolve_session_bucket


PROJ_BRANDED = "-Users-alice-Side-project-mybranded"  # no default keyword hit
PROJ_INVESTMENT = "-Users-alice-Side-project-stock"   # hits DEFAULT investment


def _write_user_rule(tmp_home: Path, bucket: str, needle: str) -> None:
    cfg = tmp_home / ".ccstory" / "config.toml"
    cfg.write_text(
        f'[categories]\n"{bucket}" = ["{needle}"]\n', encoding="utf-8",
    )


class TestHybridMode:
    def test_user_rule_wins_over_cache(self, tmp_home: Path):
        _write_user_rule(tmp_home, "writing", "mybranded")
        bucket, source = resolve_session_bucket(
            PROJ_BRANDED, cached_llm_bucket="coding",
            mode="hybrid", fallback="other",
        )
        assert (bucket, source) == ("writing", "user_rule")

    def test_cache_used_when_no_user_rule(self, tmp_home: Path):
        bucket, source = resolve_session_bucket(
            PROJ_BRANDED, cached_llm_bucket="research",
            mode="hybrid", fallback="other",
        )
        assert (bucket, source) == ("research", "llm_cache")

    def test_needs_llm_when_cache_miss(self, tmp_home: Path):
        bucket, source = resolve_session_bucket(
            PROJ_BRANDED, cached_llm_bucket=None,
            mode="hybrid", fallback="other",
        )
        # Signals caller to batch into one claude -p call
        assert bucket is None
        assert source == "needs_llm"

    def test_user_rule_beats_needs_llm(self, tmp_home: Path):
        _write_user_rule(tmp_home, "investment", "mybranded")
        bucket, source = resolve_session_bucket(
            PROJ_BRANDED, cached_llm_bucket=None,
            mode="hybrid", fallback="other",
        )
        # user_rule short-circuits before needs_llm signal
        assert (bucket, source) == ("investment", "user_rule")


class TestFolderMode:
    def test_user_rule_still_works(self, tmp_home: Path):
        _write_user_rule(tmp_home, "writing", "mybranded")
        bucket, source = resolve_session_bucket(
            PROJ_BRANDED, cached_llm_bucket="research",
            mode="folder", fallback="other",
        )
        assert (bucket, source) == ("writing", "user_rule")

    def test_cache_ignored_in_folder_mode(self, tmp_home: Path):
        # cache says research, no user rule → folder mode skips cache → fallback
        bucket, source = resolve_session_bucket(
            PROJ_BRANDED, cached_llm_bucket="research",
            mode="folder", fallback="other",
        )
        assert (bucket, source) == ("other", "fallback")

    def test_never_signals_needs_llm(self, tmp_home: Path):
        # Folder mode is deterministic — must never ask caller to fire LLM
        bucket, source = resolve_session_bucket(
            PROJ_BRANDED, cached_llm_bucket=None,
            mode="folder", fallback="other",
        )
        assert (bucket, source) == ("other", "fallback")


class TestContentMode:
    def test_user_rule_skipped(self, tmp_home: Path):
        # Even with a matching user rule, content mode goes straight to cache
        _write_user_rule(tmp_home, "writing", "mybranded")
        bucket, source = resolve_session_bucket(
            PROJ_BRANDED, cached_llm_bucket="research",
            mode="content", fallback="other",
        )
        assert (bucket, source) == ("research", "llm_cache")

    def test_cache_miss_signals_needs_llm(self, tmp_home: Path):
        bucket, source = resolve_session_bucket(
            PROJ_BRANDED, cached_llm_bucket=None,
            mode="content", fallback="other",
        )
        assert bucket is None
        assert source == "needs_llm"


class TestFallbackSource:
    def test_uses_explicit_fallback_arg(self, tmp_home: Path):
        bucket, source = resolve_session_bucket(
            PROJ_BRANDED, cached_llm_bucket="x",
            mode="folder", fallback="custom_bucket",
        )
        assert (bucket, source) == ("custom_bucket", "fallback")

    def test_reads_default_bucket_from_config(self, tmp_home: Path):
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.write_text(
            'default_bucket = "from_config"\n', encoding="utf-8",
        )
        bucket, source = resolve_session_bucket(
            PROJ_BRANDED, cached_llm_bucket="x",
            mode="folder",  # forces fallback path
        )
        assert (bucket, source) == ("from_config", "fallback")
