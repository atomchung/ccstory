from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import ccstory
from ccstory.version import _source_tree_version, resolve_version


def _canonical_pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def test_python_version_comes_from_canonical_pyproject():
    assert ccstory.__version__ == _canonical_pyproject_version()


def test_source_tree_resolver_matches_canonical_pyproject():
    """Regression for issue #78: a source/worktree checkout must resolve its
    version by reading the live ``pyproject.toml`` on disk, not a frozen
    installed-metadata value that would go stale the moment ``main`` moves
    past a tagged release.

    This is the primary, always-on assertion. It only checks that the two
    ways of reading the version agree with each other, so it keeps holding
    whether ``pyproject.toml`` currently carries a ``.dev`` pre-release or a
    clean release version -- it will not misfire the day a release PR ships
    a clean version number.
    """
    canonical = _canonical_pyproject_version()
    assert _source_tree_version() == canonical
    assert resolve_version() == canonical


def test_source_install_exposes_dev_marker_while_unreleased():
    """Secondary, conditional regression for issue #78: while ``main`` sits
    on a post-release ``.dev0`` version (the release-hygiene policy that
    issue established), a source/worktree install must surface that marker
    so it can never be mistaken for a clean, tagged public release.

    Intentionally conditional on ``.dev`` being present in
    ``pyproject.toml`` right now, so this stays a no-op -- not a failure --
    the moment a release PR bumps ``main`` to a clean version number.
    """
    canonical = _canonical_pyproject_version()
    if ".dev" not in canonical:
        pytest.skip(
            "pyproject.toml is on a clean release version; no dev marker "
            "to assert here (see issue #78)."
        )

    assert ".dev" in resolve_version()
    assert ".dev" in ccstory.__version__
