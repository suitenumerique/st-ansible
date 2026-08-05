"""Best-effort upstream-version check.

Before any subcommand, ask the collection repo (via anonymous
``git ls-remote --tags``) for the highest semver tag and, if the running CLI is
behind, **warn** the user to run ``upgrade``. The check is **best-effort
and non-fatal**: any failure (offline, git missing, timeout, parse error) is
swallowed silently and the original command proceeds untouched. The check
never prompts, never auto-runs ``upgrade``, and never raises out of the
callback — it only emits a ``ui.warn``.

A small JSON cache under ``$XDG_CACHE_HOME/st-cli/upstream.json`` (default
``~/.cache/st-cli/upstream.json``) avoids hitting the repo on every invocation
(TTL = 6h).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import __version__
from . import ui

_REPO = "https://github.com/suitenumerique/st-ansible.git"
_TTL = 6 * 3600  # seconds


def _parse_version(tag: str) -> tuple[int, ...] | None:
    """Parse a dotted numeric version (e.g. "0.0.21") to a tuple of ints.

    Return None for non-numeric/garbage tags (pre-release suffixes, "main",
    "v1", empty strings). Used both for filtering tags and for comparison.
    """
    if not tag:
        return None
    parts = tag.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def latest_upstream_version(timeout: float = 3.0) -> str | None:
    """Return the highest semver git tag on the collection repo, or None.

    Any failure (non-zero rc, git missing, timeout, no parseable tags) → None.
    Peeled-ref lines (``^{}``) are ignored; only tags ``_parse_version`` accepts
    are considered; the max is returned as its original string.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--tags", _REPO],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    best: tuple[int, ...] | None = None
    best_str: str | None = None
    for line in proc.stdout.splitlines():
        if "^{}" in line:
            continue
        if "refs/tags/" not in line:
            continue
        tag = line.rsplit("refs/tags/", 1)[1].strip()
        parsed = _parse_version(tag)
        if parsed is None:
            continue
        if best is None or parsed > best:
            best = parsed
            best_str = tag
    return best_str


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "st-cli" / "upstream.json"


def _read_cache() -> dict:
    """Read the cache dict, or {} on any error / missing file."""
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(data: dict) -> None:
    """Write the cache dict, creating parent dirs; swallow IO errors."""
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def get_latest_cached() -> str | None:
    """Return the latest upstream version, using the cache when fresh.

    If the cache was checked within the TTL, return its ``latest`` without any
    network call. Otherwise query upstream, refresh the cache, and return the
    result.
    """
    cache = _read_cache()
    checked_at = cache.get("checked_at")
    if isinstance(checked_at, (int, float)) and (time.time() - checked_at) < _TTL:
        latest = cache.get("latest")
        return latest if isinstance(latest, str) else None
    latest = latest_upstream_version()
    _write_cache({"checked_at": time.time(), "latest": latest})
    return latest


def is_behind(latest: str | None) -> bool | None:
    """Return whether the installed CLI is older than `latest`.

    Return None (unknown) when `latest` is None or when either version fails
    to parse. Return True only when the installed version is strictly older.
    """
    if latest is None:
        return None
    cur = _parse_version(__version__)
    up = _parse_version(latest)
    if cur is None or up is None:
        return None
    return cur < up


def owning_pipx() -> str | None:
    """Return the pipx executable when pipx manages this install, else None.

    pipx writes ``pipx_metadata.json`` at the root of each venv it owns, so
    the check on ``sys.prefix`` confirms ownership. pipx on the PATH alone
    does not mean pipx installed the running st-cli.
    """
    if not (Path(sys.prefix) / "pipx_metadata.json").is_file():
        return None
    return shutil.which("pipx")


def maybe_warn_upgrade(invoked_subcommand: str | None) -> None:
    """Best-effort: if a newer upstream version exists, warn to upgrade.

    Never raises. Warn-only — it never prompts, never auto-runs
    ``upgrade``, and never exits. Any failure is swallowed silently so the
    original command proceeds untouched. The hint branches on pipx ownership
    (``owning_pipx``): when pipx manages this install, ``st-cli upgrade``
    alone works; otherwise (the container image), the user must pull a new
    image first.
    """
    if os.environ.get("ST_CLI_NO_UPSTREAM_CHECK"):
        return
    # Don't nag on bare `st-cli` (help-only) or while already upgrading.
    if invoked_subcommand in (None, "upgrade"):
        return

    latest = get_latest_cached()
    if not is_behind(latest):
        return

    if owning_pipx():
        ui.warn(
            f"st-cli {__version__} is behind upstream {latest} — run `st-cli upgrade`."
        )
    else:
        ui.warn(
            f"st-cli {__version__} is behind upstream {latest}, run "
            "`docker pull ghcr.io/suitenumerique/st-cli:latest` and then `st-cli upgrade`."
        )
