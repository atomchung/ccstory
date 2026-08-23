"""Tests for `categorizer.resolve_session_bucket` — the unified priority chain
introduced in PR-A to fix bug #61.

Priority chain (high → low):
  user_pin > user_rule > llm_cache > llm_fresh (caller-batched) > fallback
"""

from __future__ import annotations

from pathlib import Path

from ccstory.categorizer import (
    builtin_or_fallback,
    builtin_rule_match,
    resolve_session_bucket,
)


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

    def test_cache_still_wins_over_builtin_default(self, tmp_home: Path):
        # PROJ_INVESTMENT's leaf would match the built-in DEFAULT_RULES
        # "investment" keyword, but a cached content classification must
        # still take precedence — the built-in tier is a fallback that only
        # applies after content classification, never a shortcut around it.
        bucket, source = resolve_session_bucket(
            PROJ_INVESTMENT, cached_llm_bucket="research",
            mode="hybrid", fallback="other",
        )
        assert (bucket, source) == ("research", "llm_cache")

    def test_needs_llm_signalled_even_when_folder_matches_builtin(
        self, tmp_home: Path,
    ):
        # A cache miss must still signal needs_llm so the caller gets a
        # chance at fresh content classification, even though the folder
        # leaf would resolve via the built-in tier if asked directly (#214).
        bucket, source = resolve_session_bucket(
            PROJ_INVESTMENT, cached_llm_bucket=None,
            mode="hybrid", fallback="other",
        )
        assert bucket is None
        assert source == "needs_llm"


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

    def test_builtin_default_wins_before_scalar_fallback(self, tmp_home: Path):
        # No user rule and no cache (folder mode ignores cache regardless) —
        # the built-in DEFAULT_RULES folder match must resolve before the
        # scalar fallback. This is the #214 fix: folder mode's precedence is
        # user_rule > builtin_rule > scalar fallback, not user_rule > scalar
        # fallback.
        bucket, source = resolve_session_bucket(
            PROJ_INVESTMENT, cached_llm_bucket=None,
            mode="folder", fallback="other",
        )
        assert (bucket, source) == ("investment", "builtin_rule")

    def test_user_rule_still_beats_builtin_default(self, tmp_home: Path):
        _write_user_rule(tmp_home, "writing", "stock")
        bucket, source = resolve_session_bucket(
            PROJ_INVESTMENT, cached_llm_bucket=None,
            mode="folder", fallback="other",
        )
        assert (bucket, source) == ("writing", "user_rule")


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

    def test_builtin_default_never_applies_in_content_mode(self, tmp_home: Path):
        # Content mode's resolve_session_bucket never reaches the built-in
        # tier — a cache miss always signals needs_llm regardless of whether
        # the folder leaf would match a DEFAULT_RULES keyword. Content mode
        # stays content-only (#214): cached content > fresh content > scalar
        # fallback, with no built-in tier spliced in.
        bucket, source = resolve_session_bucket(
            PROJ_INVESTMENT, cached_llm_bucket=None,
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


class TestBuiltinRuleMatch:
    """`builtin_rule_match` — the DEFAULT_RULES-only matching tier (#214)."""

    def test_matches_default_keyword(self):
        assert builtin_rule_match(PROJ_INVESTMENT) == "investment"

    def test_no_default_keyword_returns_none(self):
        assert builtin_rule_match(PROJ_BRANDED) is None

    def test_ignores_user_config_entirely(self, tmp_home: Path):
        # Even when the user's config redefines "stock" under a different
        # bucket, builtin_rule_match only ever consults DEFAULT_RULES — it
        # has no config_path parameter and reads no config file.
        _write_user_rule(tmp_home, "custom", "stock")
        assert builtin_rule_match(PROJ_INVESTMENT) == "investment"


class TestBuiltinOrFallback:
    """`builtin_or_fallback` — the shared deterministic tail every mode's
    fallback path (folder mode inside `resolve_session_bucket`, and the
    hybrid-mode collapse in `recap._resolve_all_sessions` /
    `trends._resolve_sessions_from_cache`) funnels through (#214)."""

    def test_folder_and_hybrid_modes_apply_builtin_tier(self, tmp_home: Path):
        for mode in ("folder", "hybrid"):
            bucket, source = builtin_or_fallback(
                PROJ_INVESTMENT, mode=mode, fallback="other",
            )
            assert (bucket, source) == ("investment", "builtin_rule")

    def test_content_mode_never_applies_builtin_tier(self, tmp_home: Path):
        bucket, source = builtin_or_fallback(
            PROJ_INVESTMENT, mode="content", fallback="other",
        )
        assert (bucket, source) == ("other", "fallback")

    def test_unmatched_project_uses_scalar_fallback(self, tmp_home: Path):
        bucket, source = builtin_or_fallback(
            PROJ_BRANDED, mode="hybrid", fallback="other",
        )
        assert (bucket, source) == ("other", "fallback")

    def test_reads_default_bucket_from_config_when_no_explicit_fallback(
        self, tmp_home: Path,
    ):
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.write_text('default_bucket = "from_config"\n', encoding="utf-8")
        bucket, source = builtin_or_fallback(
            PROJ_BRANDED, mode="hybrid", fallback=None,
        )
        assert (bucket, source) == ("from_config", "fallback")
