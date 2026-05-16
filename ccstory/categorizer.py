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

# Community-estimated underlying API-equivalent ceiling for the Max 20x plan
# ($200/mo flat fee). Override per user via config.toml `monthly_quota_usd`.
# Set to 0 to disable burn-% display.
DEFAULT_MONTHLY_QUOTA_USD = 3500.0


def load_settings(config_path: Path = CONFIG_PATH) -> dict:
    """Top-level config: default_bucket, monthly_quota_usd, etc."""
    cfg = _load_toml(config_path) or {}
    return {
        "default_bucket": cfg.get("default_bucket", DEFAULT_FALLBACK_BUCKET),
        "monthly_quota_usd": float(cfg.get("monthly_quota_usd",
                                           DEFAULT_MONTHLY_QUOTA_USD)),
    }


# Rich color names for each default bucket. Used by report.py for bar chart,
# highlight line, and per-category headings. Picked for screenshot legibility
# across light + dark terminal themes. Falls back to "white" for any
# user-defined bucket not in this map.
BUCKET_COLORS: dict[str, str] = {
    "coding":       "bright_cyan",
    "investment":   "bright_green",
    "writing":      "bright_magenta",
    "research":     "bright_yellow",
    "data":         "bright_blue",
    "ops":          "orange3",
    "other":        "grey62",
    "uncategorized": "grey50",
}


def color_for(bucket: str) -> str:
    """Rich color name for a bucket. Unknown buckets cycle through a stable palette."""
    if bucket in BUCKET_COLORS:
        return BUCKET_COLORS[bucket]
    # Deterministic fallback for user-defined buckets
    palette = ["bright_red", "bright_blue", "gold3", "spring_green3", "deep_pink3",
               "turquoise2", "salmon1", "medium_purple"]
    return palette[hash(bucket) % len(palette)]


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


