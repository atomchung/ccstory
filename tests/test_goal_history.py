"""Read-only GoalActivitySeries JSON consumer contracts (#225 PR B)."""

from __future__ import annotations

import json
import math
import os
import time as system_time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import ccstory.cli as cli_module
import ccstory.goal_history as history_module
from ccstory.goal_history import (
    DEFAULT_GOAL_ACTIVITY_WEEKS,
    MAX_GOAL_ACTIVITY_WEEKS,
    collect_goal_activity_history,
    completed_local_week_windows,
    validate_goal_activity_weeks,
)
from ccstory.goals import GoalContextError, parse_goal_context
from ccstory.time_tracking import SessionStat, session_slice_for_window


UTC = timezone.utc
LOCAL = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 22, 15, 30, tzinfo=UTC)
OLDER_SINCE = datetime(2026, 7, 6, tzinfo=UTC)
BOUNDARY = datetime(2026, 7, 13, tzinfo=UTC)
NEWER_UNTIL = datetime(2026, 7, 20, tzinfo=UTC)


def _session(
    project: str,
    session_id: str,
    start: datetime,
    seconds: int,
) -> SessionStat:
    end = start + timedelta(seconds=seconds)
    timestamps = [
        (start + timedelta(seconds=offset)).timestamp()
        for offset in range(0, seconds, 5 * 60)
    ]
    timestamps.append(end.timestamp())
    return SessionStat(
        project=project,
        category="coding",
        session_id=session_id,
        start=start,
        end=end,
        active_sec=seconds,
        msg_count=2,
        user_msg_count=2,
        first_user_text="private prompt must not escape",
        cwd="/private/workspace/must-not-escape",
        path=Path("/private/transcripts/must-not-escape.jsonl"),
        timestamps=timestamps,
    )


def _context():
    return parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {
                    "id": "always",
                    "title": "Always",
                    "projects": ["alpha"],
                },
                {
                    "id": "old-only",
                    "title": "Old only",
                    "projects": ["legacy"],
                    "valid_until": "2026-07-12",
                },
                {
                    "id": "new-only",
                    "title": "New only",
                    "projects": ["new"],
                    "valid_from": "2026-07-13",
                },
                {
                    "id": "shared-a",
                    "title": "Shared A",
                    "projects": ["shared"],
                },
                {
                    "id": "shared-b",
                    "title": "Shared B",
                    "projects": ["shared"],
                },
                {
                    "id": "zero-new",
                    "title": "Zero activity",
                    "projects": ["zero"],
                    "valid_from": "2026-07-13",
                },
            ],
        },
        aliases={},
        source_metadata={
            "source_kind": "configured",
            "path": "/private/goal-source/must-not-escape.toml",
        },
        source_fingerprint="sha256:public-fingerprint",
    )


def _window_sessions():
    physical = _session(
        "alpha",
        "private-cross-window-session-id",
        BOUNDARY - timedelta(minutes=2),
        4 * 60,
    )
    older_slice = session_slice_for_window(
        physical, OLDER_SINCE, BOUNDARY
    )
    newer_slice = session_slice_for_window(
        physical, BOUNDARY, NEWER_UNTIL
    )
    assert older_slice is not None and newer_slice is not None
    return {
        "2026-07-06": [
            _session(
                "legacy", "private-old", OLDER_SINCE + timedelta(hours=1), 3600
            ),
            _session(
                "shared", "private-shared", OLDER_SINCE + timedelta(hours=3), 1800
            ),
            _session(
                "unknown", "private-unknown", OLDER_SINCE + timedelta(hours=5), 900
            ),
            older_slice,
        ],
        "2026-07-13": [
            newer_slice,
            _session(
                "new", "private-new", BOUNDARY + timedelta(hours=1), 3600
            ),
        ],
    }


@pytest.mark.parametrize(
    "value",
    [True, False, 0, -1, MAX_GOAL_ACTIVITY_WEEKS + 1, 1.0, "4", None],
)
def test_bucket_count_validation_is_strict_and_never_clamps(value):
    with pytest.raises(ValueError, match="goal activity weeks"):
        validate_goal_activity_weeks(value)


def test_default_and_cap_are_explicit():
    assert DEFAULT_GOAL_ACTIVITY_WEEKS == 4
    assert MAX_GOAL_ACTIVITY_WEEKS == 24
    assert validate_goal_activity_weeks(1) == 1
    assert validate_goal_activity_weeks(MAX_GOAL_ACTIVITY_WEEKS) == 24


