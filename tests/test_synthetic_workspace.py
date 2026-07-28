"""Self-checks proving the #188 0.8-G synthetic fixture is what it claims.

``tests/synthetic_workspace.py`` builds a small but rich, deterministic
cross-provider workspace and describes it as a ``SyntheticWorkspace``. Every
test below drives that description through the *real* bundled providers
(``collect_provider_snapshot``) and the *real* sampling module
(``ccstory.sampling.build_session_sketch``) rather than trusting the
generator's own bookkeeping — a generator that silently produced records the
providers can't parse, or miscounted its own salience plants, would pass a
check that only inspected its input strings. This file is a companion to
``synthetic_workspace.py`` rather than the same file, so that module stays a
plain, pytest-collection-invisible library (its name does not match
``python_files = ["test_*.py"]`` in ``pyproject.toml``) importable by another
slice's tests without pulling in these assertions.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ccstory.providers import collect_provider_snapshot
from ccstory.sampling import SALIENCE_CODES, build_session_sketch
from ccstory.time_tracking import SessionSlice

from tests.synthetic_workspace import (
    DEFAULT_BOUNDARY,
    PROVIDERS,
    SyntheticWorkspace,
    build_synthetic_workspace,
)

import pytest


@pytest.fixture
def workspace(tmp_home: Path) -> SyntheticWorkspace:
    return build_synthetic_workspace(tmp_home)


@pytest.fixture
def snapshot(workspace: SyntheticWorkspace):
    return collect_provider_snapshot(workspace.windows, agent="all")


def _all_parsed_sessions(snapshot) -> list:
    """Flatten every window's session list into one population.

    The boundary-crossing physical session deliberately appears twice here
    (once per window, as two different ``SessionSlice`` objects) — that is
    exactly the real shape ``collect_provider_snapshot`` produces in
    production, and every check below is written to expect it.
    """

    sessions: list = []
    for window_key in snapshot.sessions_by_window:
        sessions.extend(snapshot.sessions_by_window[window_key])
    return sessions


def _physical_id(session) -> str:
    return getattr(session, "physical_session_id", None) or session.session_id


class TestEverySessionParses:
    """`collect_provider_snapshot` must see exactly the manifest, no more, no less."""

    def test_all_three_providers_are_represented(self, workspace: SyntheticWorkspace):
        assert {s.provider for s in workspace.sessions} == set(PROVIDERS)

    def test_session_count_per_provider_and_window_matches_the_manifest(
        self, workspace: SyntheticWorkspace, snapshot,
    ):
        for window_key in ("previous", "current"):
            expected = workspace.by_window(window_key)
            actual = snapshot.sessions_by_window[window_key]
            assert len(actual) == len(expected), (
                f"{window_key}: expected {len(expected)} sessions, parsed {len(actual)}"
            )

            expected_by_provider: dict[str, int] = {}
            for s in expected:
                expected_by_provider[s.provider] = expected_by_provider.get(s.provider, 0) + 1
            actual_by_provider: dict[str, int] = {}
            for s in actual:
                actual_by_provider[s.agent] = actual_by_provider.get(s.agent, 0) + 1
            assert actual_by_provider == expected_by_provider

    def test_no_intended_session_silently_vanished(
        self, workspace: SyntheticWorkspace, snapshot,
    ):
        physical_ids_seen = {_physical_id(s) for s in _all_parsed_sessions(snapshot)}
        for synth in workspace.sessions:
            assert synth.session_id in physical_ids_seen, (
                f"{synth.session_id} was written but never parsed back"
            )

    def test_no_unexpected_session_appears(
        self, workspace: SyntheticWorkspace, snapshot,
    ):
        physical_ids_seen = {_physical_id(s) for s in _all_parsed_sessions(snapshot)}
        expected_ids = {s.session_id for s in workspace.sessions}
        assert physical_ids_seen == expected_ids

    def test_every_session_is_engaged(self, snapshot):
        # `collect_provider_snapshot(..., agent="all")` defaults to
        # `engaged_only=True`; every synthetic session was deliberately given
        # >=2 real user turns so none of them should have been silently
        # dropped by that filter before we even got to count them above.
        for session in _all_parsed_sessions(snapshot):
            assert session.engaged


class TestBoundaryCrossing:
    def test_exactly_one_session_crosses(self, workspace: SyntheticWorkspace):
        crossing = [s for s in workspace.sessions if s.crosses_boundary]
        assert len(crossing) == 1
        assert crossing[0].session_id == workspace.crossing_session_id

    def test_crossing_session_is_a_slice_in_both_windows_with_correct_physical_id(
        self, workspace: SyntheticWorkspace, snapshot,
    ):
        crossing_id = workspace.crossing_session_id
        for window_key in ("previous", "current"):
            sessions = snapshot.sessions_by_window[window_key]
            slices = [
                s for s in sessions
                if isinstance(s, SessionSlice) and s.physical_session_id == crossing_id
            ]
            assert len(slices) == 1, f"{window_key}: expected exactly one crossing slice"
            # A window slice's internal id must differ from its physical id
            # (that is what "boundary_clipped" means) while still reporting
            # the correct physical_session_id back to the caller.
            assert slices[0].session_id != crossing_id
            assert slices[0].boundary_clipped is True

    def test_no_other_session_is_a_slice(self, workspace: SyntheticWorkspace, snapshot):
        crossing_id = workspace.crossing_session_id
        for window_key in ("previous", "current"):
            other_slices = [
                s for s in snapshot.sessions_by_window[window_key]
                if isinstance(s, SessionSlice) and s.physical_session_id != crossing_id
            ]
            assert other_slices == [], (
                f"{window_key}: a non-crossing synthetic session came back sliced"
            )


class TestCoverage:
    def test_at_least_four_distinct_projects(self, workspace: SyntheticWorkspace):
        assert len(workspace.projects) >= 4

    def test_several_distinct_local_days(self, workspace: SyntheticWorkspace):
        assert len(workspace.local_days) >= 6


class TestEvidenceBudget:
    def test_total_joined_evidence_exceeds_6000_chars(
        self, workspace: SyntheticWorkspace, snapshot,
    ):
        total = sum(
            build_session_sketch(s).evidence_chars for s in _all_parsed_sessions(snapshot)
        )
        assert total > 6000, (
            "a [:6000] prefix truncation would not even need to drop anything "
            f"here (real total was only {total})"
        )
        # The workspace's own declared total was computed at build time by
        # calling the real `build_excerpt` on the crossing session's exact
        # message lists (see synthetic_workspace._crossing_excerpt_lengths),
        # never hand-derived — so it must match this independently re-parsed
        # total exactly, not merely be "close enough".
        assert total == workspace.total_evidence_chars


class TestSalienceCodesAreGenuinelyTriggered:
    """Every check here goes through build_session_sketch on a real parse.

    Never through the generator's own input strings — a generator bug that
    planted the wrong keyword, or a provider bug that dropped the message
    carrying it, would otherwise pass silently.
    """

    def test_every_salience_code_is_hit_by_a_real_parse(self, snapshot):
        seen: set[str] = set()
        for session in _all_parsed_sessions(snapshot):
            seen.update(build_session_sketch(session).salience)
        missing = set(SALIENCE_CODES) - seen
        assert not missing, f"SALIENCE_CODES never triggered by any real session: {sorted(missing)}"

    def test_each_planted_session_triggers_at_least_its_intended_codes(
        self, workspace: SyntheticWorkspace, snapshot,
    ):
        by_physical_and_window: dict[tuple[str, str], object] = {}
        for window_key, sessions in snapshot.sessions_by_window.items():
            for session in sessions:
                by_physical_and_window[(_physical_id(session), window_key)] = session

        for synth in workspace.sessions:
            if synth.crosses_boundary:
                assert synth.expected_salience_by_window is not None
                for window_key, expected_codes in synth.expected_salience_by_window.items():
                    real = by_physical_and_window[(synth.session_id, window_key)]
                    actual = set(build_session_sketch(real).salience)
                    assert set(expected_codes).issubset(actual), (
                        f"{synth.session_id}/{window_key}: expected {expected_codes} "
                        f"subset of real salience {actual}"
                    )
                continue

            (window_key,) = synth.expected_windows
            real = by_physical_and_window[(synth.session_id, window_key)]
            actual = set(build_session_sketch(real).salience)
            assert set(synth.expected_salience).issubset(actual), (
                f"{synth.session_id}: expected {synth.expected_salience} subset of "
                f"real salience {actual}"
            )


class TestContentRequirements:
    """The #188 0.8-G content list: long/short, duplicated themes."""

    def test_long_sessions_show_more_real_activity_than_short_ones(
        self, workspace: SyntheticWorkspace, snapshot,
    ):
        by_physical_and_window: dict[tuple[str, str], object] = {}
        for window_key, sessions in snapshot.sessions_by_window.items():
            for session in sessions:
                by_physical_and_window[(_physical_id(session), window_key)] = session

        long_msg_counts = []
        short_msg_counts = []
        for synth in workspace.sessions:
            if synth.crosses_boundary:
                continue
            (window_key,) = synth.expected_windows
            real = by_physical_and_window[(synth.session_id, window_key)]
            (long_msg_counts if synth.length == "long" else short_msg_counts).append(
                real.msg_count
            )

        assert long_msg_counts and short_msg_counts
        assert min(long_msg_counts) > max(short_msg_counts)

    def test_onboarding_copy_dup_theme_is_byte_identical_through_a_real_parse(
        self, workspace: SyntheticWorkspace, snapshot,
    ):
        dup_pair = workspace.theme_group("onboarding-copy-dup")
        assert len(dup_pair) == 2
        assert len({s.provider for s in dup_pair}) == 2, "must span two different providers"

        by_physical_and_window: dict[tuple[str, str], object] = {}
        for window_key, sessions in snapshot.sessions_by_window.items():
            for session in sessions:
                by_physical_and_window[(_physical_id(session), window_key)] = session

        first_texts = set()
        for synth in dup_pair:
            (window_key,) = synth.expected_windows
            real = by_physical_and_window[(synth.session_id, window_key)]
            first_texts.add(real.first_user_text)
        assert len(first_texts) == 1, "the duplicate pair's real first_user_text diverged"

    def test_auth_incident_theme_links_two_distinct_sessions_across_the_boundary(
        self, workspace: SyntheticWorkspace,
    ):
        theme_pair = workspace.theme_group("auth-incident")
        assert len(theme_pair) == 2
        assert {s.session_id for s in theme_pair} == {
            "synthetic-claude-atlas-dashboard-previous",
            "synthetic-claude-atlas-dashboard-current",
        }
        assert {s.expected_windows for s in theme_pair} == {("previous",), ("current",)}


