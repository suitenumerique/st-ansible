"""Pre-connect guard: make sure an ssh user is resolvable before connecting.

Without one, ssh falls back to the local login user, which is rarely the
remote account. On a TTY it prompts once and persists; off a TTY it warns.
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
    """The `User` ssh would use for `host` per its config chain, or `None`.

    Uses `ssh -G` (offline, never connects). Any failure resolves to `None`.
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
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # normalise "User=x" to "User x" so both spellings match
        tokens = line.replace("=", " ", 1).split()
        if len(tokens) >= 2 and tokens[0].lower() == "user":
            return True
    return False


def _repo_config_sets_user() -> bool:
    """True if the repo ssh scaffold sets an explicit `User`.

    Unlike `ssh -G`, which reports the local user as ssh's default even when
    unset, this reads the files st-cli manages directly.
    """
    return _config_sets_user(paths.ssh_config_local_path()) or _config_sets_user(
        paths.ssh_config_path()
    )


def _persist_user(user: str) -> None:
    """Append a `Host *` `User` block to the gitignored `ssh/config.local`."""
    tree.ensure_ssh_scaffold()  # make sure the (commented) template exists
    p = paths.ssh_config_local_path()
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    p.write_text(f"{existing}\nHost *\n    User {user}\n", encoding="utf-8")


def ensure_ssh_user(hosts: list[str]) -> None:
    """Ensure an ssh user is configured before connecting to `hosts`.

    No-op once an explicit `User` is already configured. Otherwise prompts
    and persists on a TTY, or warns off one. Runs once per process.
    """
    global _checked
    if _checked:
        return
    if manifest.ssh_user():  # ST_CLI_SSH_USER already set, nothing to do
        _checked = True
        return
    if _repo_config_sets_user():  # explicit User in ssh/config.local (or ssh/config)
        _checked = True
        return

    try:
        local = getpass.getuser()
    except Exception:  # no passwd entry, treat any resolved user as configured
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
