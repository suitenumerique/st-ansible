"""Packaging metadata checks (pyproject extras)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import st_cli


def test_pyproject_optional_dependencies_extras():
    """The ansible/hashivault/full extras exist with the expected deps (base stays lean)."""
    pyproject = Path(st_cli.__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    extras = data["project"]["optional-dependencies"]

    # Assert on package names only (pins are managed by Renovate, so avoid
    # hardcoding versions here — else every dependency bump breaks this test).
    def names(reqs: list[str]) -> list[str]:
        return [re.split(r"[=<>~!]", r, maxsplit=1)[0] for r in reqs]

    assert names(extras["ansible"]) == ["ansible-core"]
    assert names(extras["hashivault"]) == ["hvac"]
    assert names(extras["full"]) == ["ansible-core", "hvac"]
    # base deps unchanged — ansible-core / hvac must not sneak into [project].dependencies
    base = data["project"]["dependencies"]
    assert not any("ansible-core" in d for d in base)
    assert not any("hvac" in d for d in base)
