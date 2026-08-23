# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `ccstory project list [--window all|week|month|YYYY-MM] [--agent AGENT]
  [--json]` — read-only discovery of the canonical project identities already
  observed in local session history (window defaults to `all`). Each row is
  `project_id` / `last_seen` / `session_count` / `agents[]`; output is bounded
  and deterministic (most-recently-observed first, canonical id tie-break),
  with an explicit display cap in the terminal and a hard maximum in `--json`.
  Never exposes a transcript path or session id, makes zero model calls, and
  scans through the existing provider snapshot seam rather than building a
  second transcript index. `ccstory goal set` now reports, per linked
  project, whether it matches an observed workspace (`observed: true`) or is
  currently unobserved (`observed: false`, with the value preserved
  unchanged) — plus optional deterministic close-spelling `candidates` shown
  only as a suggestion, never auto-applied. Add `--json` to `ccstory goal
  set` to get this feedback as a machine-readable envelope. (#222)
- A deterministic project-attribution engine (`ccstory.project_attribution`)
  that resolves an owner-initiated session to a canonical project through
  inspectable rules, and either accepts, reports a conflict, or abstains.
  It makes zero model calls. No recap, report, JSON, or MCP surface loads it
  yet — profile loading is a separate product change gated on the held-out
  evaluation in #223. (#224)
- Maintainer-only evaluation tooling for that engine:
  `scripts/project_attribution_sample.py` (stratified local sampling) and
  `scripts/project_attribution_eval.py` (rule mining and scoring). Real
  evaluation artifacts stay in the gitignored `.local-eval/`; the public
  test suite uses synthetic projects and transcripts only.
- `SessionStat.is_delegated` / `delegation_source`, carried through to
  `SessionSlice`. The Codex provider now recognizes the `Claude Code`
  originator, `<codex_delegation>`, and `<task>` wrappers. Owner-intent
  workflows can exclude dispatched transcripts while usage and cost
  accounting keep them. No existing surface reads these fields, so recap,
  report, JSON, and MCP output is unchanged. (#136, #224)
- A shared `InteractionMode`/`InteractionProvenance` model
  (`ccstory.provenance`) plus a pure, deterministic resolver that classifies
  how a physical session was driven — interactive, delegated, scheduled,
  system_review, or unknown — from the authoritative fields `SessionStat`/
  `SessionSlice` already carry, never from prompt or transcript wording.
  `UNKNOWN` is a first-class result when metadata is insufficient; a
  conflicting signal combination lowers confidence instead of guessing.
  Includes a labeled-fixture evaluation harness (`evaluate_fixtures`) that
  scores precision/recall/confusion per mode. `resolve_session_provenance`
  is a callable, session/window-pure entry point that recap, trend, MCP,
  and library callers can adopt later; nothing calls it yet, and no
  existing engagement or headline-time semantics changed. Provider signal
  adapters (a `system_review` source in particular — no bundled provider
  exposes one today) and any output wiring are separate, later slices.
  (#136)

### Changed

- `main` now carries the post-release development version
  `0.8.3.dev0` instead of the just-shipped `0.8.2`, so `ccstory --version`
  on a source checkout or worktree install can no longer read identically
  to the tagged `v0.8.2` PyPI artifact. A release PR converts this to a
  clean version immediately before publishing; a follow-up PR advances
  `main` to the next `.dev0` right after. (#78)
- Refreshed the vendored LiteLLM pricing table. Adds `claude-mythos-5`,
  `claude-mythos-preview`, `gpt-5.6-cyber`, `gemini-3.7-flash`, and
  `gemini-3.1-flash-lite-image`; picks up price cuts for `gpt-5.6`,
  `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, and `gemini-3.6-flash`.
  The table had been stale since 2026-07-26, so costs for those models were
  overstated — `gpt-5.6-luna` by 5x and `gemini-3.6-flash` by 2x.
