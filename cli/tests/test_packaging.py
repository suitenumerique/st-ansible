"""Packaging metadata checks (pyproject extras)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import st_cli


def test_pyproject_optional_dependencies_extras():
    """The ansible/hashivault/full extras exist with the expected deps (base stays lean)."""
    pyproject = Path(st_cli.__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    extras = data["project"]["optional-dependencies"]
    assert extras["ansible"] == ["ansible-core>=2.16"]
    assert extras["hashivault"] == ["hvac>=2.0"]
    assert extras["full"] == ["ansible-core>=2.16", "hvac>=2.0"]
    # base deps unchanged — ansible-core / hvac must not sneak into [project].dependencies
    base = data["project"]["dependencies"]
    assert not any("ansible-core" in d for d in base)
    assert not any("hvac" in d for d in base)
