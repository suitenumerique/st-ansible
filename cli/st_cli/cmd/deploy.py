"""`st-cli deploy` — preflight (doctor) + env-key diff + run the ansible playbooks."""

from __future__ import annotations

import st_cli

from ..core import (
    appmeta,
    drift,
    manifest,
    pin,
    runner,
    sshuser,
    tree,
    ui,
    upgrades,
    upstream,
)
from ..core.errors import StCliError


def run(
    app_name: str,
    env: str,
    components: list[str] | None,
    dry_run: bool,
    deploy_only: bool,
    host: str | None = None,
) -> None:
    """Preflight (pin/flag gate + materialize collection) then run playbooks.

    By default runs both the root 'base' phase (idempotent podman/user install)
    and the app-user 'deploy' phase. Use ``deploy_only`` for routine updates by
    an unprivileged user once the base is in place. ``host`` (an inventory alias)
    narrows the run to that single host: it is resolved per component and passed
    to ansible as ``--limit <alias>`` (default: all hosts, one at a time via
    ``serial: 1``). With ``components`` set, a missing host raises; without it, a
    component that lacks the host is skipped.

    The gate runs before any ssh or network side effect. It raises in two
    cases. The installed CLI can be older than the ``.st-cli.yml`` pin
    (``core/pin.py``); then the message names the pull command. A pending
    rebootstrap flag (``core/drift.pending_needs``) can sit at or below the
    pin. This means its replay is missing or crashed. Then the message names
    ``st-cli upgrade``.

    Every other pending flag is a warning only (``ui.warn``); the deploy
    continues. There is no override flag; this is deliberate. The
    rebootstrap questionnaire is interactive. A non-interactive or CI deploy
    must run it beforehand, as a separate step.

    After the gate passes: ensures the ssh user, materializes the scaffolding
    and pinned collection (``drift.preflight``), then runs the offline
    env-key diff (``drift.env_key_report``) and prints its advisories. This
    diff is warn-only and never blocks the deploy.
    """
    m, units = manifest.managed_units(app_name, env, components)
    meta = appmeta.load_app(app_name)

    if pin.compare(m) is pin.PinState.CLI_OLDER:
        raise StCliError(
            f"st-cli {st_cli.__version__} is older than the .st-cli.yml pin "
            f"{m.cli_version}. Run `{upstream.install_hint()}`, then retry."
        )

    blocking: list[str] = []
    for need in drift.pending_needs(app_name, env, components, m=m):
        pinned_ge_need = upgrades.parse_version(
            m.cli_version
        ) >= upgrades.parse_version(need.version)
        if pinned_ge_need:
            msg = (
                f"{app_name}/{env}/{need.component}: rebootstrap pending "
                f"({need.version} — {need.reason}) and .st-cli.yml already "
                f"pins {m.cli_version}. Run `st-cli upgrade` to resume the "
                "replay."
            )
            blocking.append(msg)
        else:
            ui.warn(drift.format_need(app_name, env, need))

    if blocking:
        raise StCliError(
            "Rebootstrap required before deploying:\n"
            + "\n".join(f"  - {b}" for b in blocking)
        )

    hosts = [
        ip
        for u in units
        for _alias, ip in tree.component_inventory(
            app_name, env, meta, meta.component(u.component)
        )
    ]
    sshuser.ensure_ssh_user(hosts)
    drift.preflight(app_name, env)

    for a in drift.env_key_report(app_name, env, components):
        ui.warn(a)

    tags = ["deploy"] if deploy_only else None
    prefix = "(dry-run) " if dry_run else ("(deploy-only) " if deploy_only else "")
    deployed_any = False
    for u in units:
        comp = meta.component(u.component)
        limit = None
        if host is not None:
            e = tree.find_host(
                tree.component_inventory(app_name, env, meta, comp), host
            )
            if e is None:
                if components:
                    raise StCliError(
                        f"Host '{host}' is not an alias of {app_name}/{env}/{u.component}."
                    )
                ui.info(f"Skipping {u.component}: alias '{host}' not in its inventory.")
                continue
            limit = e[0]  # the inventory alias — ansible --limit matches aliases
        deployed_any = True
        suffix = f" ({limit})" if limit else ""
        ui.info(f"{prefix}Deploying {app_name}/{env}/{u.component}{suffix}")
        rc = runner.play(
            app_name, env, u.component, check=dry_run, tags=tags, limit=limit
        )
        if rc != 0:
            raise StCliError(f"Deploy of {u.component} failed (rc={rc}).")
    if host is not None and not deployed_any:
        raise StCliError(
            f"Host alias '{host}' matched no managed component's inventory."
        )
    ui.success(f"{'Dry-run complete for' if dry_run else 'Deployed'} {app_name}/{env}.")
