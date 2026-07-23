"""Integration tests for `ccstory.recap.build_recap()` (#110).

The one-call library entry point is the first pipeline-level seam that can
be exercised end-to-end under the fake home: these tests lock the result
shape, the JSON envelope contract, the error semantics (exceptions instead
of `sys.exit`), and the CLI shell that now delegates to it.

`claude -p` is fenced off via `claude_bin_available → False`, so per-session
summaries take the instant first/last-message fallback and every synthesis
step degrades to None — deterministic and offline.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ccstory import cli, recap
from ccstory import session_summarizer as ss
from ccstory.recap import RecapUnavailable, build_recap, parse_window
from tests.conftest import make_assistant_msg, make_user_msg


def _recent_ts(hours_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _seed_session(jsonl_factory, project: str = "-Users-me-proj",
                  sid: str = "sess-recap-1"):
    """One engaged session (2 real user messages) a couple hours ago."""
    records = [
        make_user_msg("Fix the login bug", _recent_ts(2.5)),
        make_assistant_msg("Looking at auth.py", _recent_ts(2.4), f"{sid}-m1"),
        make_user_msg("Also add a regression test", _recent_ts(2.3)),
        make_assistant_msg("Done — patched and tested.", _recent_ts(2.2),
                           f"{sid}-m2"),
    ]
    return jsonl_factory(project, sid, records)


def _seed_codex_session(codex_factory, sid: str = "codex-recap-1"):
    records = [
        {
            "timestamp": _recent_ts(2.6),
            "type": "session_meta",
            "payload": {"session_id": sid, "id": sid, "cwd": "/Users/me/proj"},
        },
        {
            "timestamp": _recent_ts(2.5),
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Fix the Codex bug"},
        },
        {
            "timestamp": _recent_ts(2.4),
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Working on it"},
        },
        {
            "timestamp": _recent_ts(2.3),
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Add a test too"},
        },
        {
            "timestamp": _recent_ts(2.2),
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Done"},
        },
    ]
    return codex_factory(sid, records)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.setattr(ss, "claude_bin_available", lambda: False)


class TestBuildRecap:
    def test_agent_field_does_not_shift_existing_positional_fields(self):
        fields = list(recap.RecapResult.__dataclass_fields__)
        assert fields[-3:] == ["report_path", "counts", "agent"]

    def test_end_to_end_result_shape(self, tmp_home, jsonl_factory):
        _seed_session(jsonl_factory)
        result = build_recap("week")
        assert len(result.sessions) == 1
        assert result.rollups and result.rollups[0].active_min > 0
        assert result.sessions[0].category  # resolver ran — never empty
        assert result.summaries            # instant fallback summary exists
        assert result.markdown
        # Report written into the (fake) default reports dir.
        assert result.report_path is not None
        assert result.report_path.parent == tmp_home / ".ccstory" / "reports"
        assert result.report_path.name == f"recap-{result.label}.md"
        assert result.report_path.read_text(encoding="utf-8") == result.markdown
        # No claude available → no narratives, but the pipeline still lands.
        assert result.overall_narrative is None
        assert result.category_narratives == {}

    def test_to_json_envelope(self, tmp_home, jsonl_factory):
        _seed_session(jsonl_factory)
        result = build_recap("week")
        payload = result.to_json()
        assert payload["schema_version"] == 1
        assert payload["kind"] == "recap"
        assert payload["window"]["label"] == result.label
        assert len(payload["sessions"]) == 1
        # The envelope carries the report location for downstream tooling.
        assert payload["report_path"] == str(result.report_path)

    def test_write_report_false_skips_file(self, tmp_home, jsonl_factory):
        _seed_session(jsonl_factory)
        result = build_recap("week", write_report=False)
        assert result.report_path is None
        assert "report_path" not in result.to_json()
        reports_dir = tmp_home / ".ccstory" / "reports"
        assert not reports_dir.exists() or not any(reports_dir.glob("*.md"))

    def test_reports_dir_override(self, tmp_home, jsonl_factory, tmp_path):
        _seed_session(jsonl_factory)
        custom = tmp_path / "custom-reports"
        result = build_recap("week", reports_dir=custom)
        assert result.report_path is not None
        assert result.report_path.parent == custom

    def test_filtered_report_paths_do_not_collide(
        self, tmp_home, jsonl_factory, codex_factory,
    ):
        _seed_session(jsonl_factory)
        _seed_codex_session(codex_factory)
        all_result = build_recap("week", agent="all", artifacts=False)
        claude_result = build_recap("week", agent="claude", artifacts=False)
        codex_result = build_recap("week", agent="codex", artifacts=False)

        assert all_result.report_path.name == f"recap-{all_result.label}.md"
        assert claude_result.report_path.name == (
            f"recap-{claude_result.label}-claude.md"
        )
        assert codex_result.report_path.name == (
            f"recap-{codex_result.label}-codex.md"
        )
        assert len({all_result.report_path, claude_result.report_path,
                    codex_result.report_path}) == 3
        assert all_result.to_json()["agent"] == "all"
        assert claude_result.to_json()["agent"] == "claude"
        assert codex_result.to_json()["agent"] == "codex"

    def test_no_sessions_raises_recap_unavailable(self, tmp_home):
        with pytest.raises(RecapUnavailable, match="No engaged sessions"):
            build_recap("week")

    def test_missing_projects_dir_raises(self, tmp_home, monkeypatch):
        monkeypatch.setattr(recap, "CLAUDE_PROJECTS", tmp_home / "nope")
        with pytest.raises(RecapUnavailable, match="No session data"):
            build_recap("week")

    def test_missing_data_message_names_only_the_selected_agent(
        self, tmp_home, monkeypatch
    ):
        """`--agent claude` must not blame a missing Codex install."""
        monkeypatch.setattr(recap, "CLAUDE_PROJECTS", tmp_home / "nope")
        with pytest.raises(RecapUnavailable) as excinfo:
            build_recap("week", agent="claude")
        assert "Codex" not in str(excinfo.value)

    def test_unknown_agent_raises_value_error(self, tmp_home):
        with pytest.raises(ValueError, match="Unsupported agent filter"):
            build_recap("week", agent="antigravity")

    def test_every_registered_provider_has_a_data_root(self):
        """Registering a provider without a root here would report "no session
        data ()" to a user whose transcripts are sitting right there."""
        from ccstory.providers import list_providers

        assert set(list_providers()) <= set(recap._DATA_ROOTS)

    def test_bad_window_raises_value_error(self, tmp_home):
        with pytest.raises(ValueError, match="unrecognized window"):
            build_recap("2026-13-99")

    def test_minimal_skips_narrative_pipeline(self, tmp_home, jsonl_factory):
        _seed_session(jsonl_factory)
        result = build_recap("week", minimal=True)
        assert result.summaries == {}
        assert result.overall_narrative is None
        assert result.counts == {}


