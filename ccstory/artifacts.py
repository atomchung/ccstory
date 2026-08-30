"""Collect repository-activity metrics for the report period (#90).

time_tracking answers *where the time went*; this module answers *what the
time produced* — commits, merged PRs, releases, GitHub stars, PyPI downloads
for the repos the user actually worked in during the window.

Tracked repos are inferred from session ``cwd`` (no config required):
each unique cwd is resolved through ``git rev-parse --git-common-dir`` so
linked worktrees collapse into their main repository. The ``[artifacts]``
table in config.toml layers on top (enabled / exclude / pypi), mirroring
the categorizer's default-rules-plus-override pattern.

Every collector is fail-soft: subprocess and HTTP calls carry timeouts,
and any failure degrades to an explicit local-only or partial-coverage state.
Offline, you still get commit counts.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .time_tracking import SessionStat, _parse_ts

LOG = logging.getLogger("ccstory.artifacts")

DB_PATH = Path.home() / ".ccstory" / "cache.db"

GIT_TIMEOUT_SEC = 5
GH_TIMEOUT_SEC = 10
HTTP_TIMEOUT_SEC = 10
DEFAULT_GITHUB_REPO_LIMIT = 10
MARKDOWN_REPO_LIMIT = 20

# GitHub's issue/PR search API returns at most 1,000 results for one query.
# Date-range queries are recursively split before accepting a capped result,
# so lifetime PR volume cannot hide PRs from the requested report window.
_GH_PR_SEARCH_LIMIT = 1000

_GITHUB_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"
)


@dataclass
class RepoArtifacts:
    root: Path
    name: str
    github: str | None = None  # "owner/repo"; only filled when gh is usable
    commits: int = 0
    commit_subjects: list[str] = field(default_factory=list)
    prs_merged: int | None = None  # None = gh unavailable / lookup failed
    releases: list[str] = field(default_factory=list)
    stars: int | None = None
    stars_delta: int | None = None  # None = no pre-window baseline yet


@dataclass
class PyPIDownloads:
    package: str
    downloads: int
    window: str  # pypistats bucket the number comes from: last_week / last_month


@dataclass
class ArtifactsReport:
    repos: list[RepoArtifacts] = field(default_factory=list)
    pypi: list[PyPIDownloads] = field(default_factory=list)
    repos_discovered: int = 0
    github_status: str = "not_needed"
    github_repos_total: int = 0
    github_repos_queried: int = 0
    github_repos_enriched: int = 0

    @property
    def total_commits(self) -> int:
        return sum(r.commits for r in self.repos)

    @property
    def total_prs(self) -> int:
        return sum(r.prs_merged or 0 for r in self.repos)

    @property
    def total_releases(self) -> int:
        return sum(len(r.releases) for r in self.repos)

    @property
    def github_complete(self) -> bool:
        """Whether GitHub-derived totals cover every discovered GitHub repo."""
        if self.github_repos_total == 0:
            return True
        return (
            self.github_status == "connected"
            and self.github_repos_enriched == self.github_repos_total
        )


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = GIT_TIMEOUT_SEC) -> str | None:
    """Run a command, returning stripped stdout or None on any failure."""
    try:
        r = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        LOG.debug("command failed %s: %s", cmd[:2], e)
        return None
    if r.returncode != 0:
        LOG.debug("command exited %d %s: %s", r.returncode, cmd[:2], r.stderr.strip()[:200])
        return None
    return r.stdout.strip()


def repo_root_for_cwd(cwd: str) -> Path | None:
    """Main-repo root for a session cwd, or None if cwd isn't in a git repo.

    ``--git-common-dir`` (not ``--show-toplevel``) so a session that ran in a
    linked worktree attributes its commits to the main repository — otherwise
    the same repo shows up once per worktree.
    """
    if not cwd:
        return None
    path = Path(cwd)
    if not path.is_dir():
        return None  # worktree/dir deleted since the session ran
    out = _run(
        ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
    )
    if not out:
        return None
    git_dir = Path(out)
    # Normal layout: <root>/.git → parent is the repo root. Anything exotic
    # (bare repos, gitfile indirection) falls back to the dir itself.
    return git_dir.parent if git_dir.name == ".git" else git_dir


def discover_repos(sessions: list[SessionStat], exclude: list[str]) -> list[Path]:
    """Unique main-repo roots for the window's sessions, excludes applied.

    ``exclude`` entries match as substrings of the repo root path — same
    loose contract as categorizer keyword rules.
    """
    roots: dict[Path, None] = {}
    seen_cwds: set[str] = set()
    for s in sessions:
        cwd = s.cwd
        if not cwd or cwd in seen_cwds:
            continue
        seen_cwds.add(cwd)
        root = repo_root_for_cwd(cwd)
        if root is None:
            continue
        if any(pat and pat in str(root) for pat in exclude):
            continue
        roots.setdefault(root)
    return list(roots)


def count_commits(root: Path, since: datetime, until: datetime) -> tuple[int, list[str]]:
    """(commit count, up to 3 newest subjects) across all branches.

    ``--all`` because side-project work often lives on unmerged branches —
    a commit is produced work whether or not it reached main this window.
    """
    out = _run(
        [
            "git", "-C", str(root), "log", "--all", "--no-merges",
            f"--since={since.isoformat()}", f"--until={until.isoformat()}",
            "--pretty=%s",
        ]
    )
    if not out:
        return 0, []
    subjects = [line for line in out.splitlines() if line.strip()]
    return len(subjects), subjects[:3]


def github_slug(root: Path) -> str | None:
    """"owner/repo" from the origin remote, or None for non-GitHub remotes."""
    url = _run(["git", "-C", str(root), "remote", "get-url", "origin"])
    if not url:
        return None
    m = _GITHUB_REMOTE_RE.search(url)
    return f"{m.group('owner')}/{m.group('repo')}" if m else None


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _gh_authenticated() -> bool:
    """Whether gh has an active GitHub credential.

    Check once before per-repo calls so a machine without GitHub access does
    not pay one failing network timeout for every discovered repository.
    """
    return _run(
        ["gh", "auth", "status", "--hostname", "github.com"],
        timeout=GH_TIMEOUT_SEC,
    ) is not None


def _repo_limit(cfg: dict) -> int:
    raw = cfg.get("github_repo_limit", DEFAULT_GITHUB_REPO_LIMIT)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return DEFAULT_GITHUB_REPO_LIMIT


def _search_merged_prs(
    slug: str,
    start_day: date,
    end_day: date,
) -> list[dict] | None:
    """Merged PR rows for an inclusive UTC date range.

    GitHub search has a hard 1,000-result ceiling. Split a capped multi-day
    query until each slice is below the ceiling; only a single day with 1,000+
    merged PRs remains genuinely incomplete.
    """
    date_query = (
        start_day.isoformat()
        if start_day == end_day
        else f"{start_day.isoformat()}..{end_day.isoformat()}"
    )
    out = _run(
        [
            "gh", "pr", "list", "--repo", slug, "--state", "merged",
            "--search", f"merged:{date_query}",
            "--json", "number,mergedAt",
            "--limit", str(_GH_PR_SEARCH_LIMIT),
        ],
        timeout=GH_TIMEOUT_SEC,
    )
    if out is None:
        return None
    try:
        prs = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not isinstance(prs, list):
        return None
    if len(prs) < _GH_PR_SEARCH_LIMIT:
        return prs

    if start_day == end_day:
        LOG.warning(
            "%s: merged-PR search hit GitHub's %d-result cap for %s; "
            "count may be low",
            slug,
            _GH_PR_SEARCH_LIMIT,
            start_day.isoformat(),
        )
        return prs

    midpoint = start_day + timedelta(days=(end_day - start_day).days // 2)
    left = _search_merged_prs(slug, start_day, midpoint)
    right = _search_merged_prs(slug, midpoint + timedelta(days=1), end_day)
    if left is None or right is None:
        return None
    return left + right


def count_merged_prs(slug: str, since: datetime, until: datetime) -> int | None:
    """Count merged PRs in the exact half-open report window.

    GitHub performs the coarse UTC-date filter, then exact ``mergedAt``
    timestamps preserve ccstory's existing ``[since, until)`` semantics.
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    else:
        since = since.astimezone(timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    else:
        until = until.astimezone(timezone.utc)
    if until <= since:
        return 0

    prs = _search_merged_prs(slug, since.date(), until.date())
    if prs is None:
        return None

    count = 0
    seen: set[int] = set()
    for pr in prs:
        number = pr.get("number")
        if isinstance(number, int):
            if number in seen:
                continue
            seen.add(number)
        merged = _parse_ts(pr.get("mergedAt"))
        if merged and since <= merged < until:
            count += 1
    return count


def list_releases(slug: str, since: datetime, until: datetime) -> list[str] | None:
    out = _run(
        ["gh", "api", f"repos/{slug}/releases?per_page=50"],
        timeout=GH_TIMEOUT_SEC,
    )
    if out is None:
        return None
    try:
        releases = json.loads(out)
    except json.JSONDecodeError:
        return None
    tags: list[str] = []
    for rel in releases:
        if rel.get("draft"):
            continue
        published = _parse_ts(rel.get("publishedAt") or rel.get("published_at"))
        if published and since <= published < until:
            tags.append(rel.get("tagName") or rel.get("tag_name") or "?")
    return tags


def get_stars(slug: str) -> int | None:
    out = _run(
        ["gh", "api", f"repos/{slug}", "--jq", ".stargazers_count"],
        timeout=GH_TIMEOUT_SEC,
    )
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


# --- stars snapshot (delta needs a pre-window baseline) ---------------------


def _metrics_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repo_metrics (
            repo TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            stars INTEGER NOT NULL,
            PRIMARY KEY (repo, captured_at)
        )
        """
    )
    return conn


def _open_metrics_db() -> sqlite3.Connection | None:
    try:
        return _metrics_connect()
    except sqlite3.Error as e:
        LOG.debug("repo_metrics DB unavailable: %s", e)
        return None


def stars_delta_and_record(
    slug: str, stars: int, since: datetime, conn: sqlite3.Connection
) -> int | None:
    """Delta vs the newest snapshot taken before the window; records today's.

    First run for a repo has no baseline → returns None and the report shows
    the absolute count. The delta becomes meaningful from the second window
    onward — same warm-up contract as comparison narratives.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        row = conn.execute(
            "SELECT stars FROM repo_metrics WHERE repo = ? AND captured_at < ? "
            "ORDER BY captured_at DESC LIMIT 1",
            (slug, since.date().isoformat()),
        ).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO repo_metrics (repo, captured_at, stars) VALUES (?, ?, ?)",
            (slug, today, stars),
        )
        conn.commit()
    except sqlite3.Error as e:
        LOG.debug("repo_metrics snapshot failed for %s: %s", slug, e)
        return None
    return stars - row[0] if row else None


