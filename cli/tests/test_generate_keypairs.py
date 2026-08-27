"""Tests for st_cli.cmd.generate_keypairs — the caller-keypair questionnaire.

The summary is asserted by capturing the ui calls (note body + raw values)
rather than parsing rendered rich output: long single-word keys get folded by
the terminal renderer, which would make text-matching brittle.
"""

from __future__ import annotations

import base64
import re

import pytest
from helpers import script_questionary

from st_cli.cmd import generate_keypairs
from st_cli.core import keypairs, tree
from st_cli.core.errors import StCliError


def _capture_ui(monkeypatch):
    """Capture ui.note bodies, ui.value lines and ui.warn lines."""
    notes: list[str] = []
    values: list[str] = []
    warns: list[str] = []
    monkeypatch.setattr(
        generate_keypairs.ui, "note", lambda body, title="Note": notes.append(body)
    )
    monkeypatch.setattr(generate_keypairs.ui, "value", values.append)
    monkeypatch.setattr(generate_keypairs.ui, "warn", warns.append)
    return notes, values, warns


def test_generate_single_keypair_summary(repo, monkeypatch):
    """Default flow: one keypair for issuer `transferts`, no bootstrapped unit →
    the guidance points at the bootstrap questionnaire and the printed halves
    form a consistent Ed25519 pair."""
    notes, values, _ = _capture_ui(monkeypatch)
    script_questionary(
        monkeypatch,
        [
            ("text", "Issuer name", "transferts"),
            ("confirm", "another caller keypair", False),
        ],
    )
    generate_keypairs.generate("file-scanner", "prod")

    guidance = notes[0]
    assert "JWT_ISSUER_KEYS" in guidance
    assert "st-cli bootstrap file-scanner prod" in guidance
    assert "SCAN_JWT_PRIVATE_KEY" in guidance
    assert "no copy" in guidance
    # how to append to an already-set value, and the purely-local promise
    assert "append `,iss:pubkey`" in guidance
    assert "nothing is written" in guidance

    merged, private = values
    pub = re.fullmatch(r"transferts:([A-Za-z0-9_-]{43})", merged).group(1)
    seed = base64.urlsafe_b64decode(private + "=")
    assert keypairs.derive_public_key(seed) == base64.urlsafe_b64decode(pub + "=")


def test_generate_multiple_and_invalid_issuer_reprompts(repo, monkeypatch):
    """A ':'/',' issuer name or a duplicate warns + re-prompts without minting;
    the loop then mints two keypairs and joins both fragments with a comma."""
    _, values, warns = _capture_ui(monkeypatch)
    script_questionary(
        monkeypatch,
        [
            ("text", "Issuer name", "bad:name"),
            ("text", "Issuer name", "transferts"),
            ("confirm", "another caller keypair", True),
            ("text", "Issuer name", "transferts"),  # duplicate → re-prompt
            ("text", "Issuer name", "drive"),
            ("confirm", "another caller keypair", False),
        ],
    )
    generate_keypairs.generate("file-scanner", "prod")

    assert len(warns) == 2  # invalid name + duplicate
    merged = values[0]
    assert re.fullmatch(r"transferts:[A-Za-z0-9_-]{43},drive:[A-Za-z0-9_-]{43}", merged)
    assert len(values) == 3  # merged + one private key per issuer


def test_generate_merges_existing_issuer_keys(repo, monkeypatch):
    """With a bootstrapped file-scanner unit whose env blob already carries
    JWT_ISSUER_KEYS, the new fragment is appended after the existing value and
    the guidance points at vars.yml + deploy instead of bootstrap."""
    data = tree.load_vars("file-scanner", "prod", "file-scanner")
    data["st_file_scanner_env"] = "JWT_ISSUER_KEYS=old:oldpubkey\nJWT_SIGNING_KID=v1\n"
    tree.save_vars("file-scanner", "prod", "file-scanner", data)
    notes, values, _ = _capture_ui(monkeypatch)
    script_questionary(
        monkeypatch,
        [
            ("text", "Issuer name", "drive"),
            ("confirm", "another caller keypair", False),
        ],
    )
    generate_keypairs.generate("file-scanner", "prod")

    assert re.fullmatch(r"old:oldpubkey,drive:[A-Za-z0-9_-]{43}", values[0])
    guidance = notes[0]
    assert "file-scanner/prod/file-scanner/vars.yml" in guidance
    assert "st-cli deploy file-scanner prod" in guidance
    assert "st-cli bootstrap" not in guidance


def test_generate_rejects_other_apps(repo, monkeypatch):
    """Only file-scanner authenticates callers by keypair; other apps raise a
    clean StCliError (unknown apps too, via load_app)."""
    with pytest.raises(StCliError, match="only file-scanner"):
        generate_keypairs.generate("drive", "prod")
    with pytest.raises(StCliError, match="unknown app"):
        generate_keypairs.generate("bogus", "prod")
