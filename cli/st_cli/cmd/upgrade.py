"""`st-cli upgrade` — the only upgrade path.

Checks the cached upstream version first. When it is ahead of the installed
CLI, the command stops and names the concrete upgrade command instead of
replaying anything: old code must not replay a questionnaire against new
release templates. When upstream is unknown (offline, check disabled), the
run continues with the installed version.

Then compares the installed CLI against the ``.st-cli.yml`` pin
(``core/pin.py``). An installed CLI older than the pin stops here too: a
realign would move the pin backwards, which is wrong.

Once past both gates: loads ``.st-cli.yml``, realigns the collection+cli pin
to the installed version, then replays every unit flagged by
``core/upgrades.py`` — silently pre-filled from its recovered answers,
unless a flag demands a full replay. Finally cleans the
trashable scaffolding for a clean slate. Does NOT generate / galaxy-install /
doctor — the subsequent ``doctor`` and ``deploy`` do that.
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
    if os.environ.get("ST_CLI_NO_UPSTREAM_CHECK"):
        return None
    return upstream.get_latest_cached()


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

    Then checks the installed CLI against the ``.st-cli.yml`` pin
    (``core/pin.py``): an older CLI stops here too, since realigning the pin
    would move it backwards.

    Realigns the ``.st-cli.yml`` pin before replaying anything: a crash
    mid-replay leaves the pin correct and the stamp old. Because the pin is
    already aligned at that point, ``deploy`` still blocks on the pending
    flag, and a plain re-run resumes cleanly. Replays run even when the pin
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

    if pin.compare(m) is pin.PinState.CLI_OLDER:
        raise StCliError(
            f"st-cli {st_cli.__version__} is older than the .st-cli.yml pin "
            f"{m.cli_version} — run `{upstream.install_hint()}`, then re-run "
            "`st-cli upgrade`."
        )

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

        # Every group's vault is checked before any questionnaire runs — one
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
    if manual_steps:
        ui.warn("Manual steps for this upgrade:")
        for app, env, version, text in manual_steps:
            ui.warn(f"  {app}/{env} {version}: {text}")
    if changed or replayed:
        ui.success(
            "upgrade complete — run `st-cli deploy <app> <env>` to roll the new tags."
        )
