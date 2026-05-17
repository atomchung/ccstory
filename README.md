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
pipx install git+https://github.com/atomchung/ccstory.git
ccstory init
ccstory week
```

That's it. `init` is a one-time auto-categorize step that scans your
recent sessions; `ccstory week` produces the recap. Full report saves to
`~/.ccstory/reports/recap-*.md`.

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
│  Full report → ~/.ccstory/reports/recap-2026-W19.md                  │
│                                                                      │
╰────────────────────────────── ccstory ───────────────────────────────╯
```

The markdown report adds a **2–3 sentence synthesis per bucket** plus
per-session one-liners. Run with `--llm-narrative` to upgrade per-session
lines from the instant first-user-msg fallback to claude-polished prose:

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

**Refresh (apply rule changes retroactively)**

| Flag | What it does |
|---|---|
| `--refresh` | Re-classify cached sessions in this window after a rule edit |
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

Four default buckets, matched against the project folder name:

| Bucket | Keywords (sample) |
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
"work"    = ["company-repo", "internal-tool"]
"writing" = ["blog", "newsletter", "essay"]
```

Folder rules can be overridden per-session by content (`--classify content` /
`hybrid`), where one batched `claude -p` call re-buckets sessions by what they
were actually about. Results cache in `~/.ccstory/cache.db` so reruns are
free.

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

## Custom pricing

Default API list prices snapshot to `2026-01`. The report footer always
shows the snapshot date so a stale price table can't silently distort cost
over time. Override per-model in `~/.ccstory/config.toml`:

```toml
[prices]
snapshot_date = "2026-04"

[prices.opus]
input       = 15.0
output      = 75.0
cache_write = 18.75
cache_read  = 1.5
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
| Local-only / no telemetry | ✅ | ✅ |

Pair them — `ccusage monthly` for the spend, `ccstory month` for the
breakdown:

```bash
ccusage monthly
ccstory month
```

## Privacy

Everything runs locally. ccstory never sends your conversation data
anywhere.

- **Data source**: `~/.claude/projects/**/*.jsonl` — Claude Code's own logs.
- **Narratives**: subprocess-call your *local* `claude -p` (uses your own
  session / quota, no API key needed, no cost to ccstory).
- **Cache**: `~/.ccstory/cache.db` (sqlite, per-session summaries).
- **Reports**: `~/.ccstory/reports/recap-*.md`.

No telemetry, no network calls, no upload buttons. Verify in
[ccstory/session_summarizer.py](ccstory/session_summarizer.py).

## Requirements

- **Python 3.11+** and **pipx**
  (`brew install pipx` on macOS, [other platforms](https://pipx.pypa.io/stable/installation/)).
- **Claude Code CLI** on `PATH` — required for `--llm-narrative`, content
  classification, and the cross-period synthesis. Without it, narratives
  fall back to the first user message and `--classify` falls back to
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
  `2026-01`); the snapshot date renders in every report footer so stale
  numbers can't sneak past unnoticed.

## Roadmap

- [ ] More export flavors (Logseq, Notion)
- [ ] Optional PNG card export
- [ ] `ccstory year` — annual recap (Spotify-Wrapped style)
- [ ] Git commit / PR correlation per session

See the [issue tracker](https://github.com/atomchung/ccstory/issues) for the
full backlog.

## License

MIT — see [LICENSE](LICENSE).
