"""st-cli command-line entrypoint (Typer)."""

from __future__ import annotations

import contextlib

import typer

from .cmd import bootstrap as bootstrap_mod
from .cmd import deploy as deploy_mod
from .cmd import generate_keypairs as generate_keypairs_mod
from .cmd import remote
from .cmd import secrets as secrets_mod
from .cmd import upgrade as upgrade_mod
from .cmd import version as version_mod
from .core import appmeta, drift, ui, upstream
from .core.errors import StCliError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Bootstrap and operate suitenumerique.st ansible deployments.",
)

_component_option = typer.Option(
    None, "--component", "-c", help="Target component (default: core)."
)
_host_option = typer.Option(
    None,
    "--host",
    "-H",
    help="Target a specific host (default: prompt if several).",
)


def _default_component(app_name: str, component: str | None) -> str:
    return component or appmeta.load_app(app_name).core().key


@app.callback()
def _main(ctx: typer.Context) -> None:
    """Global pre-command hook. Never raises: every exception is swallowed."""
    with contextlib.suppress(Exception):
        upstream.maybe_warn_upgrade(ctx.invoked_subcommand)


def _run(fn):
    """Run fn(); a non-zero int return becomes the exit code, so scripts see remote
    failures."""
    try:
        rc = fn()
    except StCliError as exc:
        ui.error(str(exc))
        raise typer.Exit(1) from None
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


@app.command("generate-keypairs")
def generate_keypairs(
    app_name: str = typer.Argument(..., metavar="APP"),
    env: str = typer.Argument(...),
):
    """Mint Ed25519 caller keypair(s) for APP/ENV (file-scanner JWT auth).

    Asks an issuer name per keypair, then prints a wiring summary: the
    iss:pubkey fragments for the scanner's JWT_ISSUER_KEYS and, for each caller,
    the private key to hand over (shown once — st-cli stores no copy).
    """
    _run(lambda: generate_keypairs_mod.generate(app_name, env))


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

    Bare (no -c) restarts ALL managed components, warns, and asks to confirm first
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
    component: str = _component_option,
    host: str = _host_option,
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
        comp = _default_component(app_name, component)
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
    component: str = _component_option,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
    host: str = _host_option,
):
    """Destructive: stop, down -v, remove the app dir, then redeploy a component."""

    def _do():
        comp = _default_component(app_name, component)
        return remote.reset(app_name, env, comp, assume_yes=yes, host=host)

    _run(_do)


@app.command()
def logs(
    app_name: str = typer.Argument(..., metavar="APP"),
    env: str = typer.Argument(...),
    component: str = _component_option,
    host: str = _host_option,
    since: str = typer.Option(
        "15 min ago",
        "--since",
        help="journalctl --since window (e.g. '1 hour ago', '2026-07-01').",
    ),
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Stream the journal live (journalctl -f)."
    ),
):
    """Show the systemd --user journal of APP/ENV's unit (the whole compose stack)."""

    def _do():
        comp = _default_component(app_name, component)
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
    """Realign the .st-cli.yml pin, replay flagged units, and clean the scaffolding."""
    _run(upgrade_mod.upgrade)


@app.command()
def version():
    """Print the installed CLI version and the .st-cli.yml pins."""
    _run(version_mod.show_version)


if __name__ == "__main__":
    app()
