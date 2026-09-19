"""Tests for st_cli.core.upstream — the best-effort, warn-only upstream-version check.

The autouse fixture disables the check by default; these tests re-enable it and mock
network calls.
"""

from __future__ import annotations

import subprocess
import sys
import time
import types

import st_cli
from st_cli.core import manifest, upstream
from st_cli.core.models import StCliManifest


def _enable_upstream(tmp_path, monkeypatch):
    """Re-enable the check and point the cache at tmp_path, so no test touches the real
    cache."""
    monkeypatch.delenv("ST_CLI_NO_UPSTREAM_CHECK")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


def _newer(version: str) -> str:
    """A version string strictly greater than `version`, bumping the major version.

    A hardcoded sentinel below the current version would flip the CLI to "ahead" and
    break the test.
    """
    major = int(version.split(".")[0])
    return f"{major + 1}.0.0"


def test_parse_version_numeric_and_garbage():
    """_parse_version parses dotted numerics; rejects non-numeric/garbage."""
    assert upstream._parse_version("0.0.21") == (0, 0, 21)
    assert upstream._parse_version("1.2.3") == (1, 2, 3)
    assert upstream._parse_version("main") is None
    assert upstream._parse_version("v1") is None
    assert upstream._parse_version("") is None
    assert upstream._parse_version("0.0.21-beta") is None  # pre-release suffix


def test_latest_upstream_version_picks_max(tmp_path, mocker):
    """Given fake git ls-remote output, returns the max tag, ignoring peeled lines."""
    fake_stdout = (
        "abc123\trefs/tags/0.0.19\n"
        "def456\trefs/tags/0.0.21\n"
        "abc123\trefs/tags/0.0.21^{}\n"  # peeled line, ignored
        "ghi789\trefs/tags/0.0.20\n"
        "jkl012\trefs/tags/main\n"  # non-numeric, ignored
    )
    completed = types.SimpleNamespace(returncode=0, stdout=fake_stdout, stderr="")
    mocker.patch.object(upstream.subprocess, "run", return_value=completed)
    assert upstream.latest_upstream_version() == "0.0.21"


def test_latest_upstream_version_nonzero_returns_none(tmp_path, mocker):
    completed = types.SimpleNamespace(returncode=1, stdout="", stderr="err")
    mocker.patch.object(upstream.subprocess, "run", return_value=completed)
    assert upstream.latest_upstream_version() is None


def test_latest_upstream_version_filenotfound_returns_none(tmp_path, mocker):
    """A missing git binary returns None instead of raising FileNotFoundError."""
    mocker.patch.object(upstream.subprocess, "run", side_effect=FileNotFoundError)
    assert upstream.latest_upstream_version() is None


def test_latest_upstream_version_timeout_returns_none(tmp_path, mocker):
    """A git timeout returns None instead of raising."""
    mocker.patch.object(
        upstream.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=3),
    )
    assert upstream.latest_upstream_version() is None


def test_is_behind_newer_latest_is_true():
    assert upstream.is_behind(_newer(st_cli.__version__)) is True


def test_is_behind_equal_latest_is_false():
    assert upstream.is_behind(st_cli.__version__) is False


def test_is_behind_older_latest_is_false():
    # "0.0.0" is the floor for any real (non-zero) installed version.
    assert upstream.is_behind("0.0.0") is False


def test_is_behind_unparseable_latest_is_none():
    assert upstream.is_behind("not-a-version") is None


def test_is_behind_none_latest_is_none():
    assert upstream.is_behind(None) is None


