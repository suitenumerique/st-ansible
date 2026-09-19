"""Direct ssh operations: restart, ps, oneoff, reset, logs. No ansible.

``-H/--host`` is the inventory alias, validated against the unit's hosts file.
``restart`` rolls each component's hosts one at a time so it is never fully down.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import questionary

from ..core import appmeta, manifest, runner, sshuser, tree, ui
from ..core.errors import StCliError


@dataclass
class Target:
    host: str
    user: str
    app_name: str
    remote_dir: str
    alias: str


def _as_user(user: str, inner: str) -> str:
    """Wrap ``inner`` to run in the app user's login shell via `sudo -iu`.

    `sudo -iu` sources ~/.bash_profile so XDG_RUNTIME_DIR and DOCKER_HOST are set.
    `inner` is already quoted; quote it again to survive the remote shell and sudo.
    """
    return f"sudo -iu {shlex.quote(user)} bash -lc {shlex.quote(inner)}"


def _require_tty_confirm(
    non_tty_msg: str, warn_msg: str, *, confirmed: Callable[[], object]
) -> bool:
    """Guard an interactive prompt: raise off a TTY, else warn and call `confirmed`.

    Returns False, after the warning "Aborted.", when the operator declines.
    """
    if not sys.stdin.isatty():
        raise StCliError(non_tty_msg)
    ui.warn(warn_msg)
    if not confirmed():
        ui.warn("Aborted.")
        return False
    return True


def _select_host(
    entries: list[tuple[str, str]], host: str | None, pick: bool
) -> tuple[str, str]:
    """Pick one ``(alias, ip)``: an explicit alias, the sole host, or a TTY prompt.

    Raises when several hosts exist and none is chosen, rather than defaulting to the
    first.
    """
    if host is not None:
        e = tree.find_host(entries, host)
        if e is None:
            raise StCliError(
                f"Host '{host}' is not an alias of this unit: {tree.alias_list(entries)}."
            )
        return e
    if len(entries) == 1:
        return entries[0]
    if pick and sys.stdin.isatty():
        labels = {f"{a} ({ip})": (a, ip) for a, ip in entries}
        answer = questionary.select(
            "Multiple hosts for this unit — pick one:",
            choices=list(labels),
            default=next(iter(labels)),
        ).ask()
        if answer is None:  # user cancelled (Ctrl-C / ESC)
            raise StCliError("Aborted: no host selected.")
        return labels[answer]
    raise StCliError(
        f"Unit has multiple hosts ({tree.alias_list(entries)}); choose one with "
        f"--host/-H (or run on a TTY to be prompted)."
    )


def resolve_target(
    app: str, env: str, component: str, host: str | None = None, pick: bool = False
) -> Target:
    """Resolve (host ip, app user, systemd name, remote dir, alias) for a unit."""
    m = manifest.load_manifest()
    if not manifest.units_for(m, app, env, [component]):
        raise StCliError(f"No unit {app}/{env}/{component} in .st-cli.yml.")
    meta = appmeta.load_app(app)
    comp = meta.component(component)
    # A worker has no hosts/vars files of its own: vars and remote dir come
    # from the core unit, but app_name and dir_var stay the worker's.
    entries = tree.component_inventory(app, env, meta, comp)
    if not entries:
        raise StCliError(
            f"Unit {app}/{env}/{component} has no hosts (check its hosts file)."
        )
    alias, ip = _select_host(entries, host, pick)
    files = meta.files_component(component)
    data = tree.load_vars(app, env, files.key)
    remote_dir = data.get(comp.dir_var) or f"/opt/{comp.user}/{comp.app_name}"
    return Target(
        host=ip,
        user=comp.user,
        app_name=comp.app_name,
        remote_dir=str(remote_dir),
        alias=alias,
    )


def _ssh(
    target_host: str,
    remote_cmd: str,
    interactive: bool = True,
    quiet: bool = False,
    capture_stderr: bool = False,
) -> int:
    """Run ``remote_cmd`` on ``target_host`` over ssh and return the exit code.

    ``quiet`` discards both streams; ``capture_stderr`` buffers stderr and replays
    it only on failure. ``quiet`` wins when both are set.
    """
    sshuser.ensure_ssh_user([target_host])
    user = manifest.ssh_user()
    target = f"{user}@{target_host}" if user else target_host
    cmd = ["ssh", "-o", "LogLevel=ERROR"]
    if interactive:
        cmd.append("-t")
    cmd += [target, remote_cmd]
    opts: dict = {}
    if quiet:
        opts = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    elif capture_stderr:
        opts = {"stderr": subprocess.PIPE, "text": True}
    proc = subprocess.run(cmd, check=False, **opts)
    rc = proc.returncode
    if (
        rc != 0 and capture_stderr
    ):  # replay the buffered transport+error output on failure
        err = (proc.stderr or "").strip()
        if err:
            ui.warn(err)
    if (
        rc == 255 and not quiet
    ):  # ssh connection error, often a host key not yet accepted
        ui.warn(
            f"ssh could not connect to {target} (exit 255). If this host is "
            f"new, its key hasn't been accepted yet — connect once manually with "
            f"`ssh {target}` (or run this on a terminal to be "
            f"prompted) to accept it. Otherwise check the host/network/credentials."
        )
    return rc


def _restart_component(comp, hosts: list[tuple[str, str]], reporter) -> list[str]:
    """Restart one component, rolling its hosts one at a time.

    Never raises; returns the list of failure strings for the caller to aggregate.
    """
    failures: list[str] = []
    n = len(hosts)
    plural = "host" if n == 1 else "hosts"
    handle = reporter.start(f"Restarting {comp.app_name} …")
    for i, (alias, ip) in enumerate(hosts, start=1):
        reporter.update(handle, f"Restarting {comp.app_name} — {alias} ({i}/{n})")
        rc = _ssh(
            ip,
            _as_user(
                comp.user, f"systemctl --user restart {shlex.quote(comp.app_name)}"
            ),
            interactive=False,
            quiet=True,
        )
        if rc != 0:
            failures.append(f"{comp.app_name}@{alias} (rc={rc})")
    if failures:
        reporter.fail(
            handle, f"{comp.app_name} failed to restart ({len(failures)}/{n} {plural})"
        )
    else:
        reporter.done(handle, f"Successfully restarted {comp.app_name} ({n} {plural})")
    return failures


def restart(
    app: str,
    env: str,
    components: list[str] | None = None,
    host: str | None = None,
    assume_yes: bool = False,
    parallel: bool = False,
) -> None:
    """Restart systemd --user services over ssh for each targeted host.

    Bare restart (no --component) confirms first, unless ``assume_yes``.
    ``-p/--parallel`` runs components concurrently, each still rolling its own hosts one
    at a time.
    """
    meta = appmeta.load_app(app)
    _m, units = manifest.managed_units(app, env, components)
    if not components and not assume_yes:
        names = ", ".join(u.component for u in units)
        proceed = _require_tty_confirm(
            "Refusing to restart ALL components non-interactively; "
            "pass -c <component> or -y/--yes.",
            f"This will restart ALL components ({names}) of {app}/{env} "
            "across all their hosts.",
            confirmed=lambda: questionary.confirm("Proceed?", default=False).ask(),
        )
        if not proceed:
            return
    # group hosts per component, preserving deploy_order (managed_units sorts by it) and
    # inventory order within each component.
    groups: dict = {}
    for comp, alias, ip in tree.iter_targeted_hosts(
        app, env, meta, units, components, host
    ):
        groups.setdefault(comp.key, [comp, []])[1].append((alias, ip))
    if not groups:
        return

    with ui.progress_reporter() as reporter:
        if parallel:
            # Pre-warm the once-per-process ssh-user guard on the MAIN thread so worker
            # threads don't race the first prompt / double-write ssh/config.local.
            first_ip = next(iter(groups.values()))[1][0][1]
            sshuser.ensure_ssh_user([first_ip])
            with ThreadPoolExecutor(max_workers=len(groups)) as ex:
                results = list(
                    ex.map(
                        lambda g: _restart_component(g[0], g[1], reporter),
                        groups.values(),
                    )
                )
        else:
            results = [
                _restart_component(comp, hosts, reporter)
                for comp, hosts in groups.values()
            ]

    failures = [f for group_failures in results for f in group_failures]
    if failures:
        raise StCliError(
            "Restart failed on: "
            + ", ".join(failures)
            + ". Run `st-cli logs <component> -H <alias>` for details."
        )


def ps(
    app: str,
    env: str,
    components: list[str] | None = None,
    host: str | None = None,
) -> None:
    """Run ``podman ps -a`` as each managed component's app user, over ssh.

    Skips worker components; a nonzero rc on one host warns and continues.
    """
    meta = appmeta.load_app(app)
    _m, units = manifest.managed_units(app, env, components)
    for comp, alias, ip in tree.iter_targeted_hosts(
        app, env, meta, units, components, host, skip_workers=True
    ):
        ui.host_header(comp.app_name, ip)
        rc = _ssh(
            ip,
            _as_user(comp.user, "podman ps -a"),
            interactive=False,
            capture_stderr=True,
        )
        if rc != 0:
            ui.warn(f"podman ps -a on {alias} returned rc={rc}.")


def logs(
    app: str,
    env: str,
    component: str,
    host: str | None = None,
    since: str = "15 min ago",
    follow: bool = False,
) -> int:
    """Show a unit's systemd --user journal over ssh via journalctl."""
    t = resolve_target(app, env, component, host=host, pick=True)
    inner = (
        f"journalctl --user -u {shlex.quote(t.app_name)} --since {shlex.quote(since)}"
    )
    if follow:
        inner += " -f"
    ui.info(
        f"Logs for {t.app_name} on {t.host} (user {t.user})"
        + (" [follow]" if follow else "")
    )
    return _ssh(t.host, _as_user(t.user, inner))


