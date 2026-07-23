# ccstory

> **Your Claude Code week, in plain English.**
> Reads `~/.claude/projects/**/*.jsonl` locally and writes a categorized recap
> with active hours, costs, and a per-bucket narrative.

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

The default **What shipped** section may query GitHub and PyPI metadata.
For a first run with no network access at all, use:

```bash
ccstory init --skip
ccstory week --minimal --classify folder --no-artifacts
```

`--no-artifacts` alone disables ccstory's GitHub/PyPI lookups while keeping
the normal narrative flow, which may invoke your installed Claude Code CLI.

## Demo

```

╭──────────────── Claude Code Recap · May 5 – 12, 2026 ────────────────╮
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
lines from the instant first/last-message fallback to claude-polished prose:

> **Re-running upgrades retroactively.** If you viewed a window in the
> default (instant) mode first, re-running it with `--llm-narrative` upgrades
> those cached fallbacks to polished summaries — so `ccstory month
> --llm-narrative` polishes weeks you already skimmed. Already-polished
> sessions are reused (no re-burn) unless their prompt version is stale;
> add `--refresh` to force every in-window summary to regenerate (e.g. after
> a `claude` model upgrade you want reflected).

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
| `--llm-narrative` | `claude -p` per-session prose (slow, opt-in) |
| `--no-aggregate` | Skip the per-bucket synthesis |

**Comparison block** (vs-previous, auto-attached to week/month)

| Flag | What it does |
|---|---|
| `--no-compare` | Skip the entire block |
| `--no-compare-narrative` | Keep numeric deltas, drop the prose |

**Coding agent**

| Flag | What it does |
|---|---|
| `--agent all` (default) | Every agent ccstory can read |
| `--agent claude` | Claude Code only (`~/.claude/projects`) |
| `--agent codex` | OpenAI Codex only (`~/.codex/sessions`) |

Also accepted by `ccstory trend`, so a trend line and a week over the same range
describe the same population. See [Multiple coding agents](#multiple-coding-agents)
for what the numbers mean once more than one agent is in the window.

**Session classification mode**

| Flag | What it does |
|---|---|
| `--classify folder` | Folder-name rules only |
| `--classify content` | `claude -p` reads each session |
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
(`--classify content` / `hybrid`), where one batched `claude -p` call
re-buckets sessions by what they were actually about. An override changes a
session's *area* only — its project is the physical fact of which folder the
work happened in, never reassigned. Results cache in `~/.ccstory/cache.db` so
reruns are free.

## Multiple coding agents

ccstory reads Claude Code (`~/.claude/projects`) and OpenAI Codex
(`~/.codex/sessions`, plus `archived_sessions`) by default. Codex sessions are
attributed to a project from the `cwd` the transcript records, folded through
the same rules Claude Code project folders get — including git worktrees, so a
detached checkout counts toward the repo it came from rather than becoming its
own one-off project.

**Time is reported once, not per agent.** Agents run concurrently: a Codex
review and a Claude Code session routinely occupy the same ten minutes. Summing
their active time double-counts that overlap — on a real week here, raw
per-agent time added up to 177h against a deduplicated wall clock of 64h. So:

- **Total active time** is the wall clock across every session, deduplicated —
  the same number ccstory has always reported, now spanning all agents.
- **The `Coding agents` block reports shares, not hours.** Each agent's share is
  its raw interaction time relative to the others'. Shares are not durations and
  do not add up to the total.
- **Session share is shown next to time share, and they disagree on purpose.**
  Many short Codex reviews against fewer long Claude Code sessions shows up as
  75% / 25% of time but 51% / 49% of sessions — that gap is the finding.
- **`N× parallel`** is raw agent time ÷ wall clock: how much of the work
  overlapped.

Token counts and costs still cover Claude Code only — Codex usage appears in the
time breakdown but not in the cost numbers, and the report says so inline.

Use `--agent claude` to get the pre-multi-agent numbers back.

## What shipped

Time tells half the story; the other half is what the time produced. Each
report includes a **What shipped** section — per-repo output metrics for the
repos you actually worked in during the window:

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
  **PRs merged / releases / stars** need the `gh` CLI; the lookup sends the
  GitHub repo slug and requests recent merged-PR/release timestamps plus the
  current star count. ccstory applies the report window locally. Without `gh`,
  those columns degrade to `–`. **PyPI downloads** send the package name to
  pypistats.org for packages auto-detected in active repos' `pyproject.toml`.
- The artifacts collector never sends conversation text, prompts, summaries,
  commit contents, or local paths. It uses only repository/package metadata.
- **Stars delta** compares against the last snapshot taken before the window,
  so it becomes meaningful from your second run onward.

Skip all GitHub/PyPI metadata calls per run with `--no-artifacts`, or
persistently via config:

```toml
[artifacts]
enabled = false            # no GitHub/PyPI metadata lookups
exclude = ["playground"]   # substring match on repo path
pypi = ["my-package"]      # extra packages beyond auto-detection
```

## Narrative depth

`## What you did` is 2-4 goal threads (bold header + bullets) by default. For
real retrospectives, `--narrative` goes deeper:

```bash
ccstory week --narrative per-category   # header + bullets per bucket instead
ccstory week --narrative both           # overall first, then per-bucket
```

Each bucket costs one `claude -p` call, cached until its exact input or prompt
changes — rerunning the same window is normally free. A bucket whose synthesis
fails (or that has no real summaries) is simply omitted; the report never
blocks on it. In `--json` mode the same text lands in `buckets[].narrative`.

