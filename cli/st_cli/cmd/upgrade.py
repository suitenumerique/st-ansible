"""`st-cli upgrade` — the only upgrade path.

Upgrades the CLI (best-effort via pipx), realigns the committed
collection+cli pin to the freshly-installed version (read from on-disk
metadata, not the frozen ``__version__`` import which lags one run), and
cleans the trashable scaffolding for a clean slate. Does NOT generate /
galaxy-install / doctor — the subsequent ``doctor`` and ``deploy`` do that.

Realign + clean happen ONLY when the installed version actually differs from
the pin (a real upgrade). A no-op (nothing to upgrade) leaves ``.st-cli/``
intact and just informs the user — emitting a pip-upgrade hint when pipx is
absent so a pip-installed CLI knows how to move forward.
"""

from __future__ import annotations

import importlib.metadata
import shutil
import subprocess

from ..core import manifest, paths, rebootstrap, ui
from ..core.errors import StCliError
from ..core.models import StCliManifest
from .. import __version__ as CLI_VERSION


def _run_pipx_upgrade(pipx: str) -> None:
    """Actually run ``pipx upgrade st-cli``; warn on a non-zero return code.

    The caller decides whether pipx is present (``shutil.which``) so it can
    branch on a missing pipx without running anything, and passes the resolved
    path so we invoke the same executable it found (no second PATH lookup).
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


def _report_pending_rebootstraps(m: StCliManifest) -> None:
    """Best-effort: warn which units now need a rebootstrap after this upgrade.

    A new st-cli version can bundle newly-added rebootstrap flags (see
    ``core/rebootstrap.py``) that didn't exist under the old pin — re-checking
    right after realigning the pin means the operator learns immediately,
    instead of only at their next ``deploy`` (which hard-gates on this) or a
    later standalone ``doctor`` run. Purely informational: any failure here
    (a corrupt flags file, whatever) is swallowed so it can never turn an
    otherwise successful upgrade into a failure.
    """
    try:
        needs = rebootstrap.needed(m)
        if not needs:
            return
        apps = ", ".join(sorted({n.app for n in needs}))
        ui.warn(
            f"Rebootstrap needed for: {apps}. Run `st-cli bootstrap <app> <env>` "
            "for each before your next deploy (deploy hard-gates on this)."
        )
        for n in needs:
            link = f" ({n.link})" if n.link else ""
            ui.info(f"  {n.app}/{n.env}/{n.component}: {n.version} — {n.reason}{link}")
    except Exception:
        ui.warn("Could not check for pending rebootstraps after upgrade.")


def upgrade() -> None:
    """Upgrade the CLI, realign the .st-cli.yml pin, and clean the scaffolding.

    Realign + clean happen only on a real version change (installed != pin).
    A no-op leaves the trashable ``.st-cli/`` scaffolding intact and just
    informs the user — with a pip-upgrade hint when pipx is absent.
    """
    pipx = shutil.which("pipx")
    if pipx:
        _run_pipx_upgrade(pipx)

    try:
        m = manifest.load_manifest()
    except StCliError:
        ui.warn("No .st-cli.yml here — nothing to align. (Run from a deployment repo.)")
        return

    installed = _installed_version()
    if (m.collection_version, m.cli_version) == (installed, installed):
        # No version change — do NOT touch the scaffolding.
        if not pipx:
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
    _report_pending_rebootstraps(m)
    _clean_scaffolding()
    ui.success(
        "upgrade complete — run `st-cli doctor` to check for pending "
        "rebootstraps, then `st-cli deploy <app> <env>` to roll the new tags."
    )
