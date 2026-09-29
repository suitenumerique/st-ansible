"""`st-cli upgrade` realigns the CLI pin and replays flagged units.

Refuses when upstream is ahead or the installed CLI is older than the pin.
"""

from __future__ import annotations

import os
import shutil

import st_cli

from ..core import appmeta, manifest, paths, pin, ui, upgrades, upstream, writer
from ..core.errors import StCliError
from . import bootstrap as bootstrap_mod
from .bootstrap import ReplayAction


def _upstream_latest() -> str | None:
    """Return the cached upstream version, or None when the check is disabled."""
    if os.environ.get(upstream.NO_CHECK_ENV):
        return None
    return upstream.get_latest_cached()


def _clean_scaffolding() -> None:
    """Delete only the regeneratable `.st-cli/` artifacts, not the whole directory."""
    files = [
        paths.ansible_cfg_path(),
        paths.galaxy_requirements_path(),
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
    """Upgrade the CLI pin, replay flagged units, and clean the scaffolding.

    Stops before realigning the pin when upstream is ahead or the installed
    CLI is older than the pin.
    """
    # A disabled check and a failed check both collapse to `latest is None`;
    # track the opt-out separately to keep the "could not check" info accurate.
    skip_upstream_check = bool(os.environ.get(upstream.NO_CHECK_ENV))
    latest = _upstream_latest()
    behind = upstream.is_behind(latest)

    if behind is True:
        raise StCliError(
            f"A new version is available — run `{upstream.install_hint()}`, "
            "then re-run `st-cli upgrade`."
        )
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

    pin.require_not_older(m, "then re-run `st-cli upgrade`.")

    installed = st_cli.__version__
    changed = (m.collection_version, m.cli_version) != (installed, installed)
    if changed:
        m.collection_version = installed
        m.cli_version = installed
        manifest.save_manifest(m)
        ui.success(f"Realigned .st-cli.yml pin to {installed}.")

    all_needs = upgrades.needed(m)
    needs = upgrades.newest_per_unit(all_needs)
    groups: dict[tuple[str, str], list] = {}
    for n in needs:
        groups.setdefault((n.app, n.env), []).append(n)
    all_groups: dict[tuple[str, str], list] = {}
    for n in all_needs:
        all_groups.setdefault((n.app, n.env), []).append(n)

    replayed = False
    failed: list[str] = []
    manual_steps: list[tuple[str, str, str, str]] = []  # (app, env, version, text)
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

        # Collect every group's warnings for the final recap, even a group
        # skipped above (its manifest no longer loads) or one whose vault
        # check fails below.
        for app, env in sorted(all_groups):
            for version, text in upgrades.pending_warnings(all_groups[(app, env)]):
                manual_steps.append((app, env, version, text))

        # Every group's vault is checked before any questionnaire runs: one
        # bad vault must abort the whole upgrade, not just its own group.
        for app, env in sorted(core_keys):
            components = [u.component for u in manifest.units_for(m, app, env)]
            writer.ensure_vault_readable(app, env, components)

        for app, env in sorted(core_keys):
            group = groups[(app, env)]
            ui.info(f"{app}/{env}: replaying {len(group)} flagged unit(s):")
            # A flag is per app, so print each need once with its units.
            by_need: dict[tuple[str, str, str], list[str]] = {}
            for n in group:
                by_need.setdefault((n.version, n.reason, n.link), []).append(
                    n.component
                )
            for (version, reason, link), units in by_need.items():
                suffix = f" ({link})" if link else ""
                ui.info(f"  {version} — {reason}{suffix}")
                ui.info(f"    units: {', '.join(units)}")
            mode = (
                ReplayAction.MODIFY
                if any(n.full_replay for n in group)
                else ReplayAction.SILENT
            )
            if paths.vars_path(app, env, core_keys[(app, env)]).exists():
                bootstrap_mod.bootstrap(app, env, replay=mode)
                replayed = True
            else:
                for n in group:
                    try:
                        bootstrap_mod.bootstrap(
                            app, env, component=n.component, replay=mode
                        )
                    except StCliError as exc:
                        ui.warn(f"{app}/{env}/{n.component}: skipped — {exc}")
                        failed.append(f"{app}/{env}/{n.component}")
                        continue
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
    if manual_steps:
        ui.warn("Manual steps for this upgrade:")
        for app, env, version, text in manual_steps:
            ui.warn(f"  {app}/{env} {version}: {text}")
    # A failed replay leaves its unit flagged, and `st-cli deploy` refuses a
    # flagged unit once the pin is realigned. Do not tell the operator to deploy.
    if failed:
        ui.warn("These units stay flagged — fix the error above, then re-run upgrade:")
        for unit in failed:
            ui.warn(f"  {unit}")
    elif changed or replayed:
        ui.success(
            "upgrade complete — run `st-cli deploy <app> <env>` to roll the new tags."
        )
