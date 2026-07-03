"""ansible-vault helpers: vault-password bootstrap + inline ``encrypt_string``."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import paths
from .errors import StCliError
from .runner import ansible_bin

_DEFAULT_VAULT_PASS = ".vault-pass"


def vault_password_path() -> Path:
    """Resolve the ansible-vault password file to an absolute path.

    Always the repo-root default (``.vault-pass``).
    """
    return paths.repo_root() / _DEFAULT_VAULT_PASS


def ensure_vault_password(create: bool = False) -> Path:
    """Return the vault password file, generating it on first use.

    Generates a strong random password, writes the file ``chmod 600`` and prints
    a loud 'back this up' warning. Raises if the file is missing and ``create``
    is False.
    """
    pw_path = paths.repo_root() / _DEFAULT_VAULT_PASS
    if pw_path.exists():
        # Normalise to 0600 on every pass — not just at creation — so a file
        # copied in at 0644 (or seeded under a loose umask) is repaired before
        # the next encrypt/decrypt reads it. Mirrors tree.ensure_ssh_scaffold's
        # config.local chmod.
        pw_path.chmod(0o600)
        return pw_path
    if not create:
        raise StCliError(
            f"Vault password file {pw_path} not found. Run `st-cli bootstrap` first."
        )

    from . import ui
    from .secrets import gen_password

    pw = gen_password()

    pw_path.parent.mkdir(parents=True, exist_ok=True)
    # Create atomically at 0600: os.open with O_EXCL + an explicit mode avoids the
    # TOCTOU window of write_text (0644 under umask 022) → chmod(0600), where the
    # password would briefly be world-readable between the two calls. The 0o600
    # mode has no group/other bits, so umask cannot relax it.
    fd = os.open(pw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(pw + "\n")

    ui.console.print(
        "\n[yellow]st-cli generated a random ansible-vault password.[/yellow]\n"
        "[bold red on yellow] ⚠  BACK UP YOUR VAULT PASSWORD  ⚠ [/bold red on yellow]\n"
        "[yellow]Stored at[/yellow] [bold]./.vault-pass[/bold] [yellow](gitignored).[/yellow]\n"
        "[yellow]Every operator of this repo needs this exact file — share it securely[/yellow]\n"
        "[yellow](password manager / secrets tool), never commit it.[/yellow]\n"
        "[bold yellow]If you lose it, every encrypted secret in this repo is unrecoverable.[/bold yellow]\n"
    )
    return pw_path


def is_encrypted(path: Path) -> bool:
    """True if ``path`` is an ansible-vault encrypted file."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return fh.readline().startswith("$ANSIBLE_VAULT")
    except OSError:
        return False


def encrypt_file(path: Path) -> None:
    """Encrypt a plaintext YAML file in place with ansible-vault."""
    if is_encrypted(path):
        return
    pw_file = ensure_vault_password(create=False)
    proc = subprocess.run(
        [
            ansible_bin("ansible-vault"),
            "encrypt",
            "--vault-password-file",
            str(pw_file),
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise StCliError(f"ansible-vault encrypt failed: {proc.stderr.strip()}")


def decrypt_to_dict(path: Path) -> dict:
    """Decrypt an ansible-vault ``vault.yml`` and return it as a plain dict."""
    if not path.exists():
        return {}
    pw_file = ensure_vault_password(create=False)
    proc = subprocess.run(
        [
            ansible_bin("ansible-vault"),
            "view",
            "--vault-password-file",
            str(pw_file),
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise StCliError(f"ansible-vault view failed: {proc.stderr.strip()}")
    from .tree import yaml

    return dict(yaml().load(proc.stdout) or {})


def edit_file(path: Path) -> None:
    """Open an encrypted ``vault.yml`` in ``$EDITOR`` via ``ansible-vault edit``.

    Unlike the other wrappers, this does NOT capture output: ``ansible-vault
    edit`` must inherit the terminal so ``$EDITOR`` runs interactively. The
    process inherits ``os.environ`` by default, so ``$EDITOR`` is visible.
    """
    if not path.exists():
        raise StCliError(f"No encrypted secrets file at {path}.")
    if not is_encrypted(path):
        raise StCliError(f"{path} is not ansible-vault encrypted.")
    pw_file = ensure_vault_password(create=False)
    proc = subprocess.run(
        [
            ansible_bin("ansible-vault"),
            "edit",
            "--vault-password-file",
            str(pw_file),
            str(path),
        ],
    )
    if proc.returncode != 0:
        raise StCliError("ansible-vault edit failed.")
