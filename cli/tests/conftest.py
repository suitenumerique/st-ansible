"""Shared pytest fixtures for st-cli tests.

An autouse fixture disables the global upstream-version check by default.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_upstream_check(monkeypatch):
    monkeypatch.setenv("ST_CLI_NO_UPSTREAM_CHECK", "1")


@pytest.fixture(autouse=True)
def _disable_ssh_user_guard(monkeypatch):
    """Disable the ssh-user guard by default so no test makes a real ssh -G call."""
    from st_cli.core import sshuser

    monkeypatch.setattr(sshuser, "_checked", True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway deployment repo as the working directory."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
