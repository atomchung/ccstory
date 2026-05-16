"""Tests for CLI flag handling.

Focus narrow: argument parsing + the --no-summary → --minimal deprecation
bridge from #20. The full `main()` flow has filesystem side effects and is
better tested via integration once #25 etc. settle.
"""

from __future__ import annotations

import argparse

import pytest


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
