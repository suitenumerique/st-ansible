"""Tests for st_cli.core.manifest — .st-cli.yml I/O + unit selection."""

from __future__ import annotations

import pytest

from st_cli.core import manifest, tree
from st_cli.core.errors import StCliError
from st_cli.core.models import SecretConfig, StCliManifest, UnitState

from helpers import seed_creds


# --------------------------------------------------------------------------- units


def test_manifest_roundtrip_no_hosts(repo):
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    loaded = manifest.load_manifest()
    assert loaded.units[0].component == "meet"
    # upsert replaces by (app,env,component)
    manifest.upsert_unit(loaded, UnitState("meet", "prod", "meet", "external"))
    assert len(loaded.units) == 1 and loaded.units[0].mode == "external"
    manifest.upsert_unit(loaded, UnitState("meet", "prod", "livekit", "managed"))
    assert len(loaded.units) == 2
    assert (
        "hosts" not in (repo / ".st-cli.yml").read_text()
    )  # hosts live only in the ini


def test_workers_deploy_order_after_core(repo):
    """managed_units returns workers AFTER the core component (deploy_order sorted)."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                # intentionally listed out of deploy order
                UnitState("drive", "prod", "workers", "managed"),
                UnitState("drive", "prod", "drive", "managed"),
            ],
        )
    )
    tree.save_vars("drive", "prod", "drive", tree.load_vars("drive", "prod", "drive"))
    tree.write_hosts("drive", "prod", "drive", "drive", ["10.0.0.1"])

    _, units = manifest.managed_units("drive", "prod", None)
    keys = [u.component for u in units]
    assert keys.index("drive") < keys.index("workers")


def test_managed_units_multiple_components_sorted_by_deploy_order(repo):
    """managed_units(app, env, [c1, c2]) returns both, sorted by deploy_order
    (regardless of request order), and de-duplicates repeated entries."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("drive", "prod", "collabora", "managed"),
                UnitState("drive", "prod", "drive", "managed"),
            ],
        )
    )
    tree.save_vars("drive", "prod", "drive", tree.load_vars("drive", "prod", "drive"))

    # requested out of deploy order — returned sorted (collabora=10 < drive=20)
    _, units = manifest.managed_units("drive", "prod", ["drive", "collabora"])
    assert [u.component for u in units] == ["collabora", "drive"]

    # de-dupe: repeating a component yields it exactly once
    _, units2 = manifest.managed_units("drive", "prod", ["drive", "drive"])
    assert [u.component for u in units2] == ["drive"]


def test_managed_units_unknown_component_raises_naming_it(repo):
    """managed_units(app, env, ["<valid>", "bogus"]) raises StCliError whose
    message names the offender and matches 'No managed unit(s)'."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("drive", "prod", "drive", "managed")]
        )
    )
    tree.save_vars("drive", "prod", "drive", tree.load_vars("drive", "prod", "drive"))

    with pytest.raises(StCliError, match=r"No managed unit\(s\) for drive/prod: bogus"):
        manifest.managed_units("drive", "prod", ["drive", "bogus"])


def test_managed_units_unknown_component_in_unit_raises_stclierror(repo):
    """A unit referencing a component not in the bundled app manifest raises
    StCliError (not a bare KeyError) from the deploy-order sort — the clean
    message reaches the user via main._run."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [UnitState("meet", "prod", "bogus", "managed")],
        )
    )
    with pytest.raises(StCliError, match=r"unknown component 'bogus' for app 'meet'"):
        manifest.managed_units("meet", "prod", None)


# --------------------------------------------------------------------------- malformed manifest


def test_load_manifest_malformed_missing_unit_key_raises_stclierror(repo):
    """A hand-edited .st-cli.yml missing a required unit key raises StCliError
    (not a bare KeyError) so main._run surfaces a clean message."""
    seed_creds(repo)
    (repo / ".st-cli.yml").write_text(
        "versions:\n"
        "  collection: '0.0.20'\n"
        "  cli: '0.0.20'\n"
        "units:\n"
        "  - app: meet\n"
        "    env: prod\n"
        "    mode: managed\n"
        "# missing 'component' key above\n"
    )
    with pytest.raises(StCliError, match=r"\.st-cli\.yml is malformed \(missing key"):
        manifest.load_manifest()


def test_load_manifest_malformed_missing_secret_key_raises_stclierror(repo):
    """A secrets entry missing a required key raises StCliError (missing key)."""
    seed_creds(repo)
    (repo / ".st-cli.yml").write_text(
        "versions:\n"
        "  collection: '0.0.20'\n"
        "  cli: '0.0.20'\n"
        "units: []\n"
        "secrets:\n"
        "  - app: meet\n"
        "    # missing 'env' key\n"
        "    backend: ansible-vault\n"
    )
    with pytest.raises(StCliError, match=r"\.st-cli\.yml is malformed \(missing key"):
        manifest.load_manifest()


# --------------------------------------------------------------------------- ssh_user resolution


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("deployer", "deployer"),  # non-empty → that value
        (None, None),  # unset → None (defer to the ssh config chain)
        ("", None),  # empty ST_CLI_SSH_USER= must NOT yield an empty user
    ],
)
def test_ssh_user_from_env(monkeypatch, env_value, expected):
    """ST_CLI_SSH_USER resolves the ssh user; unset or empty → None."""
    if env_value is None:
        monkeypatch.delenv("ST_CLI_SSH_USER", raising=False)
    else:
        monkeypatch.setenv("ST_CLI_SSH_USER", env_value)
    assert manifest.ssh_user() == expected


# --------------------------------------------------------------------------- secret config


def test_manifest_roundtrips_secret_config(repo):
    """.st-cli.yml round-trips the per-(app,env) secrets: list; no block ⇒ ansible-vault."""
    seed_creds(repo)
    m = StCliManifest(
        "0.0.20",
        "0.0.20",
        [UnitState("meet", "prod", "meet", "managed")],
        secrets=[SecretConfig("meet", "prod", "hashi_vault")],
    )
    manifest.save_manifest(m)
    assert "secrets" in (repo / ".st-cli.yml").read_text()

    loaded = manifest.load_manifest()
    assert len(loaded.secrets) == 1
    assert loaded.secrets[0].app == "meet"
    assert loaded.secrets[0].env == "prod"
    assert loaded.secrets[0].backend == "hashi_vault"
    assert manifest.secret_config_for(loaded, "meet", "prod").backend == "hashi_vault"

    # no secrets: block ⇒ ansible-vault default (a hand-written manifest may omit it)
    m2 = StCliManifest("0.0.20", "0.0.20", [])
    manifest.save_manifest(m2)
    raw = (repo / ".st-cli.yml").read_text()
    assert "secrets" not in raw  # block omitted when empty (clean diff)
    loaded2 = manifest.load_manifest()
    assert loaded2.secrets == []
    assert (
        manifest.secret_config_for(loaded2, "meet", "prod").backend == "ansible-vault"
    )

    # upsert_secret replaces by (app, env) — not append
    manifest.upsert_secret(loaded, SecretConfig("meet", "prod", "ansible-vault"))
    assert len(loaded.secrets) == 1 and loaded.secrets[0].backend == "ansible-vault"
    manifest.upsert_secret(loaded, SecretConfig("drive", "prod", "hashi_vault"))
    assert len(loaded.secrets) == 2  # new (app,env) appended
