"""`st-cli deploy`: preflight (doctor), env-key diff, then run the ansible playbooks."""

from __future__ import annotations

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

    ``host`` (an inventory alias) narrows the run to that single host, passed to
    ansible as ``--limit``; otherwise every host runs one at a time (``serial: 1``).
    The gate raises before any ssh or network side effect; every other pending
    flag only warns.
    """
    m, units = manifest.managed_units(app_name, env, components)
    meta = appmeta.load_app(app_name)

    pin.require_not_older(m, "then retry.")

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

    targeted: dict[str, list[tuple[str, str]]] = {}
    ips: list[str] = []
    for comp, alias, ip in tree.iter_targeted_hosts(
        app_name, env, meta, units, components, host
    ):
        targeted.setdefault(comp.key, []).append((alias, ip))
        ips.append(ip)
    sshuser.ensure_ssh_user(ips)
    drift.preflight(app_name, env)

    for a in drift.env_key_report(app_name, env, components):
        ui.warn(a)

    tags = ["deploy"] if deploy_only else None
    prefix = "(dry-run) " if dry_run else ("(deploy-only) " if deploy_only else "")
    deployed_any = False
    for u in units:
        hosts_for_unit = targeted.get(u.component)
        if hosts_for_unit is None:
            continue
        deployed_any = True
        limit = hosts_for_unit[0][0] if host is not None else None
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
