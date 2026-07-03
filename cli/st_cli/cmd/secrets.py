"""`st-cli secrets` — edit an (app, env)'s ansible-vault secrets in ``$EDITOR``.

Thin command layer (see the layering rule in ``CLAUDE.md``): resolves which
component's ``vault.yml`` to hand to :func:`st_cli.core.vault.edit_file` and
reports back via :mod:`st_cli.core.ui`. The actual ``ansible-vault edit`` call
lives in :mod:`st_cli.core.vault` so it stays reachable from core only.

ansible-vault backend only — a ``(app, env)`` on the ``hashi_vault`` backend has
no ``vault.yml`` (its secrets live in OpenBao), so this command refuses with a
clear pointer instead.
"""

from __future__ import annotations

from ..core import manifest, paths, prompts, ui, vault
from ..core.errors import StCliError


def edit_secrets(app_name: str, env: str, component: str | None) -> None:
    """Open one component's encrypted ``vault.yml`` in ``$EDITOR`` for editing.

    * Backend guard: only ``ansible-vault`` (the default) is supported — a
      ``hashi_vault`` (app, env) carries no local ``vault.yml``.
    * Component selection: external units are skipped; only components whose
      ``vault.yml`` exists are editable. A single candidate is used directly;
      several prompt via :func:`prompts._ask_select`. ``-c`` narrows up front.
    """
    m = manifest.load_manifest()
    sc = manifest.secret_config_for(m, app_name, env)
    if sc.backend != "ansible-vault":
        raise StCliError(
            f"{app_name}/{env} uses the {sc.backend} backend — "
            "edit its secrets in OpenBao, not here."
        )

    units = [
        u
        for u in manifest.units_for(
            m, app_name, env, [component] if component is not None else None
        )
        if u.mode != "external"
    ]
    editable = [
        u.component
        for u in units
        if paths.vault_path(app_name, env, u.component).exists()
    ]
    # Deduplicate component keys while keeping a stable order (manifest order).
    editable = list(dict.fromkeys(editable))

    if not editable:
        if component is not None:
            raise StCliError(
                f"No encrypted secrets for {app_name}/{env} -c {component}."
            )
        raise StCliError(f"No editable secrets for {app_name}/{env}.")

    if len(editable) == 1:
        comp = editable[0]
    else:
        comp = prompts._ask_select("Which component's secrets?", editable)

    vault.edit_file(paths.vault_path(app_name, env, comp))
    ui.success(f"Updated {app_name}/{env}/{comp} secrets.")
    ui.info(f"Run `st-cli deploy {app_name} {env}` to apply it on the servers.")
