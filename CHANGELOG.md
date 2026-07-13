# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/atomchung/ccstory/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/atomchung/ccstory/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/atomchung/ccstory/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/atomchung/ccstory/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/atomchung/ccstory/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/atomchung/ccstory/releases/tag/v0.3.0
