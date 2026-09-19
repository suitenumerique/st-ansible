"""Tests for st_cli.core.ui — console helpers + `_Reporter` stream routing.

Covers a regression where `_Reporter.fail` printed to stdout on a TTY instead of stderr.
"""

from __future__ import annotations

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from st_cli.core import ui


def test_reporter_fail_off_tty_routes_to_stderr(capfd):
    """Off a TTY, `fail` routes the `✗` summary to stderr, never stdout."""
    reporter = ui._Reporter(None)
    reporter.fail(None, "boom: nope")

    captured = capfd.readouterr()
    assert "boom: nope" in captured.err
    assert "✗" in captured.err
    assert "boom: nope" not in captured.out
    assert "✗" not in captured.out


def test_reporter_fail_tty_routes_to_stderr_not_stdout(capfd):
    """On a TTY, `fail` still routes the `✗` summary to stderr, never stdout."""
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=Console(),
        transient=True,
    )
    reporter = ui._Reporter(progress)
    handle = reporter.start("restarting drive")
    reporter.fail(handle, "restarting drive failed: rc=1")

    captured = capfd.readouterr()
    # The ✗ summary lands on stderr (matches the off-TTY branch + the
    # warn/error to stderr convention), never on stdout.
    assert "restarting drive failed: rc=1" in captured.err
    assert "✗" in captured.err
    assert "restarting drive failed: rc=1" not in captured.out
    assert "✗" not in captured.out


def test_reporter_done_off_tty_stays_on_stdout(capfd):
    """`done` stays on stdout, since success routes to stdout by contract."""
    reporter = ui._Reporter(None)
    reporter.done(None, "restarted drive")

    captured = capfd.readouterr()
    assert "restarted drive" in captured.out
    assert "✓" in captured.out
    assert "restarted drive" not in captured.err
