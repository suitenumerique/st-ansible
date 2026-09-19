"""Best-effort upstream-version check, plus the `.st-cli.yml` pin check.

`maybe_warn_upgrade` checks the installed CLI against the pin, then against
the latest upstream tag, and warns through `ui.warn`. Every check is
best-effort: any failure is silent, and the command proceeds untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import st_cli

from . import manifest, pin, ui

_REPO = "https://github.com/suitenumerique/st-ansible.git"
_TTL = 6 * 3600  # seconds

NO_CHECK_ENV = "ST_CLI_NO_UPSTREAM_CHECK"


def _parse_version(tag: str) -> tuple[int, ...] | None:
    if not tag:
        return None
    parts = tag.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def latest_upstream_version(timeout: float = 3.0) -> str | None:
    """Return the highest semver git tag on the collection repo, or None.

    Peeled-ref lines and tags `_parse_version` rejects are skipped.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--tags", _REPO],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
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
    """Never raises. Returns {} on any error or a missing file."""
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(data: dict) -> None:
    """Never raises. Swallows IO errors."""
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def get_latest_cached() -> str | None:
    """Return the latest upstream version, using the cache when fresh.

    Skips the network call when the cache is within the TTL; otherwise
    queries upstream and refreshes the cache.
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

    Return None when `latest` is None or when either version fails to parse.
    Return True only when the installed version is strictly older.
    """
    if latest is None:
        return None
    cur = _parse_version(st_cli.__version__)
    up = _parse_version(latest)
    if cur is None or up is None:
        return None
    return cur < up


def owning_pipx() -> str | None:
    """Return the pipx executable when pipx manages this install, else None.

    pipx writes `pipx_metadata.json` in each venv it owns. Its presence
    under `sys.prefix` confirms ownership, even when pipx is only on PATH.
    """
    if not (Path(sys.prefix) / "pipx_metadata.json").is_file():
        return None
    return shutil.which("pipx")


def install_hint() -> str:
    """Return the command that installs a newer st-cli build, no backticks.

    Uses `pipx upgrade st-cli` when `owning_pipx` finds pipx owns this
    install, else the container-image pull command.
    """
    if owning_pipx():
        return "pipx upgrade st-cli"
    return "docker pull ghcr.io/suitenumerique/st-cli:latest"


def maybe_warn_upgrade(invoked_subcommand: str | None) -> None:
    """Warn about a stale CLI, against the pin, then against upstream.

    Best-effort and warn-only: it never raises, prompts, or runs `upgrade`.
    Skips the CLI_NEWER pin warning when also behind upstream, because
    `upgrade` refuses to run in that case.
    """
    if os.environ.get(NO_CHECK_ENV):
        return
    # Skip a bare `st-cli` call and the upgrade subcommand itself.
    if invoked_subcommand in (None, "upgrade"):
        return

    try:
        m = manifest.load_manifest()
    except Exception:
        m = None

    latest = get_latest_cached()
    behind = is_behind(latest)

    if m is not None:
        state = pin.compare(m)
        if state is pin.PinState.CLI_OLDER:
            ui.warn(
                f"st-cli {st_cli.__version__} is older than the .st-cli.yml pin "
                f"{m.cli_version} — run `{install_hint()}`."
            )
            return
        if state is pin.PinState.CLI_NEWER and behind is not True:
            ui.warn(
                f"st-cli {st_cli.__version__} is newer than the .st-cli.yml pin "
                f"{m.cli_version} — run `st-cli upgrade` to align the repo."
            )

    if not behind:
        return

    ui.warn(
        f"st-cli {st_cli.__version__} is behind upstream {latest} — run "
        f"`{install_hint()}`."
    )
