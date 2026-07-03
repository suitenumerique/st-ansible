"""Tests for st_cli.core.vault — ansible-vault wrappers + vault-password path resolver."""

from __future__ import annotations

import stat

from st_cli.core import paths, tree, vault


# --------------------------------------------------------------------------- whole-file encrypt/decrypt


def test_vault_file_roundtrip(repo):
    (repo / ".vault-pass").write_text("testpass\n")
    p = paths.vault_path("meet", "prod", "meet")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        tree.yaml().dump(
            {"vault_django_secret_key": "s3cr3t", "st_meet_livekit_api_secret": "abc"},
            fh,
        )
    vault.encrypt_file(p)

    assert vault.is_encrypted(p)
    assert p.read_text().startswith("$ANSIBLE_VAULT")
    back = vault.decrypt_to_dict(p)
    assert back["vault_django_secret_key"] == "s3cr3t"
    assert back["st_meet_livekit_api_secret"] == "abc"


# --------------------------------------------------------------------------- vault-password path resolver


def test_vault_password_path_default_is_repo_root(repo):
    """The resolver always returns the repo-root ``.vault-pass``."""
    assert vault.vault_password_path() == (repo / ".vault-pass")


# --------------------------------------------------------------------------- .vault-pass permissions


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_ensure_vault_password_generated_file_is_0600(repo):
    """A freshly generated ``.vault-pass`` is created at 0600 (never 0644 under a
    loose umask) — the password must not be world-readable, even transiently."""
    pw_path = paths.repo_root() / ".vault-pass"
    assert not pw_path.exists()

    vault.ensure_vault_password(create=True)

    assert pw_path.exists()
    assert _mode(pw_path) == 0o600


def test_ensure_vault_password_normalises_loose_perms(repo):
    """An existing ``.vault-pass`` copied in at 0644 is repaired to 0600 on every
    ensure_vault_password call (mirrors tree.ensure_ssh_scaffold's config.local
    chmod), so a loose file never survives into the next encrypt/decrypt run."""
    pw_path = paths.repo_root() / ".vault-pass"
    pw_path.write_text("testpass\n", encoding="utf-8")
    pw_path.chmod(0o644)  # simulate a file copied in under a loose umask
    assert _mode(pw_path) == 0o644

    vault.ensure_vault_password(create=False)

    assert _mode(pw_path) == 0o600
    assert pw_path.read_text(encoding="utf-8") == "testpass\n"  # content preserved
