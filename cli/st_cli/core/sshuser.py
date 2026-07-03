"""Pre-connect guard: make sure an ssh user is resolvable before we connect.

The remote ssh user is per-operator and lives outside the committed tree
(``ST_CLI_SSH_USER`` env var, or a ``User`` in the gitignored ``ssh/config.local``
/ ``~/.ssh/config`` chain — see :func:`st_cli.core.manifest.ssh_user`). When
neither is set, ssh silently falls back to the *local* login user, which is
almost never the intended remote account. This guard catches that case before
``deploy`` or the direct-ssh ops connect:

* on a TTY — prompt once for the user, persist it to ``ssh/config.local`` and
  apply it to the current run (so both ansible and st-cli's own ssh use it);
* off a TTY (CI/cron) — emit a one-line warning to set ``ST_CLI_SSH_USER``.

It never blocks a non-interactive run: the warning is advisory and the command
proceeds (connecting as the local user, exactly as before this guard existed).
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys

from . import manifest, paths, tree, ui
from .prompts import _ask

# Resolve the guard at most once per process: after the first prompt/warn nothing
# changes that a second check would catch (a TTY prompt sets ST_CLI_SSH_USER for
# the rest of the run; a non-TTY warning would only repeat). Keeps loops (e.g.
# `restart` over many hosts) from re-prompting or spamming the warning.
_checked = False


def _resolved_ssh_user(host: str) -> str | None:
    """The ``User`` ssh would use for ``host`` per its config chain, or ``None``.

    Uses ``ssh -G`` (offline config resolution — never connects) so it reflects
    every source ssh itself honours: the container's ``/etc/ssh`` Include of
    ``ssh/config.local``, a native ``~/.ssh/config``, etc. Any failure (ssh
    missing, timeout) resolves to ``None``.
    """
    try:
        out = subprocess.run(
            ["ssh", "-G", host],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line[:5].lower() == "user ":
            return line[5:].strip()
    return None


def _config_sets_user(path) -> bool:
    """True if the ssh config file at ``path`` has an active (uncommented) ``User``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # normalise "User=x" → "User x" so both spellings match
        tokens = line.replace("=", " ", 1).split()
        if len(tokens) >= 2 and tokens[0].lower() == "user":
            return True
    return False


def _repo_config_sets_user() -> bool:
    """True if the repo ssh scaffold sets an explicit ``User``.

    Scans the gitignored per-operator ``ssh/config.local`` and the committed
    ``ssh/config`` for an active ``User`` directive. Unlike the ``ssh -G`` probe
    (which reports the local login user as ssh's *default* when nothing is set, so
    it cannot tell "configured as <me>" from "defaulted to <me>"), this reads the
    files st-cli manages directly — so a ``User`` equal to the local username is
    still recognised as configured. It also covers native runs, where ssh's own
    config chain does not Include these files (only the container does).
    """
    return _config_sets_user(paths.ssh_config_local_path()) or _config_sets_user(
        paths.ssh_config_path()
    )


def _persist_user(user: str) -> None:
    """Append a ``Host *`` ``User`` block to the gitignored ``ssh/config.local``."""
    tree.ensure_ssh_scaffold()  # make sure the (commented) template exists
    p = paths.ssh_config_local_path()
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    p.write_text(f"{existing}\nHost *\n    User {user}\n", encoding="utf-8")


def ensure_ssh_user(hosts: list[str]) -> None:
    """Ensure an ssh user is configured before connecting to ``hosts``.

    No-op when ``ST_CLI_SSH_USER`` is set, when the repo ssh scaffold
    (``ssh/config.local`` / ``ssh/config``) sets an explicit ``User``, or when the
    ambient ssh config chain resolves a non-local ``User`` for the first host.
    Otherwise: prompt + persist + apply on a TTY, or warn to set ``ST_CLI_SSH_USER``
    off a TTY. Runs at most once per process.
    """
    global _checked
    if _checked:
        return
    if manifest.ssh_user():  # ST_CLI_SSH_USER already set — nothing to do
        _checked = True
        return
    if _repo_config_sets_user():  # explicit User in ssh/config.local (or ssh/config)
        _checked = True
        return

    try:
        local = getpass.getuser()
    except Exception:  # no passwd entry — treat any resolved user as configured
        local = None
    resolved = _resolved_ssh_user(hosts[0]) if hosts else None
    if resolved and resolved != local:
        _checked = True  # an explicit User is configured via the ssh config chain
        return

    _checked = True
    if sys.stdin.isatty():
        user = _ask(
            "No ssh user is configured for the target servers — enter the remote "
            "ssh user",
            placeholder="debian",
        )
        _persist_user(user)
        os.environ["ST_CLI_SSH_USER"] = user  # apply to this run (ansible + ssh)
        ui.success(
            f"Saved `User {user}` to ssh/config.local (gitignored) and using it "
            "for this run."
        )
    else:
        ui.warn(
            "No ssh user configured (ST_CLI_SSH_USER unset and no User in your ssh "
            f"config) — ssh will connect as '{local}'. Set ST_CLI_SSH_USER=<user> "
            "for non-interactive/CI runs."
        )