def user_rule_match(
    project_dir: str,
    config_path: Path = CONFIG_PATH,
) -> str | None:
    """If the project leaf matches a rule defined in `~/.ccstory/config.toml`,
    return that bucket name. Otherwise None.

    Used by hybrid session-level classification (#25) to decide whether the
    user has expressed an *explicit* opinion about this project. If yes, the
    folder rule wins; if no, content-derived bucket takes over.
    """
    cfg = _load_toml(config_path) or {}
    cats = cfg.get("categories")
    if not isinstance(cats, dict):
        return None
    user_rules: list[CategoryRule] = []
    for name, needles in cats.items():
        if isinstance(needles, list) and all(isinstance(n, str) for n in needles):
            user_rules.append(
                CategoryRule(name=str(name), needles=[n.lower() for n in needles])
            )
    if not user_rules:
        return None
    leaf = normalize_project_name(project_dir)
    if not leaf:
        return None
    tokens = leaf.split("-")
    token_set = set(tokens)
    joined = "-".join(tokens)
    for rule in user_rules:
        for needle in rule.needles:
            if "-" in needle:
                if needle in joined:
                    return rule.name
            elif needle in token_set:
                return rule.name
    return None


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
    template = """# ccstory configuration
#
# Defaults: coding, investment, writing, other.
# Unmatched projects fall back to `coding`.
#
# First-match-wins. Tokens are matched against the *normalized* project leaf
# name (worktree suffix + path prefix stripped, split by `-`).
#
# Tip: run `ccstory init` to auto-populate this from your recent sessions,
# or `ccstory category set <bucket> <keyword>...` to add rules one at a time.

# Fallback bucket for unmatched projects
default_bucket = "coding"

# API-equivalent monthly quota for burn-% display in `ccstory trend`.
# Community estimates: Max 20x ≈ $3500, Max 5x ≈ $1500, Pro ≈ $200.
# Set to 0 to hide the burn-% row entirely.
monthly_quota_usd = 3500

# Example custom buckets:
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


def _render_config(
    categories: dict[str, list[str]],
    default_bucket: str,
    monthly_quota_usd: float,
) -> str:
    """Re-render config.toml from scratch from in-memory state.

    Comments and section ordering are stable across writes so successive
    `category set/unset` commands produce minimal diffs.
    """
    import json as _json
    lines = [
        "# ccstory configuration",
        "#",
        "# Maintained by `ccstory category set/unset` and `ccstory init`.",
        "# Hand-edit is fine — the CLI re-renders on the next write.",
        "",
        f'default_bucket = "{default_bucket}"',
        "",
        "# API-equivalent monthly quota for burn-% display in `ccstory trend`.",
        "# Set to 0 to hide the burn-% row entirely.",
        f"monthly_quota_usd = {monthly_quota_usd:g}",
        "",
    ]
    if categories:
        lines.append("[categories]")
        for bucket in sorted(categories):
            kws = categories[bucket]
            if not kws:
                continue
            kw_list = ", ".join(_json.dumps(k) for k in kws)
            lines.append(f'"{bucket}" = [{kw_list}]')
    else:
        lines.append("[categories]")
    lines.append("")
    return "\n".join(lines)


def _load_state(path: Path) -> tuple[dict[str, list[str]], str, float]:
    """Read existing config (or defaults) into (categories, default_bucket, quota)."""
    cfg = _load_toml(path) or {}
    raw_cats = cfg.get("categories") if isinstance(cfg.get("categories"), dict) else {}
    categories: dict[str, list[str]] = {}
    for bucket, kws in raw_cats.items():
        if isinstance(kws, list) and all(isinstance(k, str) for k in kws):
            categories[str(bucket)] = [k for k in kws]
    default_bucket = str(cfg.get("default_bucket", DEFAULT_FALLBACK_BUCKET))
    try:
        quota = float(cfg.get("monthly_quota_usd", DEFAULT_MONTHLY_QUOTA_USD))
    except (TypeError, ValueError):
        quota = DEFAULT_MONTHLY_QUOTA_USD
    return categories, default_bucket, quota


def add_category_keywords(
    bucket: str,
    keywords: list[str],
    path: Path | None = None,
) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """Add `keywords` to `bucket` in config.toml.

    Returns `(updated_categories, moved)` where `moved` lists any
    `(keyword, prev_bucket)` keywords lifted from an existing bucket. Keywords
    are stored lowercased to match the load path's case-insensitive matching.
    """
    if path is None:
        path = CONFIG_PATH
    bucket = bucket.strip()
    if not bucket:
        raise ValueError("bucket name cannot be empty")
    cleaned: list[str] = []
    for kw in keywords:
        kw = kw.strip().lower()
        if kw and kw not in cleaned:
            cleaned.append(kw)
    if not cleaned:
        raise ValueError("at least one non-empty keyword required")

    categories, default_bucket, quota = _load_state(path)
    moved: list[tuple[str, str]] = []
    for kw in cleaned:
        for b, kws in list(categories.items()):
            if b != bucket and kw in kws:
                kws.remove(kw)
                moved.append((kw, b))
                if not kws:
                    del categories[b]
    target = categories.setdefault(bucket, [])
    for kw in cleaned:
        if kw not in target:
            target.append(kw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _render_config(categories, default_bucket, quota),
        encoding="utf-8",
    )
    return categories, moved


def remove_category_keywords(
    bucket: str,
    keywords: list[str],
    path: Path | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Remove `keywords` from `bucket`. Returns `(updated_categories, missing)`
    where `missing` lists keywords that were not present (callers can warn).
    Bucket is dropped if it becomes empty.
    """
    if path is None:
        path = CONFIG_PATH
    bucket = bucket.strip()
    cleaned = [k.strip().lower() for k in keywords if k.strip()]
    if not cleaned:
        raise ValueError("at least one non-empty keyword required")

    categories, default_bucket, quota = _load_state(path)
    missing: list[str] = []
    target = categories.get(bucket, [])
    for kw in cleaned:
        if kw in target:
            target.remove(kw)
        else:
            missing.append(kw)
    if bucket in categories and not categories[bucket]:
        del categories[bucket]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _render_config(categories, default_bucket, quota),
        encoding="utf-8",
    )
    return categories, missing


def list_user_categories(
    path: Path | None = None,
) -> dict[str, list[str]]:
    """Return the current user `[categories]` mapping (empty if none)."""
    if path is None:
        path = CONFIG_PATH
    categories, _, _ = _load_state(path)
    return categories


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
