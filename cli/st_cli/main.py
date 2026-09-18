"""st-cli command-line entrypoint (Typer)."""

from __future__ import annotations

import typer

from .cmd import (
    bootstrap as bootstrap_mod,
)
from .cmd import (
    deploy as deploy_mod,
)
from .cmd import (
    remote,
)
from .cmd import (
    secrets as secrets_mod,
)
from .cmd import (
    upgrade as upgrade_mod,
)
from .cmd import (
    version as version_mod,
)
from .core import appmeta, drift, ui, upstream
from .core.errors import StCliError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Bootstrap and operate suitenumerique.st ansible deployments.",
)


@app.callback()
def _main(ctx: typer.Context) -> None:
    """Global pre-command hook: best-effort upstream-version check.

    Fires before every subcommand. Warn-only — it never raises out of this
    callback; every exception is swallowed so the check can never break or
    slow a command noticeably.
    """
    try:
        upstream.maybe_warn_upgrade(ctx.invoked_subcommand)
    except Exception:
        pass


def _run(fn):
    """Execute fn(): StCliError → clean exit(1); a non-zero int return → exit(that rc).

    Remote ops (oneoff/reset/logs) return the ssh/ansible return code; propagating it
    lets CI/scripts detect a failed one-off or reset instead of always seeing exit 0.
    """
    try:
        rc = fn()
    except StCliError as exc:
        ui.error(str(exc))
        raise typer.Exit(1)
    if isinstance(rc, int) and rc != 0:
        raise typer.Exit(rc)


@app.command()
def bootstrap(
    app_name: str = typer.Argument(..., metavar="APP"),
    env: str = typer.Argument(...),
    component: str = typer.Option(
        None,
        "--component",
        "-c",
        help="Bootstrap only this component (e.g. a provider like livekit).",
    ),
):
    """Interactively create the versioned config tree for APP/ENV."""
    _run(lambda: bootstrap_mod.bootstrap(app_name, env, component))


@app.command()
def deploy(
    app_name: str = typer.Argument(..., metavar="APP"),
    env: str = typer.Argument(...),
    component: list[str] = typer.Option(
        None, "--component", "-c", help="Deploy only this component (repeatable)."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="ansible --check: make no changes, show what would.",
    ),
    deploy_only: bool = typer.Option(
        False,
        "--deploy-only",
        "-d",
        help="Run only the app-user deploy phase (no root). Base must already be provisioned.",
    ),
    host: str = typer.Option(
        None,
        "--host",
        "-H",
        help="Deploy only this host (inventory alias, e.g. meet1); default: all hosts, serial 1.",
    ),
):
    """Generate scaffolding and run the ansible playbooks for APP/ENV.

    By default runs both the root 'base' phase (idempotent podman/user install)
    and the app-user 'deploy' phase. Use --deploy-only for routine updates by an
    unprivileged user once the base is in place. Every play is serial: 1 (hosts
    roll out one at a time); use -H/--host <alias> to deploy a single host.
    """
    _run(lambda: deploy_mod.run(app_name, env, component, dry_run, deploy_only, host))


@app.command()
def secrets(
    app_name: str = typer.Argument(..., metavar="APP"),
    env: str = typer.Argument(...),
    component: str = typer.Option(
        None,
        "--component",
        "-c",
        help="Edit this component's secrets (default: prompt).",
    ),
):
    """Edit APP/ENV's ansible-vault secrets in $EDITOR (prompts for the component)."""
    _run(lambda: secrets_mod.edit_secrets(app_name, env, component))


@app.command()
def restart(
    app_name: str = typer.Argument(..., metavar="APP"),
    env: str = typer.Argument(...),
    component: list[str] = typer.Option(
        None, "--component", "-c", help="Restart only this component (repeatable)."
    ),
    host: str = typer.Option(
        None,
        "--host",
        "-H",
        help="Restart only this host (inventory alias, e.g. meet1); default: all its hosts.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt when restarting all."
    ),
    parallel: bool = typer.Option(
        False,
        "--parallel",
        "-p",
        help="Restart components concurrently (each still rolls its own hosts one at "
        "a time). Ignores deploy_order.",
    ),
):
    """Restart the systemd --user services for APP/ENV over ssh.

    Bare (no -c) restarts ALL managed components — warns and asks to confirm first
    (-y skips). -c restarts the listed components (no confirm); -H <alias> one host.
    -p/--parallel restarts components concurrently (each still rolls its own hosts
    one at a time) and ignores deploy_order.
    """
    _run(
        lambda: remote.restart(
            app_name, env, component, host=host, assume_yes=yes, parallel=parallel
        )
    )