def oneoff(
    app: str,
    env: str,
    component: str,
    host: str | None = None,
    service: str = "backend",
    cmd: list[str] | None = None,
    entrypoint: str | None = None,
) -> int:
    """Open an interactive one-off container shell, or run ``cmd``, for a unit.

    ``entrypoint`` overrides the image entrypoint, for a container whose default
    entrypoint starts a server.
    """
    t = resolve_target(app, env, component, host=host, pick=True)
    run_cmd = " ".join(shlex.quote(c) for c in cmd) if cmd else "sh"
    opts = "--rm"
    if entrypoint:  # --entrypoint must precede the service name
        opts += f" --entrypoint {shlex.quote(entrypoint)}"
    inner = (
        f"cd {shlex.quote(t.remote_dir)} && "
        f"podman-compose run {opts} {shlex.quote(service)} {run_cmd}"
    )
    ui.info(f"One-off on {t.host}: {service} ({t.remote_dir})")
    return _ssh(t.host, _as_user(t.user, inner))


def reset(
    app: str,
    env: str,
    component: str,
    assume_yes: bool = False,
    host: str | None = None,
) -> int:
    """Destructive: stop, down -v, rm the app dir, then redeploy a unit."""
    t = resolve_target(app, env, component, host=host, pick=True)
    if not assume_yes:
        proceed = _require_tty_confirm(
            f"Refusing to reset {t.app_name} non-interactively; pass -y/--yes.",
            f"This will STOP {t.app_name}, run 'podman-compose down -v' (removing "
            f"named volumes) and DELETE {t.remote_dir} on {t.host}, then redeploy.\n"
            "External Postgres/S3 are untouched; local container volumes are wiped.",
            confirmed=lambda: (
                questionary.text(f"Type the unit name '{t.app_name}' to confirm:").ask()
                == t.app_name
            ),
        )
        if not proceed:
            return 1

    q = shlex.quote
    teardown = (
        f"systemctl --user stop {q(t.app_name)} || true; "
        f"cd {q(t.remote_dir)} && podman-compose down -v || true; "
        f"rm -rf {q(t.remote_dir)}"
    )
    rc = _ssh(
        t.host, _as_user(t.user, teardown), interactive=False, capture_stderr=True
    )
    if rc != 0:
        raise StCliError(f"Teardown failed (rc={rc}); not redeploying.")

    from ..core import generate

    ui.info(f"Redeploying {app}/{env}/{component} …")
    generate.generate_all(app, env)
    runner.galaxy_install()
    return runner.play(app, env, component, limit=t.alias)
