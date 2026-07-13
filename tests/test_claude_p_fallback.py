"""Tests for #52 — `--no-session-persistence` retry fallback.

Some Claude Code CLI versions silently no-op with `--no-session-persistence`
(exit 0, empty stdout). `run_claude_p` is the single chokepoint all `claude
-p` callsites go through; it retries once without the flag on that exact
signature, and only that signature.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ccstory
from ccstory import session_summarizer as ss
from ccstory.session_summarizer import run_claude_p


class _FakeRun:
    """Stub subprocess.run; returns `stdout`/`returncode` for every call."""

    def __init__(self, stdout: str = "a real answer", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)

        class R:
            returncode = self.returncode
            stdout = self.stdout
            stderr = ""

        return R()


class _FlakyRun:
    """First call: exit 0 + empty stdout (the ccstory#52 signature).
    Second call: succeeds.
    """

    def __init__(self, second_stdout: str = "a real answer"):
        self.second_stdout = second_stdout
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)

        class R:
            returncode = 0
            stdout = "" if len(self.calls) == 1 else self.second_stdout
            stderr = ""

        return R()


class TestRunClaudeP:
    def test_succeeds_on_first_try(self, monkeypatch: pytest.MonkeyPatch):
        fake = _FakeRun(stdout="a real answer")
        monkeypatch.setattr(ss.subprocess, "run", fake)
        r = run_claude_p("prompt", timeout=10)
        assert r.stdout == "a real answer"
        assert len(fake.calls) == 1
        assert "--no-session-persistence" in fake.calls[0]

    def test_retries_without_flag_on_silent_empty_output(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        flaky = _FlakyRun(second_stdout="a real answer")
        monkeypatch.setattr(ss.subprocess, "run", flaky)
        r = run_claude_p("prompt", timeout=10)
        assert r.stdout == "a real answer"
        assert len(flaky.calls) == 2
        assert "--no-session-persistence" in flaky.calls[0]
        assert "--no-session-persistence" not in flaky.calls[1]

    def test_does_not_retry_on_real_failure(self, monkeypatch: pytest.MonkeyPatch):
        fail = _FakeRun(stdout="", returncode=1)
        monkeypatch.setattr(ss.subprocess, "run", fail)
        r = run_claude_p("prompt", timeout=10)
        assert r.returncode == 1
        # Non-zero exit isn't the silent-empty-output bug — retrying wastes
        # a call on a failure the retry can't fix.
        assert len(fail.calls) == 1

    def test_still_empty_after_retry_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        always_empty = _FakeRun(stdout="", returncode=0)
        monkeypatch.setattr(ss.subprocess, "run", always_empty)
        r = run_claude_p("prompt", timeout=10)
        assert r.stdout == ""
        # Bounded to one retry, not an infinite loop.
        assert len(always_empty.calls) == 2


def test_no_broken_session_flag_outside_helper() -> None:
    """No callsite should hardcode --no-session-persistence directly — it
    must go through run_claude_p() so the retry fallback always applies.
    """
    pkg_dir = Path(ccstory.__file__).parent
    offenders: list[str] = []
    for py in sorted(pkg_dir.rglob("*.py")):
        if py.name == "session_summarizer.py":
            continue  # home of run_claude_p itself
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if "--no-session-persistence" in line:
                offenders.append(f"{py.relative_to(pkg_dir.parent)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Found a raw '--no-session-persistence' outside run_claude_p(); "
        "route it through ccstory.session_summarizer.run_claude_p() instead "
        "so the ccstory#52 retry fallback applies. Offenders:\n  "
        + "\n  ".join(offenders)
    )
