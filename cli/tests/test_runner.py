"""Tests for st_cli.core.runner — ansible binary resolution, worker inventory, syntax check."""

from __future__ import annotations

import pytest

from st_cli.core import generate, manifest, paths, runner, tree
from st_cli.core.errors import StCliError
from st_cli.core.models import StCliManifest, UnitState

from helpers import seed_creds


# --------------------------------------------------------------------------- ansible_bin resolver
# (CONTRACT: resolve ansible binaries next to the interpreter first, then PATH.)


def test_ansible_bin_prefers_sys_executable_dir(tmp_path, monkeypatch):
    """ansible_bin prefers a co-located binary next to sys.executable over PATH."""
    fake_bin = tmp_path / "ansible-playbook"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(runner.sys, "executable", str(tmp_path / "python"))
    # even if PATH has another copy, the co-located one wins
    monkeypatch.setattr(runner.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert runner.ansible_bin("ansible-playbook") == str(fake_bin)


def test_ansible_bin_falls_back_to_path(tmp_path, monkeypatch):
    """With no co-located binary, ansible_bin falls back to shutil.which (PATH)."""
    monkeypatch.setattr(runner.sys, "executable", str(tmp_path / "python"))
    assert not (tmp_path / "ansible-playbook").exists()
    monkeypatch.setattr(
        runner.shutil, "which", lambda name: "/usr/bin/ansible-playbook"
    )
    assert runner.ansible_bin("ansible-playbook") == "/usr/bin/ansible-playbook"


def test_ansible_bin_missing_raises(tmp_path, monkeypatch):
    """Neither co-located nor on PATH → RunnerError (a StCliError subclass)."""
    monkeypatch.setattr(runner.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    with pytest.raises(runner.RunnerError):
        runner.ansible_bin("ansible-playbook")
    # RunnerError must be a StCliError so main._run turns it into a clean exit(1)
    assert issubclass(runner.RunnerError, StCliError)


# --------------------------------------------------------------------------- worker inventory


def test_runner_worker_uses_core_hosts(repo):
    """The runner points the workers playbook at the core unit's hosts file."""
    seed_creds(repo)
    tree.write_hosts("drive", "prod", "drive", "drive", ["10.0.0.1"])
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("drive", "prod", "drive", "managed"),
                UnitState("drive", "prod", "workers", "managed"),
            ],
        )
    )
    tree.save_vars("drive", "prod", "drive", tree.load_vars("drive", "prod", "drive"))
    generate.generate_all("drive", "prod")

    cmd = runner._playbook_cmd("drive", "prod", "workers", [])
    hosts_arg = cmd[cmd.index("-i") + 1]
    assert hosts_arg == str(
        paths.hosts_path("drive", "prod", "drive")
    )  # core hosts, not workers


def test_play_forwards_limit_to_ansible(repo, monkeypatch):
    """runner.play(limit=...) appends `--limit <pattern>` to the ansible argv."""
    seed_creds(repo)
    tree.write_hosts("drive", "prod", "drive", "drive", ["10.0.0.1", "10.0.0.2"])
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("drive", "prod", "drive", "managed")]
        )
    )
    tree.save_vars("drive", "prod", "drive", tree.load_vars("drive", "prod", "drive"))
    generate.generate_all("drive", "prod")

    captured: dict = {}
    monkeypatch.setattr(runner, "_run", lambda cmd: captured.update(cmd=cmd) or 0)

    runner.play("drive", "prod", "drive", limit="10.0.0.2")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--limit") + 1] == "10.0.0.2"
