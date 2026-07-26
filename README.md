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
> sessions are reused (no re-burn) unless their prompt version is stale;
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
| `--classify folder` | Folder-name rules only |
| `--classify content` | Configured narrator reads each session |
| `--classify hybrid` | User rule wins, else content (default) |

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
| ccstory  | 5       | 3          | v0.6.1   | 42 (+6) |
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

`Top focus` is the largest Category and its representative session. `## What
you did` is the separate cross-Category integration: 2-4 goal threads (bold
header + bullets) that explain what the period added up to. For real
retrospectives, `--narrative` goes deeper:

```bash
ccstory week --narrative per-category   # header + bullets per bucket instead
ccstory week --narrative both           # overall first, then per-bucket
```

Each bucket costs one configured local-narrator call, cached until its exact
input, prompt, or narrator policy changes — rerunning the same window is
normally free. A bucket whose synthesis fails (or that has no real summaries)
is simply omitted; the report never blocks on it. In `--json` mode the same
text lands in `buckets[].narrative`.

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
narrator. Existing unprovenanced cache rows refresh on the next
`--llm-narrative` run.

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
| `--llm-narrative` | 1 per uncached or stale session | Reused per session; `--refresh` deliberately regenerates |

The default recap uses hybrid classification, an overall narrative, and a
previous-window narrative; per-session LLM prose remains opt-in. Use
`--narrative per-category|both` to trade the overall call for, or add, bucket
calls. `--no-aggregate`, `--no-compare-narrative`, and `--classify folder`
remove those call types; `--minimal --classify folder` makes the recap itself
use zero local-narrator calls.

Deep/content classification is batched; per-session `--llm-narrative` work is
linear and the CLI budgets roughly 40 seconds per cold session, showing an ETA
before it starts. Aggregate call latency varies with the selected CLI startup and
input size. A same-window rerun is usually cache-only, but new sessions,
changed inputs/config, `--refresh`, or a newer prompt version can trigger fresh
calls. Content classification carries accepted bucket names into later
80-session batches and enforces one run-wide vocabulary cap, preventing a
large first run from fragmenting one theme into several near-duplicate labels.

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
```

stdout is pure JSON (progress goes to stderr, same as markdown mode), so
`ccstory week --json | jq .totals.active_hours` just works. The envelope
carries `schema_version` (currently 1): renames/removals bump it, additive
fields don't — consumers should tolerate unknown keys. Covers window, totals
(hours/tokens/cost/cache), buckets, per-session lines, model breakdown, unpriced models (`unpriced_models`), provider coverage (`usage_coverage`), narrative, comparison, artifacts, and the pricing snapshot date. The markdown
report file is still written either way; JSON is a view, not a replacement.

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
- **Pricing**: ccstory makes no pricing network requests. Model prices ship with each release and come from the LiteLLM registry.
- **Repo activity**: local git supplies repo-wide commit counts. If `gh` is
  installed and authenticated, ccstory sends the repo slug and report date
  range for at most 10 active repos to request matching PR timestamps, plus
  recent release timestamps and current star count. Exact report-window
  boundaries are applied locally. If GitHub is unavailable, the report
  explicitly remains local-only.
  The pypistats request sends a package name to pypistats.org. No conversation
  text, prompt, summary, local path, or commit contents are included.
- **Cache**: `~/.ccstory/cache.db` (sqlite, per-session summaries).
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
from ccstory.recap import build_recap
from ccstory.time_tracking import collect_sessions, rollup_by_category
from ccstory.categorizer import classify, load_rules

result   = build_recap("week")                   # one call = full recap
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

Semi-stable means: signatures may still change with minor versions, but
renames and behavior changes are called out in the changelog instead of
happening silently. Everything else in the package is internal. The JSON
envelope (`--json`, `schema_version: 1`) is the other supported contract.

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

Four read-only tools:

| Tool | Returns |
|---|---|
| `get_recap(window, classify, allow_llm, agent)` | Totals, per-category active hours + narrative + a `children` per-project breakdown (name + hours), the overall narrative, top 5 sessions, cost, usage coverage, and unpriced models. |
| `compare_to_previous(window, classify, agent)` | Active-hours and cost deltas vs. the immediately preceding same-length window, with current/previous usage coverage and unpriced models. |
| `get_trend(period, count, classify, agent)` | Per-period series over the last `count` weeks/months (oldest first): active hours, cost, per-category hours, usage coverage, and unpriced models. `count` clamped to 1..24. |
| `list_categories()` | The bucket rules ccstory classifies sessions into (user + built-in defaults). |

`window` accepts `week` / `month` / `all` / `YYYY-MM`, same as the CLI;
`period` is `week` or `month`. Default `classify="folder"` and
`allow_llm=False` never triggers a fresh narrator call — an MCP client
may call these tools opportunistically mid-conversation, so nothing here
should cost you latency or tokens unless you explicitly ask for it
(`classify="content"` / `"hybrid"`, or `allow_llm=True` on `get_recap`;
`compare_to_previous` and `get_trend` stay cache-only under every
parameter combination).

**This is a third, distinct JSON contract**, not the same shape as either
of the two above: not `--json` / `RecapResult.to_json()` (which lists
every session in the window) and not the Python function signatures.
MCP responses are deliberately compact — top 5 sessions, not the full
list — so they're cheap for an agent to read into its own context, and
never include raw transcript text, only summaries.


## Roadmap

- [x] `--json` structured output — one general primitive over per-destination
      export flavors
- [ ] Optional PNG card export
- [ ] `ccstory year` — annual recap (Spotify-Wrapped style)
- [x] Git commit / PR correlation — period-level **Repo activity** section
      (per-session attribution still open, #11)

See the [issue tracker](https://github.com/atomchung/ccstory/issues) for the
full backlog.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT — see [LICENSE](LICENSE).
