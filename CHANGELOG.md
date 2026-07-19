# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.1] - 2026-07-19

### Fixed

- The recap terminal card no longer assigns two different buckets the same
  bar color. `color_for()` hashes each bucket independently into a 6-color
  palette, so a report with several custom `[categories]` aliases (none
  matching the built-in English bucket names) had good odds of two buckets
  landing on the same color. A new `colors_for()` resolves the whole set
  together: each unknown bucket walks forward from its hash slot until it
  finds a color no sibling bucket in the same render has already claimed.
- The recap terminal card's "What you did" section no longer prints the
  overall narrative's raw `**bold**` / `- bullet` markup verbatim, nor its
  full multi-paragraph length. #98 reshaped the overall narrative into 2-4
  goal threads (bold header + supporting bullets), but the terminal card's
  renderer was never updated to match, so it dumped the whole thing as
  plain dim text — literal asterisks and all. The card now shows just each
  thread's bold header; full bullets stay in the markdown report, one line
  away via "Full report →".
- The overall goal-thread narrative no longer pads every thread to the
  maximum 3 bullets regardless of how much there was to say. Real cached
  narratives showed zero variance — every single thread across 8+ weekly
  windows landed on exactly 3 bullets — while the per-category narrative's
  2-4 range (deliberately widened in #108) already varied naturally with
  content. `_OVERALL_PROMPT` now explicitly says to use the minimum bullets
  the thread supports rather than splitting one outcome into parts to hit
  the cap; a live regeneration against real session summaries now produces
  2-4 bullets per thread (avg 2.5) instead of a flat 3. `_CATEGORY_PROMPT`
  is untouched — its own real-data variance (and #108's incident-visibility
  guarantee) were already healthy. Changing the prompt text invalidates
  cached overall narratives via the existing content-fingerprint check (no
  version bump needed); the next run regenerates each window once.
- `--help`, in-progress status messages, and docstrings no longer call the
  overall narrative a "3-sentence synthesis" — stale since #98 reshaped it
  into goal threads; left uncorrected everywhere except the terminal card
  itself until now. Also fixed in README.md, which repeated the same stale
  claim in two places.
- `_narrative_headers()` no longer leaks a header's own nested `**bold**`
  (e.g. around a version number) as literal asterisks — the outer
  `^\*\*(.+)\*\*$` match is greedy, so it captured inner `**...**` marks
  verbatim before this fix strips them too. Also simplified its return type
  from `list[str] | None` to plain `list[str]` (`[]` instead of `None`) —
  the caller only ever checked truthiness, so the distinction was unused.
- `render_comparison_block`'s `colors` parameter is required now instead of
  defaulting to `None` with an internal re-derivation — that fallback was
  unreachable from the only real call site and untested; keeping it invited
  a bucket to silently get a different color there than in the rest of the
  card if a future caller ever hit it.
- `ccstory category set`/`unset` confirmation lines now use the same
  collision-free `colors_for()` as `category list`, instead of the old
  per-bucket `color_for()` — previously the same bucket name could render
  in two different colors depending on which subcommand printed it. Handles
  the edge case where the bucket being colored was just emptied out and
  dropped from the config by the same command (the color map is built from
  the union of the remaining buckets and the one(s) about to be printed,
  not just what's left in the config).

## [0.6.0] - 2026-07-18

### Added

- Two-layer classification, layer 3 of 3 — MCP `get_recap` exposes the
  per-project breakdown (#69). Each `categories[]` entry gains an additive
  `children` array of `{name, active_hours}`, biggest first — the compact
  layer-2 view for MCP clients. Additive only: existing fields are unchanged
  and `get_trend` / `compare_to_previous` stay layer-1. README's Categories
  section is rewritten to document the two-layer (area → project) model,
  the exact-membership vs token-needle tiers, the `[projects]` alias table,
  and that area overrides never touch a session's project.
- Two-layer classification, layer 2 of 3 — read-time area → project rollup
  and two-layer presentation (#69). Each `CategoryRollup` now carries a
  `projects` list (biggest first), grouped by the alias-folded project leaf
  and scaled by the same wall-clock factor as its area, so project hours
  sum back to the area total. Computed entirely at read time from the
  sessions already in hand — **no new cache family, no fingerprint, no
  migration** (the #118-class regression the RFC guards against). The
  terminal card gains a "By project" block for areas that split across more
  than one project (layer-1 bar chart unchanged); the markdown report shows
  an indented top-3-projects line per area; `--json` gains an additive
  `projects` array inside each bucket (`schema_version` stays 1). trend /
  compare stay layer-1 only.
- Two-layer classification, layer 1 of 3 — resolver v2 (#69). The area
  resolver now checks **exact membership** first (the project's normalized
  leaf listed verbatim under an area) before the existing token-needle
  fuzzy tier, so a project explicitly assigned to an area wins over an
  earlier area that merely matches a token — the section-ordering hacks
  token matching forced can now be deleted. Both tiers still report as
  `user_rule`; existing token-needle configs resolve byte-identically
  (exact membership is always also a token match, so the only behavior
  change is the intended ordering fix). Adds an optional `[projects]`
  alias table (`alias_fold` / `project_identity`) to fold variant
  folder-leaf names onto one canonical project, and a load-time warning
  when a project is listed under more than one area (first wins).
  `category set/unset` now preserves the `[projects]` table across
  re-renders.
- `get_trend` MCP tool — the fourth and final tool from #35: per-period
  activity series over the last `count` weeks/months (oldest first) with
  active hours, cost, and per-category hours per point. Cache-only under
  every parameter combination (like `compare_to_previous`), applies the
  same config `[prices]` override as every other cost-reporting entry
  point, and clamps `count` to 1..24.

### Fixed

- The overall-narrative cache no longer misses on every rerun of the
  active window (#121). Its fingerprint embedded per-category hours at
  0.1h precision, and the primary flow runs ccstory from inside a live
  Claude Code session — so the current week/month drifted ~6 minutes
  between any two runs and re-burned a ~90s `claude -p` call each time.
  The fingerprint now coarsens hours to whole hours (sub-hour drift stays
  a cache hit; a whole-hour crossing still regenerates); the prompt the
  LLM sees keeps 0.1h precision. The definition change invalidates
  existing overall aggregates once (a few calls per window).
- Sessions whose model-proposed bucket is rejected by validation (a
  one-off name, or the vocabulary cap) no longer re-burn a `claude -p`
  chunk on every future run (#120). They are now negative-cached at the
  fallback bucket under the current input fingerprint — bounded cost, and
  any category-config change rotates the fingerprint and gives them a
  fresh shot at a real bucket. Model omissions and parse failures stay
  uncached on purpose: those are transient, and retrying them is correct.

## [0.5.2] - 2026-07-18

### Added

- `ccstory.recap.build_recap()` — the one-call library entry point for the
  full recap pipeline (#110). Returns a `RecapResult` with the rich objects,
  the rendered markdown, the report path, and a `.to_json()` envelope
  matching `--json` stdout. The CLI's default flow is now a thin shell over
  it, so programmatic consumers and the CLI stay behaviorally identical by
  construction. Empty windows raise `RecapUnavailable` instead of exiting.
- `ccstory mcp` — a read-only MCP server (#35), install via
  `pip install 'ccstory[mcp]'`. Three v0 tools over stdio — `get_recap`,
  `compare_to_previous`, `list_categories` — let any MCP-aware agent query
  a recap live instead of shelling out to the CLI. Each is a thin wrapper
  over the same semi-stable functions above, returning a third, more
  compact JSON shape (top 5 sessions, not the full list). Default
  `classify="folder"` and `allow_llm=False` never fire a fresh `claude -p`
  call. `get_trend` isn't included yet — see the issue for status. See
  README "MCP server" for setup.

### Changed

- The recap orchestration moved from `ccstory/cli.py` into
  `ccstory/recap.py` (`parse_window`, summary backfill, bucket resolution,
  narrative synthesis, comparison, artifacts, render). CLI flags and
  behavior are unchanged.
- `recap.CLAUDE_P_SEC_PER_SESSION` is now `recap.CLAUDE_P_SEC_FALLBACK`, and
  only seeds the first run (#113). It was never part of the documented
  library API.

### Fixed

- The overall-period narrative no longer hardcodes "Respond in Traditional
  Chinese" in its prompt (#116). The rule leaked into `_OVERALL_PROMPT` in
  v0.5.0 and overrode the resolved language directive (`CCSTORY_LANG` >
  `config.toml` > `CLAUDE.md` > `settings.json` > locale) for the overall
  synthesis only — non-Chinese users got a Traditional-Chinese overall
  narrative above correctly-localized category narratives. Language
  selection is back to `language_directive()` alone; cached overalls
  regenerate on the next `--llm-narrative` run via the prompt fingerprint.
- Upgrading a pre-0.5.1 cache no longer orphans existing content
  classifications (#118). Migration 2 stamped legacy rows with an empty
  fingerprint that no read path matches, so every pre-upgrade
  classification silently stopped resolving: recaps re-burned `claude -p`
  for sessions that were already classified, and the cache-only trend /
  compare paths permanently degraded old windows to folder/fallback
  buckets. Migration 3 adopts those rows under the current fingerprint
  (the same no-re-burn contract migration 1 applies to `prompt_version`),
  which also retroactively resurrects caches on installs that already
  upgraded — the rows were still there, just unreadable. Aggregate and
  comparison narratives are deliberately re-synthesized instead: their
  prompts changed after v0.5.1, and that costs a few calls per window,
  not one per session.
- The `--llm-narrative` ETA no longer over-states by ~6x (#113). It
  multiplied the session count by a hard-coded 40s — a cold start profiled
  once on one M1 Pro — while a backfill's calls run back-to-back and land
  ~6-8s. A real 127-session run announced `ETA ~85 min` and finished in
  ~15, which inverted the warning's purpose: it exists to save users from a
  silently-hanging job, not to scare them off a short one. The estimate now
  measures `claude -p` from the gaps between `auto` rows already in the
  cache. A genuine first run has no history to read and still shows the old
  constant, labeled `first-run estimate` rather than passed off as measured.
- A corrupt, locked, or newer-schema `~/.ccstory/cache.db` no longer kills
  the host process (#119). `_connect()` raised `SystemExit` — right for the
  CLI, fatal for in-process consumers (`build_recap()` library callers, the
  MCP server), since `except Exception` cannot catch a `BaseException`. It
  now raises `session_summarizer.CacheUnavailable`; the CLI catches it at
  the entry point and keeps the exact old behavior (message to stderr,
  exit 1). A transient `database is locked` is also no longer misreported
  as corruption with `rm ~/.ccstory/cache.db` advice — it now says another
  process holds the cache and to retry.

## [0.5.1] - 2026-07-14

### Added

- A tag-driven release workflow now validates, builds, and publishes the wheel
  and source distribution through PyPI Trusted Publishing before creating the
  matching GitHub Release (#51).
- Human-readable recaps and trends now warn when their pricing snapshot is more
  than 90 days older than the report window (#91).
- The shared SQLite cache now uses ordered, transactional schema migrations so
  upgrades preserve existing narratives and classifications (#101).

### Changed

- Zero-cost fallback narratives now show the first and last user-message
  endpoints, making the session arc more useful without an LLM call (#70).
- The README now documents actual Claude CLI call counts, latency/quota
  behavior, and the exact network metadata used by What shipped (#59, #104).

### Fixed

- `claude -p` calls that return silently empty now retry once without
  `--no-session-persistence`, recovering narratives that would otherwise be
  dropped (#99).
- The pytest suite now isolates every test from the developer's real
  `~/.ccstory`, `~/.claude`, and locale settings (#100).
- Cached aggregate, comparison, and content-classification LLM outputs now
  regenerate when their prompt or relevant category config changes (#65,
  #102).
- Content classification carries accepted bucket names across 80-session
  batches and enforces one run-wide vocabulary limit before caching (#63).
- Date labels and subagent-path exclusion now behave consistently on Windows,
  macOS, and Linux; CI includes Windows coverage (#103).

## [0.5.0] - 2026-07-13

### Added

- **What shipped** section: every report now includes per-repo output metrics
  for the repos you actually worked in during the window — commits, merged
  PRs, releases, GitHub stars, and PyPI downloads.
- `--json` / `--format=json`: structured JSON output for scripting and
  automation, with a `summary_source` field on each session recording
  whether its summary was `auto`, `llm`, or reused from cache.
- `--narrative overall|per-category|both`: per-category narrative synthesis
  alongside (or instead of) the overall one.
- Free-form narrative language selection.

### Changed

- Overall-period synthesis reframed as **goal threads** instead of a flat
  category log — ties what you did back to what you were actually working
  toward, rather than just listing categories.
- `--llm-narrative` now upgrades/refreshes cached narratives instead of
  freezing them once generated.

### Fixed

- Assorted low-hanging bugs + terminal-theme-friendly colors.

## [0.4.2] - 2026-07-11

### Fixed

- **`DEFAULT_PRICES` was ~2-3x overstating opus cost**: opus was still
  priced at the pre-4.6 $15/$75 tier — current rates are $5/$25 per MTok.
  Haiku was $0.80/$4 (~20% *under*stated) — now $1.00/$5.
- Added fable/mythos price entries ($10/$50): sessions on these models
  previously priced as $0.
- Price snapshot date bumped `2026-01` → `2026-07` (shown in every report
  footer).

## [0.4.1] - 2026-05-20

### Fixed

- Markdown report now renders cleanly to stdout when run under Claude Code.

## [0.4.0] - 2026-05-20

### Added

- Unified content-classification resolver, with three `init` modes
  (Quick / Deep / Skip).
- User `[categories]` fed into the content classifier prompt.

### Changed

- Relative date-range windows (e.g. "this week") now show human-readable
  labels instead of raw dates.
- Launch polish: language fallback, `init` UX, report polish, chunked
  progress output.
- README: install via PyPI instead of a `git+` URL.

## [0.3.0] - 2026-05-17

Initial tagged release.

### Added

- Cross-period narrative synthesis.
- `--for=obsidian` markdown export flavor.
- Category CLI + `--refresh` flag.
- pytest suite + GitHub Actions CI.

### Changed

- Dropped the plugin layer — CLI-only going forward.
- "What you did" collapsed into a single 3-sentence recap; narrative
  language now follows `CLAUDE.md`.
- Category surfaced in the CLI, with louder warnings on silent
  classification failures.

[Unreleased]: https://github.com/atomchung/ccstory/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/atomchung/ccstory/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/atomchung/ccstory/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/atomchung/ccstory/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/atomchung/ccstory/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/atomchung/ccstory/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/atomchung/ccstory/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/atomchung/ccstory/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/atomchung/ccstory/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/atomchung/ccstory/releases/tag/v0.3.0
