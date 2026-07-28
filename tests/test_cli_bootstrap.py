"""Architecture contracts for the lightweight process entry point (#177)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from ccstory import bootstrap
from ccstory.bootstrap import _is_top_level_information_request
from ccstory.provider_metadata import (
    BUNDLED_PROVIDER_DEFINITIONS,
    bundled_provider_names,
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
    "ccstory.providers",
}
blocked = sorted(
    name
    for name in sys.modules
    if name in forbidden_exact or name == "rich" or name.startswith("rich.")
)
print(
    "__CCSTORY_IMPORT_AUDIT__"
    + json.dumps(
        {
            "code": code,
            "blocked": blocked,
            "version_loaded": "ccstory.version" in sys.modules,
        }
    ),
    file=sys.stderr,
)
raise SystemExit(code)
"""

_LAZY_PROVIDER_AUDIT = r"""
import json
import sys

import ccstory.providers as providers

implementation_modules = {
    "ccstory.providers.claude",
    "ccstory.providers.codex",
    "ccstory.providers.antigravity",
}
before = sorted(implementation_modules.intersection(sys.modules))
metadata = [
    (spec.name, spec.label, spec.usage_coverage)
    for spec in providers.provider_specs()
]
after_metadata = sorted(implementation_modules.intersection(sys.modules))
provider = providers.create_providers("claude")[0]
after_factory = sorted(implementation_modules.intersection(sys.modules))
print(
    json.dumps(
        {
            "before": before,
            "metadata": metadata,
            "after_metadata": after_metadata,
            "agent": provider.agent_name,
            "after_factory": after_factory,
        }
    )
)
"""

_LAZY_COMPAT_EXPORT_AUDIT = r"""
import json
import sys

import ccstory.providers as providers

implementation_modules = {
    "ccstory.providers.claude",
    "ccstory.providers.codex",
    "ccstory.providers.antigravity",
}
before = sorted(implementation_modules.intersection(sys.modules))
listed = all(
    name in dir(providers)
    for name in (
        "ClaudeCodeProvider",
        "CodexProvider",
        "AntigravityProvider",
    )
)
after_dir = sorted(implementation_modules.intersection(sys.modules))

from ccstory.providers import ClaudeCodeProvider
from ccstory.providers.claude import ClaudeCodeProvider as DirectClaudeProvider

after_export = sorted(implementation_modules.intersection(sys.modules))
print(
    json.dumps(
        {
            "before": before,
            "listed": listed,
            "after_dir": after_dir,
            "same_class": ClaudeCodeProvider is DirectClaudeProvider,
            "after_export": after_export,
        }
    )
)
"""

_STAR_IMPORT_AUDIT = r"""
import json
import sys

import ccstory.providers as providers

implementation_modules = {
    "ccstory.providers.claude",
    "ccstory.providers.codex",
    "ccstory.providers.antigravity",
}
before = sorted(implementation_modules.intersection(sys.modules))

from ccstory.providers import *
from ccstory.providers.antigravity import (
    AntigravityProvider as DirectAntigravityProvider,
)
from ccstory.providers.claude import ClaudeCodeProvider as DirectClaudeProvider
from ccstory.providers.codex import CodexProvider as DirectCodexProvider

after = sorted(implementation_modules.intersection(sys.modules))
missing = sorted(name for name in providers.__all__ if name not in globals())
print(
    json.dumps(
        {
            "before": before,
            "after": after,
            "missing": missing,
            "claude_identity": ClaudeCodeProvider is DirectClaudeProvider,
            "codex_identity": CodexProvider is DirectCodexProvider,
            "antigravity_identity": (
                AntigravityProvider is DirectAntigravityProvider
            ),
            "all": providers.__all__,
        }
    )
)
"""

