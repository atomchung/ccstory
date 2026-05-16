# ccstory

> **ccusage tells you the bill. ccstory tells the story.**

A Claude Code usage recap that answers the question token counters can't:
**what did you actually do?**

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
│  investment      █████████████░░░░░░░░░░░░░░░    9.6h    47%         │
│  writing         ░░░░░░░░░░░░░░░░░░░░░░░░░░░░    0.1h     0%         │
│                                                                      │
│  Full report → ~/.ccstory/reports/recap-2026-W19.md                  │
│                                                                      │
╰────────────────────────────── ccstory ───────────────────────────────╯
```

(In your terminal each bucket gets its own color — investment in green, coding
in cyan, writing in magenta — and `★ Top focus` highlights the biggest bucket
with the longest session's narrative.)

The markdown report goes further — each bucket gets a 2-3 sentence **synthesis**
across its sessions, followed by per-session one-liners, all written by your
own local `claude -p`:

```
### investment

Investment work centered on portfolio thesis maintenance and AI-chain
deep-dives: parallel wiki refreshes across NVDA/AMD/MU/TSLA/ORCL/DDOG, a
Cambricon SELL initiation at RMB 650, plus targeted analyses of AVGO-OpenAI
Nexus, CoreWeave, Cloudflare, and ServiceNow's agentic-coding thesis.

- 2026-05-10 03:24 · 123m · 212 msg — Researched which software vendors
  benefit or suffer from agentic coding's rise across the dev-to-ops chain.
- 2026-05-08 12:30 · 67m · 294 msg — Produced SELL rating and RMB 650
  target price for Cambricon Technologies initiating coverage report.
```

The synthesis layer is what makes ccstory more than a fancier ccusage:
numbers + per-session lines you can get elsewhere, but the cross-session
**thread** — what was this category actually about this week? — only emerges
when you let the model read all of them together.

## What ccstory gives you that ccusage doesn't

| | [ccusage](https://github.com/ryoppippi/ccusage) | **ccstory** |
|---|---|---|
| Role | The bill | The story |
| Token / cost precision | ✅ daily/monthly/session/5h-block | (configurable; rough by default) |
| Per-model breakdown | ✅ | ✅ |
| **Active hours** (5-min gap heuristic) | ❌ | ✅ |
| **Activity categories** | ❌ | ✅ folder rules **or** content-aware via `claude -p` |
| **One-sentence narrative per session** | ❌ | ✅ via local `claude -p` |
| **Per-bucket synthesis** (cross-session thread) | ❌ | ✅ |
| **Cross-period narrative** ("focus shifted from X to Y") | ❌ | ✅ |
| **Obsidian-flavored export** (YAML + `[[wikilinks]]`) | ❌ | ✅ via `--for=obsidian` |
| **Output-tokens-based period comparison** | ❌ (uses total_tokens) | ✅ |
| Live quota | ⚠️ via `blocks` | ❌ |

**They're complementary, not competing.** Pair both:

```bash
ccusage monthly        # how much you spent
ccstory month          # what you spent it on
```

## Install

ccstory ships in two layers — the **CLI** (does the work) and the **Claude Code plugin** (lets you invoke it inside a Claude Code chat as `/ccstory:recap`).

### Option 1 — CLI only (terminal users)

```bash
pipx install git+https://github.com/atomchung/ccstory.git
ccstory init       # one-time auto-categorize from recent sessions
ccstory week       # generate a recap
```

### Option 2 — CLI + Claude Code plugin (so `/ccstory:recap` works in chat)

**1. Install the CLI in a terminal:**

```bash
pipx install git+https://github.com/atomchung/ccstory.git
pipx ensurepath        # restart your shell after this if `ccstory` isn't found
```

**2. Inside a Claude Code session, add the marketplace and install the plugin** (these are Claude Code slash commands, not shell commands):

```text
/plugin marketplace add atomchung/ccstory
/plugin install ccstory@ccstory
```

After that, in any Claude Code session: `/ccstory:recap` (or just ask "what did I do this week?" and Claude will trigger it).

### Requirements

- **Python 3.11+** and **pipx** (`brew install pipx` on macOS, [other platforms](https://pipx.pypa.io/stable/installation/))
- **Claude Code CLI** on PATH — used for per-session narrative summaries. Without it, narratives fall back to the first user message. If `/plugin` is missing inside Claude Code, update to the latest version per the [Claude Code troubleshooting docs](https://code.claude.com/docs/en/troubleshooting).

## Usage

```bash
ccstory init                  # one-shot: scan recent sessions and propose buckets
ccstory init --dry-run        # preview without writing config