- `AntigravityProvider.collect_sessions()` now parses each candidate's cheap
  transcript facts (`ParsedSessionCore`: identity, timestamps, message
  counts, engagement) before resolving its working directory, and rejects
  child, empty, out-of-window, and disengaged candidates on those facts
  alone. Only a session that survives every filter opens the companion
  `conversations/<session_id>.db` to resolve CWD/project — a transcript
  record that already carries its own `cwd` skips that DB entirely. Rejected
  candidates now trigger zero conversation-DB opens; previously every
  candidate past the coarse per-file mtime filter paid for one regardless of
  outcome. `SessionStat`, project/worktree attribution, native titles, and
  usage for sessions that are ultimately included are unchanged. (#180)
- Internal: `session_summarizer` cache calls can now share one verified/
  migrated SQLite connection across a scope (`with cache_session():`)
  instead of opening a fresh connection per call, cutting a 100-call
  sequence (e.g. a backfill loop) from 100 connection opens to 1. A direct
  call made outside any scope keeps opening its own short-lived connection,
  unchanged from before. The scope is process/thread-bound and never
  reused across a `DB_PATH` change, a fork, or another thread; an exception
  rolls back before the scope's connection is reused or closed. Corrupt,
  locked, and newer-schema recovery messages are unchanged. Not yet wired
  into `build_recap()`'s pipeline — this lands the scoping primitive and
  its safety guarantees; wiring it through recap's summary/classification/
  aggregate/comparison phases is a separate follow-up. (#175)
- Internal: `ccstory/cli.py` no longer imports every subcommand's
  dependencies at module load. Each handler (`category`, `goal`,
  `goal-history`, `project`, `init`, `mcp`, and the default recap flow) now
  imports only the modules it actually runs. Previously, dispatching to any
  subcommand — including light ones — imported the narrator cache
  (`session_summarizer`), the full recap/report pipeline, and the provider
  registry regardless of need. `ccstory category list` and `ccstory mcp
  --help` no longer import `session_summarizer`, `mcp_server`, `providers`,
  `recap`, or `report` at all; `goal list`, `goal-history`, and `init
  --skip` see similar, narrower reductions. No command's output, exit code,
  or argument parsing changed. (#177)

### Fixed

- The first-run classification preview no longer promises categories the
  very next report fails to reproduce. `resolve_session_bucket()` (and its
  cache-only counterpart used by `week`/`trend`/MCP) never consulted the
  built-in `DEFAULT_RULES` folder keywords — only an explicit `[categories]`
  config or a cached/fresh content classification could set a bucket, so a
  brand-new user with no config and no configured narrator saw the default
  recap's "First run — default bucket preview" table promise a multi-
  category split (backed by the built-ins), immediately followed by a
  report where every session collapsed into the single scalar
  `default_bucket`. `folder` mode had the same gap:
  `user_rule > scalar fallback`, skipping the built-ins entirely. A shared
  deterministic built-in-folder tier (`categorizer.builtin_rule_match()` /
  `builtin_or_fallback()`, reported as the new `category_source` value
  `builtin_rule`) now sits between content classification and the scalar
  fallback in every mode: `folder` is `user_rule > builtin_rule > fallback`;
  `hybrid` applies the same built-in tier once a session's `needs_llm`
  signal cannot be resolved by fresh content classification; `content`
  is unchanged (`cached content > fresh content > scalar fallback`, no
  built-in tier — it stays content-only by design). The first-run preview
  now resolves through this same folder contract instead of a separate
  merged-rules pass, so it can never diverge from the report again.
  Built-in rules still never outrank an explicit user rule or a content
  classification. (#214)
- The `refresh-prices` workflow wrote its diff summary to the repository
  working tree, so `create-pull-request` swept `diff_summary.txt` into the
  pricing commit. It now writes to the runner temp directory.

## [0.8.2] - 2026-08-09

### Changed

- Report-local timezone resolution moved from `goal_history` into a shared
  `ccstory.local_time` module, so every surface that maps timestamps onto
  local calendar dates resolves the host timezone the same way.
- The bounded-window session contract moved from `ccstory.goals` into
  `time_tracking.validate_window_session()`. It was previously enforced only
  for goal activity, through a private helper; every consumer of already-
  bounded `SessionStat` / `SessionSlice` facts can now check it. (#234)
- `GoalActivitySeries.coverage_status` is now derived from its buckets rather
  than an init parameter validated against the same derivation, and the
  managed GoalContext default path has a single call-time definition instead
  of an import-time constant beside a call-time function. (#234)

### Fixed

- Goal attribution no longer silently drops all activity when `[projects]`
  contains a chained alias (an alias target that is itself an alias key).
  `attribute_goals()` folded already-canonical session projects one time more
  than the goal side, so every contribution landed in `unattributed` with no
  error. Aliases are now applied symmetrically to both sides. Configurations
  without aliases, or with unchained aliases, are unaffected. (#229)
- Recap goal attribution no longer splits contributions at local midnight with
  a fixed-offset timezone. The recap window's tzinfo carries only the offset in
  effect at run time, so a window reaching across a daylight-saving transition
  attributed an hour of activity to the wrong local date — and disagreed with
  `ccstory goal-history`, which already resolved historical rules correctly.
  Both surfaces now share one resolver. Non-DST timezones are unaffected.
  (#230)
- Report and trend window boundaries are now built with historical timezone
  rules. `parse_window()` and `collect_trend()` derived their local timezone
  from `datetime.now().astimezone()`, a fixed offset frozen at the current
  daylight-saving season, so `ccstory 2026-01` requested in summer started and
  ended an hour off its true local midnight — shifting which sessions fell
  inside the window. Non-DST timezones are unaffected. (#233)

## [0.8.1] - 2026-08-06

### Added

- Added `ccstory goal-history` and MCP `get_goal_activity_history` as one
  shared, read-only JSON projection over deterministic weekly goal activity.
  Requests default to 4 completed local ISO weeks, reject counts outside 1..24,
  collect every window in one provider snapshot, and reuse the existing
  window-pure `build_goal_activity_series()` / `attribute_goals()` accounting.
  Results include zero-activity effective goals, exclusive/shared/unattributed
  hours, safe source provenance, explicit non-additive/disclaimer semantics,
  and `coverage_status: unavailable` rather than mistaking token-usage coverage
  for activity completeness. The surface never writes reports or GoalContext,
  calls a narrator, or exposes session ids, prompts, transcript/source paths,
  corrections, or internal evidence.
- Added `ccstory goal set/list/unset` for the private managed
  `~/.ccstory/goals.toml` store. Recaps select one source by
  `--goals-file` (explicit) → config `[goal_context].path` → managed-default
  precedence; external sources are read-only.
- Goal source selection now returns a runtime capability adapter: the managed
  source explicitly owns read/upsert/delete, while explicit and configured
  TOML sources are structurally read-only. This gives agents and library
  integrations one provider-neutral access seam without granting write
  authority through GoalContext data or configuration.
- Recaps can now project a selected GoalContext v1 across the terminal card,
  Markdown report, full JSON envelope, and compact MCP `get_recap` result.
  Shared per-goal hours are explicitly non-additive, global coverage buckets
  count each contribution once, unattributed activity stays visible, and every
  surface states that contribution evidence is not progress, completion,
  outcome, or acceptance. Public provenance includes only a sanitized source
  kind and content fingerprint, never the source path. An absent or empty goal
  context adds no section or JSON key.
- MCP `get_recap` reloads the configured or managed goal source for every
  call, remains read-only, and normalizes invalid goal sources into its usual
  tool-error shape. Trend and comparison tools remain goal-free.

### Changed

- The narrator-inferred cross-category `What you did` concept is now called
  **work themes** in the active prompt, CLI help, documentation, and tests, so
  it cannot be confused with owner-authored GoalContext goals. Existing public
  narrative fields and behavior are unchanged.

### Fixed

- Deleting the final managed goal now serializes a strict, reloadable
  `goals = []` GoalContext instead of leaving a schema that omitted the
  required goals collection.

## [0.8.0] - 2026-07-28

### Changed

- Recap and previous-window comparison are now window-pure. A session that
  crosses a report boundary previously contributed its **whole** self to both
  windows: a Monday-morning session that began Sunday night added all of its
  active time, message counts, and summary text to both the previous week and
  the current one. Each window now sees only the messages, active time, and
  evidence that actually happened inside it, so the two windows' figures sum
  to the session rather than double-counting it. Sessions fully inside one
  window are unaffected and their output is unchanged.
- A boundary-crossing session no longer reuses a summary generated from the
  other window's conversation. Until prose exists for a given window, that
  window falls back to its own bounded evidence rather than describing work
  the report is not reporting.
- Renamed the `session_summaries.source` vocabulary onto one axis — how the
  summary text came to be — rather than mixing in who wrote it or why:
  `record` → `provided`, `auto` → `generated`, `fallback` → `extracted`,
  `skipped` → `no_evidence`. This is an observable change to the
  `summary_source` field in the full recap JSON envelope (`--json`,
  `RecapResult.to_json()`) and to any consumer reading `session_summarizer`
  cache rows directly. An existing `~/.ccstory/cache.db` is migrated
  automatically the next time it is opened: only these four exact legacy
  values are rewritten, and caller-defined values (e.g. `cloud:<branch>`)
  are left untouched. `session_summarizer.upsert()` still accepts the
  legacy values and silently maps them onto the new names, so external
  callers that have not updated yet keep working unchanged.
- The cache schema version moves to 6. As with every previous schema bump the
  migration is one-way: once a cache has been opened by this version, an
  older ccstory will refuse to read it rather than misinterpret it. Delete
  `~/.ccstory/cache.db` to downgrade — summaries regenerate, but any
  `provided` row supplied by an external tool would be lost with it.
- `ccstory trend` is now window-pure too. A session that crosses a period
  boundary previously counted its **whole** self toward whichever period held
  its start time and nothing toward the other: a Sunday-night session that
  ran past midnight into Monday added all of its active time to the earlier
  week's bar and none to the later one. Each period's sparkline/bar now shows
  only the active time and messages that actually happened inside it, so a
  boundary-crossing session's two halves show up in both periods instead of
  being lumped into one. A session fully inside one period is unaffected.
  Token and cost figures for each period are unchanged — trend now reads them
  from one shared scan of the whole range instead of re-scanning per period,
  but the numbers themselves come from the same exact per-record usage events
  as before.
- Which sessions reach the narrator is now an explicit, explainable decision
  rather than a side effect of ordering. Previously the overall, per-category,
  and comparison prompts were assembled by joining every eligible summary and
  cutting the result at a fixed character count. Everything past the cut
  vanished no matter how short or how significant it was, and the cut landed
  wherever the provider registration order and the filesystem's directory
  listing happened to put it — so a quiet project could be permanently
  invisible in your weekly narrative for no reason you could see. ccstory now
  picks representatives deliberately: one session per provider, one per
  project, one per remaining day, plus a few carrying error/test/security
  signals, then fills the remaining room by active time. An entry that does
  not fit is skipped and the next one is still considered, so a short summary
  behind a long one is no longer lost.
- Per-session narrative work is ordered the same way. When the narrator's
  time budget runs out mid-run — normal on a large window — the sessions that
  keep every provider, project, and day represented are attempted first,
  instead of whichever the filesystem listed first.
- Cached period narratives now key on the sessions that actually represented
  the window rather than on the whole population, so adding a session that
  did not change the narrative no longer forces a regeneration. Existing
  cached rows regenerate once on first run under this version.

### Added

- The full recap JSON (`--json`, `RecapResult.to_json()`) gains an additive
  `narrative.sampling` block reporting how representatives were chosen:
  policy version, population and selected counts, per-dimension coverage
  targets and hits, and a histogram over a fixed reason vocabulary. It names
  no session — the ids sampling reasons about are internal cache keys, not
  the physical sessions you know — and carries no transcript text, prompt,
  path, or correction. MCP results gain a compact counterpart with counts and
  a single `coverage_complete` verdict.

### Removed

- Removed the automatic import that ran on every recap and pulled cached
  summaries from `~/.claude/session_summaries.db`, a file written by one
  specific personal tool; every other install unconditionally probed for it
  and found nothing. Use `ccstory.session_summarizer.upsert(session_id,
  summary, source="provided")` instead to feed ccstory an authoritative
  externally supplied summary — see the README's "Library usage" section.
  Rows already imported into `~/.ccstory/cache.db` are unaffected.

### Fixed

- Projects, categories, and top sessions with equal rounded activity could
  come back in a different order between two runs over the same data. Hours
  round to 0.1, so ties are common, and the order then fell out of whichever
  provider was registered first and whichever file the operating system
  listed first — visible as the "By project" line and the detailed session
  list reshuffling for no reason. All three now break ties by name (or, for
  sessions, by start time and id), so the same data always renders the same
  way.

### Notes

- Reported active time and message counts can therefore drop slightly for
  windows that contain a boundary-crossing session. That is the correction,
  not a regression: the previous figures counted out-of-window work.
- Token and cost attribution are unchanged. Those come from exact per-record
  usage events and were never derived from session boundaries.
- `collect_sessions()` and `collect_sessions_for_windows()` keep their
  documented behavior as raw overlap primitives: a crossing session is still
  returned unclipped in both windows. Window purity is a property of the
  recap/report layer.

## [0.7.3] - 2026-07-28

### Added

- Full recap JSON and MCP top-session entries now expose a public-safe
  `summary_evidence.status` (`current`, `stale`, `legacy`, `unavailable`, or
  `not_applicable`). Markdown detailed history adds a compact marker for
  stale, legacy, and unavailable cached summaries; no evidence fingerprint or
  transcript content is exposed.
- Configurable local narrative backends. The default fallback order is Claude
  Sonnet, Codex GPT-5.6 Terra, then Antigravity Gemini 3.6 Flash Low; every
  invocation passes an explicit model. `~/.ccstory/config.toml` can reorder,
  disable, or replace those choices, and recap JSON/Markdown/MCP now disclose
  the provider and model that actually produced aggregate prose.

### Changed

- Claude and Antigravity narrator retries now stay within the caller's exact
  per-call deadline, including compatibility and transient-error retries.
- Per-session automatic-summary cache identity now includes the exact bounded
  evidence sent to the narrator, its project, and a versioned per-lane evidence
  policy. Selected `--llm-narrative` sessions are extracted once and observed
  before cache preflight, so transcript growth rotates identity even when a
  good auto row exists. Failed, omitted, or budget-exhausted refreshes preserve
  the old summary/source/timestamp/basis and leave honest stale provenance.
  Legacy rows migrate lazily without a global transcript scan or narrator
  re-burn; human `record` rows remain authoritative even under force refresh.
- Stale and legacy automatic summaries remain visible only in detailed
  history. Fresh Top focus, content classification, category/overall
  synthesis, and comparison prose consume only current automatic summaries or
  authoritative human records.
- Top-level `ccstory --help` and `ccstory --version` now use a lightweight
  stdlib-only bootstrap, avoiding imports of transcript providers, the recap
  pipeline, and Rich rendering until a real command runs.
- Antigravity subagent discovery now caches its transcript pass and extracts
  legacy UUID references directly, avoiding a comparison against every known
  session ID for each `INVOKE_SUBAGENT` record. Regression tests now protect
  bounded Claude discovery, CWD lookup reuse, and unchanged-cache migration
  verification.
- The terminal card now gives Top focus a concise muted `subject: outcome`
  detail: it uses a short project label and the first problem/result clause
  from only the strongest work summary, while Markdown and JSON retain the
  full project metadata and evidence. Repo activity is rendered as its own
  title plus a separate metrics line for faster scanning.
- A normal recap now defaults to per-category narrative. Every eligible
  category remains visible through a deterministic local fallback when the
  narrator is unavailable or its lane deadline is reached; report and JSON
  expose that fallback provenance.
- Top focus now derives a compact category/project narrative from the strongest
  sessions rather than displaying a representative session's raw prompt or
  command-like fallback text.
- Every recap LLM lane now shares a 90-second wall-clock budget with a
  45-second per-call deadline. Per-session narration begins with a 10-session
  probe and adapts later batches between 10 and 40 sessions; completed rows
  remain available while budget-exhausted work falls back locally.
- Narrative provenance now includes metadata-only lane timing, provider
  attempts, fallback outcome, budget exhaustion, and coarse batch progress.
- Reused unchanged parsed `config.toml` within a process and skip Claude usage
  transcripts older than every requested window before opening them.
- A normal recap now takes one in-memory transcript snapshot spanning the
  current and previous comparison windows, rather than reparsing overlapping
  session files twice. The snapshot is per invocation (not an mtime cache),
  preserving boundary-overlap and resumed-session semantics.
- Claude provider snapshots now derive engaged top-level session facts and
  current/previous exact usage from the same physical JSONL pass. Subagent
  usage remains included while subagent sessions remain excluded, preserving
  existing session-overlap and inclusive usage-boundary semantics.
- Claude and Codex exact token usage now use the same one-scan, multi-window
  path for a report and its previous-window comparison. Each usage event keeps
  its existing inclusive window-boundary behavior; Codex branch-baseline and
  inherited-prefix handling remain unchanged.
- `--llm-narrative` now sends up to 40 bounded session excerpts in one
  narrator request rather than spawning one local CLI process per session.
  Long sessions preserve their first intent, latest request, and final
  assistant outcome within the fixed excerpt budget. Each returned JSONL row
  is accepted only for a requested session ID; when an otherwise valid response
  omits up to five IDs, ccstory retries that strictly smaller set once before
  unresolved rows fall back individually. A failed refresh never overwrites a
  cached automatic summary. Recap now delegates to the shared backfill
  implementation instead of retaining a second, divergent per-session loop.
- Restored the category-centered recap structure: `Top focus` shows the
  largest Category, its time share, and a representative session; `What you
  did` remains the separate cross-Category integration of 2-4 goal threads.
- Combined-agent cards and reports are branded `ccstory Recap` rather than
  `AI Coding Recap`. `--lang en` and other common language codes now expand to
  unambiguous language names before narrative generation.

- Renamed **What shipped** to **Repo activity** so repo-wide commit and GitHub
  metrics are not mistaken for author-attributed output. GitHub enrichment now
  checks access once, defaults to the 10 most locally active repos, labels
  local-only and partial coverage explicitly, and omits incomplete GitHub
  totals from the terminal card and JSON totals. Markdown shows at most 20
  repos, with the complete list retained in JSON.

- Shortened recap display labels to `Codex` and `Antigravity`, moved
  report-wide cost/coverage caveats to the card footer, bounded the Top focus
  excerpt, and let project and narrative text wrap cleanly.
- Scoped GitHub merged-PR queries to the report window and recursively split
  capped date ranges, preventing repositories with more than 200 lifetime PRs
  from emitting false warnings or undercounting current-window shipped work.

### Fixed

- Deep category initialization no longer sends stale, legacy, fallback, or
  skipped cached prose into a fresh classification; it uses current session
  text unless the cache row is a current auto summary or authoritative human
  record.
- Importing the shared Claude recap cache now promotes local generated or
  fallback rows when an incoming human `record` exists, clears obsolete
  narrator/evidence metadata, and preserves an existing local record over
  every imported row. Imported automatic summaries still never overwrite any
  local row.

## [0.7.2] - 2026-07-26

### Added

- Native protobuf wire decoder reading Google Antigravity native session titles from `agyhub_summaries_proto.pb`. Titles populate `SessionStat.native_title` and take precedence over `first_user_text` across terminal, Markdown, JSON, and MCP recap surfaces while preserving `first_user_text`.
- Authoritative exact token usage extraction for Google Antigravity from `gen_metadata` in `conversations/<session_id>.db`, including cached-content token count (usage field 5), aligned by step index timestamps with transcript logs and deduplicated per step index (DB priority, transcript exact usage fallback).
- Time-window attribution for compacted Antigravity DB steps lacking explicit transcript timestamps using session window inclusion and boundary-crossing linear interpolation.
- Refreshed vendored LiteLLM pricing table containing bare Gemini model IDs (`gemini-3.6-flash`, `gemini-3-flash-preview`), and explicit pricing aliases for `gemini-3-flash-a` and `gemini-3-flash-agent`.
- Disclosed `unpriced_models` and per-period/point `usage_coverage` payloads as additive fields in `--json` recap, comparison, and trend outputs.

### Changed

- Updated Google Antigravity provider usage coverage status from `partial` to `complete`.
- Renamed the multi-agent provenance section to `Agent Breakdown` and moved it
  below the previous-window comparison so the recap stays focused on work and
  outcomes before showing provider shares.

## [0.7.1] - 2026-07-26

### Added

- Google Antigravity session provider adapter (`--agent antigravity`), reading
  transcripts under `~/.gemini/antigravity/brain` while explicitly marking
  token and cost coverage incomplete when exact usage is unavailable.
- Coding-agent sources now register one provider descriptor and own their data
  roots plus narrative-excerpt parsing. CLI, MCP, reports, session collection,
  usage aggregation, and availability checks derive from the same registry, so
  a new bundled agent no longer needs parallel edits across every surface.
- Providers declare whether their logs expose complete exact token usage.
  Markdown, terminal, JSON, Obsidian, and trend outputs now disclose incomplete
  agent coverage, preserve partial versus unavailable states, and ignore
  dormant providers instead of silently presenting a partial total as complete.
- Source/editable builds now read the canonical source version from
  `pyproject.toml` instead of impersonating an older installed distribution.

### Changed

- Narrative and classification prompts describe agent-neutral coding sessions.
  Provider-specific transcript knowledge no longer lives in the summarizer.
- Antigravity user requests unwrap the native `<USER_REQUEST>` envelope and
  remove well-formed injected metadata blocks before appearing in recaps.

### Fixed

- Cross-session wall-clock time now unions provider-owned activity intervals
  instead of bridging idle gaps between unrelated sessions. The reported wall
  clock can no longer exceed raw agent time and produce impossible `<1×`
  parallelism.
- Codex availability checks include `archived_sessions`, matching collection;
  archived-only users are no longer rejected before parsing begins.
- Antigravity child conversations referenced by structured
  `INVOKE_SUBAGENT` records no longer inflate top-level session, active-time,
  or usage totals; ordinary user or assistant text mentioning a conversation
  ID cannot trigger the filter.
- Antigravity usage is exact-only: input/output counts must be non-negative
  integers and the actual model ID must be present. Cache-read variants are
  preserved when exposed; missing fields never fall back to character-count
  estimates or a guessed Gemini model.

## [0.7.0] - 2026-07-23

### Added

- OpenAI Codex is now a first-class data source alongside Claude Code.
  `--agent all|claude|codex` works across recap, trend, terminal, Markdown,
  JSON, and MCP surfaces, with per-agent session and time-share breakdowns.
- Codex token usage now includes model-aware input, cache, and output totals
  from rollout cumulative counters, including resumed, nested, and guardian
  subagent branches without replaying copied ancestor history.
- Model pricing is vendored into each release, including GPT-5 family entries,
  and can be refreshed through the repository's validation workflow without
  adding runtime network requests.

### Changed

- Collection is split behind provider interfaces so Claude Code and Codex use
  one aggregation pipeline while retaining source-specific parsing.
- Report titles, metadata, JSON payloads, terminal cards, and filenames retain
  the selected agent scope; agent-specific reports no longer collide.
- Content classification gives each run bounded proposal headroom and preserves
  cross-chunk evidence before accepting a new bucket.

### Fixed

- Codex-only recap and trend commands no longer require a Claude data path.
- Windowed Codex totals use cumulative deltas instead of replaying lifetime
  counters, and child rollouts subtract the longest copied ancestor prefix.
- MCP agent filters and per-agent breakdowns now match the CLI contract.
- Explicit narrative language instructions are no longer weakened by the
  language found in session content.
- Reports distinguish unpriced models from stale Claude-only pricing caveats.

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

[Unreleased]: https://github.com/atomchung/ccstory/compare/v0.8.2...HEAD
[0.8.2]: https://github.com/atomchung/ccstory/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/atomchung/ccstory/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/atomchung/ccstory/compare/v0.7.3...v0.8.0
[0.7.3]: https://github.com/atomchung/ccstory/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/atomchung/ccstory/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/atomchung/ccstory/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/atomchung/ccstory/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/atomchung/ccstory/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/atomchung/ccstory/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/atomchung/ccstory/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/atomchung/ccstory/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/atomchung/ccstory/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/atomchung/ccstory/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/atomchung/ccstory/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/atomchung/ccstory/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/atomchung/ccstory/releases/tag/v0.3.0
