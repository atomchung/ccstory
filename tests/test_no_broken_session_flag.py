"""Regression guard for ccstory#52.

The `--no-session-persistence` flag is listed in `claude --help` on Claude
Code CLI 2.1.x but is silently broken at runtime: passing it to `claude -p`
returns exit 0 with empty stdout, so every `claude -p` callsite in ccstory
silently no-ops (init bucket suggestion, per-session narrative, overall
synthesis, comparison narrative, content classification).

Until upstream Claude Code ships a fix, ccstory must not pass this flag.
This test fails loudly if any new callsite reintroduces it.
"""

from __future__ import annotations

from pathlib import Path

import ccstory


BROKEN_FLAG = "--no-session-persistence"


def test_no_broken_session_flag_in_package() -> None:
    pkg_dir = Path(ccstory.__file__).parent
    offenders: list[str] = []
    for py in sorted(pkg_dir.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if BROKEN_FLAG in line:
                offenders.append(f"{py.relative_to(pkg_dir.parent)}:{lineno}: {line.strip()}")
    assert not offenders, (
        f"{BROKEN_FLAG!r} is silently broken in Claude Code CLI 2.1.x "
        "(empty stdout, exit 0). Do not pass it to `claude -p`. "
        "See https://github.com/atomchung/ccstory/issues/52. Offenders:\n  "
        + "\n  ".join(offenders)
    )
