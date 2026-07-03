"""Direct ssh operations for restart / ps / oneoff / reset / logs (no ansible).

Every command targets hosts read from the component's ``hosts`` file. ``restart``
and ``ps`` loop ssh over each host of a unit (all by default, or one via
``-H/--host``); ``logs`` / ``oneoff`` / ``reset`` hit exactly one host (prompting
to pick when several). ``-H`` is the inventory *alias* (e.g. ``meet1``), validated
against the component's own hosts file so a stray value can't reach another env;
ssh connects to the alias's ``ansible_host`` ip. ``restart -p/--parallel`` fans
the components out concurrently while keeping each component's own hosts serial
(one at a time, so a multi-host unit is never fully down). ``restart`` shows a
per-component progress reporter — a live spinner on a TTY (advancing per host,
leaving a persistent ``✓``/``✗`` summary line) or plain info/success/error lines
off a TTY — and suppresses ssh transport stdout/stderr so banners, motd and
host-key chatter can't clutter the display or garble the spinner.

ssh transport noise (the sshd auth Banner, the ``sudo -i`` login motd, and
``Warning: Permanently added … to known_hosts``) all lands on stderr and carries no
signal on success. The non-interactive, output-producing commands (``ps``, ``reset``
teardown) run with ``_ssh(capture_stderr=True)``: stdout streams live, stderr is
buffered and replayed only if the command fails. The interactive commands
(``logs`` / ``oneoff``) can't capture (their stderr is merged onto the PTY), but
every ``_ssh`` call passes ``-o LogLevel=ERROR`` to trim client-side host-key
warnings.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterator

from ..core import appmeta, manifest, runner, sshuser, tree, ui
from ..core.errors import StCliError


@dataclass
class Target:
    host: str
    user: str
    app_name: str
    remote_dir: str


def _ssh_user() -> str | None:
    """Resolve the ssh user (ST_CLI_SSH_USER env var, else None → ssh config)."""
    return manifest.ssh_user()


def _as_user(user: str, inner: str) -> str:
    """Wrap an inner shell command to run in the app user's login shell.

    ``sudo -iu`` sources ~/.bash_profile so XDG_RUNTIME_DIR / DOCKER_HOST are set
    (needed for ``systemctl --user`` and rootless podman). ``inner`` is assembled
    from shlex.quote'd fragments; quote it (and user) once more to survive the
    remote-shell + sudo argv layers intact.
    """
    return f"sudo -iu {shlex.quote(user)} bash -lc {shlex.quote(inner)}"


def _aliases(entries: list[tuple[str, str]]) -> str:
    return ", ".join(a for a, _ in entries)


def _select_host(
    entries: list[tuple[str, str]], host: str | None, pick: bool
) -> tuple[str, str]:
    """Pick one ``(alias, ip)``: an explicit --host alias (validated), the sole
    host, or an interactive prompt when several and pick=True on a TTY. With
    several hosts and no choice, raise rather than silently defaulting to the
    first."""
    if host is not None:
        e = tree.find_host(entries, host)
        if e is None:
            raise StCliError(
                f"Host '{host}' is not an alias of this unit: {_aliases(entries)}."
            )
        return e
    if len(entries) == 1:
        return entries[0]
    if pick and sys.stdin.isatty():
        import questionary

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
        f"Unit has multiple hosts ({_aliases(entries)}); choose one with --host/-H "
        f"(or run on a TTY to be prompted)."
    )


def resolve_target(
    app: str, env: str, component: str, host: str | None = None, pick: bool = False
) -> Target:
    """Resolve (host ip, app user, systemd name, remote dir) for a unit."""
    m = manifest.load_manifest()
    if not manifest.units_for(m, app, env, [component]):
        raise StCliError(f"No unit {app}/{env}/{component} in .st-cli.yml.")
    meta = appmeta.load_app(app)
    comp = meta.component(component)
    # workers own no hosts/vars files — they reuse the core unit's. component_inventory
    # follows the effective_group rule (a worker with its own [workers] group targets
    # it, else the core group); the vars file path and remote dir stay anchored on the
    # core (files), while app_name/dir_var stay the worker's own.
    entries = tree.component_inventory(app, env, meta, comp)
    if not entries:
        raise StCliError(
            f"Unit {app}/{env}/{component} has no hosts (check its hosts file)."
        )
    _alias, ip = _select_host(entries, host, pick)
    files = meta.files_component(component)
    data = tree.load_vars(app, env, files.key)
    remote_dir = data.get(comp.dir_var) or f"/opt/{comp.user}/{comp.app_name}"
    return Target(
        host=ip, user=comp.user, app_name=comp.app_name, remote_dir=str(remote_dir)
    )


def _ssh(
    target_host: str,
    remote_cmd: str,
    interactive: bool = True,
    quiet: bool = False,
    capture_stderr: bool = False,
) -> int:
    """Run ``remote_cmd`` on ``target_host`` over ssh, returning the exit code.

    ``interactive`` allocates a tty (``-t``) for shells/pagers. ``quiet`` discards
    BOTH streams (``restart`` only needs the exit code). ``capture_stderr`` keeps
    stdout live but buffers stderr and only replays it on failure — this hides the
    ssh transport noise that carries no signal on success (the sshd auth Banner, the
    ``sudo -i`` login motd, ``Warning: Permanently added … to known_hosts``, all of
    which land on stderr) while still surfacing real errors. ``-o LogLevel=ERROR``
    trims client-side host-key warnings on every call, including the interactive ones
    that can't capture. ``quiet`` and ``capture_stderr`` are mutually exclusive
    (quiet wins)."""
    sshuser.ensure_ssh_user([target_host])
    user = _ssh_user()
    target = f"{user}@{target_host}" if user else target_host
    cmd = ["ssh", "-o", "LogLevel=ERROR"]
    if interactive:
        cmd.append("-t")
    cmd += [target, remote_cmd]
    if quiet:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif capture_stderr:
        proc = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    else:
        proc = subprocess.run(cmd)
    rc = proc.returncode
    if (
        rc != 0 and capture_stderr
    ):  # replay the buffered transport+error output on failure
        err = (proc.stderr or "").strip()
        if err:
            ui.warn(err)
    if (
        rc == 255 and not quiet
    ):  # ssh connection error — often a host key not yet accepted
        ui.warn(
            f"ssh could not connect to {target} (exit 255). If this host is "
            f"new, its key hasn't been accepted yet — connect once manually with "
            f"`ssh {target}` (or run this on a terminal to be "
            f"prompted) to accept it. Otherwise check the host/network/credentials."
        )
    return rc


def _iter_hosts(
    app: str,
    env: str,
    meta,
    units,
    components: list[str] | None,
    host: str | None,
    skip_workers: bool = False,
) -> Iterator[tuple[object, str, str]]:
    """Yield ``(comp, alias, ip)`` for every targeted host across ``units``.

    Default is all of each component's hosts; ``host`` (an alias) narrows to one.
    With ``components`` set, a missing ``host`` raises; without it, a component that
    lacks the host is skipped. If ``host`` is given but matches nothing anywhere,
    raise. ``skip_workers`` drops ``is_worker`` components (used by ``ps``: a worker
    shares the core's user+hosts)."""
    matched = False
    for u in units:
        comp = meta.component(u.component)
        if skip_workers and comp.is_worker:
            continue
        entries = tree.component_inventory(app, env, meta, comp)
        if not entries:
            raise StCliError(
                f"Unit {app}/{env}/{u.component} has no hosts (check its hosts file)."
            )
        if host is not None:
            e = tree.find_host(entries, host)
            if e is None:
                if components:
                    raise StCliError(
                        f"Host '{host}' is not an alias of {app}/{env}/{u.component}: "
                        f"{_aliases(entries)}."
                    )
                ui.info(f"Skipping {u.component}: alias '{host}' not in its inventory.")
                continue
            entries = [e]
        for alias, ip in entries:
            matched = True
            yield comp, alias, ip
    if host is not None and not matched:
        raise StCliError(
            f"Host alias '{host}' matched no managed component's inventory."
        )


def _restart_component(comp, hosts: list[tuple[str, str]], reporter) -> list[str]:
    """Restart ONE component, rolling its hosts one at a time (no downtime).

    Drives ``reporter`` (a ui spinner on a TTY, plain lines otherwise) and returns a
    list of failure strings (empty on success). Never raises — the caller aggregates
    failures across components and raises once."""
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
    """Restart systemd --user services over ssh, looping each targeted host.

    Bare (no --component) restarts ALL managed components and warns + confirms
    first (``assume_yes`` skips). ``--component`` restarts the listed components
    only (no confirm); ``--host <alias>`` narrows to a single host. ``-p/--parallel``
    restarts the components concurrently (each still rolls its own hosts one at a
    time, so a multi-host unit is never fully down) and ignores ``deploy_order``.

    Progress is shown per component: a live spinner on a TTY (advancing as each
    host of a component rolls, leaving a persistent ``✓``/``✗`` summary line on
    exit) or plain info/success/error lines off a TTY (clean CI output). ssh
    transport stdout/stderr is suppressed during restart so banners, motd and
    host-key chatter can't clutter the display or garble the spinner."""
    meta = appmeta.load_app(app)
    _m, units = manifest.managed_units(app, env, components)
    if not components and not assume_yes:
        if not sys.stdin.isatty():
            raise StCliError(
                "Refusing to restart ALL components non-interactively; "
                "pass -c <component> or -y/--yes."
            )
        import questionary

        names = ", ".join(u.component for u in units)
        ui.warn(
            f"This will restart ALL components ({names}) of {app}/{env} "
            "across all their hosts."
        )
        if not questionary.confirm("Proceed?", default=False).ask():
            ui.warn("Aborted.")
            return
    # group hosts per component, preserving deploy_order (managed_units sorts by it) and
    # inventory order within each component.
    groups: dict = {}
    for comp, alias, ip in _iter_hosts(app, env, meta, units, components, host):
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
    """Run ``podman ps -a`` as each managed component's app user, per host, over ssh.

    Workers are skipped (they share the core's user+hosts). ``--component`` narrows
    to a subset of components, ``--host <alias>`` to one host. Read-only: a nonzero rc on one
    host warns and continues."""
    meta = appmeta.load_app(app)
    _m, units = manifest.managed_units(app, env, components)
    for comp, alias, ip in _iter_hosts(
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
    """Show a unit's systemd --user journal over ssh (journalctl --user -u <unit>)."""
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
    """Open an interactive one-off container shell (or run cmd) for a unit.

    ``entrypoint`` overrides the image entrypoint (e.g. ``sh`` for containers like
    collabora whose default entrypoint starts a server instead of a shell).
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
        if not sys.stdin.isatty():
            raise StCliError(
                f"Refusing to reset {t.app_name} non-interactively; pass -y/--yes."
            )
        import questionary

        ui.warn(
            f"This will STOP {t.app_name}, run 'podman-compose down -v' (removing "
            f"named volumes) and DELETE {t.remote_dir} on {t.host}, then redeploy.\n"
            "External Postgres/S3 are untouched; local container volumes are wiped."
        )
        answer = questionary.text(
            f"Type the unit name '{t.app_name}' to confirm:"
        ).ask()
        if answer != t.app_name:
            ui.warn("Aborted.")
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

    # redeploy this single component
    from ..core import generate

    ui.info(f"Redeploying {app}/{env}/{component} …")
    generate.generate_all(app, env)
    runner.galaxy_install()
    return runner.play(app, env, component)
