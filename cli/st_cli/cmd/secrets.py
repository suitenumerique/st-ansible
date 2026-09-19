"""`st-cli secrets`: edit an (app, env)'s ansible-vault secrets in ``$EDITOR``.

ansible-vault backend only. A ``(app, env)`` on the ``hashi_vault`` backend has
no ``vault.yml`` (its secrets live in OpenBao), so this command refuses instead.
"""

from __future__ import annotations

from ..core import manifest, paths, prompts, ui, vault
from ..core.errors import StCliError
from ..core.models import BACKEND_ANSIBLE_VAULT, MODE_EXTERNAL


def edit_secrets(app_name: str, env: str, component: str | None) -> None:
    """Open one component's encrypted ``vault.yml`` in ``$EDITOR`` for editing.

    Skips external units; only components with a ``vault.yml`` are editable. A single
    candidate is used directly; several prompt for one. ``-c`` narrows up front.
    """
    m = manifest.load_manifest()
    sc = manifest.secret_config_for(m, app_name, env)
    if sc.backend != BACKEND_ANSIBLE_VAULT:
        raise StCliError(
            f"{app_name}/{env} uses the {sc.backend} backend — "
            "edit its secrets in OpenBao, not here."
        )

    units = [
        u
        for u in manifest.units_for(
            m, app_name, env, [component] if component is not None else None
        )
        if u.mode != MODE_EXTERNAL
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
