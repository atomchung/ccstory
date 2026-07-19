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
import zlib
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


def _extract_aliases(cfg: dict) -> dict[str, str]:
    """Pull the optional ``[projects]`` alias table out of a loaded config.

    Keys/values are lowercased to match ``normalize_project_name``'s output.
    Malformed entries (non-string variant or canonical) are skipped.
    """
    raw = cfg.get("projects")
    if not isinstance(raw, dict):
        return {}
    aliases: dict[str, str] = {}
    for variant, canonical in raw.items():
        if isinstance(variant, str) and isinstance(canonical, str):
            v = variant.strip().lower()
            c = canonical.strip().lower()
            if v and c:
                aliases[v] = c
    return aliases


def load_project_aliases(config_path: Path | None = None) -> dict[str, str]:
    """Load the optional ``[projects]`` alias table (variant leaf → canonical).

    Layer-2 (#69) folds variant folder-leaf names onto one canonical project
    so a repo that surfaces as both ``info_collector`` and ``info-collector``
    rolls up as a single project. Absent/empty table → ``{}``, which makes
    ``alias_fold`` a no-op and keeps every existing config's numbers unchanged.

    ``config_path`` resolves to module-level ``CONFIG_PATH`` at call time when
    omitted, so test monkeypatches take effect.
    """
    if config_path is None:
        config_path = CONFIG_PATH
    return _extract_aliases(_load_toml(config_path) or {})


def alias_fold(leaf: str, aliases: dict[str, str] | None) -> str:
    """Map a normalized project leaf onto its canonical name via ``[projects]``.

    No-op when ``aliases`` is empty/None or the leaf has no entry — so existing
    configs (no ``[projects]`` table) fold to the identity and every layer-1
    number stays byte-identical.
    """
    if not aliases:
        return leaf
    return aliases.get(leaf, leaf)


def project_identity(
    project_dir: str,
    aliases: dict[str, str] | None = None,
    config_path: Path | None = None,
) -> str:
    """Layer-2 project identity: ``alias_fold(normalize_project_name(dir))``.

    The single source of truth for a session's project leaf across the rollup
    and report paths (#69). Pass a pre-loaded ``aliases`` map to avoid
    re-reading config per session; omit it to load lazily from ``config_path``.
    """
    if aliases is None:
        aliases = load_project_aliases(config_path)
    return alias_fold(normalize_project_name(project_dir), aliases)


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
    """Load toml using stdlib tomllib (3.11+). Returns None on any error.

    Parse failures escalate to stderr so the user notices — a malformed
    config silently falling back to defaults caused subtle miscategorization
    in the past (issue #9).
    """
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
        import sys as _sys
        msg = (
            f"ccstory: warning: could not parse {path}: {e}\n"
            f"ccstory: falling back to built-in defaults. "
            f"Fix the file or rerun `ccstory init` to regenerate."
        )
        print(msg, file=_sys.stderr)
        LOG.warning("failed to parse %s: %s", path, e)
        return None


def load_rules(config_path: Path | None = None) -> list[CategoryRule]:
    """Load category rules: user override first, then defaults for unmatched.

    Integration API (semi-stable, #110) — see README "Library usage".

    If user config defines a `[categories]` table, those rules take precedence
    (first-match-wins). Defaults still kick in for projects that none of
    user-defined rules match.

    ``config_path`` defaults to the module-level ``CONFIG_PATH``, resolved at
    call time so test monkeypatches take effect.
    """
    if config_path is None:
        config_path = CONFIG_PATH
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


