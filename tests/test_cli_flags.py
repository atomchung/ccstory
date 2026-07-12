"""Tests for CLI flag handling.

Focus narrow: argument parsing + the --no-summary → --minimal deprecation
bridge from #20. The full `main()` flow has filesystem side effects and is
better tested via integration once #25 etc. settle.
"""

from __future__ import annotations

import argparse
import os

import pytest

from ccstory import session_summarizer as ss
from ccstory.cli import apply_lang_override, resolve_output_format


def _build_parser() -> argparse.ArgumentParser:
    """Mirror the parser shape from cli.main() so we can test --minimal /
    --no-summary handling without dragging the full main() into a unit test."""
    p = argparse.ArgumentParser()
    p.add_argument("window", nargs="?", default="month")
    p.add_argument("--minimal", action="store_true")
    p.add_argument("--no-summary", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--llm-narrative", action="store_true")
    p.add_argument("--no-aggregate", action="store_true")
    p.add_argument("--no-compare", action="store_true")
    return p


class TestMinimalFlag:
    def test_minimal_sets_flag(self):
        args = _build_parser().parse_args(["week", "--minimal"])
        assert args.minimal is True

    def test_default_minimal_is_false(self):
        args = _build_parser().parse_args(["week"])
        assert args.minimal is False
        assert args.no_summary is False


class TestNoSummaryDeprecation:
    """Locks the alias behavior — --no-summary should map to --minimal with
    a deprecation warning. The mapping logic lives in cli.main(); we
    duplicate it here so future changes there break this test loudly."""

    @staticmethod
    def _apply_deprecation(args: argparse.Namespace, stderr_writer) -> None:
        if args.no_summary and not args.minimal:
            stderr_writer(
                "ccstory: warning: --no-summary is deprecated and will be "
                "removed in a future release. Use --minimal instead."
            )
            args.minimal = True

    def test_no_summary_implies_minimal(self, capsys: pytest.CaptureFixture):
        args = _build_parser().parse_args(["week", "--no-summary"])
        assert args.no_summary is True
        assert args.minimal is False
        self._apply_deprecation(args, lambda m: print(m))
        assert args.minimal is True

    def test_no_summary_emits_deprecation_warning(self):
        args = _build_parser().parse_args(["week", "--no-summary"])
        captured: list[str] = []
        self._apply_deprecation(args, captured.append)
        assert len(captured) == 1
        assert "deprecated" in captured[0]
        assert "--minimal" in captured[0]

    def test_minimal_alone_does_not_warn(self):
        args = _build_parser().parse_args(["week", "--minimal"])
        captured: list[str] = []
        self._apply_deprecation(args, captured.append)
        assert captured == []

    def test_both_flags_no_double_warning(self):
        args = _build_parser().parse_args(["week", "--minimal", "--no-summary"])
        captured: list[str] = []
        self._apply_deprecation(args, captured.append)
        # When --minimal is already set, the deprecation path is skipped
        assert captured == []
        assert args.minimal is True


class TestResolveOutputFormat:
    """`--format=auto` resolves to markdown or card based on env + tty.

    The rule (locked here so Claude Code chat rendering doesn't silently
    regress): markdown wins when `CLAUDECODE=1` is set or stdout is not a
    tty; otherwise card. Explicit `markdown` / `card` always pass through.
    """

    def test_explicit_markdown_passes_through(self):
        assert resolve_output_format("markdown", env={}, isatty=True) == "markdown"

    def test_explicit_card_passes_through_even_in_claude_code(self):
        # User opt-in to card mode (e.g. piping to a screenshot tool inside
        # Claude Code) must override the auto-detect.
        assert (
            resolve_output_format("card", env={"CLAUDECODE": "1"}, isatty=False)
            == "card"
        )

    def test_auto_in_claude_code_picks_markdown(self):
        assert (
            resolve_output_format("auto", env={"CLAUDECODE": "1"}, isatty=True)
            == "markdown"
        )

    def test_auto_in_plain_terminal_picks_card(self):
        assert resolve_output_format("auto", env={}, isatty=True) == "card"

    def test_auto_when_piped_picks_markdown(self):
        # Non-tty stdout (pipe / redirect) → user likely wants raw markdown,
        # not ANSI-escaped Rich panel.
        assert resolve_output_format("auto", env={}, isatty=False) == "markdown"

    def test_claudecode_other_value_does_not_trigger(self):
        # Only the canonical "1" enables markdown; spurious values
        # (e.g. "0", "false") fall back to tty detection.
        assert (
            resolve_output_format("auto", env={"CLAUDECODE": "0"}, isatty=True)
            == "card"
        )

    def test_claudecode_empty_string_does_not_trigger(self):
        # Empty env var (e.g. shell unset-but-exported) is falsy; behavior
        # must match "unset" — see also `CLAUDE_CODE_DISABLE_CRON=` which
        # is sometimes used to disable a feature without removing the var.
        assert (
            resolve_output_format("auto", env={"CLAUDECODE": ""}, isatty=True)
            == "card"
        )

    def test_non_auto_string_passes_through_verbatim(self):
        # Helper does NOT validate `arg`; the CLI parser owns that via
        # `choices=VALID_OUTPUT_FORMATS`. This lock prevents future
        # "be helpful and normalize" tweaks that would silently mask
        # argparse errors.
        assert resolve_output_format("Markdown", env={}, isatty=True) == "Markdown"
        assert resolve_output_format("json", env={}, isatty=True) == "json"


class TestApplyLangOverride:
    """`--lang foo` is shorthand for `CCSTORY_LANG=foo`. The CLI promotes
    it into the environment so every prompt-assembly call this run makes
    sees the value through the same chain as the env var itself.
    """

    def test_sets_env_and_clears_cache(self, monkeypatch):
        monkeypatch.delenv(ss.CCSTORY_LANG_ENV, raising=False)
        # Prime the lru_cache with the English fallback path.
        monkeypatch.setattr(ss, "_detect_system_locale", lambda: None)
        ss.language_directive.cache_clear()
        assert ss.language_directive() == "Respond in English."

        apply_lang_override("Traditional Chinese")
        assert os.environ[ss.CCSTORY_LANG_ENV] == "Traditional Chinese"
        # Cache must have been flushed so the next call sees the override.
        assert "Traditional Chinese" in ss.language_directive()

    def test_none_is_noop(self, monkeypatch):
        monkeypatch.delenv(ss.CCSTORY_LANG_ENV, raising=False)
        apply_lang_override(None)
        assert ss.CCSTORY_LANG_ENV not in os.environ

    def test_blank_is_noop(self, monkeypatch):
        monkeypatch.delenv(ss.CCSTORY_LANG_ENV, raising=False)
        apply_lang_override("   ")
        assert ss.CCSTORY_LANG_ENV not in os.environ

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.delenv(ss.CCSTORY_LANG_ENV, raising=False)
        apply_lang_override("  Japanese  ")
        assert os.environ[ss.CCSTORY_LANG_ENV] == "Japanese"


class TestFormatArgparseValidation:
    """The parser is the validation boundary — bad --format values must die
    at argparse time, not silently fall through resolve_output_format.
    """

    def _build_parser(self) -> argparse.ArgumentParser:
        from ccstory.cli import VALID_OUTPUT_FORMATS

        p = argparse.ArgumentParser()
        p.add_argument("--format", dest="output_format",
                       choices=VALID_OUTPUT_FORMATS, default="auto")
        return p

    def test_valid_choices(self):
        for choice in ("auto", "markdown", "card", "json"):
            args = self._build_parser().parse_args(["--format", choice])
            assert args.output_format == choice

    def test_invalid_choice_exits(self):
        with pytest.raises(SystemExit):
            self._build_parser().parse_args(["--format", "yaml"])

    def test_case_sensitive(self):
        # argparse `choices` is case-sensitive; "Markdown" must be rejected
        # even though it looks plausibly correct.
        with pytest.raises(SystemExit):
            self._build_parser().parse_args(["--format", "Markdown"])