## Claude CLI calls, latency, and quota

There is no single fixed call total: it depends on init mode, uncached
sessions, narrative depth, and which cache entries already exist. Every
`claude -p` call runs through your installed Claude Code CLI and uses that
CLI's signed-in plan/quota; ccstory does not use an API key or add a separate
API charge.

| Operation | Fresh `claude -p` calls | Cache behavior |
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
use zero Claude calls.

Deep/content classification is batched; per-session `--llm-narrative` work is
linear and the CLI budgets roughly 40 seconds per cold session, showing an ETA
before it starts. Aggregate call latency varies with Claude CLI startup and
input size. A same-window rerun is usually cache-only, but new sessions,
changed inputs/config, `--refresh`, or a newer prompt version can trigger fresh
calls. Content classification carries accepted bucket names into later
80-session batches and enforces one run-wide vocabulary cap, preventing a
large first run from fragmenting one theme into several near-duplicate labels.

If `claude` is absent from `PATH`, LLM classification and synthesis degrade
gracefully: classification uses folder/fallback rules, per-session prose uses
the local first/last-message fallback, and Claude quota usage is zero. This does
not disable What-shipped metadata calls; add `--no-artifacts` for that.

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
(hours/tokens/cost/cache), buckets, per-session lines, model breakdown,
narrative, comparison, artifacts, and the pricing snapshot date. The markdown
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

ccstory delegates narrative writing to your local `claude -p`. By default
it inherits whatever language Claude Code itself responds in; override it
per run, per shell, or persist a per-tool choice.

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
ccstory week --lang "Traditional Chinese"   # one-off
export CCSTORY_LANG="日本語"                  # shell-scoped
# or in ~/.ccstory/config.toml:
# language = "Spanish"
```

The value is dropped straight into the prompt as `Respond in <value>.`,
so any name Claude can parse (`"Traditional Chinese"`, `"日本語"`,
`"pt-BR"`) works.

## Custom pricing

Default API list prices snapshot to `2026-07`. Every human-readable report
shows the snapshot date and warns once it is over 90 days old relative to the
report window end. This is a date-only reminder, not a live pricing lookup.
Override per-model in `~/.ccstory/config.toml`:

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
| Per-session narrative | — | ✅ via local `claude -p` |
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
What-shipped metadata providers. There is no ccstory telemetry or account.

- **Data source**: `~/.claude/projects/**/*.jsonl` — Claude Code's own logs.
- **Narratives and classification**: subprocess-call your locally installed
  `claude -p`. The Claude CLI contacts Anthropic using your signed-in session
  and plan quota; ccstory does not use your API key or operate a proxy.
- **What shipped**: local git supplies commit counts. By default, `gh` may send
  a repo slug and request recent PR/release timestamps plus the current star
  count from GitHub; ccstory filters timestamps to the report window locally.
  The pypistats request sends a package name to pypistats.org. No conversation
  text, prompt, summary, local path, or commit contents are included.
- **Cache**: `~/.ccstory/cache.db` (sqlite, per-session summaries).
- **Reports**: `~/.ccstory/reports/recap-*.md`.

Disable GitHub/PyPI metadata calls with `--no-artifacts` or persistent
`[artifacts] enabled = false`. For a fully no-network report, also avoid
Claude CLI calls with `--minimal --classify folder` (and initialize with
`ccstory init --skip`). Relevant implementations are
[ccstory/artifacts.py](ccstory/artifacts.py) and
[ccstory/session_summarizer.py](ccstory/session_summarizer.py).

## Requirements

- **Python 3.11+** and **pipx**
  (`brew install pipx` on macOS, [other platforms](https://pipx.pypa.io/stable/installation/)).
- **Claude Code CLI** on `PATH` — required for `--llm-narrative`, content
  classification, and the cross-period synthesis. Without it, narratives
  fall back to first/last user-message excerpts and `--classify` falls back to
  folder rules.

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
ccstory mcp   # stdio MCP server — read-only, no fresh `claude -p` by default
```

Point any MCP-aware client (Claude Desktop, Claude Code, or another local
agent) at the `ccstory mcp` command and it can ask for your recap live in
conversation instead of you running the CLI and pasting output back in.
Example client config (Claude Desktop's `claude_desktop_config.json`, or
Claude Code's MCP settings — same shape):

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
| `get_recap(window, classify, allow_llm)` | Totals, per-category active hours + narrative + a `children` per-project breakdown (name + hours), the overall narrative, top 5 sessions, cost. |
| `compare_to_previous(window, classify)` | Active-hours and cost deltas vs. the immediately preceding same-length window. |
| `get_trend(period, count, classify)` | Per-period series over the last `count` weeks/months (oldest first): active hours, cost, per-category hours. `count` clamped to 1..24. |
| `list_categories()` | The bucket rules ccstory classifies sessions into (user + built-in defaults). |

`window` accepts `week` / `month` / `all` / `YYYY-MM`, same as the CLI;
`period` is `week` or `month`. Default `classify="folder"` and
`allow_llm=False` never trigger a fresh `claude -p` call — an MCP client
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
- [x] Git commit / PR correlation — period-level **What shipped** section
      (per-session attribution still open, #11)

See the [issue tracker](https://github.com/atomchung/ccstory/issues) for the
full backlog.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT — see [LICENSE](LICENSE).
