"""`st-cli upgrade` — the only upgrade path.

Checks the cached upstream version first. When it is ahead of the installed
CLI: when pipx owns this install (``upstream.owning_pipx``), runs
``pipx upgrade st-cli``; otherwise (the container image, which installs with
plain pip), tells the user to
``docker pull ghcr.io/suitenumerique/st-cli:latest`` instead. When upstream
is unknown (offline, check disabled), falls back to the old best-effort
pipx-if-owner behavior.

Either way, the command then realigns the committed collection+cli pin to
the freshly-installed version (read from on-disk metadata, not the frozen
``__version__`` import which lags one run), and cleans the trashable
scaffolding for a clean slate. Does NOT generate / galaxy-install / doctor —
the subsequent ``doctor`` and ``deploy`` do that.

Realign + clean happen ONLY when the installed version actually differs from
the pin (a real upgrade). A no-op (nothing to upgrade) leaves ``.st-cli/``
intact and just informs the user — emitting a pip-upgrade hint when pipx is
absent and upstream is unknown, so a pip-installed CLI knows how to move
forward.
"""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess

from ..core import manifest, paths, ui, upstream
from ..core.errors import StCliError
from .. import __version__ as CLI_VERSION


def _upstream_latest() -> str | None:
    """Return the cached upstream version, or None when the check is disabled."""
    if os.environ.get("ST_CLI_NO_UPSTREAM_CHECK"):
        return None
    return upstream.get_latest_cached()


def _run_pipx_upgrade(pipx: str) -> None:
    """Actually run ``pipx upgrade st-cli``; warn on a non-zero return code.

    The caller decides whether pipx owns this install (``upstream.owning_pipx``)
    so it can branch without running anything, and passes the resolved path so
    we invoke the same executable the probe found (no second PATH lookup).
    """
    ui.info("Upgrading st-cli via pipx …")
    rc = subprocess.run([pipx, "upgrade", "st-cli"]).returncode
    if rc != 0:
        ui.warn("pipx upgrade did not succeed; continuing with the installed version.")


def _installed_version() -> str:
    """Read the freshly-installed st-cli version from on-disk metadata.

    After ``pipx upgrade`` runs in a subprocess, the running interpreter keeps
    executing the OLD already-imported modules (CPython never hot-reloads), so
    the frozen ``__version__`` import lags one run. Reading the on-disk
    metadata realigns the pin in a single run. Falls back to the imported
    ``__version__`` if the package isn't installed.
    """
    importlib.invalidate_caches()
    try:
        return importlib.metadata.version("st-cli")
    except importlib.metadata.PackageNotFoundError:
        return CLI_VERSION


def _clean_scaffolding() -> None:
    """Delete only the regeneratable ``.st-cli/`` artifacts for a clean slate.

    Never removes the ``.st-cli/`` dir wholesale — a vault-pass could live
    there in an edge case. Reports each removed artifact via ``ui.info``.
    """
    files = [
        paths.st_cli_dir() / "ansible.cfg",
        paths.st_cli_dir() / "galaxy-requirements.yml",
    ]
    dirs = [paths.playbooks_dir(), paths.collections_dir()]
    for p in files:
        if p.exists():
            p.unlink()
            ui.info(f"Cleaned {p}.")
    for d in dirs:
        if d.exists():
            shutil.rmtree(d)
            ui.info(f"Cleaned {d}.")


def upgrade() -> None:
    """Upgrade the CLI, realign the .st-cli.yml pin, and clean the scaffolding.

    Checks upstream first: behind + pipx-owned runs the pipx upgrade; behind +
    not pipx-owned tells the user to pull a new container image instead;
    unknown upstream falls back to running pipx when it owns the install (old
    best-effort behavior); up-to-date skips pipx entirely.

    Realign + clean happen only on a real version change (installed != pin).
    A no-op leaves the trashable ``.st-cli/`` scaffolding intact and just
    informs the user — with a pip-upgrade hint when pipx is absent and
    upstream is unknown.
    """
    pipx = upstream.owning_pipx()
    latest = _upstream_latest()
    behind = upstream.is_behind(latest)

    if behind is True:
        if pipx:
            _run_pipx_upgrade(pipx)
        else:
            ui.warn(
                "A new version is available — run `docker pull "
                "ghcr.io/suitenumerique/st-cli:latest` first, then re-run "
                "`st-cli upgrade`."
            )
    elif behind is None and pipx:
        _run_pipx_upgrade(pipx)
    # behind is False: already up to date, nothing to do via pipx.

    try:
        m = manifest.load_manifest()
    except StCliError:
        ui.warn("No .st-cli.yml here — nothing to align. (Run from a deployment repo.)")
        return

    installed = _installed_version()
    if (m.collection_version, m.cli_version) == (installed, installed):
        # No version change — do NOT touch the scaffolding.
        if behind is True and not pipx:
            # The docker-pull message above already told the user what to do.
            return
        if behind is None and not pipx:
            ui.warn(
                "pipx not found — if you installed st-cli with pip, upgrade it "
                "yourself (e.g. `pip install -U st-cli`), then re-run "
                "`st-cli upgrade`."
            )
        ui.info(f"st-cli is already at {installed}; nothing to do.")
        return

    # Real version change — realign the pin and clean for a fresh slate.
    m.collection_version = installed
    m.cli_version = installed
    manifest.save_manifest(m)
    ui.success(f"Realigned .st-cli.yml pin to {installed}.")
    _clean_scaffolding()
    ui.success(
        "upgrade complete — run `st-cli doctor` to check for drift against "
        "the new collection, then `st-cli deploy <app> <env>` to roll the new tags."
    )