def test_completed_local_iso_weeks_are_oldest_first_and_timezone_aware():
    windows = completed_local_week_windows(
        now=datetime(2026, 7, 22, 15, 30, tzinfo=LOCAL)
    )

    assert len(windows) == 4
    assert [key for key, _since, _until in windows] == [
        "2026-06-22",
        "2026-06-29",
        "2026-07-06",
        "2026-07-13",
    ]
    assert windows[0][1] == datetime(2026, 6, 22, tzinfo=LOCAL)
    assert windows[-1][2] == datetime(2026, 7, 20, tzinfo=LOCAL)
    assert all(
        (until.date() - since.date()).days == 7
        and since.weekday() == until.weekday() == 0
        and since.tzinfo == until.tzinfo == LOCAL
        for _key, since, until in windows
    )


@pytest.mark.skipif(
    not hasattr(system_time, "tzset"),
    reason="host does not expose POSIX local-time rule switching",
)
@pytest.mark.parametrize(
    ("now", "expected_since", "expected_until", "elapsed_hours"),
    [
        (
            datetime(2026, 3, 11, 12),
            "2026-03-02T00:00:00-05:00",
            "2026-03-09T00:00:00-04:00",
            167,
        ),
        (
            datetime(2026, 11, 4, 12),
            "2026-10-26T00:00:00-04:00",
            "2026-11-02T00:00:00-05:00",
            169,
        ),
    ],
)
def test_system_local_production_timezone_preserves_dst_rules(
    now, expected_since, expected_until, elapsed_hours,
):
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        system_time.tzset()
        (_key, since, until), = completed_local_week_windows(1, now=now)
        since_iso = since.isoformat()
        until_iso = until.isoformat()
        actual_elapsed = (until.timestamp() - since.timestamp()) / 3600
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        system_time.tzset()

    assert since_iso == expected_since
    assert until_iso == expected_until
    assert actual_elapsed == elapsed_hours


@pytest.mark.skipif(
    not hasattr(system_time, "tzset"),
    reason="host does not expose POSIX local-time rule switching",
)
@pytest.mark.parametrize(
    ("configured", "now", "expected_since", "expected_until", "elapsed"),
    [
        (
            "EST5EDT,M3.2.0,M11.1.0",
            datetime(2026, 3, 11, 12),
            "2026-03-02T00:00:00-05:00",
            "2026-03-09T00:00:00-04:00",
            167,
        ),
        (
            "UTC0",
            datetime(2026, 7, 22, 12),
            "2026-07-13T00:00:00+00:00",
            "2026-07-20T00:00:00+00:00",
            168,
        ),
    ],
)
def test_posix_tz_rules_take_precedence_over_system_zone_files(
    configured, now, expected_since, expected_until, elapsed,
):
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = configured
        system_time.tzset()
        (_key, since, until), = completed_local_week_windows(1, now=now)
        since_iso = since.isoformat()
        until_iso = until.isoformat()
        actual_elapsed = (until.timestamp() - since.timestamp()) / 3600
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        system_time.tzset()

    assert since_iso == expected_since
    assert until_iso == expected_until
    assert actual_elapsed == elapsed





