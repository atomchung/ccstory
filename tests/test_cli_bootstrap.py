"""Architecture contracts for the lightweight process entry point (#177)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from ccstory.bootstrap import (
    BUNDLED_PROVIDER_NAMES,
    _is_top_level_information_request,
)


REPO_ROOT = Path(__file__).resolve().parents[1]

_DIRECT_BOOTSTRAP = (
    "from ccstory.bootstrap import main; "
    "raise SystemExit(main())"
)
_DIRECT_FULL_CLI = (
    "from ccstory.cli import main; "
    "raise SystemExit(main())"
)
_IMPORT_AUDIT = r"""
import json
import sys

from ccstory.bootstrap import main

try:
    result = main(json.loads(sys.argv[1]))
except SystemExit as exc:
    code = exc.code
else:
    code = result

forbidden_exact = {
    "ccstory.cli",
    "ccstory.recap",
    "ccstory.report",
    "ccstory.session_summarizer",
    "ccstory.artifacts",
    "ccstory.token_usage",
    "ccstory.providers.claude",
    "ccstory.providers.codex",
    "ccstory.providers.antigravity",
}
blocked = sorted(
    name
    for name in sys.modules
    if name in forbidden_exact or name == "rich" or name.startswith("rich.")
)
print(
    "__CCSTORY_IMPORT_AUDIT__"
    + json.dumps({"code": code, "blocked": blocked}),
    file=sys.stderr,
)
raise SystemExit(code)
"""


def _run(
    command: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, *command],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("argv", [["--help"], ["week", "--help"]])
def test_module_and_console_help_match_canonical_full_parser(argv):
    module = _run(["-m", "ccstory", *argv])
    console = _run(["-c", _DIRECT_BOOTSTRAP, *argv])
    canonical = _run(["-c", _DIRECT_FULL_CLI, *argv])

    assert module.returncode == console.returncode == canonical.returncode == 0
    assert module.stdout == console.stdout == canonical.stdout
    assert module.stderr == console.stderr == canonical.stderr == ""
    assert "usage: ccstory" in module.stdout


def test_module_and_console_version_match_canonical_full_parser():
    argv = ["--version"]
    module = _run(["-m", "ccstory", *argv])
    console = _run(["-c", _DIRECT_BOOTSTRAP, *argv])
    canonical = _run(["-c", _DIRECT_FULL_CLI, *argv])

    assert module.returncode == console.returncode == canonical.returncode == 0
    assert module.stdout == console.stdout == canonical.stdout
    assert module.stderr == console.stderr == canonical.stderr == ""
    assert module.stdout.startswith("ccstory ")


@pytest.mark.parametrize("argv", [["--help"], ["week", "--help"], ["--version"]])
def test_top_level_information_does_not_import_runtime_pipeline(argv):
    result = _run(["-c", _IMPORT_AUDIT, json.dumps(argv)])

    assert result.returncode == 0
    marker = "__CCSTORY_IMPORT_AUDIT__"
    audit_line = next(
        line for line in result.stderr.splitlines() if line.startswith(marker)
    )
    audit = json.loads(audit_line.removeprefix(marker))
    assert audit == {"code": 0, "blocked": []}


def test_help_is_identical_in_claudecode_non_tty_environment():
    plain = _run(["-m", "ccstory", "--help"])
    claude_code = _run(
        ["-m", "ccstory", "--help"],
        extra_env={"CLAUDECODE": "1"},
    )

    assert plain.returncode == claude_code.returncode == 0
    assert plain.stdout == claude_code.stdout
    assert plain.stderr == claude_code.stderr == ""


def test_unknown_option_keeps_full_cli_argparse_contract():
    argv = ["--definitely-not-a-ccstory-option"]
    module = _run(["-m", "ccstory", *argv])
    canonical = _run(["-c", _DIRECT_FULL_CLI, *argv])

    assert module.returncode == canonical.returncode == 2
    assert module.stdout == canonical.stdout == ""
    assert module.stderr == canonical.stderr
    assert "unrecognized arguments" in module.stderr


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--help"], True),
        (["week", "--help"], True),
        (["trend", "--help"], False),
        (["mcp", "--version"], False),
        (["--", "--help"], False),
        (["week", "--", "--help"], False),
        ([], False),
    ],
)
def test_information_request_routing_respects_subcommands_and_delimiter(
    argv, expected,
):
    assert _is_top_level_information_request(argv) is expected


def test_packaged_console_script_targets_bootstrap():
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert project["scripts"]["ccstory"] == "ccstory.bootstrap:main"


def test_lightweight_provider_names_match_canonical_bundled_registry():
    # The bootstrap deliberately avoids importing the eager provider registry
    # in a fresh process.  Keep its display-only names locked to that canonical
    # registry so a new bundled source cannot silently disappear from --help.
    from ccstory.providers import list_providers

    assert BUNDLED_PROVIDER_NAMES == tuple(list_providers())
