"""Resolve one package version without duplicating it in Python source."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version as dist_version
from pathlib import Path


def _source_tree_version() -> str | None:
    """Read the canonical version when running from a source/editable tree."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with pyproject.open("rb") as handle:
            value = tomllib.load(handle)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return None
    return value if isinstance(value, str) and value else None


def resolve_version() -> str:
    """Source version for editable installs, distribution metadata otherwise."""
    source_version = _source_tree_version()
    if source_version is not None:
        return source_version
    try:
        return dist_version("ccstory")
    except PackageNotFoundError:
        return "0+unknown"