def load_settings(config_path: Path | None = None) -> dict:
    """Top-level config: default_bucket, monthly_quota_usd, language, etc.

    ``config_path`` resolves to module-level ``CONFIG_PATH`` at call time when
    omitted, so test monkeypatches take effect.
    """
    if config_path is None:
        config_path = CONFIG_PATH
    cfg = _load_toml(config_path) or {}
    lang = cfg.get("language")
    if not isinstance(lang, str) or not lang.strip():
        lang = None
    arts = cfg.get("artifacts")
    if not isinstance(arts, dict):
        arts = {}
    return {
        "default_bucket": cfg.get("default_bucket", DEFAULT_FALLBACK_BUCKET),
        "monthly_quota_usd": float(cfg.get("monthly_quota_usd",
                                           DEFAULT_MONTHLY_QUOTA_USD)),
        "language": lang.strip() if lang else None,
        "artifacts": {
            "enabled": bool(arts.get("enabled", True)),
            "exclude": [str(x) for x in arts.get("exclude", [])
                        if isinstance(x, str) and x.strip()],
            "pypi": [str(x) for x in arts.get("pypi", [])
                     if isinstance(x, str) and x.strip()],
        },
    }


# Rich color names for each default bucket. Used by report.py for bar chart,
# highlight line, and per-category headings. Base ANSI names (not `bright_*`
# or fixed 256-color shades) so the palette tracks the user's terminal theme
# — `cyan` resolves through the user's color 6, while `bright_cyan` would
# pin color 14 which most custom themes leave at a neon default.
BUCKET_COLORS: dict[str, str] = {
    "coding":        "cyan",
    "investment":    "green",
    "writing":       "magenta",
    "research":      "yellow",
    "data":          "blue",
    "ops":           "red",
    "other":         "dim",
    "uncategorized": "dim",
}


_UNKNOWN_BUCKET_PALETTE = ["cyan", "green", "magenta", "yellow", "blue", "red"]


def color_for(bucket: str) -> str:
    """Rich color name for a single bucket, independent of any others.

    Unknown buckets hash into a stable 6-color palette — stable across
    calls and processes, but NOT collision-free against sibling buckets
    rendered in the same table/panel (two custom bucket names can hash to
    the same color). When rendering several buckets together, use
    `colors_for()` instead, which resolves that.
    """
    if bucket in BUCKET_COLORS:
        return BUCKET_COLORS[bucket]
    # crc32 is stable across processes; Python's built-in hash() is salted by
    # PYTHONHASHSEED so the same bucket would pick a new color every run.
    return _UNKNOWN_BUCKET_PALETTE[
        zlib.crc32(bucket.encode("utf-8")) % len(_UNKNOWN_BUCKET_PALETTE)
    ]


def colors_for(buckets: list[str]) -> dict[str, str]:
    """Collision-avoiding Rich color assignment for buckets shown together.

    `color_for()` picks each bucket's color in isolation from a 6-color
    base-ANSI palette (kept small deliberately — see BUCKET_COLORS comment
    — so it can't just grow to dodge collisions). With only 6 slots, a
    report with several custom `[categories]` buckets that don't match any
    BUCKET_COLORS key regularly hashes two of them onto the same color —
    e.g. two different buckets both landing on "green" in the same bar
    chart.

    This resolves that for one render: known buckets keep their
    BUCKET_COLORS mapping first, then each remaining bucket walks forward
    from its own crc32 slot until it finds a color no earlier bucket in
    `buckets` has claimed yet. Still deterministic given the same input
    list — but the result depends on the *set* of sibling buckets, so pass
    every bucket that will appear in the same render (not a subset), and
    reuse the one returned mapping across that render rather than calling
    this again with a different subset.

    Two known limits, both inherent to a fixed 6-color budget rather than
    bugs in the walk itself: (1) known buckets claim their BUCKET_COLORS
    slot unconditionally, so if all 6 default buckets (coding/investment/
    writing/research/data/ops) are present, an unknown bucket has nowhere
    left to walk to and repeats one of their colors — this needs every one
    of those 6 English names in the same render, which requires deliberate
    config, not default usage. (2) "other" and "uncategorized" both map to
    "dim" in BUCKET_COLORS on purpose (they're catch-all buckets meant to
    read as "not a real category," not to compete for a distinct color) —
    colors_for() preserves that shared "dim", it does not try to split it.
    """
    assigned: dict[str, str] = {}
    used: set[str] = set()
    unknown: list[str] = []
    for bucket in buckets:
        if bucket in assigned:
            continue
        if bucket in BUCKET_COLORS:
            color = BUCKET_COLORS[bucket]
            assigned[bucket] = color
            used.add(color)
        else:
            unknown.append(bucket)
    palette = _UNKNOWN_BUCKET_PALETTE
    for bucket in unknown:
        if bucket in assigned:
            continue
        start = zlib.crc32(bucket.encode("utf-8")) % len(palette)
        chosen = palette[start]
        for offset in range(len(palette)):
            candidate = palette[(start + offset) % len(palette)]
            if candidate not in used:
                chosen = candidate
                break
        assigned[bucket] = chosen
        used.add(chosen)
    return assigned


