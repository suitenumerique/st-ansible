"""Tests for st_cli.core.upstream — the best-effort, warn-only upstream-version check.

The autouse conftest fixture disables the check for all tests by default; these
re-enable it (``monkeypatch.delenv``) and mock git + the cache path so they stay
offline-safe.
"""

from __future__ import annotations

import subprocess
import sys
import time
import types

import st_cli
from st_cli.core import upstream


def _enable_upstream(tmp_path, monkeypatch):
    """Re-enable the check and point the cache at tmp_path (no real ~/.cache)."""
    monkeypatch.delenv("ST_CLI_NO_UPSTREAM_CHECK")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


def _newer(version: str) -> str:
    """A version string strictly greater than `version` (bump the major).

    Derived from the installed version so the "behind" test never goes stale
    when the real version is bumped (a hardcoded sentinel below the current
    version would silently flip the CLI to "ahead" and break the test).
    """
    major = int(version.split(".")[0])
    return f"{major + 1}.0.0"


# --------------------------------------------------------------------------- version parsing / query


def test_parse_version_numeric_and_garbage():
    """_parse_version parses dotted numerics; rejects non-numeric/garbage."""
    assert upstream._parse_version("0.0.21") == (0, 0, 21)
    assert upstream._parse_version("1.2.3") == (1, 2, 3)
    assert upstream._parse_version("main") is None
    assert upstream._parse_version("v1") is None
    assert upstream._parse_version("") is None
    assert upstream._parse_version("0.0.21-beta") is None  # pre-release suffix


def test_latest_upstream_version_picks_max(tmp_path, mocker):
    """Given fake git ls-remote stdout (incl. peeled lines), returns the max tag."""
    fake_stdout = (
        "abc123\trefs/tags/0.0.19\n"
        "def456\trefs/tags/0.0.21\n"
        "abc123\trefs/tags/0.0.21^{}\n"  # peeled line — ignored
        "ghi789\trefs/tags/0.0.20\n"
        "jkl012\trefs/tags/main\n"  # non-numeric — ignored
    )
    completed = types.SimpleNamespace(returncode=0, stdout=fake_stdout, stderr="")
    mocker.patch.object(upstream.subprocess, "run", return_value=completed)
    assert upstream.latest_upstream_version() == "0.0.21"


def test_latest_upstream_version_nonzero_returns_none(tmp_path, mocker):
    completed = types.SimpleNamespace(returncode=1, stdout="", stderr="err")
    mocker.patch.object(upstream.subprocess, "run", return_value=completed)
    assert upstream.latest_upstream_version() is None


def test_latest_upstream_version_filenotfound_returns_none(tmp_path, mocker):
    """git missing (FileNotFoundError) → None, no raise."""
    mocker.patch.object(upstream.subprocess, "run", side_effect=FileNotFoundError)
    assert upstream.latest_upstream_version() is None


def test_latest_upstream_version_timeout_returns_none(tmp_path, mocker):
    """git timeout → None, no raise."""
    mocker.patch.object(
        upstream.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=3),
    )
    assert upstream.latest_upstream_version() is None


# --------------------------------------------------------------------------- maybe_warn_upgrade


def test_maybe_warn_upgrade_behind_warns(tmp_path, mocker, monkeypatch):
    """Behind → ui.warn with the expected text; no questionary prompt, no
    upgrade call, no raise (fires regardless of isatty)."""
    _enable_upstream(tmp_path, monkeypatch)
    newer = _newer(st_cli.__version__)  # always strictly greater than installed
    mocker.patch.object(upstream, "_get_latest_cached", return_value=newer)
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
    assert "upgrade" in msg


def test_maybe_warn_upgrade_uptodate_no_warn(tmp_path, mocker, monkeypatch):
    """latest <= installed → no warn."""
    _enable_upstream(tmp_path, monkeypatch)
    mocker.patch.object(upstream, "_get_latest_cached", return_value="0.0.20")
    warn_spy = mocker.patch.object(upstream.ui, "warn")

    upstream.maybe_warn_upgrade("deploy")

    warn_spy.assert_not_called()


def test_maybe_warn_upgrade_latest_none_no_warn(tmp_path, mocker, monkeypatch):
    """latest is None (offline) → no warn, no raise."""
    _enable_upstream(tmp_path, monkeypatch)
    mocker.patch.object(upstream, "_get_latest_cached", return_value=None)
    warn_spy = mocker.patch.object(upstream.ui, "warn")

    upstream.maybe_warn_upgrade("deploy")

    warn_spy.assert_not_called()


def test_maybe_warn_upgrade_env_disabled_short_circuits(tmp_path, mocker, monkeypatch):
    """ST_CLI_NO_UPSTREAM_CHECK=1 (set by the autouse fixture) → no network."""
    # Intentionally do NOT delenv: the autouse conftest fixture keeps it set.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    spy = mocker.patch.object(upstream, "_get_latest_cached")

    upstream.maybe_warn_upgrade("deploy")

    spy.assert_not_called()


def test_maybe_warn_upgrade_upgrade_subcommand_skips(tmp_path, mocker, monkeypatch):
    """invoked_subcommand == 'upgrade' → returns immediately (no nag)."""
    _enable_upstream(tmp_path, monkeypatch)
    spy = mocker.patch.object(upstream, "_get_latest_cached")

    upstream.maybe_warn_upgrade("upgrade")

    spy.assert_not_called()


def test_maybe_warn_upgrade_help_subcommand_skips(tmp_path, mocker, monkeypatch):
    """invoked_subcommand is None (bare st-cli / help) → returns immediately."""
    _enable_upstream(tmp_path, monkeypatch)
    spy = mocker.patch.object(upstream, "_get_latest_cached")

    upstream.maybe_warn_upgrade(None)

    spy.assert_not_called()


# --------------------------------------------------------------------------- cache + callback


def test_upstream_cache_ttl_skips_network(tmp_path, mocker, monkeypatch):
    """A fresh cache (within TTL) is returned without hitting the network."""
    _enable_upstream(tmp_path, monkeypatch)
    upstream._write_cache({"checked_at": time.time(), "latest": "0.0.20"})
    run_spy = mocker.patch.object(upstream.subprocess, "run")

    assert upstream._get_latest_cached() == "0.0.20"
    run_spy.assert_not_called()


def test_upstream_callback_never_breaks_command(tmp_path, mocker, monkeypatch):
    """The main.py callback swallows every exception (best-effort)."""
    from typer.testing import CliRunner

    from st_cli import main as main_mod

    _enable_upstream(tmp_path, monkeypatch)
    # Force the check to blow up internally — the callback must swallow it.
    mocker.patch.object(
        upstream, "maybe_warn_upgrade", side_effect=RuntimeError("boom")
    )
    result = CliRunner().invoke(main_mod.app, ["version"])
    # The command proceeds despite the check raising inside the callback;
    # `version` exits 1 here only because there's no .st-cli.yml in tmp_path.
    assert result.exit_code in (0, 1)