def test_one_snapshot_builds_independent_sanitized_hour_buckets(monkeypatch):
    calls: list[tuple[dict, str]] = []
    sessions_by_window = _window_sessions()

    def fake_snapshot(windows, *, agent, engaged_only=True):
        calls.append((dict(windows), agent))
        assert engaged_only is True
        return SimpleNamespace(sessions_by_window=sessions_by_window)

    monkeypatch.setattr(
        history_module, "collect_provider_snapshot", fake_snapshot
    )
    payload = collect_goal_activity_history(
        _context(), weeks=2, now=NOW, agent="all", aliases={}
    )

    assert len(calls) == 1
    assert list(calls[0][0]) == ["2026-07-06", "2026-07-13"]
    assert calls[0][1] == "all"
    assert payload["ok"] is True
    assert payload["agent"] == "all"
    assert payload["count"] == 2
    assert payload["bucket_unit"] == "week"
    assert payload["source_kind"] == "configured"
    assert payload["context_fingerprint"] == "sha256:public-fingerprint"
    assert payload["coverage_status"] == "unavailable"
    assert payload["per_goal_shared_semantics"] == "overlapping_non_additive"
    assert payload["global_bucket_semantics"] == (
        "additive_each_contribution_counted_once"
    )
    assert "not goal progress" in payload["disclaimer"]

    older, newer = payload["buckets"]
    assert older["coverage"] == {
        "covered_hours": 1.78,
        "exclusive_hours": 1.03,
        "shared_hours": 0.5,
        "unattributed_hours": 0.25,
        "unattributed_share": 0.1402,
    }
    assert newer["coverage"] == {
        "covered_hours": 1.03,
        "exclusive_hours": 1.03,
        "shared_hours": 0.0,
        "unattributed_hours": 0.0,
        "unattributed_share": 0.0,
    }
    assert older["coverage_status"] == newer["coverage_status"] == "unavailable"
    assert (
        older["coverage"]["exclusive_hours"]
        + older["coverage"]["shared_hours"]
        + older["coverage"]["unattributed_hours"]
        == older["coverage"]["covered_hours"]
    )
    assert [goal["id"] for goal in older["goals"]] == [
        "always",
        "old-only",
        "shared-a",
        "shared-b",
    ]
    assert [goal["id"] for goal in newer["goals"]] == [
        "always",
        "new-only",
        "shared-a",
        "shared-b",
        "zero-new",
    ]
    zero = next(goal for goal in newer["goals"] if goal["id"] == "zero-new")
    assert zero == {
        "id": "zero-new",
        "title": "Zero activity",
        "total_hours": 0.0,
        "exclusive_hours": 0.0,
        "shared_hours": 0.0,
        "shared_hours_is_non_additive": True,
        "projects_touched": [],
        "latest_activity": None,
    }
    shared = [
        goal for goal in older["goals"] if goal["id"] in {"shared-a", "shared-b"}
    ]
    assert [goal["shared_hours"] for goal in shared] == [0.5, 0.5]
    assert older["coverage"]["shared_hours"] == 0.5

    serialized = json.dumps(payload, sort_keys=True)
    for secret in (
        "private-cross-window-session-id",
        "private prompt must not escape",
        "/private/workspace/must-not-escape",
        "/private/transcripts/must-not-escape.jsonl",
        "/private/goal-source/must-not-escape.toml",
    ):
        assert secret not in serialized
    for seconds_field in (
        "covered_contribution",
        "exclusive_contribution",
        "shared_contribution",
        "unattributed_contribution",
        "contribution_unit",
    ):
        assert seconds_field not in serialized


def test_displayed_global_and_per_goal_hours_reconcile_after_rounding(
    monkeypatch,
):
    context = parse_goal_context(
        {
            "schema_version": 1,
            "goals": [
                {
                    "id": "primary",
                    "title": "Primary",
                    "projects": ["alpha", "shared"],
                },
                {
                    "id": "other-shared",
                    "title": "Other shared",
                    "projects": ["shared"],
                },
            ],
        },
        aliases={},
        source_metadata={"source_kind": "provided"},
    )

    def fake_snapshot(windows, *, agent, engaged_only=True):
        key, (since, _until) = next(iter(windows.items()))
        return SimpleNamespace(
            sessions_by_window={
                key: [
                    _session(
                        "alpha",
                        "tiny-exclusive",
                        since + timedelta(hours=1),
                        18,
                    ),
                    _session(
                        "shared",
                        "tiny-shared",
                        since + timedelta(hours=2),
                        18,
                    ),
                    _session(
                        "unknown",
                        "tiny-unattributed",
                        since + timedelta(hours=3),
                        18,
                    ),
                ]
            }
        )

    monkeypatch.setattr(
        history_module, "collect_provider_snapshot", fake_snapshot
    )
    payload = collect_goal_activity_history(
        context, weeks=1, now=NOW, aliases={}
    )

    coverage = payload["buckets"][0]["coverage"]
    displayed_lanes = (
        coverage["exclusive_hours"],
        coverage["shared_hours"],
        coverage["unattributed_hours"],
    )
    assert displayed_lanes == (0.01, 0.01, 0.01)
    assert coverage["covered_hours"] == round(
        math.fsum(displayed_lanes), 2
    ) == 0.03

    primary = next(
        goal
        for goal in payload["buckets"][0]["goals"]
        if goal["id"] == "primary"
    )
    assert primary["exclusive_hours"] == primary["shared_hours"] == 0.01
    assert primary["total_hours"] == round(
        math.fsum(
            (primary["exclusive_hours"], primary["shared_hours"])
        ),
        2,
    ) == 0.02


def test_snapshot_permutation_does_not_change_projection(monkeypatch):
    original = _window_sessions()

    def project(reversed_order: bool):
        def fake_snapshot(windows, *, agent, engaged_only=True):
            return SimpleNamespace(
                sessions_by_window={
                    key: (
                        list(reversed(original[key]))
                        if reversed_order
                        else list(original[key])
                    )
                    for key in windows
                }
            )

        monkeypatch.setattr(
            history_module, "collect_provider_snapshot", fake_snapshot
        )
        return collect_goal_activity_history(
            _context(), weeks=2, now=NOW, agent="claude", aliases={}
        )

    assert project(False) == project(True)


