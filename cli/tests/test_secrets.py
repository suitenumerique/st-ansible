"""Tests for st_cli.cmd.secrets — the `st-cli secrets APP ENV` command.

Covers the backend guard (hashi_vault refused), the no-editable-components
errors, single vs multi component selection, the `-c` narrow path, the
missing-`.vault-pass` propagation, and the interactive (no capture_output)
subprocess requirement.
"""

from __future__ import annotations

import pytest

from st_cli.cmd import secrets as secrets_mod
from st_cli.core import manifest, paths, vault
from st_cli.core.errors import StCliError
from st_cli.core.models import SecretConfig, StCliManifest, UnitState

from helpers import seed_creds, script_questionary

_VAULT_HEADER = "$ANSIBLE_VAULT;1.1;AES256\nfake-encrypted-body\n"


def _seed_manifest(repo, units, *, backend="ansible-vault", app="meet", env="prod"):
    """Write .st-cli.yml with the given units + secret backend for (app, env)."""
    sc = (
        [SecretConfig(app=app, env=env, backend=backend)]
        if backend != "ansible-vault"
        else []
    )
    manifest.save_manifest(StCliManifest("0.0.20", "0.0.20", units, secrets=sc))


def _seed_vault_file(app, env, component):
    """Create a vault.yml that passes vault.is_encrypted (fake header)."""
    p = paths.vault_path(app, env, component)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_VAULT_HEADER, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- backend guard


def test_hashi_vault_backend_refused(repo, mocker):
    """A (app, env) on the hashi_vault backend → StCliError pointing to OpenBao."""
    seed_creds(repo)
    _seed_manifest(
        repo,
        [UnitState("meet", "prod", "meet", "managed")],
        backend="hashi_vault",
    )
    edit_spy = mocker.patch.object(vault, "edit_file")

    with pytest.raises(StCliError, match="hashi_vault"):
        secrets_mod.edit_secrets("meet", "prod", None)

    edit_spy.assert_not_called()


# --------------------------------------------------------------------------- no editable components


def test_no_editable_components(repo, mocker):
    """Units exist but none has a vault.yml → StCliError (no prompt, no edit)."""
    seed_creds(repo)
    _seed_manifest(repo, [UnitState("meet", "prod", "meet", "managed")])
    edit_spy = mocker.patch.object(vault, "edit_file")

    with pytest.raises(StCliError, match="No editable secrets for meet/prod"):
        secrets_mod.edit_secrets("meet", "prod", None)

    edit_spy.assert_not_called()


# --------------------------------------------------------------------------- single component


def test_single_component_edits_without_prompt(repo, mocker):
    """One component with a vault.yml → edit_file called directly, no _ask_select."""
    seed_creds(repo)
    _seed_manifest(repo, [UnitState("meet", "prod", "meet", "managed")])
    _seed_vault_file("meet", "prod", "meet")
    edit_spy = mocker.patch.object(vault, "edit_file")

    secrets_mod.edit_secrets("meet", "prod", None)

    expected = paths.vault_path("meet", "prod", "meet")
    edit_spy.assert_called_once_with(expected)


# --------------------------------------------------------------------------- multiple components


def test_multiple_components_prompts_and_edits_chosen(repo, mocker, monkeypatch):
    """Several components each with a vault.yml → _ask_select is offered the
    right keys; the chosen component's vault path is the one edited."""
    seed_creds(repo)
    _seed_manifest(
        repo,
        [
            UnitState("messages", "prod", "messages", "managed"),
            UnitState("messages", "prod", "mpa", "managed"),
        ],
        app="messages",
    )
    _seed_vault_file("messages", "prod", "messages")
    _seed_vault_file("messages", "prod", "mpa")

    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Which component's secrets?", "mpa"),
        ],
    )
    edit_spy = mocker.patch.object(vault, "edit_file")

    secrets_mod.edit_secrets("messages", "prod", None)

    # the select was offered both component keys
    assert sq.select_calls, "expected a component select prompt"
    msg, choices = sq.select_calls[0]
    assert "Which component's secrets?" in msg
    assert "messages" in choices
    assert "mpa" in choices

    # the chosen component's vault path was edited
    edit_spy.assert_called_once_with(paths.vault_path("messages", "prod", "mpa"))


