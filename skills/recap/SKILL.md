---
description: Show a recap of recent Claude Code usage with narrative summary, comparison vs the previous window, and sparkline trends. Use when the user asks "what did I do this week", "claude usage summary", "where did my time go", or mentions ccstory / recap / trend.
argument-hint: "[week | month | YYYY-MM | all | trend ...]"
allowed-tools: Bash(ccstory *), Bash(which ccstory)
---

# ccstory recap

Run the `ccstory` CLI to produce a usage recap and present the result to the user, highlighting the **Top focus** line and any notable comparison deltas.

## Common invocations

Default to `week` if the user didn't specify a window:

| User wants | Command |
|---|---|
| Recent snapshot (default) | `ccstory week` |
| This month so far | `ccstory month` |
| Specific past month | `ccstory 2026-04` |
| All-time | `ccstory all` |
| 8-week sparkline trend | `ccstory trend` |
| N-month sparkline trend | `ccstory trend --months 6` |
| One-time category setup | `ccstory init` |

For longer windows (month / all), warn the user that backfill of new per-session summaries can take ~7s per uncached session. Suggest `--no-summary` if they want a fast answer without narrative.

## How to act

1. Pick the right invocation based on what the user asked. Pass `$ARGUMENTS` through if they specified.
2. Run the command via Bash.
3. The CLI prints a Rich-formatted card and writes a full markdown report to `~/.ccstory/reports/`. Paste the terminal card and call out:
   - The **★ Top focus** bucket and its top-session narrative
   - Any bucket that swung sharply in the vs-previous comparison
   - For trend mode: which buckets are climbing vs fading
4. Don't paraphrase the numbers — quote them directly from the card.

## If ccstory is not installed

If `which ccstory` returns nothing, tell the user:

```
ccstory isn't installed on this machine. Install with:

    pipx install git+https://github.com/atomchung/ccstory.git

Then re-run me.
```

Don't try to install it yourself — let the user decide.
