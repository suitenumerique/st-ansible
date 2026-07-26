"""Preflight checks run before a deploy, and the standalone `doctor` sweep.

Historically this module also materialized the pinned collection to diff
committed ``st_*`` vars against each role's ``meta/argument_specs.yml``. That
check is gone: it flagged hand-edited ``vars.yml`` keys, which is backwards
for a config tree we explicitly tell operators to edit by hand (see
``core/writer.py`` / the bootstrap docs). ``check_app`` is now the
**rebootstrap-status report**: it surfaces which bootstrapped units have an
outstanding rebootstrap flag (``core/rebootstrap.py``) so operators learn
*before* a deploy that a release requires them to replay the bootstrap
questionnaire.

``preflight`` (single pair, used by ``deploy``) still materializes the pinned
collection first — a deploy needs it installed regardless of drift status.
``preflight_all`` (sweep, used by ``doctor``) no longer touches the collection
or the network at all: with the argspec check gone, there is nothing left
that needs it, so `doctor` is now fast and fully offline.

Neither function ever touches the committed config tree.
"""

from __future__ import annotations

from . import generate, manifest, rebootstrap, runner, ui
from .errors import StCliError
from .models import RebootstrapNeed


def check_app(app: str, env: str, components: list[str] | None = None) -> list[str]:
    """Return human-readable rebootstrap-status warnings for an app/env.

    Checks every (optionally ``components``-narrowed) unit of ``(app, env)``
    against ``rebootstrap.needed()`` and reports the ones with an outstanding
    flag. External units are already skipped inside ``rebootstrap.needed()``
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
    newest: dict[str, RebootstrapNeed] = {}
    for n in rebootstrap.needed(m, app, env):
        if n.component not in wanted:
            continue
        cur = newest.get(n.component)
        if cur is None or rebootstrap.parse_version(
            n.version
        ) > rebootstrap.parse_version(cur.version):
            newest[n.component] = n

    out: list[str] = []
    for comp in sorted(newest):
        n = newest[comp]
        msg = (
            f"{app}/{env}/{comp}: rebootstrap needed ({n.version} — {n.reason}). "
            f"Run `st-cli bootstrap {app} {env}`."
        )
        if n.link:
            msg += f" See {n.link}."
        out.append(msg)
    return out


def preflight(app: str, env: str, components: list[str] | None = None) -> list[str]:
    """Materialize the pinned collection, then report rebootstrap status.

    Shared preflight used by ``deploy``: render the trashable scaffolding
    (``generate.generate_all``) and install the pinned collection
    (``runner.galaxy_install``) — a deploy needs the collection materialized
    regardless of drift status — then return the ``check_app`` rebootstrap
    warnings (warn-only / non-blocking; ``deploy`` itself hard-gates on a
    pending rebootstrap separately, see ``cmd/deploy.py``).
    """
    generate.generate_all(app, env)
    runner.galaxy_install()
    return check_app(app, env, components)


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
        warnings += check_app(a, e, components if (app and env) else None)
    return warnings
