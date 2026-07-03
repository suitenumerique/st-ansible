"""Shared rich-based console helpers.

All user-facing output goes through these so styling stays consistent and we
never accidentally print secrets via stray ``print`` calls.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()
_err_console = Console(stderr=True)


def info(msg: str) -> None:
    """Print an informational line."""
    console.print(f"[cyan]›[/cyan] {msg}")


def warn(msg: str) -> None:
    """Print a warning line to stderr."""
    _err_console.print(f"[yellow]![/yellow] {msg}")


def error(msg: str) -> None:
    """Print an error line to stderr."""
    _err_console.print(f"[red]✗[/red] {msg}")


def success(msg: str) -> None:
    """Print a success line."""
    console.print(f"[green]✓[/green] {msg}")


def note(body: str, title: str = "Note") -> None:
    """Print a titled panel with guidance text (stdout)."""
    console.print(Panel(body, title=title))


def host_header(name: str, host: str) -> None:
    """Print a compact, colored ``<name> on <host>`` section header (e.g. for ``ps``).

    Sets a component/unit name apart from the host it runs on so per-host output
    blocks are easy to scan."""
    console.print(f"[bold cyan]{name}[/bold cyan] [dim]on[/dim] [green]{host}[/green]")


class _Reporter:
    """Per-item progress reporter: a live Rich spinner on a TTY, plain lines otherwise.

    Thread-safe — the parallel restart drives several components concurrently. Each
    item gets a handle from :meth:`start`; call :meth:`update` to advance its line,
    then :meth:`done` OR :meth:`fail` exactly once."""

    def __init__(self, progress: "Progress | None") -> None:
        self._p = progress  # a live Rich Progress on a TTY, else None
        self._lock = threading.Lock()

    def start(self, label: str):
        if self._p is not None:
            return self._p.add_task(label, total=None)
        info(label)
        return None

    def update(self, handle, label: str) -> None:
        if self._p is not None:
            self._p.update(handle, description=label)

    def done(self, handle, label: str) -> None:
        with self._lock:
            if self._p is not None:
                self._p.remove_task(handle)
                self._p.console.print(f"[green]✓[/green] {label}")
            else:
                success(label)

    def fail(self, handle, label: str) -> None:
        with self._lock:
            if self._p is not None:
                self._p.remove_task(handle)
                error(label)
            else:
                error(label)


@contextmanager
def progress_reporter():
    """Yield a :class:`_Reporter`. On a TTY it drives a transient Rich spinner group
    (spinner rows vanish on exit, leaving only the printed ✓/✗ summary lines); off a
    TTY it degrades to plain info/success/error lines (clean, linear CI output)."""
    if console.is_terminal:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        )
        with progress:
            yield _Reporter(progress)
    else:
        yield _Reporter(None)
