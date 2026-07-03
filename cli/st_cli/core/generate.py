"""Render the trashable ansible scaffolding under ``.st-cli/`` from the tree."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import appmeta, manifest, paths, tree, ui, vault
from .errors import StCliError

_COLLECTION_REPO = "https://github.com/suitenumerique/st-ansible.git"
_TEMPLATES = Path(__file__).resolve().parent / "resources" / "templates" / "scaffold"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def playbook_path(app: str, env: str, component: str) -> Path:
    """Path to the generated playbook for a unit."""
    return paths.playbooks_dir() / f"{app}-{env}-{component}.yml"


def _render(template: str, **ctx) -> str:
    return _env().get_template(template).render(**ctx)


def generate_all(app: str, env: str) -> None:
    """Generate ansible.cfg, galaxy-requirements.yml and per-component playbooks."""
    m = manifest.load_manifest()
    meta = appmeta.load_app(app)

    # Pick the scaffolding flags from the per-(app, env) secret backend choice:
    #   ansible-vault → emit vault_password_file in ansible.cfg
    #   hashi_vault   → also install community.hashi_vault in galaxy-requirements
    sc = manifest.secret_config_for(m, app, env)
    use_vault = sc.backend != "hashi_vault"
    hashi_vault = sc.backend == "hashi_vault"
    # Only nag when hvac is actually missing — the warning is then actionable and
    # goes away once it's installed, instead of firing on every deploy.
    if hashi_vault and importlib.util.find_spec("hvac") is None:
        ui.warn(
            "hashi_vault backend selected but the 'hvac' Python library is not "
            "installed — the community.hashi_vault lookup plugin needs it to "
            "resolve secrets at deploy time. Install it with `pip install hvac`."
        )

    paths.st_cli_dir().mkdir(parents=True, exist_ok=True)
    paths.playbooks_dir().mkdir(parents=True, exist_ok=True)
    paths.collections_dir().mkdir(parents=True, exist_ok=True)

    (paths.st_cli_dir() / "ansible.cfg").write_text(
        _render(
            "ansible.cfg.j2",
            collections_path=str(paths.collections_dir()),
            vault_password_file=str(vault.vault_password_path()),
            ssh_user=manifest.ssh_user(),
            use_vault=use_vault,
        ),
        encoding="utf-8",
    )

    # Optional local collection override (ST_CLI_COLLECTION_SOURCE env var):
    # install a built tarball or a source dir instead of the pinned git tag.
    collection_source = None
    collection_source_type = None
    source = os.environ.get("ST_CLI_COLLECTION_SOURCE")
    if source:
        resolved = Path(source).expanduser()
        if not resolved.is_absolute():
            resolved = paths.repo_root() / resolved
        if not resolved.exists():
            raise StCliError(
                f"collection_source path not found: {resolved} "
                "(set via ST_CLI_COLLECTION_SOURCE)."
            )
        collection_source = str(resolved)
        collection_source_type = "dir" if resolved.is_dir() else None
        ui.warn(
            f"Using local collection source {resolved} — "
            f"ignoring version pin {m.collection_version}."
        )

    (paths.st_cli_dir() / "galaxy-requirements.yml").write_text(
        _render(
            "galaxy-requirements.yml.j2",
            collection_repo=_COLLECTION_REPO,
            collection_version=m.collection_version,
            collection_source=collection_source,
            collection_source_type=collection_source_type,
            hashi_vault=hashi_vault,
        ),
        encoding="utf-8",
    )

    units = [u for u in manifest.units_for(m, app, env) if u.mode != "external"]
    if not units:
        raise StCliError(f"No managed units for {app}/{env} in .st-cli.yml.")

    # Remove stale playbooks for THIS (app, env) only: the '{app}-{env}-*.yml'
    # glob spans dashes, so a bare unlink would also clobber a sibling env whose
    # name extends this one (e.g. env 'prod' glob also matches 'meet-prod-staging-*').
    # Filter to files whose derived component is a real component key of this app.
    valid_keys = {c.key for c in meta.components}
    prefix = f"{app}-{env}-"
    for stale in paths.playbooks_dir().glob(f"{prefix}*.yml"):
        component = stale.name[len(prefix) : -len(".yml")]
        if component in valid_keys:
            stale.unlink()

    tree.ensure_common(app, env)
    tree.ensure_ssh_scaffold()

    for u in units:
        comp = meta.component(u.component)
        # workers own no files/hosts of their own — they reuse the core unit's
        # vars.yml/vault.yml. The targeted inventory group, however, follows the
        # effective_group rule: a worker with its own [workers] group (in the
        # core's hosts file) targets it, else it falls back to the core group.
        files = meta.files_component(u.component)
        vars_files = []
        common = paths.common_path(app, env)
        if common.exists():
            vars_files.append(str(common.resolve()))
        vars_files.append(str(paths.vars_path(app, env, files.key).resolve()))
        vault_yml = paths.vault_path(app, env, files.key)
        if vault_yml.exists():
            vars_files.append(str(vault_yml.resolve()))
        pb = _render(
            "playbook.yml.j2",
            app=app,
            env=env,
            component=u.component,
            role=comp.role,
            user=comp.user,
            group=tree.effective_group(app, env, meta, comp),
            enabled_var=comp.enabled_var,
            vars_files=vars_files,
        )
        playbook_path(app, env, u.component).write_text(pb, encoding="utf-8")

    tree.ensure_gitignore()
