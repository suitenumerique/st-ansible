"""ansible-vault helpers: vault-password bootstrap and whole-file encrypt/decrypt."""

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

    Writes a new password file at mode 0600. Raises if the file is missing and
    ``create`` is False.
    """
    pw_path = vault_password_path()
    if pw_path.exists():
        # Normalise on every pass, so a file copied in at 0644 is repaired
        # before the next read.
        pw_path.chmod(paths.SECRET_FILE_MODE)
        return pw_path
    if not create:
        raise StCliError(
            f"Vault password file {pw_path} not found. Run `st-cli bootstrap` first."
        )

    from . import ui
    from .secrets import gen_password

    pw = gen_password()

    pw_path.parent.mkdir(parents=True, exist_ok=True)
    # Create atomically at 0600: write_text then chmod(0600) leaves a TOCTOU
    # window where the password is briefly world-readable.
    fd = os.open(pw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, paths.SECRET_FILE_MODE)
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


def _run_vault(
    verb: str, path: Path, *, capture: bool = True
) -> subprocess.CompletedProcess:
    """Run ``ansible-vault <verb> <path>`` and raise on a non-zero exit.

    ``capture=False`` lets the child inherit the terminal, for a verb (``edit``)
    that must run ``$EDITOR`` interactively.
    """
    pw_file = ensure_vault_password(create=False)
    cmd = [
        ansible_bin("ansible-vault"),
        verb,
        "--vault-password-file",
        str(pw_file),
        str(path),
    ]
    opts = {"capture_output": True, "text": True} if capture else {}
    proc = subprocess.run(cmd, check=False, **opts)
    if proc.returncode != 0:
        stderr = proc.stderr.strip() if capture and proc.stderr else ""
        detail = f": {stderr}" if stderr else "."
        raise StCliError(f"ansible-vault {verb} failed{detail}")
    return proc


def encrypt_file(path: Path) -> None:
    """Encrypt a plaintext YAML file in place with ansible-vault."""
    if is_encrypted(path):
        return
    _run_vault("encrypt", path)


def decrypt_to_dict(path: Path) -> dict:
    """Decrypt an ansible-vault ``vault.yml`` and return it as a plain dict."""
    if not path.exists():
        return {}
    proc = _run_vault("view", path)
    from .tree import yaml

    return dict(yaml().load(proc.stdout) or {})


def edit_file(path: Path) -> None:
    """Open an encrypted ``vault.yml`` in ``$EDITOR`` via ``ansible-vault edit``.

    Does not capture output: ``ansible-vault edit`` must inherit the terminal
    so ``$EDITOR`` runs interactively.
    """
    if not path.exists():
        raise StCliError(f"No encrypted secrets file at {path}.")
    if not is_encrypted(path):
        raise StCliError(f"{path} is not ansible-vault encrypted.")
    _run_vault("edit", path, capture=False)
