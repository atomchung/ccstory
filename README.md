# ccstory

> **Your AI coding-agent week, in plain English.**
> Reads local coding-agent session logs and writes a categorized recap with
> active hours, costs, and a per-bucket narrative. This release bundles Claude
> Code, OpenAI Codex, and Google Antigravity; the provider registry is designed
> to add more agents without changing the recap contract.

Sibling to [ccusage](https://github.com/ryoppippi/ccusage):
**ccusage tells you how much you spent · ccstory tells you what on.**

## Who this is for

- People who want to write a weekly status without scrolling scrollback.
- People who saw a ccusage number and want to know what kind of work those
  tokens went to.
- People who do a Sunday-night reflection on what they actually shipped.

## Quick start

```bash
pipx install ccstory
ccstory init
ccstory week
```

That's it. `init` is a one-time auto-categorize step that scans your
recent sessions; `ccstory week` produces the recap. Full report saves to
`~/.ccstory/reports/recap-*.md`.

The default **Repo activity** section always reads local git. When GitHub is
connected, it lightly enriches up to 10 active repos with GitHub metadata.
For a first run with no network access at all, use:

```bash
ccstory init --skip
ccstory week --minimal --classify folder --no-artifacts
```

`--no-artifacts` alone disables ccstory's GitHub/PyPI lookups while keeping
the normal narrative flow, which may invoke one of your configured local
coding-agent CLIs.

## Demo

```

╭──────────────── ccstory Recap · May 5 – 12, 2026 ────────────────────╮
│                                                                      │
│  ★ Top focus  coding  10.9h  (53% of active time)                    │
│    ↳ Built /show-routine slash command using bash+python to fetch…   │
│                                                                      │
│  Active  20.6h  Sessions  74   Output  2.92M                         │
│  Turns   3,692  Cache     96%  Cost    $1,608                        │
│                                                                      │
│  Time by category                                                    │
│  coding          ███████████████░░░░░░░░░░░░░   10.9h    53%         │
│  writing         █████████░░░░░░░░░░░░░░░░░░░    6.2h    30%         │
│  research        █████░░░░░░░░░░░░░░░░░░░░░░░    3.5h    17%         │
│                                                                      │
│  Full report → ~/.ccstory/reports/recap-2026-05-10_2026-05-17.md     │
│                                                                      │
╰────────────────────────────── ccstory ───────────────────────────────╯
```

The markdown report adds a **header + 2-4 bullet points per bucket** plus
per-session one-liners. Run with `--llm-narrative` to upgrade per-session
lines from the instant first/last-message fallback to LLM-polished prose:

> **Re-running upgrades retroactively.** If you viewed a window in the
> default (instant) mode first, re-running it with `--llm-narrative` upgrades
> those cached fallbacks to polished summaries — so `ccstory month
> --llm-narrative` polishes weeks you already skimmed. Already-polished
> sessions are reused (no re-burn) while their exact bounded transcript
> evidence, project, evidence policy, prompt, and narrator policy remain
> current. Transcript growth is detected on the next selected
> `--llm-narrative` run;
> add `--refresh` to force every in-window summary to regenerate (e.g. after
> a narrator model-policy change you want reflected).

```
### coding

Shipped the /show-routine slash command end-to-end this week — bash+python
wrapper to surface scheduled-task output, plus a routine-detail bookmark
flow after the live debug session on Wednesday.

- 2026-05-10 03:24 · 123m · 212 msg — Built /show-routine slash command using
  bash+python to fetch scheduled-task output and surface it inline.
- 2026-05-08 12:30 · 67m · 294 msg — Debugged hook race condition in
  background-task notification dispatch; landed fix in main.
```

## Usage

### Basic

| Command | What it does |
|---|---|
| `ccstory init` | One-time auto-categorize from recent sessions |
| `ccstory` | Current month so far (default window) |
| `ccstory week` | Past 7 days |
| `ccstory month` | Current month |
| `ccstory 2026-04` | A specific month |
| `ccstory trend` | Last 8 weeks of sparklines |
| `ccstory category list` | Show your custom bucket rules |
| `ccstory category set <bucket> <keyword>…` | Pin a project to a bucket |
| `ccstory category unset <bucket> <keyword>…` | Remove a keyword from a bucket |
| `ccstory project list` | Discover observed project IDs for `goal set --project` (`--all` includes ephemeral/synthetic ones) |

### Advanced

**Window**

| Command | What it does |
|---|---|
| `ccstory all` | Entire history |
| `ccstory trend --weeks 12` | Custom trend range |
| `ccstory trend --months 6` | By calendar months |

**Narrative depth**

| Flag | What it does |
|---|---|
| `--minimal` | Numbers only, no per-session lines |
| `--llm-narrative` | Per-session prose through the configured local narrator (slow, opt-in) |
| `--no-aggregate` | Skip the per-bucket synthesis |

**Comparison block** (vs-previous, auto-attached to week/month)

| Flag | What it does |
|---|---|
| `--no-compare` | Skip the entire block |
| `--no-compare-narrative` | Keep numeric deltas, drop the prose |

**Coding agent (currently bundled providers)**

| Flag | What it does |
|---|---|
| `--agent all` (default) | Every provider bundled by this installed version |
| `--agent claude` | Claude Code only (`~/.claude/projects`) |
| `--agent codex` | OpenAI Codex only (`~/.codex/sessions`) |
| `--agent antigravity` | Google Antigravity only (`~/.gemini/antigravity/brain`) |

Also accepted by `ccstory trend`, so a trend line and a week over the same range
describe the same population. See [Multiple coding agents](#multiple-coding-agents)
for what the numbers mean once more than one agent is in the window.

**Session classification mode**

| Flag | What it does |
|---|---|
| `--classify folder` | Folder-name rules only: your `[categories]`, else the built-in keyword table |
| `--classify content` | Configured narrator reads each session; no folder rules at all |
| `--classify hybrid` | User rule wins, else content, else the built-in keyword table (default) |

**Export**

| Flag | What it does |
|---|---|
| `--for=obsidian` | YAML frontmatter + `[[wikilinks]]` |

**Output format**

| Flag | What it does |
|---|---|
| `--format=card` | Force the Rich terminal card (default in a real tty) |
| `--format=markdown` | Force the full Markdown report to stdout |
| `--format=auto` (default) | Markdown when `CLAUDECODE=1` or stdout is not a tty (piped / redirected), else card |

The auto-detect means asking Claude Code "show me my week with ccstory" renders an actual Markdown report in the chat instead of ANSI escape codes. The Markdown body is the same content saved to `~/.ccstory/reports/` (`recap-*.md` for the default window, `trend-*.md` for `ccstory trend`), just printed to stdout so the chat can render it inline. In markdown mode all progress / status lines route to stderr, so stdout is a clean Markdown stream you can pipe.

**Refresh (apply rule changes retroactively)**

| Flag | What it does |
|---|---|
| `--refresh` | Re-do this window's cached work: re-classify after a rule edit, and (with `--llm-narrative`) force-regenerate every per-session summary |
| `--refresh-all` | Wipe the entire content-classification cache, not just this window |

### Trend output

```
Hours by bucket
total          ▁▄▆▇▃█    16.5h   avg 9.0h   ▲ +183%
coding         ▁▂▃▄▁█    10.2h   avg 3.3h   ▲ +1148%
writing        ▁▇█▆▁▁     6.2h   avg 4.1h   ▲ +51%
research       ▁▃▅█▆█     3.5h   avg 2.0h   ▲ +75%

Overall
output         ▁▁▁▄▁█     3.0M   avg 0.8M   ▲ +260%
cost           ▁▁▂▃▁█   $1,643   avg $463   ▲ +255%
burn %         ▁▁▂▃▁█     201%   avg 57%    ▲ +255%
```

The `burn %` row is API-equivalent cost as a percentage of your prorated
monthly quota. Set `monthly_quota_usd` in `~/.ccstory/config.toml`
(default $3,500 ≈ Max 20x plan); set to `0` to hide the row.

## Categories

ccstory classifies each session into two layers:

- **Area** (layer 1) — the coarse bucket (`coding`, `investment`, …). Trend and
  compare aggregate at this layer, and its numbers are the stable contract
  downstream tools (dashboards, the MCP `get_recap` / `get_trend` shapes) read.
- **Project** (layer 2) — the normalized project-folder leaf (e.g. `ccstory`,
  `stock`). Projects emerge automatically from your session folders — no extra
  config — and the recap card, markdown report, and `--json` break each area
  down by project. This breakdown is computed at read time, so it adds no cache
  and never re-classifies history.

Four default areas, matched against the project folder name:

| Area | Keywords (sample) |
|---|---|
| `investment` | investment, stock, portfolio, trading, ticker, etf, finance |
| `writing` | blog, newsletter, post, docs, content, article |
| `coding` | app, sdk, cli, plugin, mcp, server, frontend, backend, lib, … |
| `other` | playground, scratch, sandbox, experiment |

Unmatched projects fall back to `coding`. Customize in
`~/.ccstory/config.toml`:

```toml
default_bucket = "coding"

[categories]
# An entry equal to a project's normalized leaf is an *exact member* of that
# area. Substrings still work as a fuzzy fallback, so existing configs keep
# resolving exactly as before.
"learning"   = ["info-collector", "ai-project-research"]
"investment" = ["stock", "kol-collector"]
```

**Two matching tiers.** The resolver checks *exact membership* first (the
project's normalized leaf listed verbatim under an area), then falls back to
the older *token-needle* fuzzy match. Because an exact member always wins over
an earlier area's fuzzy hit, you can delete the section-ordering workarounds
fuzzy matching used to force (listing one area before another just so a shared
substring resolved the way you wanted). Listing the same project under two
areas prints a warning at load and keeps the first.

**Aliases** (optional). Fold variant folder-leaf names onto one canonical
project with a `[projects]` table — useful when the same work shows up under
more than one folder name:

```toml
[projects]
"infocollector" = "info-collector"   # both roll up as one project
```

**Area overrides.** Folder rules can be overridden per-session by content
(`--classify content` / `hybrid`), where one batched local narrator call
re-buckets sessions by what they were actually about. An override changes a
session's *area* only — its project is the physical fact of which folder the
work happened in, never reassigned. Results cache in `~/.ccstory/cache.db` so
reruns are free.

In `hybrid` mode, a session with no user rule and no narrator configured (or
no eligible content to classify) doesn't skip straight to `default_bucket` —
it still checks the built-in keyword table above before falling back, so a
no-config, no-narrator install still reports across more than one area
instead of collapsing everything into `coding`.

**Classification coverage.** Every recap discloses which layer actually
resolved each session — `user_rule` / `llm_cache` / `llm_fresh` /
`builtin_rule` / `fallback` — as a `classification_coverage` block in
`--json` (sessions + active hours per source, plus `content_lane`: whether
fresh content classification could run *at all* this invocation). The
Markdown report always includes the full breakdown as one line; the
terminal card adds a compact version only when the content lane never had a
chance to run this window, or the fallback share is at least 25%, e.g.
`classification: rules 141 · content 0 (lane off) · fallback 10`. A lane
that is "on" can still legitimately show `content 0` when every session
resolved via a rule or a cache hit — that reads differently from a lane
that never ran (`--minimal`, `--classify folder`, or no configured
narrator), which always shows `(lane off)`. `ccstory trend` carries the
same per-period breakdown (its own `content_lane` is always `"off"`: trend
resolution is cache-only and never fires a fresh classification call).

## Project discovery

`ccstory goal set --project <id>` needs the exact canonical project string a
recap already uses internally — which is not always obvious from a folder
name alone once aliases and worktrees are involved. `ccstory project list`
answers that directly, read-only:

```bash
ccstory project list                       # configuration-relevant projects
ccstory project list --all                 # every observed identity
ccstory project list --window month        # this month only
ccstory project list --agent codex --json
```

Each row is `project_id` (the same canonical normalization + alias fold
recap and GoalContext use), `category` (see below), `last_seen`,
`session_count`, and `agents[]` — never a transcript path or session id. A
project touched by more than one coding agent, or reached through more than
one folder alias, collapses onto one row. Output is bounded and
deterministic: most-recently-observed project first, canonical id as the
tie-break; the terminal shows a capped top set with an explicit count when
there are more, and `--json` carries its own explicit hard maximum plus
`total_count` / `truncated` rather than silently growing without bound. The
window defaults to `all` (`week` / `month` / `YYYY-MM` also accepted) and the
command scans through the same provider snapshot seam as everything else —
no persistent project registry, no second transcript index, no model calls.

### The default relevance view

Discovery exists so you can configure a goal against a workspace you
actually work in. Two identity classes can never be that workspace, so the
default view hides them and always says how many it hid:

- **Ephemeral roots** — every session for that project ran under a temporary
  filesystem root (`/tmp`, `/private/tmp`, `/var/tmp`, `/private/var/tmp`, or
  the macOS per-user `TMPDIR` under `/var/folders`). Decided from the
  recorded working directory, matched component-wise: a real repository named
  `tmp-runner`, or one living in `~/code/tmpdata`, is never affected. One
  session outside scratch space is enough to keep the whole project visible.
- **Provider-synthesized dated identities** — the per-day placeholder
  workspace a coding agent creates for a chat you started without opening a
  folder, which normalizes to `<agent>-YYYY-MM-DD-<leaf>` (for example
  `codex-2026-08-21-new-chat`). The leading token must be a registered
  coding agent and the date must be a real calendar date, so an ordinary
  project named `release-2026-08-21-notes` is never affected.

Both rules are deterministic and run zero model calls. `--all` restores the
complete listing unchanged — same order, same caps, same alias fold. In
`--json`, every row carries its own `relevance`
(`relevant` / `ephemeral_root` / `synthetic_dated_id`), and the envelope
gains `filtered_count` (how many identities the default view removed, always
`0` under `--all`) and `all`. Both additions are additive: every field the
command already emitted keeps its name, type, and meaning.

### The resolved category column

`category` / `category_source` answer how a project relates to your report
categories, using only the deterministic folder layer — `user_rule >
builtin_rule > fallback`, the same chain `--classify folder` uses. Discovery
never reads the per-session content-classification cache and never calls a
narrator, so the column costs nothing and is stable across runs. A project
reached through several folder variants resolves once, from the group's
lexicographically smallest folder, so the value never depends on provider or
filesystem iteration order.

## Goal activity

GoalContext v1 lets a recap relate measured activity to owner-defined goals
without asking a narrator to infer those goals. Manage the private default
file with:

```bash
ccstory goal set ship-recap --title "Ship the recap" --project ccstory
ccstory goal list
ccstory goal unset ship-recap
```

`goal set` reports, per linked project, whether it matches a project
`ccstory project list` already observes:

```
✓ Set goal `ship-recap` · Ship the recap · ccstory
  observed: `ccstory`
```

An unobserved project is not an error — it is kept exactly as given (a valid
future project can be declared before its first session), just labeled:

```
✓ Set goal `future-launch` · Future launch · future-app
  unobserved: `future-app` has no session history yet — did you mean: future-ap? (value unchanged)
```

The observed check judges against the complete observed population, not the
relevance-filtered default view: naming an ephemeral or synthetic identity on
purpose still counts as observed. A close-spelling suggestion is
deterministic guidance only; it never rewrites the stored value. Add `--json` for a machine-readable envelope instead —
`{"ok": true, "replaced": false, "goal": {...}, "projects": [{"project":
"future-app", "observed": false, "candidates": ["future-ap"]}]}` — where
`candidates` is present only when there is a close-spelling suggestion to
offer.

The managed source is `~/.ccstory/goals.toml`. To read an external,
read-only source instead, configure `~/.ccstory/config.toml`:

```toml
[goal_context]
path = "/absolute/or/config-relative/goals.toml"
```

Override that source for one recap with `--goals-file PATH`. Explicit,
configured, and managed sources have that precedence; `ccstory goal
set/unset` always mutates only the managed default.

Internally, source selection returns a capability-bearing Goal Source Adapter.
The managed adapter explicitly permits `read`, `upsert`, and `delete`;
configured and explicit TOML adapters permit only `read`. Agents and library
integrations must inspect those runtime capabilities rather than treating a
path or a `source_kind` value as write authority. This lets an agent safely use
`ccstory goal set ...` for the managed source while guiding the user back to an
external source's owning system when that source is selected.

The external file uses the same strict v1 TOML schema:

```toml
schema_version = 1

[[goals]]
id = "ship-recap"
title = "Ship the recap"
projects = ["ccstory"]
valid_from = 2026-07-01       # optional, inclusive local report date
valid_until = 2026-09-30      # optional, inclusive local report date
```

Parsing is strict: unknown fields, invalid dates, and duplicate goal ids fail
with an actionable error. Project references do not have to appear in current
session history, so a valid future project can be declared before its first
session.

When goals are selected, the terminal card adds up to five highest-contribution
rows, Markdown and full JSON include every goal, and MCP `get_recap` reloads
the current configured/default source on every call. `compare_to_previous` and
`get_trend` remain goal-free. Shared per-goal hours overlap and are
**non-additive**; the global covered/exclusive/shared/unattributed buckets
count each activity contribution once. Unattributed time remains visible.

Every goal section also carries a coverage line — always, zero included — plus
a bounded hint naming where the unattributed hours actually sit, so a mapping
gap never reads as lost time:

```
unattributed: 189.25h (92%) — projects mapped to no goal
top unmapped: investment-note 71.4h · kol-collector 60.0h · ccstory 24.9h
```

The hint lists the three highest-hour projects mapped to no goal (hours
descending, canonical project id breaks ties), and JSON/MCP carry the same
three under `goals.coverage.top_unmapped_projects` as `project_id` /
`active_hours`. Add those projects with `ccstory goal set --project`, or leave
the gap knowingly — either way the number stops being a mystery.

These values are activity/contribution evidence, not goal progress,
completion, outcome, or acceptance. Goal titles and source content never enter
narrator prompts or narrator traces. Public output includes only a sanitized
source kind and content fingerprint, never the source path.

For a bounded history that a dashboard can visualize without rebuilding goal
accounting, use the dedicated read-only JSON command:

```bash
ccstory goal-history                         # 4 completed local ISO weeks
ccstory goal-history --weeks 8 --agent codex
ccstory goal-history --goals-file ./goals.toml | jq .buckets
```

Buckets are completed Monday 00:00 → Monday 00:00 weeks in the machine's local
timezone, oldest first. The default is 4 and the hard maximum is 24; zero,
negative, non-integer, and over-limit counts are rejected rather than silently
clamped. All windows are passed through one provider snapshot, then each bucket
uses the same window-pure session slices, canonical project identity,
effective-date rules, wall-clock accounting, and `attribute_goals()` path as a
current recap. A session crossing Monday contributes only its bounded facts to
each side.

The result is hours-only and includes every goal effective in each bucket,
including zero-activity goals, plus exclusive/shared/unattributed coverage,
safe source kind/fingerprint, and explicit additive/non-additive semantics.
`coverage_status` is currently `unavailable`: provider token
`usage_coverage` does not prove that historical activity collection is
complete, so ccstory does not relabel it as activity coverage. The command
requires a selected GoalContext and fails before collection when none is
configured or when the source is invalid. It never writes a report, touches
the narrator/cache, mutates GoalContext, or exposes session ids, transcript
paths, prompts, correction text, or source paths.

## Multiple coding agents

This release currently reads Claude Code (`~/.claude/projects`), OpenAI Codex
(`~/.codex/sessions`, plus `archived_sessions`), and Google Antigravity
(`~/.gemini/antigravity/brain`). That list is an implementation snapshot, not
an architecture limit: each future agent belongs in the same provider registry
and receives the same recap, report, JSON, trend, and MCP contracts.

Where a provider records a working directory, ccstory attributes its sessions
to a project through the shared rules — including git worktrees, so a detached
checkout counts toward the repo it came from rather than becoming its own
one-off project.

The source boundary is registry-driven. A provider supplies one descriptor plus
its data roots, transcript parser, narrative-excerpt extractor, usage
collector, and coverage declaration; CLI choices, MCP filtering, availability
checks, report labels, and incomplete-cost warnings derive from that descriptor.
This keeps future transcript formats inside their provider instead of adding
agent-specific branches across every output surface.

**Time is reported once, not per agent.** Agents run concurrently: a Codex
review and a Claude Code session routinely occupy the same ten minutes. Summing
their active time double-counts that overlap — on a real week here, raw
per-agent time added up to 177h against a deduplicated wall clock of 64h. So:

- **Total active time** is the wall clock across every session, deduplicated —
  the same number ccstory has always reported, now spanning all agents.
- **The terminal card keeps agent provenance to one compact share line.** Each
  agent's share is its raw interaction time relative to the others'. Shares are
  not durations and do not add up to the total.
- **The Markdown `Agent Breakdown` retains time share and session share.** Many
  short Codex reviews against fewer long Claude Code sessions can show up as
  75% / 25% of time but 51% / 49% of sessions — that gap remains available
  without taking over the screenshot card.
- **`N× parallel`** is raw agent time ÷ wall clock: how much of the work
  overlapped.

Usage and cost coverage are provider-specific. ccstory never estimates tokens
or infers a model when a provider's local source does not expose an exact value;
unknown values stay visible through `usage_coverage` and `unpriced_models`.
For Google Antigravity, native titles are read from
`~/.gemini/antigravity/agyhub_summaries_proto.pb`, and token fields are read
from `gen_metadata` in companion SQLite databases. Some compacted database
steps have no source timestamp, so their report-window membership is
deterministically attributed from neighboring transcript step indexes. Treat
that as local-parser evidence, not a provider billing-portal reconciliation.
Models with known tokens but no known rate remain visible and trigger a
missing-price warning.

Use `--agent <provider-id>` to isolate one bundled provider; run
`ccstory --help` to see the IDs available in the installed version.

## Repo activity

Time tells half the story; the other half is what the time produced. Each
report includes a **Repo activity** section — repo-wide output metrics for
repos inferred from sessions in the window:

```markdown
| Repo     | Commits | PRs merged | Releases | Stars   |
|----------|--------:|-----------:|----------|--------:|
| ccstory  | 5       | 3          | v0.8.0   | 42 (+6) |
| myapp    | 21      | 1          | –        | 12      |

- PyPI **ccstory**: 107 downloads (last week)
```

- **Repos are inferred from session working directories** — no config needed.
  Worktrees collapse into their main repository.
- **Commits** come from local git (works offline, counts all branches).
  These metrics are repo-wide, not author-filtered.
- **PRs merged / releases / stars** are optional GitHub enrichment. ccstory
  checks `gh` once, then queries at most the 10 repos with the most local
  commits. Missing CLI, authentication, repo permission, or network access
  never blocks the recap: the card says it is showing local commits only.
  Partial coverage is labeled and is never presented as a complete GitHub
  total.
- **PyPI downloads** send the package name to pypistats.org. Auto-detection is
  bounded to the same top 10 repos; explicitly configured packages are still
  queried.
- The artifacts collector never sends conversation text, prompts, summaries,
  commit contents, or local paths. It uses only repository/package metadata.
- **Stars delta** compares against the last snapshot taken before the window,
  so it becomes meaningful from your second run onward.
- The terminal card stays compact. Markdown shows at most 20 repo rows and
  points to `--json` for the full list.

Skip all GitHub/PyPI metadata calls per run with `--no-artifacts`, or
persistently via config:

```toml
[artifacts]
enabled = false              # no repo-activity collection
exclude = ["playground"]     # substring match on repo path
github_repo_limit = 10       # 0 = local commits only
pypi = ["my-package"]        # extra packages beyond auto-detection
```

## Narrative depth

`Top focus` is the largest Category plus a compact, multi-session account of
the strongest work and projects in it. It never promotes a raw prompt, command,
or path as the explanation. `## What you did` is the separate cross-Category
integration: 2-4 work themes (bold header + bullets) that explain what the
period added up to. Per-category narrative is the normal recap flow; choose a
different depth explicitly:

```bash
ccstory week --narrative per-category   # header + bullets per bucket instead
ccstory week --narrative both           # overall first, then per-bucket
```

Eligible buckets use cached or configured local-narrator prose, but always
fall back to a deterministic local category summary when narration is absent,
unavailable, or reaches its lane deadline. Rerunning the same inputs is
normally free. In `--json` mode the same text and its provenance land in
`buckets[].narrative`.

## Narrative backends, latency, and quota

ccstory tries available local narrative backends in this default order. Every
configured backend has an explicit low-cost model; no model is inferred from a
source transcript. These are the backends bundled in this release, not a limit
on future registered providers.

```toml
# ~/.ccstory/config.toml
[narrative]
providers = ["claude", "codex", "antigravity"]

[narrative.claude]
model = "sonnet"

[narrative.codex]
model = "gpt-5.6-terra"

[narrative.antigravity]
model = "gemini-3.6-flash-low"
effort = "low"
```

Set `providers = []` to disable every LLM path, or select a subset/order for
an organization. A failed or unavailable backend advances to the next one.
The current release ignores unknown IDs rather than issuing an unmodelled call;
after an upgrade, newly bundled provider IDs can be added to the same ordered
policy.
Codex calls run with `--ephemeral --sandbox read-only`; Claude calls use
`--no-session-persistence`. Antigravity has no equivalent ephemeral mode, so
it may retain local CLI session metadata. JSON records `summary_narrator` for each LLM summary and
`narrative.provenance` for aggregate prose; Markdown identifies each aggregate
narrator. Existing unprovenanced cache rows are retained as `legacy` history
without a global transcript scan or narrator re-burn. They refresh lazily only
when their session is selected by a later `--llm-narrative` run.

There is no single fixed call total: it depends on init mode, uncached
sessions, narrative depth, and which cache entries already exist. Calls use
the signed-in plan/quota of the backend that actually answered; ccstory does
not use an API key or add a separate API charge.

| Operation | Fresh local-narrator calls | Cache behavior |
|---|---:|---|
| `ccstory init --quick` | 1 (usually ~10s) | One-time config proposal |
| `ccstory init --deep` | 1 per 80 sampled sessions (up to 3 with the default cap of 200) | Writes per-session classification cache |
| `ccstory init --skip` | 0 | Uses local folder rules only |
| Hybrid/content classification | 1 per 80 uncached or stale sessions | Reused until its prompt or category vocabulary changes |
| Overall narrative | 0 or 1 on a cache miss | Reused while its rendered inputs and prompt are unchanged |
| Per-category narrative | Up to 1 per eligible bucket on a cache miss | Reused while that bucket's inputs and prompt are unchanged |
| Previous-window narrative | 0 or 1 on a cache miss | Reused while its comparison inputs and prompt are unchanged |
| `--llm-narrative` | 1 per 40 uncached or stale sessions | Reused while exact bounded evidence is unchanged; `--refresh` deliberately regenerates |

The default recap uses hybrid classification, a per-category narrative, and a
previous-window narrative; per-session LLM prose remains opt-in. Use
`--narrative overall|both` to trade the category lane for, or add, an overall
call. `--no-aggregate`, `--no-compare-narrative`, and `--classify folder`
remove those call types; `--minimal --classify folder` makes the recap itself
use zero local-narrator calls.

Deep/content classification and per-session `--llm-narrative` work are batched.
The latter sends up to 40 bounded excerpts in one strict JSONL request, validates
every returned session id, and falls back only entries the model omitted.
Aggregate call latency varies with the selected CLI startup and input size. A
same-window rerun is usually cache-only, but new sessions,
changed inputs/config, `--refresh`, or a newer prompt version can trigger fresh
calls. Content classification carries accepted bucket names into later
80-session batches and enforces one run-wide vocabulary cap, preventing a
large first run from fragmenting one theme into several near-duplicate labels.

Every recap has one invocation-local **90-second LLM budget**, shared by
per-session narration, content classification, and aggregate/comparison prose.
The first summary request is a 10-session probe; later requests grow toward 40
or shrink toward 10 according to observed latency. Each narrator call has a
45-second deadline. When either limit is reached, already-completed summaries
remain usable, untouched sessions use the local first/last-message fallback,
uncached classifications use folder/fallback rules, and aggregate/category
prose uses its deterministic local fallback rather than starting another model
call. The terminal and JSON provenance report this partial-LLM state,
successful provider/model, lane timing, attempts, and coarse batch progress
without exposing transcript text or prompts.

Each automatic session summary records a private fingerprint of only that
session's exact bounded evidence (up to 700 characters in the batch lane), project,
and evidence-policy version. Neighboring sessions in a batch do not participate
in its identity. If evidence changes and regeneration fails or the budget ends,
the old summary remains in the detailed session history with a compact
`summary evidence: stale` marker; it is excluded from fresh Top focus,
classification, category/overall synthesis, and comparison prose. Legacy rows
are marked `legacy` until lazily refreshed. `provided` rows (see
[Library usage](#library-usage-integration-api)) remain authoritative even
under `--refresh`.

If no configured local narrator is available, LLM classification and synthesis degrade
gracefully: classification uses folder/fallback rules, per-session prose uses
the local first/last-message fallback, and no narrator quota is used. This does
not disable Repo activity metadata calls; add `--no-artifacts` for that.

## JSON output

For dashboards, bots, and sync scripts — one machine-readable object instead
of parsing markdown:

```bash
ccstory week --json          # shorthand for --format=json
ccstory month --format json
ccstory trend --weeks 8 --json
ccstory goal-history         # dedicated sanitized weekly goal series
ccstory project list --json  # observed project IDs, bounded and deterministic
ccstory project list --json --all  # …including ephemeral/synthetic identities
ccstory goal set my-goal --title "My goal" --project my-app --json
```

stdout is pure JSON (progress goes to stderr, same as markdown mode), so
`ccstory week --json | jq .totals.active_hours` just works. For recap and trend
commands, the envelope
carries `schema_version` (currently 1): renames/removals bump it, additive
fields don't — consumers should tolerate unknown keys. Covers window, totals
(hours/tokens/cost/cache), buckets, per-session lines, model breakdown, unpriced models (`unpriced_models`), provider coverage (`usage_coverage`), classification-source coverage (`classification_coverage`), narrative, optional `goals`, comparison, artifacts, and the pricing snapshot date. The markdown
report file is still written either way; JSON is a view, not a replacement.
`goal-history` is deliberately different: it returns only the sanitized series
and writes no report.
Every full-JSON session includes additive
`summary_evidence.status` (`current`, `stale`, `legacy`, `unavailable`, or
`not_applicable`). Raw evidence, fingerprints, transcript paths, and additional
session identifiers are never included. Library callers can derive the same
enum without I/O using the pure
`ccstory.session_summarizer.summary_evidence_status(summary)` helper.

## Obsidian export

`ccstory --for=obsidian` swaps the plain markdown for a PKM-vault-ready
variant with YAML frontmatter and `[[wikilinks]]`:

```yaml
---
date_start: 2026-05-10
date_end: 2026-05-17
active_hours: 20.6
top_focus: coding
buckets: [coding, writing, research]
cost_usd: 1608.42
output_tokens: 2920000
---
```

Queryable in Obsidian's Dataview / Bases (`WHERE top_focus = "coding"`).
Bucket names with special characters are JSON-quoted so the frontmatter stays
valid even for `client: acme, inc`.

## Narrative language

ccstory delegates narrative writing to the first available configured local
backend. Language is set in the prompt, so the same override applies to current
and future providers. Claude Code preferences remain an optional
backwards-compatibility fallback when they exist; Claude is not required.

Precedence (high → low):

| Source | Notes |
|---|---|
| `--lang "Traditional Chinese"` | One-off, this invocation only |
| `CCSTORY_LANG=日本語` env var | Shell-scoped |
| `language = "Spanish"` in `~/.ccstory/config.toml` | Persistent, ccstory-only |
| `~/.claude/CLAUDE.md` | Pasted verbatim, so it can carry richer directives |
| `~/.claude/settings.json` `language` | Set by Claude Code's `/config` UI |
| System locale (`$LANG`) | Auto-detected — `zh_TW` → Traditional Chinese, etc. |
| English | Final fallback |

```bash
ccstory week --lang en                       # one-off English (also accepts "English")
ccstory week --lang "Traditional Chinese"   # one-off
export CCSTORY_LANG="日本語"                  # shell-scoped
# or in ~/.ccstory/config.toml:
# language = "Spanish"
```

The value is dropped straight into the prompt as `Respond in <value>.`, so a
standard language name such as `"Traditional Chinese"`, `"日本語"`, or
`"pt-BR"` works across configured providers.

## Custom pricing

Default API list prices snapshot to `2026-07`. Every human-readable report
shows the snapshot date and warns once it is over 90 days old relative to the
report window end. ccstory makes no pricing network requests at runtime; model rates ship with each release and come from the LiteLLM registry.

Override per-model rates in `~/.ccstory/config.toml`:

```toml
[prices]
snapshot_date = "2026-08"

[prices.opus]
input       = 6.0
output      = 30.0
cache_write = 7.5
cache_read  = 0.6
```

Partial overrides are fine — unspecified keys keep their default. Defining a
brand-new model (`[prices.custom]`) with only some keys defaults the rest to
`$0` with a warning so misconfig is loud.

## How ccstory differs from ccusage

|  | [ccusage](https://github.com/ryoppippi/ccusage) | **ccstory** |
|---|---|---|
| Role | The bill | The story |
| Active hours (5-min gap heuristic) | — | ✅ |
| Activity categories | — | ✅ folder rules + content-aware |
| Per-session narrative | — | ✅ via configured local narrator |
| Per-bucket synthesis | — | ✅ |
| Cross-period narrative | — | ✅ |
| Conversation logs stay local / no telemetry | ✅ | ✅ |

Pair them — `ccusage monthly` for the spend, `ccstory month` for the
breakdown:

```bash
ccusage monthly
ccstory month
```

## Privacy and network behavior

ccstory never sends your conversation data to its own service or to the
Repo activity metadata providers. There is no ccstory telemetry or account.

- **Data source**: Current built-in providers read Claude Code logs under
  `~/.claude/projects/**/*.jsonl`, Codex live and archived rollouts under
  `~/.codex/{sessions,archived_sessions}/**/*.jsonl`, and Antigravity step
  logs under
  `~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl`
  and companion metadata under `~/.gemini/antigravity/conversations/*.db`.
  Future providers declare their own local roots; ccstory does not send
  transcript contents to a metadata service.
- **Narratives and classification**: invoke the configured local backend with
  its explicit model policy. In this release that policy defaults to
  `claude -p --model sonnet`, `codex exec --ephemeral --sandbox read-only
  --model gpt-5.6-terra`, then `agy -p --model gemini-3.6-flash-low --effort
  low`. The selected CLI contacts its provider using your signed-in session and
  plan quota; ccstory does not use your API key or operate a proxy.
- **GoalContext**: goal titles, source content/path/fingerprint, and
  attribution never enter narrator prompts or traces. Recap output exposes the
  goal rows, a sanitized source kind, and the fingerprint, but never the source
  path.
- **Pricing**: ccstory makes no pricing network requests. Model prices ship with each release and come from the LiteLLM registry.
- **Repo activity**: local git supplies repo-wide commit counts. If `gh` is
  installed and authenticated, ccstory sends the repo slug and report date
  range for at most 10 active repos to request matching PR timestamps, plus
  recent release timestamps and current star count. Exact report-window
  boundaries are applied locally. If GitHub is unavailable, the report
  explicitly remains local-only.
  The pypistats request sends a package name to pypistats.org. No conversation
  text, prompt, summary, local path, or commit contents are included.
- **Cache**: `~/.ccstory/cache.db` (sqlite, per-session summaries and private
  evidence fingerprints; reports expose only the status enum).
- **Reports**: `~/.ccstory/reports/recap-*.md`.

Disable GitHub/PyPI metadata calls with `--no-artifacts` or persistent
`[artifacts] enabled = false`. For a fully no-network report, also avoid
local narrator calls with `--minimal --classify folder` (and initialize with
`ccstory init --skip`). Relevant implementations are
[ccstory/artifacts.py](ccstory/artifacts.py) and
[ccstory/session_summarizer.py](ccstory/session_summarizer.py).

## Requirements

- **Python 3.11+** and **pipx**
  (`brew install pipx` on macOS, [other platforms](https://pipx.pypa.io/stable/installation/)).
- **At least one configured narrative CLI** for `--llm-narrative`, content
  classification, and cross-period synthesis. This release supports Claude
  Code (`claude`), Codex (`codex`), and Antigravity (`~/.local/bin/agy`); later
  releases may add providers. Without an available configured backend,
  narratives fall back to first/last user-message excerpts and `--classify`
  falls back to folder rules.

## Implementation notes

- **Time math**: 5-minute gap heuristic — consecutive messages within 5
  minutes count as active, longer gaps are "stepped away". Wall-clock dedup
  prevents parallel sessions from double-counting. The 5-min cap is a
  practical floor for "still at the keyboard"; comparable across periods
  even though not precise.
- **Timezone**: session timestamps are parsed UTC-aware. Window boundaries
  (`week`, `month`) are local-midnight aligned, so "this week" matches the
  calendar week you actually lived in. `--weeks N` for trend mode does the
  same.
- **Cost comparison**: cross-period diffs use **output tokens**, not
  `total_tokens`. In typical use ~96% of total_tokens is `cache_read`,
  which inflates with turn count and system prompt size and isn't a stable
  signal of work done. Output tokens stay comparable month over month.
- **Pricing**: prices are list prices snapshotted by date (default
  `2026-07`); every human-readable report shows the snapshot date and warns
  when it is over 90 days old relative to that report's window end.

## Library usage (integration API)

ccstory is primarily a CLI, but a small set of functions is maintained as a
**semi-stable integration API** for programmatic consumers — dashboards,
scripts, and the [MCP server](#mcp-server) below all call these instead of
shelling out to the CLI:

```python
from pathlib import Path

from ccstory.recap import build_recap, parse_window
from ccstory.goal_history import collect_goal_activity_history
from ccstory.goal_store import resolve_goal_context_source
from ccstory.project_discovery import collect_observed_projects
from ccstory.time_tracking import collect_sessions, rollup_by_category
from ccstory.categorizer import classify, load_rules

result   = build_recap("week")                   # one call = full recap
goals    = resolve_goal_context_source(
    config_path=Path.home() / ".ccstory" / "config.toml"
)
history  = collect_goal_activity_history(goals)  # 4 completed weekly buckets
since, until, _label = parse_window("all")
observed = collect_observed_projects(since, until)  # canonical project IDs
sessions = collect_sessions(since, until)        # any window, tz-aware
rollups  = rollup_by_category(sessions)          # per-bucket hours/share
bucket   = classify(project_dir)                 # folder-rule bucketing
rules    = load_rules()                          # parsed ~/.ccstory/config.toml
```

`build_recap()` runs the same pipeline as the CLI (the CLI is a thin shell
over it) and returns a `RecapResult`: rich objects (`.sessions`,
`.rollups`, `.usage`, narratives, comparison) plus `.markdown`,
`.report_path`, and `.to_json()` — the `schema_version: 1` envelope, same
shape as `--json` stdout. Keyword args mirror the CLI flags one-to-one
(`llm_narrative=`, `narrative=`, `classify=`, …); pass a Rich `Console` via
`console=` for progress output, or nothing for silence. An empty window
raises `RecapUnavailable` instead of exiting the process.

`collect_goal_activity_history()` is the focused, read-only alternative for
historical goal activity. It accepts one already-selected `GoalContext`,
collects all requested completed local weeks in one provider snapshot, and
returns the exact sanitized hour-denominated shape used by both
`ccstory goal-history` and MCP `get_goal_activity_history`. It defaults to 4
weeks, enforces a hard maximum of 24, never runs narrative generation, and
raises a clear error for a missing context.

`collect_observed_projects(since, until, agent="all", aliases=None,
config_path=None)` is the read-only alternative behind
`ccstory project list`. It scans one provider snapshot for the given window
and returns `ObservedProject` rows (`project_id`, `last_seen`,
`session_count`, `agents`, `category`, `category_source`, `relevance`)
through the same canonical normalization + alias fold as recap and
GoalContext — deterministic order, no transcript path or session id, no
persistent registry. It never drops a row; `partition_by_relevance(projects)`
is the separate, pure, order-preserving split into
`(relevant, filtered)` that the CLI's default view applies.

`classify()` applies the same folder rules the CLI does — a project pinned
with `ccstory category set <bucket> <leaf>` resolves identically in both.

Semi-stable means: signatures may still change with minor versions, but
renames and behavior changes are called out in the changelog instead of
happening silently. Everything else in the package is internal. The JSON
envelope (`--json`, `schema_version: 1`) is the other supported contract.

### Supplying an authoritative summary

An external tool that already knows what a session was about — another
program, a human-reviewed note — can write that summary once and have
ccstory treat it as final:

```python
from ccstory.session_summarizer import upsert

upsert(session_id, "Fixed the auth regression from the 0.7 release.",
       source="provided")
```

A `provided` row is authoritative: ccstory never overwrites or regenerates
it, even under `--refresh` or `--llm-narrative --force`. Callers may also
write their own prefixed `source` values (e.g. `"cloud:main"`) for their own
bookkeeping; ccstory stores those verbatim and treats them as any other
non-authoritative row.

## MCP server

```bash
pip install 'ccstory[mcp]'
ccstory mcp   # stdio MCP server — read-only, no fresh narrator call by default
```

Point any MCP-aware client at the `ccstory mcp` command and it can ask for your
recap live in conversation instead of you running the CLI and pasting output
back in. Claude Desktop and Claude Code are examples, not requirements. Example
client config (Claude Desktop's `claude_desktop_config.json`, or Claude Code's
MCP settings — same shape):

```json
{
  "mcpServers": {
    "ccstory": {
      "command": "ccstory",
      "args": ["mcp"]
    }
  }
}
```

Five read-only tools:

| Tool | Returns |
|---|---|
| `get_recap(window, classify, allow_llm, agent)` | Totals, per-category active hours + narrative + a `children` per-project breakdown (name + hours), legacy overall narrative, additive deterministic `top_focus_projection`, optional current goal-activity projection, top 5 sessions with `summary_evidence.status`, cost, usage coverage, unpriced models, and classification-source coverage. |
| `compare_to_previous(window, classify, agent)` | Active-hours and cost deltas vs. the immediately preceding same-length window, with current/previous usage coverage and unpriced models. |
| `get_trend(period, count, classify, agent)` | Per-period series over the last `count` weeks/months (oldest first): active hours, cost, per-category hours, usage coverage, unpriced models, and classification-source coverage. `count` clamped to 1..24. |
| `get_goal_activity_history(weeks, agent)` | The same sanitized JSON object as `ccstory goal-history`: 4 completed local ISO weeks by default (max 24, invalid counts rejected), every effective goal including zero activity, exclusive/shared/unattributed hours, unavailable activity coverage, source fingerprint, and explicit accounting/disclaimer semantics. Requires configured or managed GoalContext. |
| `list_categories()` | The bucket rules ccstory classifies sessions into (user + built-in defaults). |

`window` accepts `week` / `month` / `all` / `YYYY-MM`, same as the CLI;
`period` is `week` or `month`. Default `classify="folder"` and
`allow_llm=False` never triggers a fresh narrator call — an MCP client
may call these tools opportunistically mid-conversation, so nothing here
should cost you latency or tokens unless you explicitly ask for it
(`classify="content"` / `"hybrid"`, or `allow_llm=True` on `get_recap`;
`compare_to_previous` and `get_trend` stay cache-only under every
parameter combination). `get_goal_activity_history` bypasses classification,
cache, report writing, and narrative generation entirely and performs one
provider snapshot for all of its requested weeks.

The recap/comparison/trend MCP responses are a third, distinct JSON contract,
not the same shape as either
of the two above: not `--json` / `RecapResult.to_json()` (which lists
every session in the window) and not the Python function signatures.
MCP responses are deliberately compact — top 5 sessions, not the full
list — so they're cheap for an agent to read into its own context, and
never include raw transcript text, only summaries. Goal history is the focused
exception: its MCP success object is intentionally identical to the dedicated
CLI/library projection so local visualizers do not need a second schema.


## Roadmap

- [x] `--json` structured output — one general primitive over per-destination
      export flavors
- [ ] Optional PNG card export
- [ ] `ccstory year` — annual recap (Spotify-Wrapped style)
- [x] Git commit / PR correlation — period-level **Repo activity** section
      (per-session attribution still open, #11)

The version directions below are plans, not completed product capabilities;
their scope may change as the evidence and privacy contracts are validated.

- **0.8 — Trustworthy evidence pipeline:** freshness and window-purity
  guarantees, provider snapshots, metadata-only provenance traces, and
  deterministic sampling.
- **0.9 — Correctable, corroborated local memory:** preserve explicit
  corrections and corroborate local evidence before reuse.
- **1.0 — Longitudinal, shareable work memory:** connect work over time and
  support intentional, privacy-aware sharing.

See the [issue tracker](https://github.com/atomchung/ccstory/issues) for the
full backlog.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT — see [LICENSE](LICENSE).
