"""Shared pytest fixtures for st-cli tests.

The Typer ``@app.callback()`` in ``st_cli.main`` fires an upstream-version check
on EVERY ``CliRunner`` invocation. To keep the offline test suite fast and
deterministic, an autouse fixture disables that check by default for all tests.
The dedicated upstream tests re-enable it (``monkeypatch.delenv``) and mock the
git call + cache path so they stay offline-safe.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_upstream_check(monkeypatch):
    """Disable the global upstream check for every test by default."""
    monkeypatch.setenv("ST_CLI_NO_UPSTREAM_CHECK", "1")


@pytest.fixture(autouse=True)
def _disable_ssh_user_guard(monkeypatch):
    """Disable the ssh-user guard for every test by default.

    Keeps the offline suite hermetic: no real ``ssh -G`` call and no TTY prompt
    (mirrors the upstream-check disabler). The dedicated ``test_sshuser.py``
    tests re-enable the guard per-test (``_checked = False``).
    """
    import st_cli.core.sshuser as sshuser

    monkeypatch.setattr(sshuser, "_checked", True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway deployment repo as the working directory."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
