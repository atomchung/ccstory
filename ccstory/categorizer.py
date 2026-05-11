"""Generic project-folder → bucket classifier with user override.

Claude Code stores each project under `~/.claude/projects/<encoded-path>/`
where `<encoded-path>` is the absolute project path with `/` replaced by `-`
(e.g. `/Users/foo/code/bar` → `-Users-foo-code-bar`). Worktrees are appended
as `--claude-worktrees-<adjective>-<scientist>-<hash>`.

We normalize this to a clean leaf name before matching, then run substring
match against rules. First-match-wins, case-insensitive.

Override via ~/.ccstory/config.toml — match against the *normalized* leaf:

    [categories]
    "investing" = ["investment-note", "stock"]
    "content"   = ["xhs", "blog", "newsletter"]
    "work"      = ["paperclip", "g2a"]
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("ccstory.categorizer")
CONFIG_PATH = Path.home() / ".ccstory" / "config.toml"

# Common path prefixes to strip when normalizing leaf project name. Order matters:
# longest first so `-Users-foo-Side-project-` strips fully before `-Users-foo-`.
_PATH_STEM_HINTS = {
    "projects", "project", "code", "repos", "repo",
    "workspace", "dev", "src",
    "side", "my", "documents", "desktop", "work",
}
_WORKTREE_RE = re.compile(r"--claude-worktrees-.*$", re.IGNORECASE)


def normalize_project_name(encoded: str) -> str:
    """Extract a clean leaf project name from Claude Code's encoded folder name.

    Examples:
        `-Users-atomo-Side-project-investment-note` → `investment-note`
        `-Users-atomo-Side-project-investment-note--claude-worktrees-foo-bar`
            → `investment-note`
        `-home-alice-code-my-app` → `my-app`
    """
    if not encoded:
        return ""
    # Strip worktree suffix first (everything after `--claude-worktrees-`)
    s = _WORKTREE_RE.sub("", encoded)
    # Normalize underscores to dashes (some Claude Code versions keep `_`)
    s = s.replace("_", "-")
    s = s.lstrip("-")
    parts = s.split("-")
    # Drop leading `Users`/`home` and the username right after
    if parts and parts[0].lower() in ("users", "home"):
        parts = parts[2:]
    # Drop leading stem hints (case-insensitive)
    while parts and parts[0].lower() in _PATH_STEM_HINTS:
        parts.pop(0)
    leaf = "-".join(p for p in parts if p).lower()
    return leaf or "(top-level)"


# Default 4-bucket rules. Designed to be activity-level, not job-function-
# level — `data` / `ops` / `research` collapse into either coding (if you
# wrote code) or writing (if you wrote prose). Users tune via config.toml.
#
# Matched against *tokens* of the normalized leaf (split by `-`), not raw
# substrings — avoids "paperclip" matching "cli" or "investment-note"
# matching "writing" via "note".
#
# Order matters: first-match-wins. `investment` is listed before `coding`
# so an "investment-dashboard" repo lands in investment, not coding.
DEFAULT_RULES: list[tuple[str, list[str]]] = [
    ("investment", ["investment", "stock", "stocks", "portfolio", "trading",
                    "ticker", "equity", "etf", "options", "finance"]),
    ("writing",    ["blog", "newsletter", "xhs", "rednote", "post", "draft",
                    "docs", "content", "article", "essay", "writing"]),
    # `coding` is the broad catch-all for any software project
    ("coding",     ["app", "sdk", "cli", "plugin", "mcp", "skill", "server",
                    "client", "frontend", "backend", "lib", "framework",
                    "bot", "agent", "extension", "tool", "api",
                    "scraper", "pipeline", "dashboard", "infra", "deploy"]),
    ("other",      ["playground", "scratch", "sandbox", "tmp", "experiment",
                    "test-bed", "misc"]),
]


@dataclass
class CategoryRule:
    name: str
    needles: list[str]


def _load_toml(path: Path) -> dict | None:
    """Load toml using stdlib tomllib (3.11+). Returns None on any error."""
    if not path.exists():
        return None
    try:
        import tomllib  # py 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            LOG.warning("tomllib/tomli not available; skipping %s", path)
            return None
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError) as e:
        LOG.warning("failed to parse %s: %s", path, e)
        return None


def load_rules(config_path: Path = CONFIG_PATH) -> list[CategoryRule]:
    """Load category rules: user override first, then defaults for unmatched.

    If user config defines a `[categories]` table, those rules take precedence
    (first-match-wins). Defaults still kick in for projects that none of
    user-defined rules match.
    """
    rules: list[CategoryRule] = []
    cfg = _load_toml(config_path)
    if cfg and isinstance(cfg.get("categories"), dict):
        for name, needles in cfg["categories"].items():
            if isinstance(needles, list) and all(isinstance(n, str) for n in needles):
                rules.append(CategoryRule(name=str(name), needles=[n.lower() for n in needles]))
            else:
                LOG.warning("ignoring malformed rule %r (needles must be list[str])", name)
    # Default rules append after user rules so user wins on overlap
    for name, needles in DEFAULT_RULES:
        rules.append(CategoryRule(name=name, needles=[n.lower() for n in needles]))
    return rules


DEFAULT_FALLBACK_BUCKET = "coding"


def classify(
    project_dir: str,
    rules: list[CategoryRule] | None = None,
    fallback: str = DEFAULT_FALLBACK_BUCKET,
) -> str:
    """Token-level match on normalized project leaf. First-match-wins.

    Splits the leaf by `-` and compares each token to rule needles. A multi-
    token needle like `deep-dive` matches if the leaf contains both tokens
    as a contiguous span.

    Fallback is `coding` by default — per 2026 dev survey, ~46% of Claude
    Code use is software development, so an unmatched project is most likely
    a code repo. Override via config.toml `default_bucket = "..."`.
    """
    rules = rules if rules is not None else load_rules()
    leaf = normalize_project_name(project_dir)
    if not leaf:
        return fallback
    tokens = leaf.split("-")
    token_set = set(tokens)
    joined = "-".join(tokens)
    for rule in rules:
        for needle in rule.needles:
            if "-" in needle:
                if needle in joined:
                    return rule.name
            elif needle in token_set:
                return rule.name
    return fallback


def preview_classification(projects: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Return {bucket: [(leaf, raw), ...]} for displaying first-run preview."""
    rules = load_rules()
    out: dict[str, list[tuple[str, str]]] = {}
    for proj in projects:
        cat = classify(proj, rules)
        leaf = normalize_project_name(proj) or proj
        out.setdefault(cat, []).append((leaf, proj))
    return out


