"""`st-cli upgrade` — the only upgrade path.

Checks the cached upstream version first. When it is ahead of the installed
CLI, the command stops and names the concrete upgrade command instead of
replaying anything: old code must not replay a questionnaire against new
release templates. When upstream is unknown (offline, check disabled), the
run continues with the installed version.

Once past that gate: loads ``.st-cli.yml``, realigns the collection+cli pin
to the freshly-installed version (read from on-disk metadata, not the frozen
``__version__`` import which lags one run), then replays every unit flagged
by ``core/upgrades.py`` — silently pre-filled from its recovered answers,
unless a flag demands a full interactive review. Finally cleans the
trashable scaffolding for a clean slate. Does NOT generate / galaxy-install /
doctor — the subsequent ``doctor`` and ``deploy`` do that.
"""

from __future__ import annotations

import importlib.metadata
import os
import shutil

from .. import __version__ as CLI_VERSION
from ..core import appmeta, manifest, paths, ui, upgrades, upstream, writer
from ..core.errors import StCliError
from . import bootstrap as bootstrap_mod
from .bootstrap import ReplayAction


def _upstream_latest() -> str | None:
    """Return the cached upstream version, or None when the check is disabled."""
    if os.environ.get("ST_CLI_NO_UPSTREAM_CHECK"):
        return None
    return upstream.get_latest_cached()


def _installed_version() -> str:
    """Read the freshly-installed st-cli version from on-disk metadata.

    The frozen ``__version__`` import lags one run whenever the package was
    upgraded out-of-process (e.g. ``pipx upgrade st-cli`` in a prior run),
    since CPython never hot-reloads an already-imported module. Reading the
    on-disk metadata realigns the pin in a single run. Falls back to the
    imported ``__version__`` if the package isn't installed.
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
    """Upgrade the CLI's pin, replay flagged units, and clean the scaffolding.

    Checks upstream first: behind the latest release, warns with the concrete
    command to run and stops — replaying with old templates would leave units
    half-migrated. Unknown upstream continues with the installed version.

    Realigns the ``.st-cli.yml`` pin before replaying anything: a crash
    mid-replay then leaves the pin correct and the stamp old, so ``deploy``
    still gates and a re-run resumes cleanly. Replays run even when the pin
    was already aligned — a prior run may have realigned the pin but failed
    before finishing every replay.
    """
    # ST_CLI_NO_UPSTREAM_CHECK is a deliberate opt-out, not a failed check —
    # both collapse to `latest is None` in `_upstream_latest`, so track the
    # opt-out separately to keep the "could not check" info accurate.
    skip_upstream_check = bool(os.environ.get("ST_CLI_NO_UPSTREAM_CHECK"))
    latest = _upstream_latest()
    behind = upstream.is_behind(latest)

    if behind is True:
        if upstream.owning_pipx():
            ui.warn(
                "A new version is available — run `pipx upgrade st-cli`, "
                "then re-run `st-cli upgrade`."
            )
        else:
            ui.warn(
                "A new version is available — run `docker pull "
                "ghcr.io/suitenumerique/st-cli:latest`, then re-run "
                "`st-cli upgrade`."
            )
        return
    if behind is None and not skip_upstream_check:
        ui.info(
            "Could not check for a newer st-cli version; continuing with the "
            "installed version."
        )

    try:
        m = manifest.load_manifest()
    except StCliError:
        ui.warn("No .st-cli.yml here — nothing to align. (Run from a deployment repo.)")
        return

    installed = _installed_version()
    changed = (m.collection_version, m.cli_version) != (installed, installed)
    if changed:
        m.collection_version = installed
        m.cli_version = installed
        manifest.save_manifest(m)
        ui.success(f"Realigned .st-cli.yml pin to {installed}.")

    needs = upgrades.newest_per_unit(upgrades.needed(m))
    groups: dict[tuple[str, str], list] = {}
    for n in needs:
        groups.setdefault((n.app, n.env), []).append(n)

    replayed = False
    if groups:
        # Resolve app availability first: a group this CLI version cannot
        # replay must not abort the run through its vault check below.
        core_keys: dict[tuple[str, str], str] = {}
        for app, env in sorted(groups):
            try:
                core_keys[(app, env)] = appmeta.load_app(app).core().key
            except StCliError as exc:
                ui.warn(
                    f"{app}/{env}: skipped — {exc} (this CLI version no "
                    "longer ships that app's manifest)."
                )

        # Every group's vault is checked before any questionnaire runs — one
        # bad vault must abort the whole upgrade, not just its own group.
        for app, env in sorted(core_keys):
            components = [u.component for u in manifest.units_for(m, app, env)]
            writer.ensure_vault_readable(app, env, components)

        for app, env in sorted(core_keys):
            group = groups[(app, env)]
            ui.info(f"{app}/{env}: replaying {len(group)} flagged unit(s):")
            for n in group:
                link = f" ({n.link})" if n.link else ""
                ui.info(f"  {n.component}: {n.version} — {n.reason}{link}")
            mode = (
                ReplayAction.MODIFY
                if any(n.interactive for n in group)
                else ReplayAction.SILENT
            )
            if paths.vars_path(app, env, core_keys[(app, env)]).exists():
                bootstrap_mod.bootstrap(app, env, replay=mode)
                replayed = True
            else:
                for n in group:
                    bootstrap_mod.bootstrap(
                        app, env, component=n.component, replay=mode
                    )
                    replayed = True
                ui.warn(
                    f"{app}/{env}: no core tree here (provider-only repo) — "
                    "new-component offers are skipped. Run `st-cli bootstrap "
                    f"{app} {env} -c <comp>` to add one."
                )
    else:
        ui.info("No pending rebootstraps — all units are up to date.")

    if changed:
        _clean_scaffolding()
    if changed or replayed:
        ui.success(
            "upgrade complete — run `st-cli deploy <app> <env>` to roll the new tags."
        )
