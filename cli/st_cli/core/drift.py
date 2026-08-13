"""Preflight checks run before a deploy, and the standalone `doctor` sweep.

Historically this module also materialized the pinned collection to diff
committed ``st_*`` vars against each role's ``meta/argument_specs.yml``. That
check is gone: it flagged hand-edited ``vars.yml`` keys, which is backwards
for a config tree we explicitly tell operators to edit by hand (see
``core/writer.py`` / the bootstrap docs). ``check_app`` is now the
**rebootstrap-status report**: it surfaces which bootstrapped units have an
outstanding rebootstrap flag (``core/upgrades.py``) so operators learn
*before* a deploy that a release requires them to replay the bootstrap
questionnaire.

``preflight`` (single pair, used by ``deploy``) only materializes the pinned
collection now — the rebootstrap hard gate lives in ``cmd/deploy.py`` and runs
earlier, before any remote or network side effect. ``preflight_all`` (sweep,
used by ``doctor``) still touches neither the collection nor the network at
all: with the argspec check gone, there is nothing left that needs it, so
`doctor` is fast and fully offline.

``env_key_report`` is a second, unrelated signal: an offline diff between each
unit's committed env blob and a fresh render from the current templates. It
surfaces new upstream env keys (advisories) and operator/leftover keys unknown
to the current templates (infos). It is warn-only and never gates ``deploy``.

Neither ``check_app`` nor ``env_key_report`` ever touches the committed config
tree.
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


def check_app(app: str, env: str, components: list[str] | None = None) -> list[str]:
    """Return human-readable rebootstrap-status warnings for an app/env.

    Checks every (optionally ``components``-narrowed) unit of ``(app, env)``
    against ``upgrades.needed()`` and reports the ones with an outstanding
    flag. External units are already skipped inside ``upgrades.needed()``
    (they have no local tree for bootstrap to rewrite); if EVERY matched unit
    is external, this returns a single explicit warning rather than an empty
    list — an empty result would read as "clean" even though nothing was
    actually evaluated.

    When several flags apply to the same unit, only the newest is reported:
    an operator only needs to run the rebootstrap once, and the
    freshly-replayed questionnaire naturally re-asks everything older flags
    would have too, so listing every historical flag would just be noise.

    Each warning names the unit (``app/env/component``), the flagged version,
    the reason, the changelog/PR link when the flag carries one, and the
    exact command to run (a plain, un-narrowed ``st-cli bootstrap <app>
    <env>`` — rebootstrap replays the whole questionnaire for the pair, not
    just one component).
    """
    m = manifest.load_manifest()
    units = manifest.units_for(m, app, env, components)
    if not units:
        raise StCliError(f"No units for {app}/{env} in .st-cli.yml.")
    managed = [u for u in units if u.mode != "external"]
    if not managed:
        scope = f"{app}/{env}" + (f"/{','.join(components)}" if components else "")
        return [f"{scope}: all units are external — nothing to rebootstrap-check."]

    wanted = {u.component for u in managed}
    needs = [n for n in upgrades.needed(m, app, env) if n.component in wanted]

    out: list[str] = []
    for n in upgrades.newest_per_unit(needs):
        msg = (
            f"{app}/{env}/{n.component}: rebootstrap needed ({n.version} — {n.reason}). "
            f"Run `st-cli bootstrap {app} {env}`."
        )
        if n.link:
            msg += f" See {n.link}."
        out.append(msg)
    return out


def env_key_report(
    app: str, env: str, components: list[str] | None = None
) -> tuple[list[str], list[str]]:
    """Diff each unit's committed env blob against a fresh offline render.

    Returns ``(advisories, infos)``. For every non-external unit of ``(app,
    env)`` (optionally ``components``-narrowed, via ``manifest.units_for``):
    recovers ``answers`` from the committed tree (``core/recover.py``),
    re-renders the env blobs offline from the current templates
    (``core/envrender.render_env``), and compares each rendered blob's key
    list against the committed one (``core/envblob.keys``, both
    order-preserving).

    A key present in the render but missing from the committed blob is a new
    upstream key the operator has not set yet — collected per unit into one
    advisory. A key present in the committed blob but absent from the render
    is unknown to the current templates (an operator's own addition, or a
    leftover from a removed template key) — collected per unit into one info
    line. A unit whose blobs match exactly reports nothing.

    Units with no ``env_render`` spec (providers with no env blob, workers)
    and units with nothing recoverable (``recover.recover`` returns ``{}``,
    e.g. no committed ``vars.yml`` yet) are skipped — there is nothing to
    compare. Best-effort: any exception while processing one unit (unknown
    app, a render error) skips just that unit and reports the skip via an
    info line, so one bad unit never breaks the sweep this feeds (``doctor``).

    **Detection is deliberately partial**: a template line guarded by
    ``{% if answers.X %}`` renders nothing at all when ``X`` has no answer, so
    only unconditionally-emitted keys are ever reported as missing — this
    catches new mandatory vars, not new optional ones.
    """
    m = manifest.load_manifest()
    units = manifest.units_for(m, app, env, components)
    managed = [u for u in units if u.mode != "external"]

    advisories: list[str] = []
    infos: list[str] = []
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
            unknown: list[str] = []
            for info in spec.values():
                blob_var = info.get("blob_var")
                committed_text = data.get(blob_var) if blob_var else None
                if not isinstance(committed_text, str):
                    continue
                rendered_keys = envblob.keys(rendered.get(blob_var, ""))
                committed_keys = envblob.keys(committed_text)
                committed_set = set(committed_keys)
                rendered_set = set(rendered_keys)
                missing += [k for k in rendered_keys if k not in committed_set]
                unknown += [k for k in committed_keys if k not in rendered_set]

            if missing:
                advisories.append(
                    f"{app}/{env}/{u.component}: new env keys available: "
                    f"{', '.join(missing)} — run `st-cli bootstrap {app} {env}` "
                    "to set them."
                )
            if unknown:
                infos.append(
                    f"{app}/{env}/{u.component}: {len(unknown)} keys unknown to "
                    f"the current templates: {', '.join(unknown)} — custom vars "
                    "or leftovers; remove only if you did not add them."
                )
        except Exception as exc:
            ui.info(f"{app}/{env}/{u.component}: env-key check skipped ({exc}).")
            continue

    return advisories, infos


def preflight(app: str, env: str) -> None:
    """Materialize the scaffolding + pinned collection for a deploy.

    Renders the trashable scaffolding (``generate.generate_all``) and
    installs the pinned collection (``runner.galaxy_install``) — a deploy
    needs both regardless of drift status. The rebootstrap hard gate lives in
    ``cmd/deploy.py`` and runs earlier, before this (and before any other
    remote/network side effect); it no longer runs from here.
    """
    generate.generate_all(app, env)
    runner.galaxy_install()


def preflight_all(
    app: str | None = None, env: str | None = None, components: list[str] | None = None
) -> list[str]:
    """Rebootstrap-status sweep across every managed ``(app, env)`` pair (warn-only).

    Sweeps the whole ``.st-cli.yml`` when no args are given (external units
    are skipped), narrows to one app when only APP is given, or checks a
    single ``(app, env)`` unit when both are given. ``--component`` requires
    both APP and ENV (a component is meaningless without its unit).

    Unlike ``preflight``, this never touches the collection or the network:
    now that the argspec drift check is gone, ``check_app`` only reads
    ``.st-cli.yml`` and the rebootstrap flag declaration, both local — so
    `doctor` stays fast and fully offline.

    Also runs ``env_key_report`` per pair: its advisories are appended to the
    returned warnings (so a new-env-key advisory suppresses doctor's "No
    rebootstrap needed." success line, same as a rebootstrap warning), while
    its infos are printed directly via ``ui.info`` — they are not warnings.
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
    for a, e in pairs:
        ui.info(f"Checking {a}/{e} …")
        scoped = components if (app and env) else None
        warnings += check_app(a, e, scoped)
        advisories, infos = env_key_report(a, e, scoped)
        warnings += advisories
        for msg in infos:
            ui.info(msg)
    return warnings