@app.command()
def ps(
    app_name: str = typer.Argument(..., metavar="APP"),
    env: str = typer.Argument(...),
    component: list[str] = typer.Option(
        None, "--component", "-c", help="Only this component (repeatable)."
    ),
    host: str = typer.Option(
        None,
        "--host",
        "-H",
        help="Only this host (inventory alias, e.g. meet1); default: all its hosts.",
    ),
):
    """Show `podman ps -a` across APP/ENV's hosts over ssh.

    Runs `podman ps -a` as each managed component's app user, per host (workers
    skipped). -c narrows to a subset of components, -H <alias> to one host.
    """
    _run(lambda: remote.ps(app_name, env, component, host=host))


@app.command()
def oneoff(
    app_name: str = typer.Argument(..., metavar="APP"),
    env: str = typer.Argument(...),
    component: str = typer.Option(
        None, "--component", "-c", help="Target component (default: core)."
    ),
    host: str = typer.Option(
        None,
        "--host",
        "-H",
        help="Target a specific host (default: prompt if several).",
    ),
    service: str = typer.Option("backend", "--service", "-s", help="Compose service."),
    entrypoint: str = typer.Option(
        None,
        "--entrypoint",
        "-e",
        help="Override the container entrypoint (e.g. 'sh' for collabora).",
    ),
    cmd: list[str] = typer.Argument(
        None, help="Command to run (default: interactive shell)."
    ),
):
    """Run a one-off container command (default: a shell in the backend)."""

    def _do():
        comp = component or appmeta.load_app(app_name).core().key
        return remote.oneoff(
            app_name,
            env,
            comp,
            host=host,
            service=service,
            cmd=cmd or None,
            entrypoint=entrypoint or None,
        )

    _run(_do)


@app.command()
def reset(
    app_name: str = typer.Argument(..., metavar="APP"),
    env: str = typer.Argument(...),
    component: str = typer.Option(
        None, "--component", "-c", help="Target component (default: core)."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
    host: str = typer.Option(
        None,
        "--host",
        "-H",
        help="Target a specific host (default: prompt if several).",
    ),
):
    """Destructive: stop, down -v, remove the app dir, then redeploy a component."""

    def _do():
        comp = component or appmeta.load_app(app_name).core().key
        return remote.reset(app_name, env, comp, assume_yes=yes, host=host)

    _run(_do)


@app.command()
def logs(
    app_name: str = typer.Argument(..., metavar="APP"),
    env: str = typer.Argument(...),
    component: str = typer.Option(
        None, "--component", "-c", help="Target component (default: core)."
    ),
    host: str = typer.Option(
        None,
        "--host",
        "-H",
        help="Target a specific host (default: prompt if several).",
    ),
    since: str = typer.Option(
        "15 min ago",
        "--since",
        help="journalctl --since window (e.g. '1 hour ago', '2026-07-01').",
    ),
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Stream the journal live (journalctl -f)."
    ),
):
    """Show the systemd --user journal for APP/ENV (default: the core backend)."""

    def _do():
        comp = component or appmeta.load_app(app_name).core().key
        return remote.logs(app_name, env, comp, host=host, since=since, follow=follow)

    _run(_do)


@app.command()
def doctor(
    app_name: str = typer.Argument(None, metavar="[APP]"),
    env: str = typer.Argument(None, metavar="[ENV]"),
    component: list[str] = typer.Option(
        None, "--component", "-c", help="(repeatable)."
    ),
):
    """Report units with an outstanding rebootstrap requirement (warn-only).

    With no args, sweep every managed (app, env) pair in .st-cli.yml (external
    units are skipped). With APP only, check all envs of that app. With both
    APP and ENV, check that single unit (optionally narrowed by --component,
    which is repeatable). Fully offline: does not touch the collection or the
    network.
    """

    def _do():
        warnings = drift.preflight_all(app_name, env, component)
        if not warnings:
            ui.success("No rebootstrap needed.")
        for w in warnings:
            ui.warn(w)

    _run(_do)


@app.command()
def upgrade():
    """Upgrade the CLI, realign the .st-cli.yml pin, and clean the scaffolding."""
    _run(upgrade_mod.upgrade)


@app.command()
def version():
    """Print versions and warn on manifest/installed mismatch."""
    _run(version_mod.show_version)


if __name__ == "__main__":
    app()