# --- PyPI --------------------------------------------------------------------


def _normalize_pypi_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def detect_pypi_package(root: Path) -> str | None:
    """[project].name from the repo's pyproject.toml, if any."""
    py = root / "pyproject.toml"
    if not py.is_file():
        return None
    try:
        data = tomllib.loads(py.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    name = (data.get("project") or {}).get("name")
    return _normalize_pypi_name(str(name)) if name else None


def pypi_downloads(package: str, window: str) -> PyPIDownloads | None:
    """Recent downloads from pypistats.org. 404 (never published) → None.

    One retry on transient failures per the house rule for network calls.
    """
    import urllib.error
    import urllib.request

    url = f"https://pypistats.org/api/packages/{package}/recent"
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ccstory"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            count = (data.get("data") or {}).get(window)
            if not isinstance(count, int):
                return None
            return PyPIDownloads(package=package, downloads=count, window=window)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # auto-detected name that was never published
            LOG.debug("pypistats %s attempt %d: HTTP %s", package, attempt, e.code)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            LOG.debug("pypistats %s attempt %d: %s", package, attempt, e)
    return None


# --- entry point --------------------------------------------------------------


def collect_artifacts(
    sessions: list[SessionStat],
    since: datetime,
    until: datetime,
    settings: dict | None = None,
) -> ArtifactsReport | None:
    """Gather repo-activity metrics for the window. None → nothing to show."""
    cfg = (settings or {}).get("artifacts") or {}
    if not cfg.get("enabled", True):
        return None
    exclude = [str(x) for x in cfg.get("exclude", [])]
    explicit_pypi = [_normalize_pypi_name(str(x)) for x in cfg.get("pypi", [])]

    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)

    gh_installed = _gh_available()
    repo_limit = _repo_limit(cfg)
    repos: list[RepoArtifacts] = []
    metrics_conn: sqlite3.Connection | None = None
    # dict-as-ordered-set: explicit config packages render first, then
    # auto-detected ones in discovery order — a plain set would make the
    # PyPI lines' order unstable across runs.
    pypi_candidates: dict[str, None] = dict.fromkeys(explicit_pypi)

    discovered: list[tuple[RepoArtifacts, str | None]] = []
    for root in discover_repos(sessions, exclude):
        commits, subjects = count_commits(root, since, until)
        art = RepoArtifacts(
            root=root, name=root.name, commits=commits, commit_subjects=subjects,
        )
        # Remote lookup is local and cheap. Keep it separate from gh access so
        # the report can explain that GitHub-backed repos were found but could
        # not be enriched.
        discovered.append((art, github_slug(root)))

    # Local activity decides priority. This keeps a 100-repo report bounded
    # without pretending the GitHub totals cover repositories we did not query.
    discovered.sort(key=lambda pair: pair[0].commits, reverse=True)
    github_candidates = [(art, slug) for art, slug in discovered if slug]
    gh_connected = (
        bool(github_candidates)
        and repo_limit > 0
        and gh_installed
        and _gh_authenticated()
    )
    github_targets = github_candidates[:repo_limit] if gh_connected else []
    github_target_ids = {id(art) for art, _ in github_targets}
    github_enriched = 0

    for art, slug in github_targets:
        if slug:
            art.github = slug
            art.prs_merged = count_merged_prs(slug, since, until)
            if art.prs_merged is None:
                # Missing repo permission and offline failures most commonly
                # surface here. Do not spend two more requests proving the
                # same repo is unavailable.
                continue
            releases = list_releases(slug, since, until)
            art.releases = releases or []
            if releases is None:
                continue
            art.stars = get_stars(slug)
            # Aggregate PR/release totals are complete only when both core
            # activity lookups succeeded for this repo. Stars are optional
            # context and may remain unavailable without invalidating totals.
            github_enriched += 1
            if art.stars is not None:
                if metrics_conn is None:
                    metrics_conn = _open_metrics_db()
                if metrics_conn is not None:
                    art.stars_delta = stars_delta_and_record(
                        slug, art.stars, since, metrics_conn,
                    )
    # Preserve local output for every discovered repo. GitHub-only activity can
    # add a quiet local repo only when that repo was inside the bounded target
    # set and the remote lookup found activity.
    auto_pypi_roots = {
        art.root for art, _ in discovered
        if art.commits or id(art) in github_target_ids
    }
    for art, _slug in discovered:
        if art.commits or art.prs_merged or art.releases:
            repos.append(art)
            pkg = (
                detect_pypi_package(art.root)
                if art.root in auto_pypi_roots
                else None
            )
            if pkg:
                pypi_candidates.setdefault(pkg)

    if metrics_conn is not None:
        metrics_conn.close()

    # week-ish windows read pypistats' last_week bucket; anything longer,
    # last_month. The buckets never align exactly with the report window —
    # the report labels which bucket the number came from.
    window = "last_week" if (until - since).days <= 8 else "last_month"
    pypi = [
        hit for pkg in pypi_candidates
        if (hit := pypi_downloads(pkg, window)) is not None
    ]

    if not repos and not pypi and not github_candidates:
        return None
    if not github_candidates:
        github_status = "not_needed"
    elif repo_limit == 0:
        github_status = "disabled"
    elif not gh_installed:
        github_status = "not_installed"
    elif not gh_connected:
        github_status = "not_connected"
    else:
        github_status = "connected"
    return ArtifactsReport(
        repos=repos,
        pypi=pypi,
        repos_discovered=len(discovered),
        github_status=github_status,
        github_repos_total=len(github_candidates),
        github_repos_queried=len(github_targets),
        github_repos_enriched=github_enriched,
    )
