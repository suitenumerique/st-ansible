"""Preflight checks run before a deploy, and the standalone `doctor` sweep.

`check_app` reports units with an outstanding rebootstrap flag.
`env_key_report` is a warn-only diff between a fresh render and the blob.
"""

from __future__ import annotations

from . import (
    appmeta,
    envblob,
    envrender,
    generate,
    manifest,
    recover,
    runner,
    tree,
    ui,
    upgrades,
)
from .errors import StCliError
from .models import MODE_EXTERNAL, StCliManifest, UnitState, UpgradeNeed


def _load_units(
    app: str,
    env: str,
    components: list[str] | None = None,
    m: StCliManifest | None = None,
) -> tuple[StCliManifest, list[UnitState]]:
    """Load the manifest, unless given, and this app/env's units.

    Callers decide whether an empty result is an error.
    """
    if m is None:
        m = manifest.load_manifest()
    return m, manifest.units_for(m, app, env, components)


def pending_needs(
    app: str,
    env: str,
    components: list[str] | None = None,
    m: StCliManifest | None = None,
) -> list[UpgradeNeed]:
    """Return the newest outstanding rebootstrap need per unit of an app/env.

    Raises StCliError when no unit matches. Pass `m` to reuse an already
    loaded manifest.
    """
    m, units = _load_units(app, env, components, m)
    if not units:
        raise StCliError(f"No units for {app}/{env} in .st-cli.yml.")
    wanted = {u.component for u in units if u.mode != MODE_EXTERNAL}
    needs = [n for n in upgrades.needed(m, app, env) if n.component in wanted]
    return upgrades.newest_per_unit(needs)


def check_app(app: str, env: str, components: list[str] | None = None) -> list[str]:
    """Return rebootstrap-status warnings for an app/env's bootstrapped units.

    Raises StCliError when no unit matches. Reports one line, not an empty
    list, when every matched unit is external.
    """
    m, units = _load_units(app, env, components)
    if not units:
        raise StCliError(f"No units for {app}/{env} in .st-cli.yml.")
    managed = [u for u in units if u.mode != MODE_EXTERNAL]
    if not managed:
        scope = f"{app}/{env}" + (f"/{','.join(components)}" if components else "")
        return [f"{scope}: all units are external — nothing to rebootstrap-check."]

    return [format_need(app, env, n) for n in pending_needs(app, env, components, m)]


def format_need(app: str, env: str, need: UpgradeNeed) -> str:
    """Return the one-line warning for a pending rebootstrap need.

    Excludes the link and the manual steps; `st-cli upgrade` prints those.
    """
    return (
        f"{app}/{env}/{need.component}: rebootstrap needed "
        f"({need.version} — {need.reason}). Run `st-cli upgrade`."
    )


def env_key_report(
    app: str, env: str, components: list[str] | None = None
) -> list[str]:
    """Return advisories for keys a fresh render has that the committed blob lacks.

    Detects only unconditionally-rendered keys; a template line guarded by
    `{% if %}` never triggers one. A unit that errors is skipped, not fatal.
    """
    _m, units = _load_units(app, env, components)
    managed = [u for u in units if u.mode != MODE_EXTERNAL]

    advisories: list[str] = []
    for u in managed:
        try:
            meta = appmeta.load_app(app)
            spec = meta.env_render_spec(u.component)
            if not spec:
                continue

            answers = recover.recover(app, env, u.component)
            if not answers:
                continue

            rendered = envrender.render_env(app, u.component, answers)
            data = tree.load_vars(app, env, u.component)

            missing: list[str] = []
            for info in spec.values():
                blob_var = info.get("blob_var")
                committed_text = data.get(blob_var) if blob_var else None
                if not isinstance(committed_text, str):
                    continue
                rendered_keys = envblob.keys(rendered.get(blob_var, ""))
                committed_keys = envblob.keys(committed_text)
                committed_set = set(committed_keys)
                missing += [k for k in rendered_keys if k not in committed_set]

            if missing:
                advisories.append(
                    f"{app}/{env}/{u.component}: new env keys available: "
                    f"{', '.join(missing)} — run `st-cli bootstrap {app} {env}` "
                    "to set them."
                )
        except Exception as exc:
            ui.info(f"{app}/{env}/{u.component}: env-key check skipped ({exc}).")
            continue

    return advisories


def preflight(app: str, env: str) -> None:
    """Materialize the scaffolding and the pinned collection ahead of a deploy.

    The rebootstrap hard gate lives in `cmd/deploy.py` and runs earlier.
    """
    generate.generate_all(app, env)
    runner.galaxy_install()


def preflight_all(
    app: str | None = None, env: str | None = None, components: list[str] | None = None
) -> list[str]:
    """Run the rebootstrap-status and env-key sweep across managed app/env pairs.

    Sweeps every pair when app and env are omitted; `--component` requires
    both. Offline and warn-only: never touches the collection or the network.
    """
    if components and not (app and env):
        raise StCliError("--component requires both APP and ENV.")

    m = manifest.load_manifest()
    if app and env:
        pairs: list[tuple[str, str]] = [(app, env)]
    else:
        managed = {(u.app, u.env) for u in m.units if u.mode != MODE_EXTERNAL}
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
    for a, e in pairs:
        ui.info(f"Checking {a}/{e} …")
        scoped = components if (app and env) else None
        warnings += check_app(a, e, scoped)
        warnings += env_key_report(a, e, scoped)
    return warnings
