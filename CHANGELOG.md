# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `get_trend` MCP tool — the fourth and final tool from #35: per-period
  activity series over the last `count` weeks/months (oldest first) with
  active hours, cost, and per-category hours per point. Cache-only under
  every parameter combination (like `compare_to_previous`), applies the
  same config `[prices]` override as every other cost-reporting entry
  point, and clamps `count` to 1..24.

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

[Unreleased]: https://github.com/atomchung/ccstory/compare/v0.5.2...HEAD
[0.5.2]: https://github.com/atomchung/ccstory/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/atomchung/ccstory/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/atomchung/ccstory/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/atomchung/ccstory/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/atomchung/ccstory/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/atomchung/ccstory/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/atomchung/ccstory/releases/tag/v0.3.0