def test_empty_present_context_returns_buckets_and_missing_context_fails_before_scan(
    monkeypatch,
):
    calls = 0

    def fake_snapshot(windows, *, agent, engaged_only=True):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            sessions_by_window={key: [] for key in windows}
        )

    monkeypatch.setattr(
        history_module, "collect_provider_snapshot", fake_snapshot
    )
    empty = parse_goal_context(
        {"schema_version": 1, "goals": []},
        aliases={},
        source_metadata={"source_kind": "managed"},
    )

    payload = collect_goal_activity_history(empty, weeks=1, now=NOW)
    assert calls == 1
    assert payload["buckets"][0]["goals"] == []
    assert payload["buckets"][0]["coverage"]["covered_hours"] == 0.0

    with pytest.raises(GoalContextError, match="No GoalContext source"):
        collect_goal_activity_history(None, weeks=1, now=NOW)
    assert calls == 1


def _write_goal_file(path: Path, *, title: str = "Ship history") -> bytes:
    payload = (
        "schema_version = 1\n\n"
        "[[goals]]\n"
        'id = "ship-history"\n'
        f'title = "{title}"\n'
        'projects = ["ccstory"]\n'
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def test_cli_is_json_only_defaults_to_four_and_preserves_explicit_source(
    tmp_home, tmp_path, monkeypatch, capsys
):
    source = tmp_path / "external" / "goals.toml"
    before = _write_goal_file(source)
    snapshot_calls = 0

    def fake_snapshot(windows, *, agent, engaged_only=True):
        nonlocal snapshot_calls
        snapshot_calls += 1
        assert len(windows) == DEFAULT_GOAL_ACTIVITY_WEEKS
        return SimpleNamespace(
            sessions_by_window={key: [] for key in windows}
        )

    monkeypatch.setattr(
        history_module, "collect_provider_snapshot", fake_snapshot
    )

    rc = cli_module._run_goal_history(
        ["--goals-file", str(source), "--agent", "claude"]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 0
    assert captured.err == ""
    assert snapshot_calls == 1
    assert payload["ok"] is True
    assert payload["agent"] == "claude"
    assert payload["count"] == 4
    assert payload["source_kind"] == "explicit"
    assert payload["buckets"][0]["goals"][0]["total_hours"] == 0.0
    assert source.read_bytes() == before
    assert not (tmp_home / ".ccstory" / "cache.db").exists()
    assert not (tmp_home / ".ccstory" / "reports").exists()


def test_cli_missing_invalid_and_out_of_range_context_fail_without_mutation(
    tmp_home, tmp_path, monkeypatch, capsys
):
    snapshot_calls = 0

    def forbidden_snapshot(*args, **kwargs):
        nonlocal snapshot_calls
        snapshot_calls += 1
        raise AssertionError("invalid requests must fail before collection")

    monkeypatch.setattr(
        history_module, "collect_provider_snapshot", forbidden_snapshot
    )

    assert cli_module._run_goal_history(["--weeks", "1"]) == 1
    missing = capsys.readouterr()
    assert missing.out == ""
    assert "No GoalContext source is configured" in missing.err

    invalid = tmp_path / "invalid.toml"
    invalid_bytes = b"schema_version = 1\ngoals = [not valid"
    invalid.write_bytes(invalid_bytes)
    assert cli_module._run_goal_history(
        ["--weeks", "1", "--goals-file", str(invalid)]
    ) == 1
    malformed = capsys.readouterr()
    assert malformed.out == ""
    assert (
        "could not load or validate its selected GoalContext"
        in malformed.err
    )
    assert str(invalid) not in malformed.err
    assert invalid.read_bytes() == invalid_bytes

    valid = tmp_path / "valid.toml"
    valid_bytes = _write_goal_file(valid)
    assert cli_module._run_goal_history(
        [
            "--weeks",
            str(MAX_GOAL_ACTIVITY_WEEKS + 1),
            "--goals-file",
            str(valid),
        ]
    ) == 1
    out_of_range = capsys.readouterr()
    assert out_of_range.out == ""
    assert "must be between 1 and 24" in out_of_range.err
    assert valid.read_bytes() == valid_bytes
    assert snapshot_calls == 0
    assert not (tmp_home / ".ccstory" / "cache.db").exists()
    assert not (tmp_home / ".ccstory" / "reports").exists()


def test_cli_rejects_non_integer_weeks_at_argparse_boundary(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_module._run_goal_history(["--weeks", "4.0"])
    assert exc.value.code == 2
    assert "invalid int value" in capsys.readouterr().err
