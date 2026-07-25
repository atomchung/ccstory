"""A third provider should plug in without editing shared product surfaces."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from rich.console import Console

from ccstory import cli, recap
from ccstory.providers import (
    AgentProviderSpec,
    TranscriptResolver,
    agent_label,
    create_providers,
    list_providers,
    register_provider,
)
from ccstory.providers import _PROVIDER_SPECS
from ccstory.providers.base import BaseAgentProvider
from ccstory.report import (
    _agent_title,
    build_report_json,
    build_trend_json,
    render_report,
    render_terminal_card,
    render_trend_markdown,
)
from ccstory.session_summarizer import summarize_session
from ccstory.time_tracking import SessionStat
from ccstory.token_usage import collect_usage
from ccstory.trends import PeriodPoint


class _ThirdProvider(BaseAgentProvider):
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def agent_name(self) -> str:
        return "antigravity"

    def data_roots(self) -> tuple[Path, ...]:
        return (self.root,)

    def extract_excerpt(self, path: Path) -> tuple[str, str]:
        return "-Users-me-demo", "[USER 1]\nship the provider adapter"

    def collect_sessions(
        self,
        since: datetime,
        until: datetime | None = None,
        engaged_only: bool = True,
    ) -> list[SessionStat]:
        return []

    def parse_session(self, path: Path) -> SessionStat | None:
        return None

    def collect_usage(self, since: datetime, until: datetime, by_model: dict) -> int:
        return 0


@pytest.fixture
def third_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "antigravity" / "sessions"
    root.mkdir(parents=True)
    spec = AgentProviderSpec(
        "antigravity",
        "Google Antigravity",
        lambda: _ThirdProvider(root),
        usage_coverage="partial",
    )
    monkeypatch.setitem(_PROVIDER_SPECS, spec.name, spec)
    return root


def test_descriptor_drives_registry_preflight_usage_and_titles(third_provider):
    assert "antigravity" in list_providers()
    assert agent_label("antigravity") == "Google Antigravity"
    assert create_providers("antigravity")[0].agent_name == "antigravity"
    assert recap._agent_data_roots("antigravity") == [
        ("antigravity", third_provider)
    ]
    assert _agent_title("antigravity", "Recap") == "Google Antigravity Recap"

    now = datetime.now().astimezone()
    usage = collect_usage(
        now,
        now,
        agent="antigravity",
        active_agents={"antigravity"},
    )
    assert usage.assistant_turns == 0
    assert usage.incomplete_agents == ["antigravity"]
    assert usage.partial_agents == ["antigravity"]
    assert usage.unavailable_agents == []
    assert not usage.usage_complete

    markdown = render_report(
        "week", now, now, [], [], usage, {}, agent="antigravity"
    )
    assert "partial exact usage for antigravity" in markdown
    obsidian = render_report(
        "week",
        now,
        now,
        [],
        [],
        usage,
        {},
        flavor="obsidian",
        agent="antigravity",
    )
    assert "usage_complete: false" in obsidian
    assert "usage_partial_agents: [antigravity]" in obsidian
    console = Console(record=True, width=100)
    console.print(
        render_terminal_card(
            now, now, [], [], usage, agent="antigravity"
        )
    )
    terminal = console.export_text()
    assert "partial exact usage for" in terminal
    assert "antigravity." in terminal
    payload = build_report_json(
        "week", now, now, [], [], usage, {}, agent="antigravity"
    )
    assert payload["usage_coverage"] == {
        "complete": False,
        "incomplete_agents": ["antigravity"],
        "providers": {"antigravity": "partial"},
    }

    point = PeriodPoint(
        "2026-W30",
        now,
        now,
        [],
        0,
        0,
        0,
        provider_coverage=usage.provider_coverage,
    )
    assert "partial exact usage for antigravity" in render_trend_markdown(
        [point], "week", agent="antigravity"
    )
    assert build_trend_json(
        [point], "week", agent="antigravity"
    )["usage_coverage"]["complete"] is False


def test_provider_owned_excerpt_reaches_summary_cache(
    third_provider, tmp_path: Path
):
    transcript = third_provider / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    now = datetime.now().astimezone()
    session = SessionStat(
        project="-Users-me-demo",
        category="coding",
        session_id="antigravity-1",
        start=now,
        end=now,
        active_sec=0,
        msg_count=1,
        user_msg_count=1,
        agent="antigravity",
        path=transcript,
    )
    resolver = TranscriptResolver()

    result = summarize_session(
        session.session_id,
        transcript,
        provider=resolver.provider_for(session),
    )

    assert result is not None
    assert result.source == "fallback"
    assert "ship the provider adapter" in result.summary


def test_cli_help_lists_registered_provider(third_provider, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    assert "antigravity" in capsys.readouterr().out


def test_cli_preflight_accepts_third_provider_only(
    third_provider, tmp_home
):
    (tmp_home / ".claude" / "projects").rmdir()

    with pytest.raises(SystemExit, match="No engaged sessions"):
        cli.main(["week", "--agent", "antigravity", "--no-artifacts"])


@pytest.mark.parametrize(
    "name",
    ["", "all", " antigravity", "vendor/agent", "../agent", "Agent"],
)
def test_registration_rejects_reserved_or_ambiguous_names(name):
    with pytest.raises(ValueError):
        register_provider(
            AgentProviderSpec(name, "Invalid", lambda: _ThirdProvider(Path()))
        )


def test_factory_agent_name_must_match_descriptor(monkeypatch):
    spec = AgentProviderSpec(
        "wrong-id",
        "Wrong ID",
        lambda: _ThirdProvider(Path()),
    )
    monkeypatch.setitem(_PROVIDER_SPECS, spec.name, spec)

    with pytest.raises(ValueError, match="wrong-id.*antigravity"):
        create_providers("wrong-id")


def test_registration_rejects_invalid_usage_coverage():
    with pytest.raises(ValueError, match="invalid usage coverage"):
        register_provider(
            AgentProviderSpec(
                "invalid-coverage",
                "Invalid",
                lambda: _ThirdProvider(Path()),
                usage_coverage="estimated",  # type: ignore[arg-type]
            )
        )


def test_usage_coverage_defaults_fail_closed():
    spec = AgentProviderSpec(
        "new-agent",
        "New Agent",
        lambda: _ThirdProvider(Path()),
    )

    assert spec.usage_coverage == "unavailable"


def test_inactive_partial_provider_does_not_warn(third_provider):
    now = datetime.now().astimezone()

    usage = collect_usage(
        now,
        now,
        agent="all",
        active_agents=set(),
    )

    assert usage.provider_coverage == {}
    assert usage.usage_complete
