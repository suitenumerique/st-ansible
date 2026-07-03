"""Tests for st_cli.core.sshuser — the pre-connect ssh-user guard.

The autouse conftest fixture disables the guard for the whole suite; here we
re-enable it per test (``_checked = False``) and mock the resolution / TTY
surfaces so the four branches are covered offline.
"""

from __future__ import annotations

import os
import re

import pytest

import st_cli.core.sshuser as sshuser
from st_cli.core import paths, ui

from helpers import script_questionary


@pytest.fixture(autouse=True)
def _enable_guard(monkeypatch):
    """Re-enable the guard (conftest disables it) and clear ST_CLI_SSH_USER."""
    monkeypatch.delenv("ST_CLI_SSH_USER", raising=False)
    monkeypatch.setattr(sshuser, "_checked", False)


def test_noop_when_ssh_user_env_set(repo, monkeypatch):
    """ST_CLI_SSH_USER is already set → guard returns early: no resolve, no
    prompt, no warn, no file written."""
    monkeypatch.setenv("ST_CLI_SSH_USER", "deployer")

    def _boom(host):
        raise AssertionError(
            "_resolved_ssh_user must not run when ST_CLI_SSH_USER is set"
        )

    monkeypatch.setattr(sshuser, "_resolved_ssh_user", _boom)
    warns: list[str] = []
    monkeypatch.setattr(ui, "warn", lambda msg: warns.append(msg))

    sshuser.ensure_ssh_user(["10.0.0.1"])

    assert warns == []
    assert not paths.ssh_config_local_path().exists()
    assert sshuser._checked is True


def test_noop_when_ssh_config_resolves_nonlocal_user(repo, monkeypatch):
    """`ssh -G` resolves a User != local login user → a User is configured → no-op
    (no prompt, no warn, no env var, no file written)."""
    monkeypatch.setattr(sshuser.getpass, "getuser", lambda: "localuser")
    monkeypatch.setattr(sshuser, "_resolved_ssh_user", lambda host: "remotedeployer")
    warns: list[str] = []
    monkeypatch.setattr(ui, "warn", lambda msg: warns.append(msg))

    sshuser.ensure_ssh_user(["10.0.0.1"])

    assert warns == []
    assert "ST_CLI_SSH_USER" not in os.environ
    assert not paths.ssh_config_local_path().exists()
    assert sshuser._checked is True


def test_noop_when_config_local_sets_user_equal_to_local(repo, monkeypatch):
    """An explicit `User` in ssh/config.local is honoured even when it equals the
    local login user — the exact case `ssh -G` cannot disambiguate. No prompt/warn."""
    monkeypatch.setattr(sshuser.getpass, "getuser", lambda: "bogier")
    # `ssh -G` reports the local user as ssh's default (no ambient User configured)
    monkeypatch.setattr(sshuser, "_resolved_ssh_user", lambda host: "bogier")
    cfg = paths.ssh_config_local_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("Host *\n    User bogier\n", encoding="utf-8")

    def _boom_tty():
        raise AssertionError("guard must not reach the TTY prompt when User is set")

    monkeypatch.setattr(sshuser.sys.stdin, "isatty", _boom_tty)
    warns: list[str] = []
    monkeypatch.setattr(ui, "warn", lambda msg: warns.append(msg))

    sshuser.ensure_ssh_user(["10.0.0.1"])

    assert warns == []
    assert "ST_CLI_SSH_USER" not in os.environ
    assert sshuser._checked is True


def test_seeded_config_local_does_not_false_match(repo, monkeypatch):
    """The pristine (all-commented) seed sets no active User → guard still fires."""
    from st_cli.core import tree

    tree.ensure_ssh_scaffold()  # writes the fully-commented seed
    assert sshuser._repo_config_sets_user() is False


def test_prompt_persist_apply_on_tty(repo, monkeypatch):
    """Not configured + TTY → prompt once, append a `Host *` / `User` block to
    ssh/config.local, and set ST_CLI_SSH_USER for this run."""
    monkeypatch.setattr(sshuser.getpass, "getuser", lambda: "localuser")
    monkeypatch.setattr(sshuser, "_resolved_ssh_user", lambda host: None)
    monkeypatch.setattr(sshuser.sys.stdin, "isatty", lambda: True)
    script_questionary(
        monkeypatch,
        [("text", "enter the remote ssh user", "deployer")],
    )

    sshuser.ensure_ssh_user(["10.0.0.1"])

    cfg = paths.ssh_config_local_path()
    assert cfg.exists()
    text = cfg.read_text()
    # an uncommented `Host *` block is appended (the seed only has `#   Host *`)
    assert re.search(r"^Host \*", text, re.MULTILINE)
    assert "User deployer" in text
    assert os.environ["ST_CLI_SSH_USER"] == "deployer"
    assert sshuser._checked is True


def test_warn_when_non_tty(repo, monkeypatch):
    """Not configured + non-TTY → ui.warn mentioning ST_CLI_SSH_USER; no env var
    set, no file written. The command proceeds (the guard never blocks)."""
    monkeypatch.setattr(sshuser.getpass, "getuser", lambda: "localuser")
    monkeypatch.setattr(sshuser, "_resolved_ssh_user", lambda host: None)
    monkeypatch.setattr(sshuser.sys.stdin, "isatty", lambda: False)
    warns: list[str] = []
    monkeypatch.setattr(ui, "warn", lambda msg: warns.append(msg))

    sshuser.ensure_ssh_user(["10.0.0.1"])

    assert len(warns) == 1
    assert "ST_CLI_SSH_USER" in warns[0]
    assert "ST_CLI_SSH_USER" not in os.environ
    assert not paths.ssh_config_local_path().exists()
    assert sshuser._checked is True
