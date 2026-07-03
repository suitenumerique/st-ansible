"""Read/write ``.st-cli.yml`` (committed)."""

from __future__ import annotations

import os

from ruamel.yaml.comments import CommentedMap

from . import appmeta, paths
from .errors import StCliError
from .models import SecretConfig, StCliManifest, UnitState
from .tree import yaml


def load_manifest() -> StCliManifest:
    """Load ``.st-cli.yml`` into a :class:`StCliManifest`."""
    p = paths.manifest_path()
    if not p.exists():
        raise StCliError(
            ".st-cli.yml not found — run `st-cli bootstrap <app> <env>` first."
        )
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml().load(fh) or {}
    versions = raw.get("versions", {}) or {}
    try:
        units = [
            UnitState(
                app=u["app"],
                env=u["env"],
                component=u["component"],
                mode=u.get("mode", "managed"),
            )
            for u in (raw.get("units", []) or [])
        ]
        secrets = [
            SecretConfig(
                app=s["app"],
                env=s["env"],
                backend=s.get("backend", "ansible-vault"),
            )
            for s in (raw.get("secrets", []) or [])
        ]
    except KeyError as e:
        raise StCliError(f".st-cli.yml is malformed (missing key {e})") from e
    return StCliManifest(
        collection_version=str(versions.get("collection", "")),
        cli_version=str(versions.get("cli", "")),
        units=units,
        secrets=secrets,
    )


def save_manifest(m: StCliManifest) -> None:
    """Write ``.st-cli.yml`` from a :class:`StCliManifest`."""
    doc = CommentedMap()
    doc["versions"] = CommentedMap()
    doc["versions"]["collection"] = m.collection_version
    doc["versions"]["cli"] = m.cli_version
    units = []
    for u in m.units:
        cm = CommentedMap()
        cm["app"] = u.app
        cm["env"] = u.env
        cm["component"] = u.component
        cm["mode"] = u.mode
        units.append(cm)
    doc["units"] = units
    # Omit the secrets block entirely when empty (clean diff): a manifest with no
    # secrets: key defaults to ansible-vault on load.
    if m.secrets:
        secrets = []
        for s in m.secrets:
            cm = CommentedMap()
            cm["app"] = s.app
            cm["env"] = s.env
            cm["backend"] = s.backend
            secrets.append(cm)
        doc["secrets"] = secrets
    with paths.manifest_path().open("w", encoding="utf-8") as fh:
        yaml().dump(doc, fh)


def ssh_user() -> str | None:
    """Resolve the remote ssh user (per-operator).

    Returns the ``ST_CLI_SSH_USER`` env var when set and non-empty, else ``None``.
    When ``None``, both the deploy path (the generated ``ansible.cfg`` omits
    ``remote_user``) and st-cli's own ssh (``cmd/remote._ssh`` builds a bare host)
    defer to the ssh config chain (``ssh/config.local`` / ``~/.ssh/config``).
    """
    return os.environ.get("ST_CLI_SSH_USER") or None


def upsert_unit(m: StCliManifest, unit: UnitState) -> None:
    """Insert or replace the unit matching (app, env, component)."""
    for i, u in enumerate(m.units):
        if (u.app, u.env, u.component) == (unit.app, unit.env, unit.component):
            m.units[i] = unit
            return
    m.units.append(unit)


def secret_config_for(m: StCliManifest, app: str, env: str) -> SecretConfig:
    """Return the :class:`SecretConfig` for ``(app, env)`` or the ansible-vault default.

    A manifest with no ``secrets:`` block resolves every (app, env) to ``ansible-vault``.
    """
    for s in m.secrets:
        if s.app == app and s.env == env:
            return s
    return SecretConfig(app=app, env=env, backend="ansible-vault")


def upsert_secret(m: StCliManifest, sc: SecretConfig) -> None:
    """Insert or replace the secret config matching (app, env)."""
    for i, s in enumerate(m.secrets):
        if (s.app, s.env) == (sc.app, sc.env):
            m.secrets[i] = sc
            return
    m.secrets.append(sc)


def units_for(
    m: StCliManifest, app: str, env: str, components: list[str] | None = None
) -> list[UnitState]:
    """Return the units for an (app, env), optionally narrowed to a set of components.

    ``components`` is a list (repeatable ``-c``); an empty list / ``None`` means
    "all components" for the (app, env). Duplicates are ignored via set membership.
    """
    out = [u for u in m.units if u.app == app and u.env == env]
    if components:
        wanted = set(components)
        out = [u for u in out if u.component in wanted]
    return out


def managed_units(app_name: str, env: str, components: list[str] | None):
    """Return managed units for app/env (optionally a subset of components), in deploy order.

    ``components`` is a list (repeatable ``-c``); ``None``/empty means all managed
    units. When a list is given, every requested component must match a managed
    unit — any that don't are raised by name — and the list is de-duplicated while
    preserving request order before being sorted by ``deploy_order``.
    """
    m = load_manifest()
    meta = appmeta.load_app(app_name)
    managed = [u for u in units_for(m, app_name, env) if u.mode != "external"]
    if components:
        by_key = {u.component: u for u in managed}
        missing = [c for c in dict.fromkeys(components) if c not in by_key]
        if missing:
            raise StCliError(
                f"No managed unit(s) for {app_name}/{env}: {', '.join(missing)}."
            )
        units = [by_key[c] for c in dict.fromkeys(components)]
    else:
        units = managed
    if not units:
        raise StCliError(f"No managed units for {app_name}/{env}.")
    units.sort(key=lambda u: meta.component(u.component).deploy_order)
    return m, units