_SINGLE_SOURCE_AUDIT = r"""
import contextlib
import io
import json

import ccstory.provider_metadata as metadata

future = metadata.BundledProviderDefinition(
    name="future-agent",
    label="Future Agent",
    usage_coverage="partial",
    module=".provider_metadata",
    class_name="BundledProviderDefinition",
)
metadata.BUNDLED_PROVIDER_DEFINITIONS += (future,)

from ccstory.bootstrap import build_top_level_parser
import ccstory.providers as providers

stream = io.StringIO()
with contextlib.redirect_stdout(stream):
    try:
        build_top_level_parser(version="test").parse_args(["--help"])
    except SystemExit as exc:
        code = exc.code

print(
    json.dumps(
        {
            "code": code,
            "names": providers.list_providers(),
            "label": providers.agent_label("future-agent"),
            "coverage": providers.provider_specs("future-agent")[0].usage_coverage,
            "help_has_choice": "future-agent" in stream.getvalue(),
        }
    )
)
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


@pytest.mark.parametrize(
    ("argv", "version_loaded"),
    [
        (["--help"], False),
        (["week", "--help"], False),
        (["--version"], True),
    ],
)
def test_top_level_information_does_not_import_runtime_pipeline(
    argv, version_loaded,
):
    result = _run(["-c", _IMPORT_AUDIT, json.dumps(argv)])

    assert result.returncode == 0
    marker = "__CCSTORY_IMPORT_AUDIT__"
    audit_line = next(
        line for line in result.stderr.splitlines() if line.startswith(marker)
    )
    audit = json.loads(audit_line.removeprefix(marker))
    assert audit == {
        "code": 0,
        "blocked": [],
        "version_loaded": version_loaded,
    }


@pytest.mark.parametrize(
    ("argv", "expected_calls", "expected_prefix"),
    [
        (["--help"], 0, "usage: ccstory"),
        (["week", "--help"], 0, "usage: ccstory"),
        (["--help", "--version"], 0, "usage: ccstory"),
        (["--version"], 1, "ccstory 9.9-test"),
        (["--version", "--help"], 1, "ccstory 9.9-test"),
    ],
)
def test_version_metadata_resolves_only_when_version_is_first_action(
    argv, expected_calls, expected_prefix, monkeypatch, capsys,
):
    calls = 0

    def resolve() -> str:
        nonlocal calls
        calls += 1
        return "9.9-test"

    monkeypatch.setattr(bootstrap, "_resolve_version", resolve)
    with pytest.raises(SystemExit) as exc:
        bootstrap.main(argv)

    assert exc.value.code == 0
    assert calls == expected_calls
    assert capsys.readouterr().out.startswith(expected_prefix)


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


def test_bundled_metadata_matches_full_registry_order_labels_and_coverage():
    from ccstory.provider_metadata import UsageCoverage as MetadataUsageCoverage
    from ccstory.providers import UsageCoverage, agent_label, provider_specs
    from ccstory.providers.base import BaseAgentProvider

    assert UsageCoverage is MetadataUsageCoverage
    definitions = BUNDLED_PROVIDER_DEFINITIONS
    specs = provider_specs()
    assert bundled_provider_names() == tuple(spec.name for spec in specs)
    assert [
        (definition.name, definition.label, definition.usage_coverage)
        for definition in definitions
    ] == [
        (spec.name, spec.label, spec.usage_coverage)
        for spec in specs
    ]
    assert [agent_label(spec.name) for spec in specs] == [
        definition.label for definition in definitions
    ]
    for definition, spec in zip(definitions, specs, strict=True):
        provider = spec.factory()
        assert isinstance(provider, BaseAgentProvider)
        assert type(provider).__module__ == f"ccstory{definition.module}"
        assert type(provider).__name__ == definition.class_name


def test_provider_implementations_load_only_when_factory_is_called():
    result = _run(["-c", _LAZY_PROVIDER_AUDIT])

    assert result.returncode == 0, result.stderr
    audit = json.loads(result.stdout)
    assert audit["before"] == []
    assert audit["after_metadata"] == []
    assert audit["agent"] == "claude"
    assert audit["after_factory"] == ["ccstory.providers.claude"]
    assert audit["metadata"] == [
        ["claude", "Claude Code", "complete"],
        ["codex", "Codex", "complete"],
        ["antigravity", "Antigravity", "complete"],
    ]


def test_legacy_provider_class_exports_are_lazy_and_identity_compatible():
    result = _run(["-c", _LAZY_COMPAT_EXPORT_AUDIT])

    assert result.returncode == 0, result.stderr
    audit = json.loads(result.stdout)
    assert audit == {
        "before": [],
        "listed": True,
        "after_dir": [],
        "same_class": True,
        "after_export": ["ccstory.providers.claude"],
    }


def test_star_import_preserves_provider_api_and_explicitly_loads_classes():
    result = _run(["-c", _STAR_IMPORT_AUDIT])

    assert result.returncode == 0, result.stderr
    audit = json.loads(result.stdout)
    assert audit["before"] == []
    assert audit["after"] == [
        "ccstory.providers.antigravity",
        "ccstory.providers.claude",
        "ccstory.providers.codex",
    ]
    assert audit["missing"] == []
    assert audit["claude_identity"] is True
    assert audit["codex_identity"] is True
    assert audit["antigravity_identity"] is True
    assert audit["all"] == [
        "AgentProviderSpec",
        "ProviderSnapshot",
        "BaseAgentProvider",
        "ProviderRecord",
        "SnapshotMetrics",
        "UsageCoverage",
        "ClaudeCodeProvider",
        "CodexProvider",
        "AntigravityProvider",
        "TranscriptResolver",
        "register_provider",
        "provider_specs",
        "create_providers",
        "collect_provider_snapshot",
        "get_provider",
        "list_providers",
        "agent_label",
        "provider_data_roots",
        "collect_multi_agent_sessions",
    ]


def test_one_metadata_definition_drives_bootstrap_and_full_registry():
    result = _run(["-c", _SINGLE_SOURCE_AUDIT])

    assert result.returncode == 0, result.stderr
    audit = json.loads(result.stdout)
    assert audit["code"] == 0
    assert audit["names"][-1] == "future-agent"
    assert audit["label"] == "Future Agent"
    assert audit["coverage"] == "partial"
    assert audit["help_has_choice"] is True
