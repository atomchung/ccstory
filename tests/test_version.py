from __future__ import annotations

import tomllib
from pathlib import Path

import ccstory


def test_python_version_comes_from_canonical_pyproject():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject.open("rb") as handle:
        canonical = tomllib.load(handle)["project"]["version"]

    assert ccstory.__version__ == canonical
    assert canonical.endswith(".dev0")
