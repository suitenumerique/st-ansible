"""Filesystem path helpers anchored at the deployment repo root (CWD)."""

from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    """Root of the deployment repo (the process working directory)."""
    return Path.cwd()


def st_cli_dir() -> Path:
    """Directory holding generated/trashable scaffolding (``.st-cli/``)."""
    return repo_root() / ".st-cli"


def playbooks_dir() -> Path:
    """Where generated per-component playbooks live."""
    return st_cli_dir() / "playbooks"


def collections_dir() -> Path:
    """ansible-galaxy collection install target."""
    return st_cli_dir() / "collections"


def manifest_path() -> Path:
    """Committed manifest path."""
    return repo_root() / ".st-cli.yml"


def unit_dir(app: str, env: str, component: str) -> Path:
    """Path to ``<app>/<env>/<component>`` in the committed config tree."""
    return repo_root() / app / env / component


def vars_path(app: str, env: str, component: str) -> Path:
    """Path to a unit's ``vars.yml`` (committed, plaintext)."""
    return unit_dir(app, env, component) / "vars.yml"


def hosts_path(app: str, env: str, component: str) -> Path:
    """Path to a unit's INI ``hosts`` inventory (committed)."""
    return unit_dir(app, env, component) / "hosts"


def vault_path(app: str, env: str, component: str) -> Path:
    """Path to a unit's encrypted ``vault.yml`` (committed, ansible-vault)."""
    return unit_dir(app, env, component) / "vault.yml"


def common_path(app: str, env: str) -> Path:
    """Path to the app/env-wide ``<app>/<env>/common.yml`` (committed, plaintext)."""
    return repo_root() / app / env / "common.yml"


def ssh_dir() -> Path:
    """Directory holding the committed shared SSH client config (``ssh/``)."""
    return repo_root() / "ssh"


def ssh_config_path() -> Path:
    """Committed shared SSH client config (``ssh/config``)."""
    return ssh_dir() / "config"


def ssh_config_local_path() -> Path:
    """Gitignored per-operator SSH client config (``ssh/config.local``).

    Included first by the container (see ``Dockerfile``) so operator overrides win
    over the shared committed ``ssh/config``. Missing file → silent no-op.
    """
    return ssh_dir() / "config.local"


def ssh_known_hosts_path() -> Path:
    """Committed pinned host keys (``ssh/known_hosts``)."""
    return ssh_dir() / "known_hosts"
