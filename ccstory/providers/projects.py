"""Shared project attribution helpers for transcript providers."""

from __future__ import annotations

from pathlib import Path


def encode_project_dir(cwd: str) -> str:
    """Render a cwd the way Claude Code names its project folders.

    Providers whose transcript path does not encode the project use this shape
    so categorization, aliases, and rollups identify the same repository across
    coding agents.
    """
    return (
        str(cwd)
        .replace("\\", "-")
        .replace("/", "-")
        .replace(".", "-")
        .replace("_", "-")
    )


def worktree_origin(cwd: str) -> str:
    """Fold a linked git worktree checkout back onto its origin repository."""
    if not cwd:
        return cwd
    pointer = Path(cwd) / ".git"
    try:
        if not pointer.is_file():
            return cwd
        line = pointer.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return cwd
    if not line.startswith("gitdir:"):
        return cwd
    gitdir = Path(line[len("gitdir:"):].strip())
    parts = gitdir.parts
    try:
        # <repo>/.git/worktrees/<name>  →  <repo>
        idx = len(parts) - 1 - parts[::-1].index("worktrees")
    except ValueError:
        return cwd
    if idx < 2 or parts[idx - 1] != ".git":
        return cwd
    return str(Path(*parts[: idx - 1]))
