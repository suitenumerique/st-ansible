"""Tests for the `version` command (st_cli.cmd.version)."""

from __future__ import annotations

import st_cli
from st_cli.cmd import version as version_mod
from st_cli.core import manifest
from st_cli.core.models import StCliManifest


def test_show_version_prints_pins_and_never_warns(repo, mocker):
    """The pin warning belongs to the global callback, so `version` prints
    the pins without a second warning."""
    manifest.save_manifest(StCliManifest("0.1.0", "0.1.0", []))
    info_spy = mocker.patch.object(version_mod.ui, "info")
    warn_spy = mocker.patch.object(version_mod.ui, "warn")

    version_mod.show_version()

    lines = [c.args[0] for c in info_spy.call_args_list]
    assert lines == [
        f"st-cli (installed): {st_cli.__version__}",
        ".st-cli.yml pins  : collection=0.1.0 cli=0.1.0",
    ]
    warn_spy.assert_not_called()


def test_show_version_without_manifest(repo, mocker):
    info_spy = mocker.patch.object(version_mod.ui, "info")

    version_mod.show_version()

    lines = [c.args[0] for c in info_spy.call_args_list]
    assert lines == [
        f"st-cli (installed): {st_cli.__version__}",
        "No .st-cli.yml in this directory (not a deployment repo).",
    ]
