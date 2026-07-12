"""Tests for #90 — artifact-level output metrics (What shipped)."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccstory import artifacts
from ccstory.artifacts import (
    ArtifactsReport,
    PyPIDownloads,
    RepoArtifacts,
    collect_artifacts,
    count_commits,
    count_merged_prs,
    detect_pypi_package,
    discover_repos,
    github_slug,
    list_releases,
    pypi_downloads,
    repo_root_for_cwd,
    stars_delta_and_record,
)
from ccstory.report import render_artifacts_markdown, render_report
from ccstory.time_tracking import SessionStat, parse_session
from tests.conftest import _ts, make_user_msg, write_jsonl

SINCE = datetime(2026, 7, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 7, 8, tzinfo=timezone.utc)


def _git(root: Path, *args: str, env: dict | None = None) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, env=env,
    )


def _commit(root: Path, msg: str, when: datetime) -> None:
    iso = when.isoformat()
    env = {**os.environ, "GIT_COMMITTER_DATE": iso, "GIT_AUTHOR_DATE": iso}
    marker = root / "f.txt"
    marker.write_text(marker.read_text() + msg if marker.exists() else msg)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", msg, env=env)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    return root


def _stat(cwd: str, sid: str = "s1") -> SessionStat:
    base = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    return SessionStat(
        project="-Users-t-repo", category="coding", session_id=sid,
        start=base, end=base, active_sec=600, msg_count=5, cwd=cwd,
    )


class TestRepoDiscovery:
    def test_repo_root_for_plain_repo(self, git_repo: Path):
        assert repo_root_for_cwd(str(git_repo)) == git_repo

    def test_subdir_resolves_to_root(self, git_repo: Path):
        sub = git_repo / "pkg" / "inner"
        sub.mkdir(parents=True)
        assert repo_root_for_cwd(str(sub)) == git_repo

    def test_worktree_collapses_to_main_repo(self, git_repo: Path, tmp_path: Path):
        _commit(git_repo, "seed", SINCE)
        wt = tmp_path / "wt"
        _git(git_repo, "worktree", "add", "-q", str(wt), "-b", "feature")
        assert repo_root_for_cwd(str(wt)) == git_repo

    def test_non_repo_dir_is_none(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert repo_root_for_cwd(str(plain)) is None

    def test_missing_dir_is_none(self, tmp_path: Path):
        assert repo_root_for_cwd(str(tmp_path / "gone")) is None

    def test_empty_cwd_is_none(self):
        assert repo_root_for_cwd("") is None

    def test_discover_dedupes_and_excludes(self, git_repo: Path, tmp_path: Path):
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        sessions = [
            _stat(str(git_repo), "a"),
            _stat(str(git_repo), "b"),          # duplicate cwd
            _stat(str(plain), "c"),             # not a repo
            _stat("", "d"),                     # pre-cwd-era transcript
        ]
        assert discover_repos(sessions, exclude=[]) == [git_repo]
        assert discover_repos(sessions, exclude=["repo"]) == []


class TestCountCommits:
    def test_window_filtering(self, git_repo: Path):
        _commit(git_repo, "before window", datetime(2026, 6, 20, tzinfo=timezone.utc))
        _commit(git_repo, "inside one", datetime(2026, 7, 2, tzinfo=timezone.utc))
        _commit(git_repo, "inside two", datetime(2026, 7, 3, tzinfo=timezone.utc))
        count, subjects = count_commits(git_repo, SINCE, UNTIL)
        assert count == 2
        assert subjects == ["inside two", "inside one"]

    def test_unmerged_branch_counts(self, git_repo: Path, tmp_path: Path):
        _commit(git_repo, "seed", datetime(2026, 6, 20, tzinfo=timezone.utc))
        wt = tmp_path / "wt"
        _git(git_repo, "worktree", "add", "-q", str(wt), "-b", "feature")
        _commit(wt, "branch work", datetime(2026, 7, 2, tzinfo=timezone.utc))
        count, subjects = count_commits(git_repo, SINCE, UNTIL)
        assert count == 1
        assert subjects == ["branch work"]

    def test_empty_window(self, git_repo: Path):
        _commit(git_repo, "old", datetime(2026, 6, 1, tzinfo=timezone.utc))
        assert count_commits(git_repo, SINCE, UNTIL) == (0, [])


class TestGithubSlug:
    @pytest.mark.parametrize("url,slug", [
        ("https://github.com/alice/proj.git", "alice/proj"),
        ("https://github.com/alice/proj", "alice/proj"),
        ("git@github.com:alice/proj.git", "alice/proj"),
        ("ssh://git@github.com/alice/proj.git", "alice/proj"),
        ("https://gitlab.com/alice/proj.git", None),
    ])
    def test_parsing(self, git_repo: Path, url: str, slug: str | None):
        _git(git_repo, "remote", "add", "origin", url)
        assert github_slug(git_repo) == slug

    def test_no_remote(self, git_repo: Path):
        assert github_slug(git_repo) is None


class TestGhCollectors:
    def test_merged_prs_filters_window(self, monkeypatch: pytest.MonkeyPatch):
        payload = json.dumps([
            {"mergedAt": "2026-07-02T10:00:00Z"},   # in
            {"mergedAt": "2026-06-30T10:00:00Z"},   # before
            {"mergedAt": "2026-07-08T00:00:00Z"},   # at until → excluded (half-open)
            {"mergedAt": None},                       # gh oddity
        ])
        monkeypatch.setattr(artifacts, "_run", lambda *a, **k: payload)
        assert count_merged_prs("a/b", SINCE, UNTIL) == 1

    def test_merged_prs_gh_failure(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(artifacts, "_run", lambda *a, **k: None)
        assert count_merged_prs("a/b", SINCE, UNTIL) is None

    def test_merged_prs_bad_json(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(artifacts, "_run", lambda *a, **k: "not json")
        assert count_merged_prs("a/b", SINCE, UNTIL) is None

    def test_releases_filter_and_drafts(self, monkeypatch: pytest.MonkeyPatch):
        payload = json.dumps([
            {"tag_name": "v1.2.0", "published_at": "2026-07-03T09:00:00Z"},
            {"tag_name": "v1.1.0", "published_at": "2026-06-01T09:00:00Z"},
            {"tag_name": "v1.3.0", "published_at": "2026-07-04T09:00:00Z", "draft": True},
        ])
        monkeypatch.setattr(artifacts, "_run", lambda *a, **k: payload)
        assert list_releases("a/b", SINCE, UNTIL) == ["v1.2.0"]

    def test_releases_gh_failure(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(artifacts, "_run", lambda *a, **k: None)
        assert list_releases("a/b", SINCE, UNTIL) is None


class TestStarsSnapshot:
    @pytest.fixture(autouse=True)
    def _db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(artifacts, "DB_PATH", tmp_path / "cache.db")

    def test_first_run_records_but_no_delta(self):
        assert stars_delta_and_record("a/b", 40, SINCE) is None

    def test_delta_vs_pre_window_baseline(self):
        import sqlite3
        conn = artifacts._metrics_connect()
        conn.execute(
            "INSERT INTO repo_metrics (repo, captured_at, stars) VALUES (?, ?, ?)",
            ("a/b", "2026-06-28", 35),
        )
        conn.commit()
        conn.close()
        assert stars_delta_and_record("a/b", 41, SINCE) == 6

    def test_in_window_snapshot_not_baseline(self):
        conn = artifacts._metrics_connect()
        conn.execute(
            "INSERT INTO repo_metrics (repo, captured_at, stars) VALUES (?, ?, ?)",
            ("a/b", "2026-07-03", 39),
        )
        conn.commit()
        conn.close()
        assert stars_delta_and_record("a/b", 41, SINCE) is None


class TestPyPI:
    def test_detect_package_name(self, tmp_path: Path):
        root = tmp_path / "p"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "My_Fancy.Pkg"\n', encoding="utf-8"
        )
        assert detect_pypi_package(root) == "my-fancy-pkg"

    def test_detect_no_pyproject(self, tmp_path: Path):
        assert detect_pypi_package(tmp_path) is None

    def test_detect_bad_toml(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("not = [valid", encoding="utf-8")
        assert detect_pypi_package(tmp_path) is None

    def _fake_urlopen(self, body: dict):
        class _Resp:
            def read(self):
                return json.dumps(body).encode("utf-8")
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return lambda req, timeout=None: _Resp()

    def test_downloads_ok(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            self._fake_urlopen({"data": {"last_week": 123, "last_month": 999}}),
        )
        hit = pypi_downloads("ccstory", "last_week")
        assert hit == PyPIDownloads(package="ccstory", downloads=123, window="last_week")

    def test_404_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        def raise_404(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", None, None)
        monkeypatch.setattr("urllib.request.urlopen", raise_404)
        assert pypi_downloads("never-published", "last_week") is None

    def test_transient_error_retries_once(self, monkeypatch: pytest.MonkeyPatch):
        calls = {"n": 0}
        good = self._fake_urlopen({"data": {"last_week": 7}})
        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("reset")
            return good(req, timeout)
        monkeypatch.setattr("urllib.request.urlopen", flaky)
        hit = pypi_downloads("ccstory", "last_week")
        assert hit is not None and hit.downloads == 7
        assert calls["n"] == 2

    def test_persistent_error_gives_up(self, monkeypatch: pytest.MonkeyPatch):
        def always_fail(req, timeout=None):
            raise urllib.error.URLError("down")
        monkeypatch.setattr("urllib.request.urlopen", always_fail)
        assert pypi_downloads("ccstory", "last_week") is None


class TestCollectArtifacts:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(artifacts, "DB_PATH", tmp_path / "cache.db")
        # Never let the integration tests reach gh or the network.
        monkeypatch.setattr(artifacts, "_gh_available", lambda: False)
        monkeypatch.setattr(artifacts, "pypi_downloads", lambda pkg, win: None)

    def test_disabled_via_config(self, git_repo: Path):
        out = collect_artifacts(
            [_stat(str(git_repo))], SINCE, UNTIL,
            settings={"artifacts": {"enabled": False}},
        )
        assert out is None

    def test_no_sessions(self):
        assert collect_artifacts([], SINCE, UNTIL) is None

    def test_repo_with_commits(self, git_repo: Path):
        _commit(git_repo, "work", datetime(2026, 7, 2, tzinfo=timezone.utc))
        out = collect_artifacts([_stat(str(git_repo))], SINCE, UNTIL)
        assert out is not None
        assert [r.name for r in out.repos] == ["repo"]
        assert out.repos[0].commits == 1
        assert out.repos[0].prs_merged is None  # gh unavailable

    def test_quiet_repo_dropped(self, git_repo: Path):
        _commit(git_repo, "old", datetime(2026, 6, 1, tzinfo=timezone.utc))
        assert collect_artifacts([_stat(str(git_repo))], SINCE, UNTIL) is None

    def test_pypi_window_bucket(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch):
        _commit(git_repo, "w", datetime(2026, 7, 2, tzinfo=timezone.utc))
        seen: list[tuple[str, str]] = []
        def spy(pkg, win):
            seen.append((pkg, win))
            return PyPIDownloads(package=pkg, downloads=1, window=win)
        monkeypatch.setattr(artifacts, "pypi_downloads", spy)
        settings = {"artifacts": {"pypi": ["ccstory"]}}

        collect_artifacts([_stat(str(git_repo))], SINCE, UNTIL, settings)
        month_until = datetime(2026, 7, 31, tzinfo=timezone.utc)
        collect_artifacts([_stat(str(git_repo))], SINCE, month_until, settings)
        assert seen == [("ccstory", "last_week"), ("ccstory", "last_month")]

    def test_pypi_autodetected_from_active_repo(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (git_repo / "pyproject.toml").write_text(
            '[project]\nname = "repo-pkg"\n', encoding="utf-8"
        )
        _commit(git_repo, "w", datetime(2026, 7, 2, tzinfo=timezone.utc))
        seen: list[str] = []
        monkeypatch.setattr(
            artifacts, "pypi_downloads",
            lambda pkg, win: seen.append(pkg) or None,
        )
        collect_artifacts([_stat(str(git_repo))], SINCE, UNTIL)
        assert seen == ["repo-pkg"]


class TestFailSoft:
    def test_run_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)
        monkeypatch.setattr(artifacts.subprocess, "run", boom)
        assert artifacts._run(["git", "status"]) is None

    def test_run_nonzero_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        class R:
            returncode = 128
            stdout = ""
            stderr = "fatal: not a repo"
        monkeypatch.setattr(artifacts.subprocess, "run", lambda *a, **k: R())
        assert artifacts._run(["git", "status"]) is None


class TestSessionCwdCapture:
    def test_parse_session_reads_cwd(self, tmp_home: Path):
        rec = make_user_msg("hello there", _ts(2026, 7, 2))
        rec["cwd"] = "/Users/t/proj"
        rec2 = make_user_msg("follow up", _ts(2026, 7, 2, 12, 30))
        rec2["cwd"] = "/Users/t/elsewhere"  # first cwd wins
        path = tmp_home / ".claude" / "projects" / "-Users-t-proj" / "s1.jsonl"
        write_jsonl(path, [rec, rec2])
        stat = parse_session(path)
        assert stat is not None
        assert stat.cwd == "/Users/t/proj"

    def test_missing_cwd_is_empty(self, tmp_home: Path):
        path = tmp_home / ".claude" / "projects" / "-Users-t-proj" / "s2.jsonl"
        write_jsonl(path, [make_user_msg("hello", _ts(2026, 7, 2))])
        stat = parse_session(path)
        assert stat is not None
        assert stat.cwd == ""


class TestRendering:
    def _arts(self) -> ArtifactsReport:
        return ArtifactsReport(
            repos=[
                RepoArtifacts(
                    root=Path("/x/ccstory"), name="ccstory", github="a/ccstory",
                    commits=12, prs_merged=3, releases=["v0.4.2"],
                    stars=41, stars_delta=6,
                ),
                RepoArtifacts(
                    root=Path("/x/quiet"), name="quiet|repo",
                    commits=2, prs_merged=None, releases=[],
                    stars=None, stars_delta=None,
                ),
            ],
            pypi=[PyPIDownloads(package="ccstory", downloads=1234, window="last_week")],
        )

    def test_markdown_section(self):
        md = render_artifacts_markdown(self._arts())
        assert "## What shipped" in md
        assert "| ccstory | 12 | 3 | v0.4.2 | 41 (+6) |" in md
        assert "| quiet\\|repo | 2 | – | – | – |" in md  # pipe escaped, N/A dashes
        assert "1,234 downloads (last week)" in md

    def _render(self, artifacts: ArtifactsReport | None) -> str:
        from ccstory.time_tracking import CategoryRollup
        from ccstory.token_usage import ModelUsage, UsageReport

        s = _stat("/x/ccstory")
        rollup = CategoryRollup(
            category="coding", active_min=10.0, sessions=1, messages=5,
            top_sessions=[s],
        )
        usage = UsageReport(since=SINCE, until=UNTIL)
        usage.by_model["claude-opus-4-7"] = ModelUsage(
            model="claude-opus-4-7", turns=5, input_tokens=1000, output_tokens=500,
        )
        usage.assistant_turns = 5
        return render_report(
            label="2026-W27", since=SINCE, until=UNTIL,
            sessions=[s], rollups=[rollup], usage=usage, summaries={},
            artifacts=artifacts,
        )

    def test_report_includes_section(self):
        md = self._render(self._arts())
        assert "## What shipped" in md

    def test_report_omits_section_when_none(self):
        md = self._render(None)
        assert "What shipped" not in md