class TestDeterministicRebuild:
    """Two builds with the same arguments must write byte-identical files."""

    def test_two_independent_builds_are_byte_identical(self, tmp_path: Path):
        home_a = tmp_path / "workspace_a"
        home_b = tmp_path / "workspace_b"
        home_a.mkdir()
        home_b.mkdir()

        build_synthetic_workspace(home_a, boundary=DEFAULT_BOUNDARY)
        build_synthetic_workspace(home_b, boundary=DEFAULT_BOUNDARY)

        files_a = {
            p.relative_to(home_a): p for p in sorted(home_a.rglob("*")) if p.is_file()
        }
        files_b = {
            p.relative_to(home_b): p for p in sorted(home_b.rglob("*")) if p.is_file()
        }
        assert set(files_a) == set(files_b)
        assert files_a, "the fixture must actually write files"
        for rel_path, path_a in files_a.items():
            assert path_a.read_bytes() == files_b[rel_path].read_bytes(), (
                f"{rel_path} differs between two builds with identical arguments"
            )

    def test_rebuilding_the_same_directory_is_idempotent(self, tmp_path: Path):
        home = tmp_path / "workspace_rebuild"
        home.mkdir()

        def _hashes() -> dict[Path, str]:
            return {
                p.relative_to(home): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(home.rglob("*"))
                if p.is_file()
            }

        build_synthetic_workspace(home)
        first = _hashes()
        build_synthetic_workspace(home)
        second = _hashes()
        assert first == second


class TestConfigurableBoundary:
    """The boundary is a real parameter, not a disguised constant."""

    def test_a_non_default_boundary_shifts_the_windows(self, tmp_home: Path):
        from datetime import datetime, timedelta, timezone

        custom_boundary = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ws = build_synthetic_workspace(tmp_home, boundary=custom_boundary)

        assert ws.boundary == custom_boundary
        assert ws.windows["previous"][1] == custom_boundary
        assert ws.windows["current"][0] == custom_boundary
        assert ws.windows["previous"][0] == custom_boundary - timedelta(days=7)
        assert ws.windows["current"][1] == custom_boundary + timedelta(days=7)