def test_owning_pipx_metadata_present_returns_path(tmp_path, mocker, monkeypatch):
    """A pipx_metadata.json at the venv root marks pipx ownership."""
    (tmp_path / "pipx_metadata.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    mocker.patch.object(upstream.shutil, "which", return_value="/usr/bin/pipx")
    assert upstream.owning_pipx() == "/usr/bin/pipx"


def test_owning_pipx_no_metadata_returns_none(tmp_path, mocker, monkeypatch):
    """pipx on the PATH alone does not mean pipx owns this install."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    mocker.patch.object(upstream.shutil, "which", return_value="/usr/bin/pipx")
    assert upstream.owning_pipx() is None


def test_maybe_warn_upgrade_behind_no_pipx_warns_docker_pull(
    tmp_path, mocker, monkeypatch
):
    """Behind upstream with no pipx, warns with only the docker pull command."""
    _enable_upstream(tmp_path, monkeypatch)
    newer = _newer(st_cli.__version__)  # always strictly greater than installed
    mocker.patch.object(upstream, "get_latest_cached", return_value=newer)
    mocker.patch.object(upstream, "owning_pipx", return_value=None)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(isatty=lambda: True))
    fake_confirm = mocker.patch("questionary.confirm")
    fake_upgrade = mocker.patch("st_cli.cmd.upgrade.upgrade")
    warn_spy = mocker.patch.object(upstream.ui, "warn")

    upstream.maybe_warn_upgrade("deploy")

    fake_confirm.assert_not_called()
    fake_upgrade.assert_not_called()
    warn_spy.assert_called_once()
    msg = warn_spy.call_args[0][0]
    assert st_cli.__version__ in msg
    assert newer in msg
    assert "docker pull ghcr.io/suitenumerique/st-cli:latest" in msg
    assert "st-cli upgrade" not in msg


def test_maybe_warn_upgrade_behind_with_pipx_warns_pipx_upgrade_only(
    tmp_path, mocker, monkeypatch
):
    """Behind upstream with pipx present, warns with only the pipx upgrade command."""
    _enable_upstream(tmp_path, monkeypatch)
    newer = _newer(st_cli.__version__)
    mocker.patch.object(upstream, "get_latest_cached", return_value=newer)
    mocker.patch.object(upstream, "owning_pipx", return_value="/usr/bin/pipx")
    warn_spy = mocker.patch.object(upstream.ui, "warn")

    upstream.maybe_warn_upgrade("deploy")

    warn_spy.assert_called_once()
    msg = warn_spy.call_args[0][0]
    assert st_cli.__version__ in msg
    assert newer in msg
    assert "pipx upgrade st-cli" in msg
    assert "st-cli upgrade" not in msg
    assert "docker pull" not in msg


def test_maybe_warn_upgrade_uptodate_no_warn(tmp_path, mocker, monkeypatch):
    """Warns nothing when the latest version is not newer than the installed one."""
    _enable_upstream(tmp_path, monkeypatch)
    mocker.patch.object(upstream, "get_latest_cached", return_value="0.0.20")
    warn_spy = mocker.patch.object(upstream.ui, "warn")

    upstream.maybe_warn_upgrade("deploy")

    warn_spy.assert_not_called()


def test_maybe_warn_upgrade_latest_none_no_warn(tmp_path, mocker, monkeypatch):
    """Warns nothing and does not raise when the latest version is unknown."""
    _enable_upstream(tmp_path, monkeypatch)
    mocker.patch.object(upstream, "get_latest_cached", return_value=None)
    warn_spy = mocker.patch.object(upstream.ui, "warn")

    upstream.maybe_warn_upgrade("deploy")

    warn_spy.assert_not_called()


def test_maybe_warn_upgrade_env_disabled_short_circuits(tmp_path, mocker, monkeypatch):
    """ST_CLI_NO_UPSTREAM_CHECK=1 skips the network call."""
    # Intentionally do NOT delenv: the autouse conftest fixture keeps it set.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    spy = mocker.patch.object(upstream, "get_latest_cached")

    upstream.maybe_warn_upgrade("deploy")

    spy.assert_not_called()


def test_maybe_warn_upgrade_upgrade_subcommand_skips(tmp_path, mocker, monkeypatch):
    """The upgrade subcommand itself returns immediately without a warning."""
    _enable_upstream(tmp_path, monkeypatch)
    spy = mocker.patch.object(upstream, "get_latest_cached")

    upstream.maybe_warn_upgrade("upgrade")

    spy.assert_not_called()


def test_maybe_warn_upgrade_help_subcommand_skips(tmp_path, mocker, monkeypatch):
    """A bare st-cli call, with no subcommand, returns immediately."""
    _enable_upstream(tmp_path, monkeypatch)
    spy = mocker.patch.object(upstream, "get_latest_cached")

    upstream.maybe_warn_upgrade(None)

    spy.assert_not_called()


def _save_pin(cli_version: str) -> None:
    manifest.save_manifest(StCliManifest("0.0.19", cli_version, []))


def test_maybe_warn_upgrade_pin_aligned_upstream_newer_warns_once(
    repo, mocker, monkeypatch
):
    """Pin aligned with the installed CLI, upstream newer: one warning that
    names only the pull command."""
    _enable_upstream(repo, monkeypatch)
    _save_pin(st_cli.__version__)
    upstream_latest = _newer(st_cli.__version__)
    mocker.patch.object(upstream, "get_latest_cached", return_value=upstream_latest)
    mocker.patch.object(upstream, "owning_pipx", return_value=None)
    warn_spy = mocker.patch.object(upstream.ui, "warn")

    upstream.maybe_warn_upgrade("deploy")

    warn_spy.assert_called_once()
    msg = warn_spy.call_args[0][0]
    assert "is behind upstream" in msg
    assert upstream_latest in msg
    assert "docker pull ghcr.io/suitenumerique/st-cli:latest" in msg
    assert "st-cli upgrade" not in msg


def test_maybe_warn_upgrade_cli_older_than_pin_warns_pull_hint_only(
    repo, mocker, monkeypatch
):
    """CLI older than the pin: warns the install hint; skips the upstream message."""
    _enable_upstream(repo, monkeypatch)
    pinned = _newer(st_cli.__version__)
    _save_pin(pinned)
    mocker.patch.object(upstream, "owning_pipx", return_value=None)
    # Upstream is also newer, to prove this branch never reaches the upstream check.
    mocker.patch.object(upstream, "get_latest_cached", return_value=_newer(pinned))
    warn_spy = mocker.patch.object(upstream.ui, "warn")

    upstream.maybe_warn_upgrade("deploy")

    warn_spy.assert_called_once()
    msg = warn_spy.call_args[0][0]
    assert st_cli.__version__ in msg
    assert pinned in msg
    assert "older than the .st-cli.yml pin" in msg
    assert "docker pull ghcr.io/suitenumerique/st-cli:latest" in msg
    assert "is behind upstream" not in msg


def test_maybe_warn_upgrade_cli_newer_than_pin_and_behind_upstream_warns_upstream_only(
    repo, mocker, monkeypatch
):
    """CLI newer than the pin but also behind upstream: only the upstream warning
    fires."""
    _enable_upstream(repo, monkeypatch)
    _save_pin("0.0.1")  # well below the installed version
    upstream_latest = _newer(st_cli.__version__)
    mocker.patch.object(upstream, "get_latest_cached", return_value=upstream_latest)
    mocker.patch.object(upstream, "owning_pipx", return_value=None)
    warn_spy = mocker.patch.object(upstream.ui, "warn")

    upstream.maybe_warn_upgrade("deploy")

    warn_spy.assert_called_once()
    msg = warn_spy.call_args[0][0]
    assert "is behind upstream" in msg
    assert upstream_latest in msg
    assert "newer than the .st-cli.yml pin" not in msg
    assert "st-cli upgrade" not in msg


def test_maybe_warn_upgrade_cli_newer_than_pin_upstream_uptodate_warns_pin_only(
    repo, mocker, monkeypatch
):
    """CLI newer than the pin, upstream up to date: a single warning to
    align the repo with `st-cli upgrade`."""
    _enable_upstream(repo, monkeypatch)
    _save_pin("0.0.1")  # well below the installed version
    mocker.patch.object(upstream, "get_latest_cached", return_value=st_cli.__version__)
    mocker.patch.object(upstream, "owning_pipx", return_value=None)
    warn_spy = mocker.patch.object(upstream.ui, "warn")

    upstream.maybe_warn_upgrade("deploy")

    warn_spy.assert_called_once()
    msg = warn_spy.call_args[0][0]
    assert "newer than the .st-cli.yml pin" in msg
    assert "0.0.1" in msg
    assert "st-cli upgrade" in msg
    assert "is behind upstream" not in msg


def test_maybe_warn_upgrade_no_manifest_skips_pin_keeps_upstream(
    repo, mocker, monkeypatch
):
    """No .st-cli.yml: the pin cases are skipped; the upstream check still runs."""
    _enable_upstream(repo, monkeypatch)
    upstream_latest = _newer(st_cli.__version__)
    mocker.patch.object(upstream, "get_latest_cached", return_value=upstream_latest)
    mocker.patch.object(upstream, "owning_pipx", return_value=None)
    warn_spy = mocker.patch.object(upstream.ui, "warn")

    upstream.maybe_warn_upgrade("deploy")

    warn_spy.assert_called_once()
    msg = warn_spy.call_args[0][0]
    assert "is behind upstream" in msg
    assert "pin" not in msg


def test_maybe_warn_upgrade_manifest_load_failure_never_raises(
    repo, mocker, monkeypatch
):
    """A manifest.load_manifest failure of any kind never breaks the check."""
    _enable_upstream(repo, monkeypatch)
    mocker.patch.object(manifest, "load_manifest", side_effect=RuntimeError("boom"))
    mocker.patch.object(upstream, "get_latest_cached", return_value=None)
    warn_spy = mocker.patch.object(upstream.ui, "warn")

    upstream.maybe_warn_upgrade("deploy")  # must not raise

    warn_spy.assert_not_called()


def test_upstream_cache_ttl_skips_network(tmp_path, mocker, monkeypatch):
    """A fresh cache within the TTL is returned without hitting the network."""
    _enable_upstream(tmp_path, monkeypatch)
    upstream._write_cache({"checked_at": time.time(), "latest": "0.0.20"})
    run_spy = mocker.patch.object(upstream.subprocess, "run")

    assert upstream.get_latest_cached() == "0.0.20"
    run_spy.assert_not_called()


def test_upstream_callback_never_breaks_command(tmp_path, mocker, monkeypatch):
    """The main.py callback swallows every exception."""
    from typer.testing import CliRunner

    from st_cli import main as main_mod

    _enable_upstream(tmp_path, monkeypatch)
    # Force the check to blow up internally; the callback must swallow it.
    mocker.patch.object(
        upstream, "maybe_warn_upgrade", side_effect=RuntimeError("boom")
    )
    result = CliRunner().invoke(main_mod.app, ["version"])
    # The command proceeds despite the check raising inside the callback;
    # `version` exits 1 here only because there's no .st-cli.yml in tmp_path.
    assert result.exit_code in (0, 1)
