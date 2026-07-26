# ccstory agent guide

This is the canonical repository guide for coding agents. Codex and
Antigravity read it directly; Claude Code reads it through the thin
`CLAUDE.md` import. Keep shared rules here rather than copying them into
tool-specific files.

## Repository contract

- ccstory is a local-first Python 3.11+ CLI and library that turns Claude
  Code, Codex, and Antigravity session logs into recaps, trends, JSON, and
  read-only MCP results.
- Preserve behavior across every user-facing surface affected by a change:
  terminal cards, Markdown reports, JSON, trend/comparison output, and MCP.
- Keep provider-specific parsing under `ccstory/providers/`. Shared
  attribution, usage, pricing, recap, and rendering logic stays
  provider-neutral.
- Never estimate token usage or infer a model when the source does not expose
  authoritative values. Keep incomplete coverage and unknown prices explicit
  through the existing `usage_coverage` and `unpriced_models` lanes.
- Preserve the local-data boundary documented in `README.md`. Do not add
  telemetry or send transcript content to metadata services.

## Concurrent-agent safety

- Before changing files or git state, inspect `git status` and
  `git worktree list`. Preserve unrelated changes and never reset, stash, or
  overwrite work owned by another session.
- Use one issue and one branch/worktree per independent change. Check for an
  existing branch or PR before starting overlapping work.
- Treat `.claude/worktrees/` as shadow worktrees, not additional source trees
  to edit or include in repository-wide searches.
- Base new branches on the latest available `origin/main`. Keep commits and
  diffs scoped to the assigned change; do not push, merge, publish, or delete
  branches unless the user explicitly asks.

## Development and verification

Install the package and test extras:

```bash
python -m pip install -e ".[test,mcp]"
```

Run focused tests while iterating, then the complete suite before handoff:

```bash
pytest tests/test_target_area.py
pytest
```

- Add or update tests for behavior changes. Prefer fixtures with synthetic
  session data; never commit real conversation logs or credentials.
- Update `README.md` for user-facing behavior and `CHANGELOG.md` for
  release-visible changes.
- When changing version or release behavior, keep `pyproject.toml`,
  `ccstory/__init__.py`, tags, built artifacts, and release notes consistent.
- A green test suite proves the code contract, not live product acceptance.
  For terminal, report, JSON, MCP, packaging, or install behavior, verify the
  affected surface directly when the change reaches it.

## Maintaining these instructions

Keep this file concise and repository-wide. Put conditional or multi-step
workflows in focused docs or skills and reference them from here. If a
subdirectory later needs genuinely different rules, add a nested `AGENTS.md`
there rather than expanding every session's context.

Do not duplicate this file into `CLAUDE.md`, `GEMINI.md`, or
`.agents/rules/`. Tool-specific files should be thin adapters containing only
tool-specific additions.
