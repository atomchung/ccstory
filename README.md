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

The markdown report goes further — one-sentence narrative per session, written
by your own local `claude -p`:

```
### investment

- 2026-05-09 10:59 · 28m · 76 msg — Evaluated ONDS pre-earnings add/trim
  strategy and defined scoring metrics for AI semis exposure.
- 2026-05-08 23:53 · 22m · 88 msg — Screened Q1 AI application-layer winners
  after the megacap earnings wave to identify next-leg setups.
```

## What ccstory gives you that ccusage doesn't

| | [ccusage](https://github.com/ryoppippi/ccusage) | **ccstory** |
|---|---|---|
| Role | The bill | The story |
| Token / cost precision | ✅ daily/monthly/session/5h-block | (rough estimate) |
| Per-model breakdown | ✅ | ✅ |
| **Active hours** (5-min gap heuristic) | ❌ | ✅ |
| **Activity categories** (not just folder name) | ❌ | ✅ |
| **One-sentence narrative per session** | ❌ | ✅ via local `claude -p` |
| **Output-tokens-based period comparison** | ❌ (uses total_tokens) | ✅ |
| Live quota | ⚠️ via `blocks` | ❌ |

**They're complementary, not competing.** Pair both:

```bash
ccusage monthly        # how much you spent
ccstory month          # what you spent it on
```

## Install

```bash
pipx install ccstory
# or, for one-off:
pip install ccstory
```

Requires Python 3.11+. No external CLI needed for the default fast path —
ccstory reads the `aiTitle` records Claude Code already writes into each
session's jsonl (the gray title at the top of the CLI). For outcome-focused
narratives ("Refactored auth middleware…"), use `--rich`, which falls back to
your local `claude -p` when an `aiTitle` is missing.

## Usage

```bash
ccstory init             # one-shot: scan recent sessions and propose buckets
ccstory init --dry-run   # preview without writing config

ccstory                  # current month so far (default)
ccstory week             # past 7 days
ccstory 2026-04          # any specific month
ccstory all              # entire history

ccstory trend            # last 8 weeks of sparklines
ccstory trend --weeks 12 # custom range
ccstory trend --months 6 # by calendar months

ccstory --rich           # outcome-focused narratives via local `claude -p`
ccstory --no-summary     # skip per-session narratives entirely
ccstory --no-compare     # skip the vs-previous block
```

**Recommended first run**: `ccstory init` scans the last 30 days of sessions
and asks claude (via a single `claude -p` call, ~15s) to suggest a category
bucket for each project. It writes a starter `~/.ccstory/config.toml` you can
edit later.

`ccstory week` / `ccstory month` automatically appends a **vs-previous-window**
comparison (▲/▼ deltas per bucket). `ccstory trend` shows per-bucket
sparklines so you can see the shape of your usage across N weeks/months in
one glance:

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

## Privacy

Everything runs locally. ccstory never sends your conversation data anywhere.

- **Data source**: `~/.claude/projects/**/*.jsonl` — Claude Code's own logs.
- **Narratives (default)**: the `aiTitle` records Claude Code itself writes
  into each session's jsonl. Pure local file read, no LLM call.
- **Narratives (`--rich`)**: subprocess-call your *local* `claude -p`, which
  uses your own Claude Code session / quota. No API key required, no cost
  to ccstory.
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

## Roadmap

- [x] v0.1 — Time + tokens + per-session narrative + 4-bucket defaults
- [x] v0.1.1 — Per-bucket colors, date-range title, ★ Top focus highlight
- [x] v0.1.2 — vs-previous-window comparison + `ccstory trend` sparklines
- [x] v0.1.3 — `ccstory init` auto-categorization + quota burn % in trend
- [x] v0.2 — Read `aiTitle` from jsonl by default (instant, no `claude -p`);
      `--rich` opts in to outcome-focused narratives
- [ ] v0.3 — Per-category aggregate narrative (2-3 line summary of "what
      the whole bucket was about this period")
- [ ] v0.3 — Session-level classification (override folder bucket via
      `claude -p` content-aware tagging)
- [ ] v0.4 — Claude Code plugin form (`/ccstory` slash command)
- [ ] v0.5 — Optional PNG card export

## License

MIT — see [LICENSE](LICENSE).