def classify(
    project_dir: str,
    rules: list[CategoryRule] | None = None,
    fallback: str = DEFAULT_FALLBACK_BUCKET,
) -> str:
    """Token-level match on normalized project leaf. First-match-wins.

    Integration API (semi-stable, #110) — see README "Library usage".

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


def _membership_index(
    categories: dict,
) -> tuple[dict[str, str], list[tuple[str, list[str]]]]:
    """Build the exact-membership lookup from a ``[categories]`` table.

    Returns ``(needle → first area, duplicates)`` where a *duplicate* is a
    needle listed verbatim under more than one area. Config order defines
    "first wins": ``tomllib`` preserves table order, so the first area in the
    file keeps the needle and later ones are recorded for the load-time
    warning (#69). Needles are lowercased but NOT stripped — identical to the
    token-needle tier below — so an exact match is always also a token match,
    which is what keeps layer-1 numbers byte-identical (the only behavior
    change is that an exact membership now wins over an *earlier* area's fuzzy
    match, the documented ordering-hack fix).

    Only well-formed ``list[str]`` rules participate — malformed rules are
    skipped exactly as ``load_rules`` skips them.
    """
    index: dict[str, str] = {}
    seen: dict[str, list[str]] = {}
    for area, needles in categories.items():
        if not (isinstance(needles, list) and all(isinstance(n, str) for n in needles)):
            continue
        for needle in needles:
            key = needle.lower()
            if not key:
                continue
            seen.setdefault(key, []).append(str(area))
            if key not in index:
                index[key] = str(area)
    duplicates = [(needle, areas) for needle, areas in seen.items() if len(areas) > 1]
    return index, duplicates


def duplicate_memberships(
    config_path: Path | None = None,
) -> list[tuple[str, list[str]]]:
    """Project names listed under more than one area in ``[categories]`` (#69).

    Each entry is ``(project_name, [areas in config order])``. The resolver
    keeps the first area (see ``_membership_index``); this surfaces the rest so
    a run can warn that the config silently shadows a membership. Empty list
    means the config is unambiguous.

    ``config_path`` resolves to module-level ``CONFIG_PATH`` at call time when
    omitted, so test monkeypatches take effect.
    """
    if config_path is None:
        config_path = CONFIG_PATH
    cfg = _load_toml(config_path) or {}
    cats = cfg.get("categories")
    if not isinstance(cats, dict):
        return []
    _, duplicates = _membership_index(cats)
    return duplicates


def user_rule_match(
    project_dir: str,
    config_path: Path | None = None,
) -> str | None:
    """If the project leaf matches a rule defined in `~/.ccstory/config.toml`,
    return that area name. Otherwise None.

    Used by hybrid session-level classification (#25) to decide whether the
    user has expressed an *explicit* opinion about this project. If yes, the
    folder rule wins; if no, content-derived bucket takes over.

    Two tiers, both reported as ``user_rule`` upstream (#69):
      1. **exact membership** — the (alias-folded) project leaf is listed
         verbatim under an area. Unambiguous by construction, so it wins over
         the fuzzy match below; this is what lets configs drop the section-
         ordering hacks token matching forces.
      2. **token-needle match** — today's fuzzy matching, kept as a compat
         tier so every existing config keeps working unmodified. Original
         first-match-wins-in-config-order semantics preserved.

    ``config_path`` resolves to module-level ``CONFIG_PATH`` at call time when
    omitted, so test monkeypatches take effect.
    """
    if config_path is None:
        config_path = CONFIG_PATH
    cfg = _load_toml(config_path) or {}
    cats = cfg.get("categories")
    if not isinstance(cats, dict):
        return None
    # Alias-fold the leaf first so a folded variant matches membership/needles
    # under its canonical name. Empty [projects] → identity fold → unchanged.
    leaf = alias_fold(normalize_project_name(project_dir), _extract_aliases(cfg))
    if not leaf:
        return None

    # Tier 1: exact membership across all areas (first area in config wins).
    index, _ = _membership_index(cats)
    exact = index.get(leaf)
    if exact:
        return exact

    # Tier 2: token-needle fuzzy match (compat).
    user_rules: list[CategoryRule] = []
    for name, needles in cats.items():
        if isinstance(needles, list) and all(isinstance(n, str) for n in needles):
            user_rules.append(
                CategoryRule(name=str(name), needles=[n.lower() for n in needles])
            )
    if not user_rules:
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


def resolve_session_bucket(
    project_dir: str,
    cached_llm_bucket: str | None,
    mode: str = "hybrid",
    fallback: str | None = None,
    config_path: Path | None = None,
) -> tuple[str | None, str]:
    """Resolve a session's bucket using a priority chain.

    Returns ``(bucket, source)`` where source ∈
    ``{"user_rule", "llm_cache", "needs_llm", "fallback"}``.

    Priority (high → low):
      1. user_rule    — folder leaf matches ``[categories]`` in config.toml
      2. llm_cache    — caller-supplied ``cached_llm_bucket`` (from
                        ``session_content_buckets``)
      3. fallback     — config ``default_bucket`` or ``DEFAULT_FALLBACK_BUCKET``

    ``mode`` controls which layers participate:
      - ``"hybrid"``  → 1 → 2 → 3  (default)
      - ``"content"`` → 2 → 3      (skip folder rule, LLM-first)
      - ``"folder"``  → 1 → 3      (skip LLM cache, deterministic only)

    When mode permits LLM (``hybrid`` / ``content``) but ``cached_llm_bucket``
    is ``None``, returns ``(None, "needs_llm")`` so the caller can batch this
    session into a single ``classify_sessions_by_content`` call. Callers that
    cannot afford fresh LLM work (e.g. ``compare_to_previous``) should treat
    ``needs_llm`` as ``fallback`` to keep cost predictable.
    """
    if config_path is None:
        config_path = CONFIG_PATH
    # Layer 1: user folder rule (skipped in content mode)
    if mode != "content":
        folder_bucket = user_rule_match(project_dir, config_path)
        if folder_bucket:
            return folder_bucket, "user_rule"

    # Layer 2: LLM cache (skipped in folder mode)
    if mode != "folder":
        if cached_llm_bucket:
            return cached_llm_bucket, "llm_cache"
        # Mode wants LLM but cache miss — signal caller
        return None, "needs_llm"

    # Layer 3: fallback (folder mode, or other resolved nothing)
    return _resolved_fallback(fallback, config_path), "fallback"


def _resolved_fallback(explicit: str | None, config_path: Path | None) -> str:
    """Pick fallback bucket. Explicit override > config default > built-in."""
    if explicit:
        return explicit
    if config_path is None:
        config_path = CONFIG_PATH
    return load_settings(config_path).get("default_bucket", DEFAULT_FALLBACK_BUCKET)


def preview_classification(projects: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Return {bucket: [(leaf, raw), ...]} for displaying first-run preview."""
    rules = load_rules()
    out: dict[str, list[tuple[str, str]]] = {}
    for proj in projects:
        cat = classify(proj, rules)
        leaf = normalize_project_name(proj) or proj
        out.setdefault(cat, []).append((leaf, proj))
    return out


def ensure_default_config(path: Path | None = None) -> bool:
    """If no config exists, scaffold a commented template. Returns True if written.

    ``path`` resolves to module-level ``CONFIG_PATH`` at call time when
    omitted, so test monkeypatches take effect.
    """
    if path is None:
        path = CONFIG_PATH
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

# Narrative response language for `claude -p` outputs. Free-form — the value
# is dropped straight into the prompt as `Respond in <language>.`
# Examples: "Traditional Chinese", "日本語", "Spanish".
# Precedence (high → low): --lang flag · $CCSTORY_LANG · this field ·
# ~/.claude/CLAUDE.md · ~/.claude/settings.json language · system locale · English.
# language = "Traditional Chinese"

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
    language: str | None = None,
    projects: dict[str, str] | None = None,
) -> str:
    """Re-render config.toml from scratch from in-memory state.

    Comments and section ordering are stable across writes so successive
    `category set/unset` commands produce minimal diffs. The optional
    ``[projects]`` alias table (#69) is preserved verbatim and only emitted
    when non-empty, so a config that never used aliases renders byte-for-byte
    as before.
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
        "# Narrative response language. Free-form — passed straight to claude -p.",
        "# Examples: \"Traditional Chinese\", \"日本語\", \"Spanish\". Comment out",
        "# or leave empty to inherit from $CCSTORY_LANG / CLAUDE.md / system locale.",
        f'language = {_json.dumps(language)}' if language else '# language = ""',
        "",
    ]
    if projects:
        lines.append(
            "# Fold variant folder-leaf names onto one canonical project (#69)."
        )
        lines.append("[projects]")
        for variant in sorted(projects):
            lines.append(f'{_json.dumps(variant)} = {_json.dumps(projects[variant])}')
        lines.append("")
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


def _load_state(
    path: Path,
) -> tuple[dict[str, list[str]], dict[str, str], str, float, str | None]:
    """Read existing config (or defaults) into
    ``(categories, projects, default_bucket, quota, language)``.

    ``projects`` is the raw ``[projects]`` alias table, preserved so a
    ``category set/unset`` re-render never silently drops the user's aliases.
    """
    cfg = _load_toml(path) or {}
    raw_cats = cfg.get("categories") if isinstance(cfg.get("categories"), dict) else {}
    categories: dict[str, list[str]] = {}
    for bucket, kws in raw_cats.items():
        if isinstance(kws, list) and all(isinstance(k, str) for k in kws):
            categories[str(bucket)] = [k for k in kws]
    raw_projs = cfg.get("projects") if isinstance(cfg.get("projects"), dict) else {}
    projects: dict[str, str] = {
        str(k): str(v) for k, v in raw_projs.items() if isinstance(v, str)
    }
    default_bucket = str(cfg.get("default_bucket", DEFAULT_FALLBACK_BUCKET))
    try:
        quota = float(cfg.get("monthly_quota_usd", DEFAULT_MONTHLY_QUOTA_USD))
    except (TypeError, ValueError):
        quota = DEFAULT_MONTHLY_QUOTA_USD
    lang = cfg.get("language")
    if not isinstance(lang, str) or not lang.strip():
        lang = None
    else:
        lang = lang.strip()
    return categories, projects, default_bucket, quota, lang


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

    categories, projects, default_bucket, quota, language = _load_state(path)
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
        _render_config(categories, default_bucket, quota, language, projects),
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

    categories, projects, default_bucket, quota, language = _load_state(path)
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
        _render_config(categories, default_bucket, quota, language, projects),
        encoding="utf-8",
    )
    return categories, missing


def list_user_categories(
    path: Path | None = None,
) -> dict[str, list[str]]:
    """Return the current user `[categories]` mapping (empty if none)."""
    if path is None:
        path = CONFIG_PATH
    categories, _, _, _, _ = _load_state(path)
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