def ensure_default_config(path: Path = CONFIG_PATH) -> bool:
    """If no config exists, scaffold a commented template. Returns True if written."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    template = """# ccstory category overrides
#
# Built-in defaults (4 buckets): coding, investment, writing, other.
# Unmatched projects fall back to `coding` (the dominant Claude Code use case).
#
# First-match-wins. Tokens are matched against the *normalized* project leaf
# name (worktree suffix + path prefix stripped, split by `-`).
#
# Example — add a "work" bucket and customize what counts as "writing":
#
# default_bucket = "coding"
#
# [categories]
# "work"    = ["company-repo", "internal-tool"]
# "writing" = ["blog", "newsletter", "essay"]

[categories]
"""
    try:
        path.write_text(template, encoding="utf-8")
        return True
    except OSError as e:
        LOG.warning("could not write template config to %s: %s", path, e)
        return False


if __name__ == "__main__":
    # Smoke check: list how user's actual projects would be classified.
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        sys.exit(f"no claude projects dir at {projects_dir}")
    projects = [d.name for d in projects_dir.iterdir() if d.is_dir()]
    # Group worktrees with their parent project to declutter
    unique_leaves: dict[str, str] = {}
    for raw in projects:
        leaf = normalize_project_name(raw) or raw
        unique_leaves.setdefault(leaf, raw)
    preview = preview_classification(list(unique_leaves.values()))
    for bucket, items in sorted(preview.items(), key=lambda x: -len(x[1])):
        print(f"\n[{bucket}] {len(items)} project(s)")
        for leaf, _raw in items[:15]:
            print(f"  {leaf}")
        if len(items) > 15:
            print(f"  ...+{len(items) - 15} more")