# --------------------------------------------------------------------------- -c with no vault.yml


def test_component_flag_no_vault_yml(repo, mocker):
    """`-c <component>` where that component has no vault.yml → specific StCliError."""
    seed_creds(repo)
    _seed_manifest(repo, [UnitState("meet", "prod", "meet", "managed")])
    # no vault.yml created for meet
    edit_spy = mocker.patch.object(vault, "edit_file")

    with pytest.raises(StCliError, match=r"No encrypted secrets for meet/prod -c meet"):
        secrets_mod.edit_secrets("meet", "prod", "meet")

    edit_spy.assert_not_called()


def test_component_flag_edits_existing_vault(repo, mocker):
    """`-c <component>` where that component HAS a vault.yml → edit_file called
    directly for it (no prompt). Regression guard: units_for now takes a list, so a
    single -c must be wrapped — char-iterating the bare string would miss the unit
    and wrongly report 'No encrypted secrets'."""
    seed_creds(repo)
    _seed_manifest(repo, [UnitState("meet", "prod", "meet", "managed")])
    _seed_vault_file("meet", "prod", "meet")
    edit_spy = mocker.patch.object(vault, "edit_file")

    secrets_mod.edit_secrets("meet", "prod", "meet")

    edit_spy.assert_called_once_with(paths.vault_path("meet", "prod", "meet"))


# --------------------------------------------------------------------------- missing .vault-pass


def test_missing_vault_pass_propagates(repo, mocker):
    """Without .vault-pass, edit_file→ensure_vault_password(create=False) raises;
    edit_secrets propagates that StCliError (no swallow, no subprocess)."""
    # NOTE: deliberately NOT calling seed_creds — no .vault-pass
    _seed_manifest(repo, [UnitState("meet", "prod", "meet", "managed")])
    _seed_vault_file("meet", "prod", "meet")  # is_encrypted → True (header)

    with pytest.raises(StCliError, match="Vault password file"):
        secrets_mod.edit_secrets("meet", "prod", None)


# --------------------------------------------------------------------------- interactive (no capture_output)


def test_edit_subprocess_has_no_capture_output(repo, mocker):
    """ansible-vault edit must inherit the terminal for $EDITOR — verify
    subprocess.run is called WITHOUT capture_output / text= kwargs."""
    seed_creds(repo)
    _seed_manifest(repo, [UnitState("meet", "prod", "meet", "managed")])
    _seed_vault_file("meet", "prod", "meet")  # passes is_encrypted

    run_spy = mocker.patch.object(
        vault.subprocess, "run", return_value=mocker.MagicMock(returncode=0)
    )
    mocker.patch.object(vault, "ansible_bin", return_value="/fake/ansible-vault")

    secrets_mod.edit_secrets("meet", "prod", None)

    run_spy.assert_called_once()
    _, kwargs = run_spy.call_args
    assert "capture_output" not in kwargs
    assert "text" not in kwargs


# --------------------------------------------------------------------------- generated .vault-pass


def test_ensure_vault_password_generates_file(repo):
    """With no .vault-pass, ensure_vault_password(create=True) generates a strong
    random password, writes it chmod 0600, and is idempotent (a second call
    returns the same contents — no regeneration)."""
    pw_path = paths.repo_root() / ".vault-pass"
    assert not pw_path.exists()

    vault.ensure_vault_password(create=True)

    assert pw_path.exists()
    contents = pw_path.read_text(encoding="utf-8")
    assert contents.strip() != ""
    assert pw_path.stat().st_mode & 0o777 == 0o600

    # second call is idempotent — same contents, no regeneration
    vault.ensure_vault_password(create=True)
    assert pw_path.read_text(encoding="utf-8") == contents
