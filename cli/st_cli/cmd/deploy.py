"""`st-cli deploy` — preflight (doctor) + run the ansible playbooks."""

from __future__ import annotations

from ..core import appmeta, drift, manifest, runner, sshuser, tree, ui
from ..core.errors import StCliError


def run(
    app_name: str,
    env: str,
    components: list[str] | None,
    dry_run: bool,
    deploy_only: bool,
    host: str | None = None,
) -> None:
    """Preflight (materialize collection + drift check) then run playbooks.

    By default runs both the root 'base' phase (idempotent podman/user install)
    and the app-user 'deploy' phase. Use ``deploy_only`` for routine updates by
    an unprivileged user once the base is in place. ``host`` (an inventory alias)
    narrows the run to that single host: it is resolved per component and passed
    to ansible as ``--limit <alias>`` (default: all hosts, one at a time via
    ``serial: 1``). With ``components`` set, a missing host raises; without it, a
    component that lacks the host is skipped.

    Hard-gates on a pending rebootstrap: if any of the components being
    deployed still needs the bootstrap questionnaire replayed (see
    ``core/rebootstrap.py``), this raises instead of deploying — there is no
    override flag, deliberately. Because the rebootstrap questionnaire is
    interactive, a non-interactive/CI deploy must have it run beforehand
    (e.g. as a separate, manual step).
    """
    _, units = manifest.managed_units(app_name, env, components)
    meta = appmeta.load_app(app_name)
    hosts = [
        ip
        for u in units
        for _alias, ip in tree.component_inventory(
            app_name, env, meta, meta.component(u.component)
        )
    ]
    sshuser.ensure_ssh_user(hosts)
    warnings = drift.preflight(app_name, env, components)

    # Hard gate: managed_units above already guarantees `units` is non-empty and
    # non-external, so every check_app warning here is a rebootstrap-needed
    # message (never the "all units are external" one) — scoped to exactly the
    # components this deploy was asked to act on. No override flag: deliberate.
    # The warnings are NOT also emitted via ui.warn — they are reproduced
    # verbatim in the error below, and printing both makes the operator read the
    # same paragraph twice and hunt for the difference between them.
    if warnings:
        raise StCliError(
            "Rebootstrap required before deploying:\n"
            + "\n".join(f"  - {w}" for w in warnings)
        )

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
