"""Tests for #25 — session-level content classification + cache + hybrid."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from ccstory.categorizer import user_rule_match
from ccstory.session_summarizer import (
    _build_category_context,
    _classify_cache_get_many,
    _classify_cache_upsert_many,
    _DEFAULT_VOCAB_BLOCK,
    _parse_classification_lines,
    classify_sessions_by_content,
)


class TestUserRuleMatch:
    def test_no_config_returns_none(self, tmp_path: Path):
        nonexistent = tmp_path / "config.toml"
        assert user_rule_match("-Users-alice-code-myrepo", nonexistent) is None

    def test_matches_user_rule(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[categories]\n'
            '"work" = ["myrepo"]\n',
            encoding="utf-8",
        )
        assert user_rule_match("-Users-alice-code-myrepo", cfg) == "work"

    def test_unmatched_returns_none(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[categories]\n'
            '"work" = ["specific-repo-only"]\n',
            encoding="utf-8",
        )
        # Default rules would catch "myapp" as coding, but user_rule_match
        # only looks at user rules.
        assert user_rule_match("-Users-alice-code-myapp", cfg) is None

    def test_hyphenated_needle(self, tmp_path: Path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[categories]\n'
            '"client-work" = ["acme-inc"]\n',
            encoding="utf-8",
        )
        assert user_rule_match("-Users-alice-code-acme-inc", cfg) == "client-work"


class TestParseClassificationLines:
    def test_well_formed_lines(self):
        text = (
            '{"session_id": "s1", "bucket": "coding"}\n'
            '{"session_id": "s2", "bucket": "investment"}\n'
        )
        assert _parse_classification_lines(text) == {
            "s1": "coding",
            "s2": "investment",
        }

    def test_strips_code_fences(self):
        text = (
            "```json\n"
            '{"session_id": "s1", "bucket": "Coding"}\n'
            "```"
        )
        assert _parse_classification_lines(text) == {"s1": "coding"}

    def test_skips_malformed_lines(self):
        text = (
            '{"session_id": "s1", "bucket": "coding"}\n'
            'this is not json\n'
            '{"session_id": "s2", "bucket": "writing"}\n'
        )
        result = _parse_classification_lines(text)
        assert result == {"s1": "coding", "s2": "writing"}

    def test_skips_missing_keys(self):
        text = (
            '{"session_id": "s1"}\n'
            '{"bucket": "writing"}\n'
            '{"session_id": "s2", "bucket": "coding"}\n'
        )
        assert _parse_classification_lines(text) == {"s2": "coding"}

    def test_empty_input(self):
        assert _parse_classification_lines("") == {}


class TestCacheOps:
    def test_upsert_and_get(self, tmp_home: Path):
        _classify_cache_upsert_many({"a": "coding", "b": "writing"})
        result = _classify_cache_get_many(["a", "b", "missing"])
        assert result == {"a": "coding", "b": "writing"}

    def test_get_empty(self, tmp_home: Path):
        assert _classify_cache_get_many([]) == {}

    def test_category_config_change_invalidates_only_compatible_rows(
        self, tmp_home: Path,
    ):
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.write_text(
            '[categories]\n"content" = ["blog"]\n', encoding="utf-8",
        )
        _classify_cache_upsert_many({"s1": "content"})
        assert _classify_cache_get_many(["s1"]) == {"s1": "content"}

        cfg.write_text(
            '[categories]\n"work" = ["blog"]\n', encoding="utf-8",
        )
        assert _classify_cache_get_many(["s1"]) == {}


class TestClassifySessionsByContent:
    def test_empty_items_returns_empty(self, tmp_home: Path):
        assert classify_sessions_by_content([]) == {}

    def test_full_cache_hit_no_claude_call(self, tmp_home: Path):
        _classify_cache_upsert_many({"s1": "coding", "s2": "writing"})

        with patch("ccstory.session_summarizer.claude_bin_available",
                   side_effect=AssertionError("should not be called")):
            result = classify_sessions_by_content([
                ("s1", "myapp", "did stuff"),
                ("s2", "blog", "wrote post"),
            ])
        assert result == {"s1": "coding", "s2": "writing"}

    def test_partial_cache_hits_call_claude_for_rest(self, tmp_home: Path):
        _classify_cache_upsert_many({"s1": "coding"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"session_id": "s2", "bucket": "writing"}\n',
            stderr="",
        )
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   return_value=mock_proc):
            result = classify_sessions_by_content([
                ("s1", "myapp", "did stuff"),
                ("s2", "blog", "wrote post"),
            ])
        assert result == {"s1": "coding", "s2": "writing"}
        # Subsequent identical call hits cache fully (no subprocess)
        with patch("ccstory.session_summarizer.subprocess.run",
                   side_effect=AssertionError("should not run")):
            again = classify_sessions_by_content([
                ("s1", "myapp", "did stuff"),
                ("s2", "blog", "wrote post"),
            ])
        assert again == {"s1": "coding", "s2": "writing"}

    def test_claude_unavailable_returns_cache_only(self, tmp_home: Path):
        _classify_cache_upsert_many({"s1": "coding"})
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=False):
            result = classify_sessions_by_content([
                ("s1", "myapp", "did stuff"),
                ("s2", "blog", "wrote post"),
            ])
        assert result == {"s1": "coding"}  # s2 not classified

    def test_claude_failure_returns_cache_only(self, tmp_home: Path):
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="oops",
        )
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   return_value=failed):
            result = classify_sessions_by_content([
                ("s1", "myapp", "did stuff"),
            ])
        assert result == {}

    def test_force_refresh_ignores_cache(self, tmp_home: Path):
        _classify_cache_upsert_many({"s1": "investment"})
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"session_id": "s1", "bucket": "coding"}\n',
            stderr="",
        )
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   return_value=mock_proc):
            result = classify_sessions_by_content(
                [("s1", "x", "y")], force_refresh=True,
            )
        # Refreshed value, not the stale cached "investment"
        assert result == {"s1": "coding"}

    def test_drops_invented_session_ids(self, tmp_home: Path):
        # Claude hallucinates a session_id we didn't ask about — drop it
        mock_proc = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=(
                '{"session_id": "s1", "bucket": "coding"}\n'
                '{"session_id": "ghost", "bucket": "writing"}\n'
            ),
            stderr="",
        )
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   return_value=mock_proc):
            result = classify_sessions_by_content([("s1", "x", "y")])
        assert result == {"s1": "coding"}
        assert "ghost" not in result

    def test_more_than_batch_size_is_chunked(self, tmp_home: Path):
        # 200 pending sessions with batch_size=80 must produce 3 claude
        # invocations and classify every session — regression for the
        # pending[:80] silent truncation.
        items = [(f"s{i:03d}", "myapp", "did stuff") for i in range(200)]

        def mock_run(cmd, *, capture_output, text, timeout, check):
            # Reconstruct which session ids are in this chunk's prompt
            prompt = cmd[-1]
            sids = [
                line.split("]")[0].lstrip("[")
                for line in prompt.splitlines()
                if line.startswith("[s")
            ]
            stdout = "".join(
                f'{{"session_id": "{sid}", "bucket": "coding"}}\n' for sid in sids
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=stdout, stderr="",
            )

        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   side_effect=mock_run) as run_mock:
            result = classify_sessions_by_content(items, batch_size=80)

        assert run_mock.call_count == 3  # 80 + 80 + 40
        assert len(result) == 200
        assert all(b == "coding" for b in result.values())

    def test_new_bucket_is_carried_into_later_chunk_prompt(self, tmp_home: Path):
        items = [(f"s{i}", "platform", "maintained infra") for i in range(4)]
        prompts: list[str] = []

        def mock_run(cmd, *, capture_output, text, timeout, check):
            prompt = cmd[-1]
            prompts.append(prompt)
            sids = [
                line.split("]")[0].lstrip("[")
                for line in prompt.splitlines()
                if line.startswith("[s")
            ]
            stdout = "".join(
                f'{{"session_id": "{sid}", "bucket": "infrastructure"}}\n'
                for sid in sids
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=stdout, stderr="",
            )

        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   side_effect=mock_run):
            result = classify_sessions_by_content(items, batch_size=2)

        assert len(prompts) == 2
        assert "(none yet)" in prompts[0]
        assert "infrastructure" in prompts[1].split("Pick from", 1)[0]
        assert result == {f"s{i}": "infrastructure" for i in range(4)}

    def test_run_wide_vocab_cap_rejects_third_invented_bucket(
        self, tmp_home: Path,
    ):
        items = [(f"s{i}", "platform", "maintained systems") for i in range(6)]
        proposed = ["research", "ops", "devops"]
        calls = 0

        def mock_run(cmd, *, capture_output, text, timeout, check):
            nonlocal calls
            bucket = proposed[calls]
            calls += 1
            sids = [
                line.split("]")[0].lstrip("[")
                for line in cmd[-1].splitlines()
                if line.startswith("[s")
            ]
            stdout = "".join(
                f'{{"session_id": "{sid}", "bucket": "{bucket}"}}\n'
                for sid in sids
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=stdout, stderr="",
            )

        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   side_effect=mock_run):
            result = classify_sessions_by_content(items, batch_size=2)

        # Default config supplies four preferred buckets, leaving room for
        # two genuinely new names. The third never enters the vocabulary —
        # but its sessions are negative-cached at the fallback bucket (#120)
        # instead of silently re-burning a claude -p chunk every future run.
        assert result["s0"] == result["s1"] == "research"
        assert result["s2"] == result["s3"] == "ops"
        assert result["s4"] == result["s5"] == "coding"  # default fallback
        assert _classify_cache_get_many([f"s{i}" for i in range(6)]) == result

    def test_one_off_invented_bucket_never_enters_vocab_but_is_cached(
        self, tmp_home: Path,
    ):
        proposed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=(
                '{"session_id": "s1", "bucket": "one-off-label"}\n'
                '{"session_id": "s2", "bucket": "coding"}\n'
            ),
            stderr="",
        )
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   return_value=proposed):
            result = classify_sessions_by_content([
                ("s1", "x", "unique task"),
                ("s2", "y", "software task"),
            ])

        # The one-off name is rejected from the vocabulary, but the session
        # is negative-cached at the fallback bucket (#120) so it never
        # re-burns a chunk. "one-off-label" itself must not reach the cache.
        assert result == {"s1": "coding", "s2": "coding"}
        cached = _classify_cache_get_many(["s1", "s2"])
        assert cached == result
        assert "one-off-label" not in cached.values()

    def test_dropped_sessions_do_not_reburn_on_next_run(self, tmp_home: Path):
        """#120 regression: a validation-dropped session used to get no
        cache row at all, so every future run re-entered it into `pending`
        and re-burned a claude -p chunk — forever. Second run must make
        ZERO LLM calls."""
        calls = 0

        def mock_run(cmd, *, capture_output, text, timeout, check):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout='{"session_id": "s1", "bucket": "one-off-label"}\n',
                stderr="",
            )

        items = [("s1", "x", "unique task")]
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   side_effect=mock_run):
            first = classify_sessions_by_content(items)
            second = classify_sessions_by_content(items)

        assert calls == 1  # run 2 is served entirely from the cache
        assert first == second == {"s1": "coding"}

    def test_model_omissions_stay_uncached_and_retry(self, tmp_home: Path):
        """The negative cache covers only validation drops — a sid the
        model failed to answer for is transient and must retry next run."""
        calls = 0

        # Patch run_claude_p (the logical LLM call), not subprocess.run:
        # empty stdout at the subprocess layer would trip #99's broken-flag
        # retry and double-count.
        def fake_claude(prompt, timeout):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="",  # no rows
            )

        items = [("s1", "x", "some task")]
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.run_claude_p",
                   side_effect=fake_claude):
            classify_sessions_by_content(items)
            classify_sessions_by_content(items)

        assert calls == 2  # omission is retried, not frozen into the cache
        assert _classify_cache_get_many(["s1"]) == {}

    def test_on_chunk_complete_fires_per_chunk(self, tmp_home: Path):
        """Issue #75: progress callback lets callers update a console.status
        with `chunks_done/total_chunks` so 200-session windows don't look
        frozen at the same opaque spinner for 3 minutes."""
        items = [(f"s{i:03d}", "myapp", "did stuff") for i in range(200)]

        def mock_run(cmd, *, capture_output, text, timeout, check):
            prompt = cmd[-1]
            sids = [
                line.split("]")[0].lstrip("[")
                for line in prompt.splitlines()
                if line.startswith("[s")
            ]
            stdout = "".join(
                f'{{"session_id": "{sid}", "bucket": "coding"}}\n' for sid in sids
            )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=stdout, stderr="",
            )

        ticks: list[tuple[int, int]] = []

        def _tick(done: int, total: int) -> None:
            ticks.append((done, total))

        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   side_effect=mock_run):
            classify_sessions_by_content(
                items, batch_size=80, on_chunk_complete=_tick,
            )

        # 200 items / 80 batch = 3 chunks (80 + 80 + 40)
        assert ticks == [(1, 3), (2, 3), (3, 3)]

    def test_on_chunk_complete_fires_even_on_failed_chunk(self, tmp_home: Path):
        """Counter should advance whether the chunk succeeded or failed —
        otherwise a transient claude -p failure looks like a hang."""
        items = [(f"s{i:03d}", "myapp", "did stuff") for i in range(160)]
        results_seq = [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
            subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="".join(
                    f'{{"session_id": "s{i:03d}", "bucket": "coding"}}\n'
                    for i in range(80, 160)
                ),
                stderr="",
            ),
        ]
        ticks: list[tuple[int, int]] = []
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   side_effect=results_seq):
            classify_sessions_by_content(
                items, batch_size=80, on_chunk_complete=lambda d, t: ticks.append((d, t)),
            )
        assert ticks == [(1, 2), (2, 2)]

    def test_one_failed_chunk_does_not_kill_remaining(self, tmp_home: Path):
        items = [(f"s{i:03d}", "myapp", "did stuff") for i in range(160)]
        results_seq = [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
            subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="".join(
                    f'{{"session_id": "s{i:03d}", "bucket": "writing"}}\n'
                    for i in range(80, 160)
                ),
                stderr="",
            ),
        ]
        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   side_effect=results_seq):
            result = classify_sessions_by_content(items, batch_size=80)
        # First chunk failed → those sessions absent; second chunk succeeded.
        assert len(result) == 80
        assert all(sid.startswith("s") and int(sid[1:]) >= 80 for sid in result)


class TestCategoryContextInPrompt:
    """Issue #62: LLM classifier must see the user's [categories] vocabulary
    so it doesn't invent parallel bucket names (`writing` next to user's
    `content`, `ops` next to user's `infra`, etc.)."""

    def test_no_user_categories_uses_default_vocab(self, tmp_home: Path):
        # No config.toml written — _build_category_context falls back to the
        # 4-bucket default block (coding/investment/writing/other).
        ctx = _build_category_context()
        assert ctx == _DEFAULT_VOCAB_BLOCK
        assert "coding" in ctx
        assert "writing" in ctx
        assert "other" in ctx

    def test_empty_categories_table_uses_default_vocab(self, tmp_home: Path):
        # Config exists but [categories] is empty (e.g. `ccstory init`
        # template, Skip mode). Should still fall back to default vocab.
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.write_text("[categories]\n", encoding="utf-8")
        assert _build_category_context() == _DEFAULT_VOCAB_BLOCK

    def test_categories_with_only_empty_needles_uses_default_vocab(
        self, tmp_home: Path,
    ):
        # `[categories]` table is non-empty (truthy dict) but every bucket's
        # needle list is empty — can happen after `ccstory category unset`
        # removes the last keyword from a bucket if the bucket-drop path
        # ever regresses. Exercises the second fallback branch in
        # _build_category_context (lines == [] → _DEFAULT_VOCAB_BLOCK).
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.write_text(
            "[categories]\n"
            '"content" = []\n'
            '"work"    = []\n',
            encoding="utf-8",
        )
        assert _build_category_context() == _DEFAULT_VOCAB_BLOCK

    def test_user_categories_render_as_vocab_block(self, tmp_home: Path):
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.write_text(
            "[categories]\n"
            '"work"    = ["paperclip", "g2a"]\n'
            '"content" = ["xhs", "blog"]\n',
            encoding="utf-8",
        )
        ctx = _build_category_context()
        # Sorted bucket names + their needles render as a list
        assert "- content: project leaves xhs, blog" in ctx
        assert "- work: project leaves paperclip, g2a" in ctx
        # Default vocab should NOT leak in once user has explicit categories
        assert "coding: software projects" not in ctx

    def test_prompt_carries_user_vocab_into_claude_call(self, tmp_home: Path):
        """End-to-end: editing config.toml between runs changes the prompt
        the LLM sees on the next run (no process restart needed)."""
        cfg = tmp_home / ".ccstory" / "config.toml"
        cfg.write_text(
            "[categories]\n"
            '"content" = ["xhs"]\n',
            encoding="utf-8",
        )
        captured_prompts: list[str] = []

        def fake_run(cmd, *, capture_output, text, timeout, check):
            captured_prompts.append(cmd[-1])
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout='{"session_id": "s1", "bucket": "content"}\n',
                stderr="",
            )

        with patch("ccstory.session_summarizer.claude_bin_available",
                   return_value=True), \
             patch("ccstory.session_summarizer.subprocess.run",
                   side_effect=fake_run):
            classify_sessions_by_content([("s1", "newsletter-draft",
                                           "wrote newsletter")])

        assert len(captured_prompts) == 1
        assert "- content: project leaves xhs" in captured_prompts[0]
        # The old hard-coded inline vocab list is gone
        assert "like coding, investment, writing, research, ops, other" \
            not in captured_prompts[0]


class TestParseClassificationRejectsBlankBucket:
    def test_whitespace_only_bucket_dropped(self):
        # `"bucket": "   "` previously normalized to "" and got cached,
        # locking the session out of future reclassification.
        text = (
            '{"session_id": "s1", "bucket": "   "}\n'
            '{"session_id": "s2", "bucket": "coding"}\n'
        )
        assert _parse_classification_lines(text) == {"s2": "coding"}

    def test_empty_string_bucket_dropped(self):
        text = '{"session_id": "s1", "bucket": ""}\n'
        assert _parse_classification_lines(text) == {}

    def test_control_character_and_overlong_bucket_dropped(self):
        text = (
            '{"session_id": "s1", "bucket": "bad\\u0000name"}\n'
            f'{{"session_id": "s2", "bucket": "{"x" * 61}"}}\n'
        )
        assert _parse_classification_lines(text) == {}