class TestParseWindow:
    def test_bad_window_raises(self):
        with pytest.raises(ValueError, match="unrecognized window"):
            parse_window("junk")

    def test_relative_window_uses_range_label(self):
        since, until, label = parse_window("week")
        assert label == f"{since:%Y-%m-%d}_{until:%Y-%m-%d}"

    def test_past_month_keeps_symbolic_label(self):
        since, until, label = parse_window("2020-01")
        assert label == "2020-01"
        assert since.year == 2020 and until.month == 2


class TestCliShell:
    """cli.main() is a thin shell over build_recap — lock the seam."""

    def test_json_flow_end_to_end(self, tmp_home, jsonl_factory, capsys):
        _seed_session(jsonl_factory)
        rc = cli.main(["week", "--json", "--no-artifacts"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] == 1
        assert payload["kind"] == "recap"
        assert payload["report_path"].endswith(".md")
        assert len(payload["sessions"]) == 1

    def test_markdown_flow_writes_report(self, tmp_home, jsonl_factory, capsys):
        _seed_session(jsonl_factory)
        rc = cli.main(["week", "--format", "markdown", "--no-artifacts"])
        assert rc == 0
        out = capsys.readouterr().out
        reports = list((tmp_home / ".ccstory" / "reports").glob("recap-*.md"))
        assert len(reports) == 1
        assert reports[0].read_text(encoding="utf-8") == out

    def test_corrupt_cache_exits_one_with_message_not_death(
        self, tmp_home, jsonl_factory, capsys,
    ):
        # CacheUnavailable is a normal exception since #119; the CLI seam
        # must translate it back to the old contract: message on stderr,
        # exit code 1 — no traceback, no SystemExit.
        _seed_session(jsonl_factory)
        cache = tmp_home / ".ccstory" / "cache.db"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"definitely not a sqlite database" * 4)

        rc = cli.main(["week", "--no-artifacts"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "corrupted" in err
        assert f"rm {cache}" in err

    def test_unknown_window_exits_with_message(self, tmp_home):
        with pytest.raises(SystemExit) as exc:
            cli.main(["bogus-window"])
        assert "unrecognized window" in str(exc.value)

    def test_empty_window_exits_with_message(self, tmp_home):
        with pytest.raises(SystemExit) as exc:
            cli.main(["week"])
        assert "No engaged sessions" in str(exc.value)
