"""Materialize the pinned collection, then warn-only drift check.

Never touches the committed config tree, but DOES populate the trashable
``.st-cli/`` scaffolding and install the collection so the drift check runs
against the same collection the upcoming play will use.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from ruamel.yaml import YAML

from . import appmeta, generate, manifest, paths, runner, tree, ui
from .errors import StCliError

_COLLECTION_SUBPATH = Path("ansible_collections/suitenumerique/st/roles")


def _role_name(role_fqcn: str) -> str:
    return role_fqcn.split(".")[-1]


def _argument_specs_path(role_fqcn: str) -> Path | None:
    role = _role_name(role_fqcn)
    candidate = (
        paths.collections_dir()
        / _COLLECTION_SUBPATH
        / role
        / "meta"
        / "argument_specs.yml"
    )
    if candidate.is_file():
        return candidate
    # dev fallback: the collection repo this CLI ships inside
    dev = paths.repo_root().parent / "roles" / role / "meta" / "argument_specs.yml"
    return dev if dev.is_file() else None


def argument_spec_options(role: str) -> set[str]:
    """Return the set of valid option names for a role (union of all entrypoints)."""
    path = _argument_specs_path(role)
    if path is None:
        return set()
    data = YAML(typ="safe").load(path) or {}
    specs = data.get("argument_specs", {}) or {}
    options: set[str] = set()
    for entry in specs.values():
        options.update((entry.get("options", {}) or {}).keys())
    return options


def check_unit(app: str, env: str, component: str) -> list[str]:
    """Return human-readable warnings for one unit's vars.yml (never writes)."""
    warnings: list[str] = []
    comp = appmeta.load_app(app).component(component)
    options = argument_spec_options(comp.role)
    if not options:
        warnings.append(
            f"{app}/{env}/{component}: could not load argument_specs for role "
            f"{comp.role} (collection installed?); skipping drift check."
        )
        return warnings

    data = tree.load_vars(app, env, component)
    for key in data:
        if not str(key).startswith("st_"):
            continue
        if key not in options:
            near = difflib.get_close_matches(str(key), options, n=1)
            hint = f" — did you mean '{near[0]}'?" if near else ""
            warnings.append(f"{app}/{env}/{component}: unknown var '{key}'{hint}")
    return warnings


def check_app(app: str, env: str, components: list[str] | None = None) -> list[str]:
    """Check all (or a subset of) managed units of an app/env.

    External units are skipped (their vars live elsewhere, so there is nothing
    committed to drift-check). If EVERY matched unit is external, return a single
    warning saying so rather than an empty list — an empty result would read as a
    clean check even though nothing was actually evaluated.
    """
    m = manifest.load_manifest()
    units = manifest.units_for(m, app, env, components)
    if not units:
        raise StCliError(f"No units for {app}/{env} in .st-cli.yml.")
    managed = [u for u in units if u.mode != "external"]
    if not managed:
        scope = f"{app}/{env}" + (f"/{','.join(components)}" if components else "")
        return [f"{scope}: all units are external — no committed vars to drift-check."]
    out: list[str] = []
    for u in managed:
        out.extend(check_unit(app, env, u.component))
    return out


def preflight(app: str, env: str, components: list[str] | None = None) -> list[str]:
    """Materialize the pinned collection, then check vars drift.

    Shared preflight used by both ``doctor`` and ``deploy``: render the
    trashable scaffolding (``generate.generate_all``) and install the pinned
    collection (``runner.galaxy_install``) so the drift check runs against the
    same collection the upcoming play will use. Returns the ``check_app``
    warnings (warn-only / non-blocking).
    """
    generate.generate_all(app, env)
    runner.galaxy_install()
    return check_app(app, env, components)


def preflight_all(
    app: str | None = None, env: str | None = None, components: list[str] | None = None
) -> list[str]:
    """Materialize + drift-check every managed ``(app, env)`` pair (warn-only).

    Sweeps the whole ``.st-cli.yml`` when no args are given (external units are
    skipped), narrows to one app when only APP is given, or checks a single
    ``(app, env)`` unit when both are given. ``--component`` requires both APP
    and ENV (a component is meaningless without its unit).

    The collection is installed ONCE per sweep: the pin
    (``m.collection_version``) is a single value installed into the shared
    ``.st-cli/collections/`` dir, and the drift check (``check_app``/
    ``check_unit``) only reads the installed collection's
    ``argument_specs.yml`` + the committed ``vars.yml`` (NOT the generated
    scaffolding, NOT ``community.hashi_vault``), so per-pair reinstalls are
    unnecessary. ``generate_all`` still runs per pair (cheap, local; also
    produces the requirements file the one install reads and validates each
    pair has managed units). The per-pair / hashi_vault reasoning only applies
    to ``deploy``, which uses the single-pair :func:`preflight`.
    """
    if components and not (app and env):
        raise StCliError("--component requires both APP and ENV.")

    m = manifest.load_manifest()
    if app and env:
        pairs: list[tuple[str, str]] = [(app, env)]
    else:
        managed = {(u.app, u.env) for u in m.units if u.mode != "external"}
        if app:
            managed = {p for p in managed if p[0] == app}
        if not managed:
            raise StCliError(
                f"No managed units for app {app}."
                if app
                else "No managed units in .st-cli.yml."
            )
        pairs = sorted(managed)

    warnings: list[str] = []
    for i, (a, e) in enumerate(pairs):
        ui.info(f"Checking {a}/{e} …")
        generate.generate_all(a, e)
        if i == 0:
            runner.galaxy_install()
        warnings += check_app(a, e, components if (app and env) else None)
    return warnings