ccstory                       # current month so far (default)
ccstory week                  # past 7 days
ccstory 2026-04               # any specific month
ccstory all                   # entire history

ccstory trend                 # last 8 weeks of sparklines
ccstory trend --weeks 12      # custom range
ccstory trend --months 6      # by calendar months

# Narrative depth
ccstory --minimal             # numbers only, no per-session lines (fastest)
ccstory --llm-narrative       # polish per-session via claude -p (slow, opt-in)
ccstory --no-aggregate        # skip the per-bucket synthesis

# Comparison block
ccstory --no-compare          # skip the vs-previous block entirely
ccstory --no-compare-narrative # keep the numeric deltas, drop the synthesis prose

# Classification mode (how sessions get bucketed)
ccstory --classify folder     # folder-name rules only
ccstory --classify content    # batch claude -p over each session's narrative
ccstory --classify hybrid     # folder rule when config.toml matched, else content (default)

# Export flavor
ccstory --for=obsidian        # YAML frontmatter + [[wikilinks]] for PKM vaults
```

**Recommended first run**: `ccstory init` scans the last 30 days of sessions
and asks claude (via a single `claude -p` call, ~15s) to suggest a category
bucket for each project. It writes a starter `~/.ccstory/config.toml` you can
edit later.

`ccstory week` / `ccstory month` automatically appends a **vs-previous-window**
comparison (▲/▼ deltas per bucket) with a 1-2 sentence narrative on what
shifted. `ccstory trend` shows per-bucket sparklines so you can see the shape
of your usage across N weeks/months in one glance:

```
Hours by bucket
total          ▁▄▆▇▃█    16.5h   avg 9.0h   ▲ +183%
investment     ▁▃▅█▆█     6.3h   avg 4.0h   ▲ +29%
coding         ▁▂▃▄▁█    10.2h   avg 3.3h   ▲ +1148%
writing        ▁▇█▆▁▁     0.1h   avg 1.8h   ▼ -51%

Overall
output         ▁▁▁▄▁█     3.0M   avg 0.8M   ▲ +2460%
cost           ▁▁▂▃▁█   $1,643   avg $463   ▲ +1877%
burn %         ▁▁▂▃▁█     201%   avg 57%    ▲ +1877%
```

The `burn %` row shows API-equivalent cost as a percentage of your prorated
monthly quota — set `monthly_quota_usd` in `~/.ccstory/config.toml`
(defaults to $3,500 ≈ Max 20x plan). Set to `0` to hide the row.

First run scaffolds `~/.ccstory/config.toml` and shows you how your projects
got bucketed.

## Categories

Four default buckets, matched against the project folder name:

| Bucket | Keywords (sample) |
|---|---|
| `investment` | investment, stock, portfolio, trading, ticker, etf, finance |
| `writing` | blog, newsletter, post, docs, content, article |
| `coding` | app, sdk, cli, plugin, mcp, server, frontend, backend, lib, … |
| `other` | playground, scratch, sandbox, experiment |

Unmatched projects fall back to `coding` — per the 2026 Pragmatic Engineer
dev survey, ~46% of Claude Code usage is software development.

Customize via `~/.ccstory/config.toml`:

```toml
default_bucket = "coding"

