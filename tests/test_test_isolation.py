"""Regression coverage for the suite-wide fake-home boundary."""

from pathlib import Path

from ccstory import (
    artifacts,
    categorizer,
    cli,
    init_categories,
    recap,
    session_summarizer,
    time_tracking,
    token_usage,
)


def test_autouse_fake_home_redirects_all_home_bound_paths():
    """No test may inherit ccstory or Claude state from the real home."""
    home = Path.home()
    claude_dir = home / ".claude"
    projects = claude_dir / "projects"
    ccstory_dir = home / ".ccstory"
    config = ccstory_dir / "config.toml"
    cache = ccstory_dir / "cache.db"

    assert time_tracking.CLAUDE_PROJECTS == projects
    assert token_usage.PROJECTS_DIR == projects
    assert session_summarizer.PROJECTS_DIR == projects
    assert session_summarizer.DB_PATH == cache
    assert session_summarizer.RECAP_DB_PATH == claude_dir / "session_summaries.db"
    assert session_summarizer.CLAUDE_MD_PATH == claude_dir / "CLAUDE.md"
    assert session_summarizer.CLAUDE_SETTINGS_PATH == claude_dir / "settings.json"
    assert session_summarizer.CCSTORY_CONFIG_PATH == config
    assert categorizer.CONFIG_PATH == config
    assert artifacts.DB_PATH == cache

    # These modules import the constants by value and need their own patches.
    assert recap.CLAUDE_PROJECTS == projects
    assert recap.SUMMARIZER_PROJECTS_DIR == projects
    assert recap.CONFIG_PATH == config
    assert recap.REPORTS_DIR == ccstory_dir / "reports"
    assert cli.CLAUDE_PROJECTS == projects
    assert cli.CONFIG_PATH == config
    assert cli.REPORTS_DIR == ccstory_dir / "reports"
    assert init_categories.CONFIG_PATH == config
