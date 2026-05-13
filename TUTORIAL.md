# ccstory tutorial

[English](TUTORIAL.md) · [繁體中文](TUTORIAL.zh-TW.md) · [简体中文](TUTORIAL.zh-CN.md)

A 5-minute walkthrough from install to your first recap.

> **TL;DR**: `pipx install ccstory && ccstory init && ccstory week`

---

## Prerequisites

- Python 3.11+
- You've used Claude Code (the CLI) at least a few times — ccstory reads
  `~/.claude/projects/**/*.jsonl`, which Claude Code creates automatically.

That's it. No API key, no `claude` CLI on PATH (only needed if you opt into
`--rich` later).

---

## Step 1 — Install

```bash
pipx install ccstory
```

(or `pip install ccstory` if you don't use pipx)

Verify:

```bash
ccstory --version
# → ccstory 0.2.0
```

---

## Step 2 — Bucket your projects

```bash
ccstory init
```

What this does:

1. Scans the last 30 days of sessions in `~/.claude/projects/`.
2. Lists every distinct project folder (e.g. `my-portfolio-app`,
   `ondc-research`, `personal-blog`).
3. Sends one `claude -p` request asking Claude to suggest a category bucket
   for each — `coding`, `writing`, `investment`, or `other`.
4. Writes `~/.ccstory/config.toml` with the proposal. You can edit this file
   anytime.

Preview first if you're cautious:

```bash
ccstory init --dry-run
```

Skip the confirmation prompt:

```bash
ccstory init -y
```

> No `claude` CLI installed? `init` falls back to keyword matching against
> the folder name. Less accurate but still useful — you can refine the
> config manually.

---

## Step 3 — Your first recap

```bash
ccstory week
```

You'll see a panel like:

```
╭──── Claude Code Recap · May 5 – 12 ────╮
│  ★ Top focus  coding  10.9h  (53%)     │
│    ↳ Refactored auth middleware…       │
│                                        │
│  Active  20.6h  Sessions  74           │
│  Output  2.92M  Cost      $1,608       │
│                                        │
│  Time by category                      │
│  coding      ████████░░░░░░  10.9h     │
│  investment  █████░░░░░░░░░   9.6h     │
│  writing     ░░░░░░░░░░░░░░   0.1h     │
│                                        │
│  vs previous window (2026-W18)         │
│  total       20.6h  ▲ +47%             │
╰────────────────── ccstory ─────────────╯
```

How to read it:

| Field | What it means |
|---|---|
| **★ Top focus** | The biggest bucket + the one-line narrative of the longest session in it |
| **Active** | Hours where consecutive messages were ≤ 5 min apart (5-min gap heuristic). Longer gaps = "stepped away" |
| **Sessions** | Count of session jsonl files in the window (engaged sessions only — auto-fired scheduled tasks excluded) |
| **Output / Cost** | Output tokens and API-equivalent cost. *Pair with [ccusage](https://github.com/ryoppippi/ccusage) for precise billing.* |
| **Time by category** | Hours per bucket. Wall-clock dedup means parallel sessions don't double-count |
| **vs previous window** | Per-bucket ▲/▼ delta vs. the same-length window before. Uses **output tokens** for cross-period comparison (not `total_tokens`) |

The full markdown report is at `~/.ccstory/reports/recap-2026-W19.md` — it
includes per-session narrative lines, ideal for pasting into a weekly review
doc.

---

## Step 4 — Customize categories

Open `~/.ccstory/config.toml`:

```toml
default_bucket = "coding"
monthly_quota_usd = 3500    # Max 20× plan, used for "burn %"

[categories]
"work"      = ["company-repo", "internal-tool", "infra"]
"writing"   = ["blog", "newsletter", "essay"]
"learning"  = ["leetcode", "tutorial", "scratch"]
```

Rules:

- Categories are matched against tokens split on `-` from the project folder
  leaf (worktree suffixes and path prefixes are stripped first).
- First match wins, case-insensitive.
- Your rules **always** take precedence over built-in defaults.
- Unmatched projects fall back to `default_bucket` (or `coding` if unset).

Re-run `ccstory week` to see the new bucketing. Categories are computed at
report time — no rebuild needed.

---

## Step 5 — See the longer arc

```bash
ccstory trend           # last 8 weeks
ccstory trend --weeks 12
ccstory trend --months 6
```

Sparklines show the shape of each bucket over time:

```
Hours by bucket
total          ▁▄▆▇▃█    16.5h   avg 9.0h   ▲ +183%
investment     ▁▃▅█▆█     6.3h   avg 4.0h   ▲ +29%
coding         ▁▂▃▄▁█    10.2h   avg 3.3h   ▲ +1148%

Overall
output         ▁▁▁▄▁█     3.0M   avg 0.8M   ▲ +2460%
cost           ▁▁▂▃▁█   $1,643   avg $463   ▲ +1877%
burn %         ▁▁▂▃▁█     201%   avg 57%    ▲ +1877%
```

`burn %` is your API-equivalent cost as a percentage of a prorated monthly
quota. Set `monthly_quota_usd = 0` in config to hide the row.

---

## Common flags

```bash
ccstory                  # current month so far
ccstory month            # same
ccstory week             # past 7 days + vs previous
ccstory 2026-04          # any specific month
ccstory all              # entire history

ccstory --rich           # use `claude -p` for outcome-focused narratives
                         # (slower; spends real Claude Code turns)
ccstory --no-summary     # skip per-session narratives entirely
ccstory --no-compare     # skip the vs-previous block
```

---

## FAQ

**Q. ccstory says "No engaged sessions in this window" — but I used Claude Code.**

The engagement filter requires either ≥ 2 real user messages or 1 message
with ≥ 60s of activity. Very short or auto-fired sessions get excluded so
they don't pollute the report. If you think a session was wrongly excluded,
open an issue.

**Q. Why are some session narratives short / generic?**

The default narrative source is the `aiTitle` Claude Code writes into each
session's jsonl. Brand-new sessions might not have one yet — those fall
back to the first user message. For richer, outcome-focused phrasing,
add `--rich`.

**Q. Does `--rich` cost real money / quota?**

Yes — `--rich` invokes your local `claude -p`, which uses your Claude Code
session. One short turn per session without a cached narrative. For a 50-
session week, expect ~5–10 minutes of background calls. Default (no flag)
is free and instant.

**Q. Can I change the 5-minute gap threshold?**

Currently no flag — it's `GAP_CAP_SEC` in `ccstory/time_tracking.py`. Open
an issue if you have a use case for tuning it.

**Q. Does ccstory upload anything?**

No. Zero network calls. Verify in
[ccstory/session_summarizer.py](ccstory/session_summarizer.py) — the only
subprocess is your local `claude -p` (and only with `--rich`).

**Q. How does this differ from `ccusage`?**

[ccusage](https://github.com/ryoppippi/ccusage) is the canonical tool for
**cost / token precision** — pair it with ccstory. The README table summarizes
the split. Short version: ccusage answers "how much", ccstory answers "what
on".

---

## Where to go next

- **Issues / ideas**: <https://github.com/atomchung/ccstory/issues>
- **Roadmap**: see the README
- **Pair with ccusage**: `ccusage monthly && ccstory month`