[categories]
"work"    = ["company-repo", "internal-tool"]
"writing" = ["blog", "newsletter", "essay"]
```

Matching rules:

- Tokens are split on `-` from the **normalized** project leaf
  (worktree suffix and path prefix get stripped).
- First-match-wins, case-insensitive.
- Your rules take precedence over built-in defaults.

## Content-aware classification

Folder names lie. A session in your `playground/` repo could be a serious
debugging dive into a production bug; a session in `myapp/` could be a 5-min
README tweak. `--classify` lets `claude -p` look at what each session was
*actually* about (its first/last messages) and re-bucket accordingly.

| Mode | What it does |
|---|---|
| `folder` | Pure folder-name rules from `config.toml`. Fastest, no `claude -p` calls. |
| `content` | Every session gets re-bucketed by content, one batched `claude -p` per ~80 sessions. |
| `hybrid` (default) | If a *user-defined* rule in `config.toml` matched the folder, keep that bucket (explicit overrides win). Otherwise fall back to content classification. |

Results cache in `~/.ccstory/cache.db` keyed by session id, so subsequent
runs are free.

## Obsidian export

`ccstory --for=obsidian` swaps the plain markdown for a PKM-vault-ready
variant:

```yaml
---
date_start: 2026-05-10
date_end: 2026-05-17
active_hours: 20.6
top_focus: coding
buckets: [coding, investment, writing]
cost_usd: 1608.42
output_tokens: 2920000
---
```

YAML frontmatter is queryable in Obsidian's Dataview / Bases
(`WHERE top_focus = "coding"`), and per-session lines wrap the project leaf
in `[[wikilinks]]` so the report drops into a vault with live cross-linking
on day one. Bucket names with special characters are JSON-quoted so the
frontmatter stays valid even for buckets like `client: acme, inc`.

## Custom pricing

Default API list prices snapshot to a date (currently `2026-01`) and the
report footer always shows that snapshot date so a stale price table can't
silently distort cost over time.

Override per-model in `~/.ccstory/config.toml` (e.g. when Anthropic ships
new pricing or you're modeling a custom contract):

```toml
[prices]
snapshot_date = "2026-04"

[prices.opus]
input       = 15.0
output      = 75.0
cache_write = 18.75
cache_read  = 1.5

[prices.sonnet]
input       = 3.0
output      = 15.0
cache_write = 3.75
cache_read  = 0.3
```

Partial overrides are fine — unspecified keys keep their default. Defining a
brand-new model name (e.g. `[prices.custom]`) with only some keys defaults the
missing ones to `$0`, with a warning so misconfig is loud.

## Privacy

Everything runs locally. ccstory never sends your conversation data anywhere.

- **Data source**: `~/.claude/projects/**/*.jsonl` — Claude Code's own logs.
- **Narratives**: subprocess-call your *local* `claude -p`, which uses your
  own Claude Code session / quota. No API key required, no cost to ccstory.
- **Cache**: `~/.ccstory/cache.db` (sqlite, per-session summaries).
- **Reports**: `~/.ccstory/reports/recap-*.md`.

No telemetry, no network calls, no upload buttons. The repo can verify this
in [ccstory/session_summarizer.py](ccstory/session_summarizer.py).

## How time is measured

5-minute gap heuristic: consecutive messages within 5 minutes count as
active; longer gaps are treated as "stepped away". Not precise, but stable
enough to compare across periods. Wall-clock dedup ensures parallel
sessions don't double-count.

## Cross-period comparison

When you run ccstory across multiple periods, the markdown report uses
**output tokens** for comparison, not `total_tokens`. Why? In typical use,
96%+ of `total_tokens` is `cache_read`, which inflates with turn count and
system prompt size — it's not a stable signal of actual work done. Output
tokens stay comparable month over month.

On top of the numeric deltas table, week/month reports include a 1-2
sentence **synthesis narrative** describing how focus shifted between
windows (which bucket grew or shrank most, what replaced what). The
narrative caches on `(window_keys, summary_content, deltas)` so it
regenerates whenever any of those change. Drop it with
`--no-compare-narrative` if you only want the numbers.

## Roadmap

- [x] v0.1 — Time + tokens + per-session narrative + 4-bucket defaults
- [x] v0.1.1 — Per-bucket colors, date-range title, ★ Top focus highlight
- [x] v0.1.2 — vs-previous-window comparison + `ccstory trend` sparklines
- [x] v0.1.3 — `ccstory init` auto-categorization + quota burn % in trend
- [x] v0.1.5 — Claude Code plugin wrapper (`/ccstory:recap` in chat) +
      self-hosted marketplace so this repo is installable without official approval
- [x] v0.2.0 — Per-category aggregate narrative wired into the default flow
      (2-3 sentence synthesis per bucket; `--no-aggregate` to skip)
- [x] v0.3.0 — Instant fallback default + import from `/recap` cache;
      tz-aware datetime correctness; pytest suite + CI;
      content-aware classification (`--classify hybrid`); configurable
      pricing with snapshot disclosure; cross-period narrative synthesis;
      Obsidian export (`--for=obsidian`); `--no-summary` renamed to
      `--minimal` (old name deprecated, still works)
- [ ] v0.4 — More export flavors (Logseq, Notion)
- [ ] v0.5 — Optional PNG card export

## License

MIT — see [LICENSE](LICENSE).
