"""Tests for st_cli.core.recover — rebuilding bootstrap answers from a committed
unit."""

from __future__ import annotations

import pytest
from helpers import seed_creds, seed_livekit_provider, seed_meet_unit
from ruamel.yaml.comments import CommentedMap

from st_cli.core import appmeta, paths, recover, tree
from st_cli.core.manifest import save_manifest
from st_cli.core.models import StCliManifest, UnitState


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("TRUE", True),
        ("Yes", True),
        ("on", True),
        ("1", True),
        ("  true  ", True),
        ("false", False),
        ("FALSE", False),
        ("No", False),
        ("off", False),
        ("0", False),
        ("  false  ", False),
        ("maybe", None),
        ("", None),
        (None, None),
        (2, None),
    ],
)
def test_parse_bool_truth_table(value, expected):
    assert recover.parse_bool(value) is expected


def test_recover_nonexistent_unit_returns_empty(repo):
    assert recover.recover("meet", "prod", "meet") == {}


def test_recover_unknown_app_returns_empty(repo):
    # also exercises the appmeta.load_app(StCliError) guard directly
    assert recover.recover("not-an-app", "prod", "meet") == {}


def test_recover_parses_blob_verbatim_refs_and_equals(repo):
    seed_meet_unit(repo)
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_backend_env"] = (
        "DJANGO_SECRET_KEY={{ vault_django_secret_key }}\n"
        "DATABASE_URL={{ lookup('community.hashi_vault.hashi_vault', 'kv/data/db:url') }}\n"
        "DB_PASSWORD=postgres://user:pw@host/db?opt=1\n"
    )
    tree.save_vars("meet", "prod", "meet", data)

    answers = recover.recover("meet", "prod", "meet")
    assert answers["DJANGO_SECRET_KEY"] == "{{ vault_django_secret_key }}"
    assert (
        answers["DATABASE_URL"]
        == "{{ lookup('community.hashi_vault.hashi_vault', 'kv/data/db:url') }}"
    )
    assert answers["DB_PASSWORD"] == "postgres://user:pw@host/db?opt=1"


def test_recover_merges_multiple_layers(repo):
    """meet has TWO env_render layers (backend + caddy) — both blobs must merge."""
    seed_meet_unit(repo)
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_backend_env"] = "DJANGO_SETTINGS_MODULE=meet.settings\n"
    data["st_meet_caddy_env"] = "CADDY_DOMAIN=meet.example.org\n"
    tree.save_vars("meet", "prod", "meet", data)

    answers = recover.recover("meet", "prod", "meet")
    assert answers["DJANGO_SETTINGS_MODULE"] == "meet.settings"
    assert answers["CADDY_DOMAIN"] == "meet.example.org"


def test_recover_no_env_render_spec_contributes_nothing_from_blobs(repo):
    """Check that a provider with no env_render spec recovers only via component-var
    inversion."""
    seed_livekit_provider(repo)
    meta = appmeta.load_app("meet")
    assert meta.env_render_spec("livekit") == {}
    answers = recover.recover("meet", "prod", "livekit")
    # DOMAIN cannot be recovered here, but the call must not raise
    assert "DOMAIN" not in answers or isinstance(answers["DOMAIN"], str)


def test_recover_inversion_recovers_domain(repo):
    seed_meet_unit(repo)
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_public_host"] = "meet.example.org"
    tree.save_vars("meet", "prod", "meet", data)

    answers = recover.recover("meet", "prod", "meet")
    assert answers["DOMAIN"] == "meet.example.org"


def test_recover_returns_drive_legacy_s3_indirection_verbatim(repo):
    """drive has no st_drive_s3_* component var any more, so S3_PROTOCOL is
    never recovered. A pre-0.4.0 backend blob still carries the legacy
    indirection string verbatim in AWS_S3_ENDPOINT_URL/AWS_STORAGE_BUCKET_NAME
    — recover() must return it unchanged, per the module's rule to never
    resolve a recovered value. This pins the contract
    bootstrap._seed_drive_legacy_s3 relies on to migrate it."""
    seed_creds(repo)
    save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("drive", "prod", "drive", "managed")]
        )
    )
    data = tree.load_vars("drive", "prod", "drive")
    data["st_drive_public_host"] = "drive.example.org"
    data["st_drive_s3_protocol"] = "https"
    data["st_drive_s3_host"] = "s3.example.org"
    data["st_drive_s3_bucket"] = "drive-bucket"
    data["st_drive_backend_env"] = (
        "DJANGO_SETTINGS_MODULE=drive.settings\n"
        "AWS_S3_ENDPOINT_URL={{ st_drive_s3_protocol }}://{{ st_drive_s3_host }}\n"
        "AWS_STORAGE_BUCKET_NAME={{ st_drive_s3_bucket }}\n"
    )
    tree.save_vars("drive", "prod", "drive", data)

    answers = recover.recover("drive", "prod", "drive")
    assert answers["DOMAIN"] == "drive.example.org"
    assert "S3_PROTOCOL" not in answers
    assert (
        answers["AWS_S3_ENDPOINT_URL"]
        == "{{ st_drive_s3_protocol }}://{{ st_drive_s3_host }}"
    )
    assert answers["AWS_STORAGE_BUCKET_NAME"] == "{{ st_drive_s3_bucket }}"


def test_recover_skips_multi_placeholder_and_literal_templates(repo):
    """Check that a literal Jinja expression var is never treated as an invertible
    placeholder."""
    seed_meet_unit(repo)
    meta = appmeta.load_app("meet")
    tmpl = meta.component_vars("meet")["st_meet_backend_run_migrations"]
    assert not recover._PLACEHOLDER_RE.match(tmpl)

    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_backend_run_migrations"] = (
        "{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}"
    )
    tree.save_vars("meet", "prod", "meet", data)

    answers = recover.recover("meet", "prod", "meet")
    # no key should have been derived from the (skipped) literal template
    assert "inventory_hostname" not in answers
    assert not any("run_migrations" in k.lower() for k in answers)


def test_recover_blob_takes_precedence_over_inversion(repo):
    """Check that a value recoverable from both the blob and inversion takes the blob's
    value."""
    seed_meet_unit(repo)
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_public_host"] = "from-vars.example.org"
    # engineer a collision to exercise the precedence rule directly
    data["st_meet_backend_env"] = "DOMAIN=from-blob.example.org\n"
    tree.save_vars("meet", "prod", "meet", data)

    answers = recover.recover("meet", "prod", "meet")
    assert answers["DOMAIN"] == "from-blob.example.org"


def test_recover_dotenv_inversion_recovers_socks_proxy_trio(repo):
    """Check that the multi-placeholder dotenv inversion recovers all three socks-proxy
    lines."""
    seed_creds(repo)
    save_manifest(
        StCliManifest(
            "0.0.20",
            "0.0.20",
            [UnitState("messages", "prod", "socks-proxy", "managed")],
        )
    )
    data = tree.load_vars("messages", "prod", "socks-proxy")
    data["st_messages_socks_proxy_env"] = (
        "PROXY_EXTERNAL=eth1\n"
        "PROXY_INTERNAL_PORT=51000\n"
        "PROXY_USERS={{ vault_proxy_users }}\n"
    )
    tree.save_vars("messages", "prod", "socks-proxy", data)

    answers = recover.recover("messages", "prod", "socks-proxy")
    assert answers["PROXY_EXTERNAL"] == "eth1"
    assert answers["PROXY_INTERNAL_PORT"] == "51000"
    assert answers["PROXY_USERS"] == "{{ vault_proxy_users }}"


def test_recover_dotenv_inversion_skips_embedded_placeholder_lines(repo):
    """Check that a placeholder embedded inside a larger value is skipped, not
    inverted."""
    seed_creds(repo)
    save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("messages", "prod", "mta-in", "managed")]
        )
    )
    data = tree.load_vars("messages", "prod", "mta-in")
    data["st_messages_mta_in_env"] = (
        "MDA_API_SECRET={{ vault_mda_api_secret }}\n"
        "MDA_API_BASE_URL=https://messages.example.org/api/v1.0/\n"
        "MYHOSTNAME=mx.example.org\n"
    )
    tree.save_vars("messages", "prod", "mta-in", data)

    answers = recover.recover("messages", "prod", "mta-in")
    assert answers["MDA_API_SECRET"] == "{{ vault_mda_api_secret }}"
    assert answers["MYHOSTNAME"] == "mx.example.org"
    assert "DOMAIN" not in answers


def test_recover_dotenv_inversion_blob_takes_precedence(repo, monkeypatch):
    """Check that the blob-wins rule also holds for the dotenv-line inversion pass."""
    seed_creds(repo)
    save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("messages", "prod", "mta-in", "managed")]
        )
    )
    data = tree.load_vars("messages", "prod", "mta-in")
    data["st_messages_mta_in_env"] = (
        "MDA_API_SECRET={{ vault_mda_api_secret }}\n"
        "MDA_API_BASE_URL=https://messages.example.org/api/v1.0/\n"
        "MYHOSTNAME=from-dotenv.example.org\n"
    )
    data["st_fake_blob"] = "MYHOSTNAME=from-blob.example.org\n"
    tree.save_vars("messages", "prod", "mta-in", data)

    real_env_render_spec = appmeta.AppMeta.env_render_spec

    def fake_env_render_spec(self, component_key):
        if component_key == "mta-in":
            return {"fake": {"blob_var": "st_fake_blob"}}
        return real_env_render_spec(self, component_key)

    monkeypatch.setattr(appmeta.AppMeta, "env_render_spec", fake_env_render_spec)

    answers = recover.recover("messages", "prod", "mta-in")
    assert answers["MYHOSTNAME"] == "from-blob.example.org"


def test_recover_cadvisor_true(repo):
    seed_meet_unit(repo)
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_cadvisor_enabled"] = True
    tree.save_vars("meet", "prod", "meet", data)
    assert recover.recover_cadvisor("meet", "prod", "meet") is True


def test_recover_cadvisor_false(repo):
    seed_meet_unit(repo)
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_cadvisor_enabled"] = False
    tree.save_vars("meet", "prod", "meet", data)
    assert recover.recover_cadvisor("meet", "prod", "meet") is False


def test_recover_cadvisor_absent_returns_none(repo):
    seed_meet_unit(repo)
    assert recover.recover_cadvisor("meet", "prod", "meet") is None


def test_recover_cadvisor_missing_unit_returns_none(repo):
    assert recover.recover_cadvisor("meet", "prod", "meet") is None


def test_recover_cadvisor_string_roundtrip(repo):
    seed_meet_unit(repo)
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_cadvisor_enabled"] = "true"
    tree.save_vars("meet", "prod", "meet", data)
    assert recover.recover_cadvisor("meet", "prod", "meet") is True

    data["st_meet_cadvisor_enabled"] = "False"
    tree.save_vars("meet", "prod", "meet", data)
    assert recover.recover_cadvisor("meet", "prod", "meet") is False


def test_recover_hosts_present(repo):
    seed_meet_unit(repo)
    assert recover.recover_hosts("meet", "prod", "meet") == ["10.0.0.5"]


def test_recover_hosts_missing_returns_empty(repo):
    seed_creds(repo)
    assert recover.recover_hosts("meet", "prod", "meet") == []


def test_recover_oidc_proconnect_prod():
    answers = {
        "OIDC_OP_URL": "https://auth.agentconnect.gouv.fr/api/v2",
        "OIDC_OP_JWKS_ENDPOINT": "https://auth.agentconnect.gouv.fr/api/v2/jwks",
    }
    assert recover.recover_oidc(answers) == ("proconnect-prod", None, None)


def test_recover_oidc_proconnect_integ():
    answers = {
        "OIDC_OP_URL": "https://fca.integ01.dev-agentconnect.fr/api/v2",
    }
    assert recover.recover_oidc(answers) == ("proconnect-integ", None, None)


def test_recover_oidc_keycloak():
    answers = {
        "OIDC_OP_JWKS_ENDPOINT": (
            "https://sso.example.org/realms/myrealm/protocol/openid-connect/certs"
        ),
    }
    assert recover.recover_oidc(answers) == (
        "keycloak",
        "https://sso.example.org",
        "myrealm",
    )


def test_recover_oidc_custom():
    answers = {"OIDC_OP_URL": "https://idp.example.org"}
    assert recover.recover_oidc(answers) == ("custom", "https://idp.example.org", None)


def test_recover_oidc_custom_without_url():
    answers = {"OIDC_OP_AUTHORIZATION_ENDPOINT": "https://idp.example.org/auth"}
    assert recover.recover_oidc(answers) == ("custom", None, None)


def test_recover_oidc_none_when_no_oidc_answers():
    assert recover.recover_oidc({}) == (None, None, None)
    assert recover.recover_oidc({"DOMAIN": "x"}) == (None, None, None)


def test_recover_shared_returns_generated_secrets_and_plain_vars(repo):
    seed_livekit_provider(repo)
    meta = appmeta.load_app("meet")
    dep = next(d for d in meta.dependencies if d.of == "meet" and d.on == "livekit")

    out = recover.recover_shared("meet", "prod", "livekit", dep.shared)

    # generate: token/secret rules -> raw value lives ONLY in vault.yml
    assert out["st_meet_livekit_api_key"] == "real-token"
    assert out["st_meet_livekit_api_secret"] == "real-secret"
    # non-secret rules -> plain scalars directly in vars.yml
    assert out["st_meet_livekit_domain"] == "livekit.example.org"
    assert out["st_meet_livekit_turn_domain"] == "turn.example.org"


def test_recover_shared_omits_absent_vars(repo):
    seed_creds(repo)
    save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("meet", "prod", "livekit", "managed")]
        )
    )
    data = tree.load_vars("meet", "prod", "livekit")
    data["st_meet_livekit_domain"] = "livekit.example.org"
    tree.save_vars("meet", "prod", "livekit", data)
    tree.write_hosts("meet", "prod", "livekit", "livekit", ["10.0.0.1"])

    meta = appmeta.load_app("meet")
    dep = next(d for d in meta.dependencies if d.of == "meet" and d.on == "livekit")
    out = recover.recover_shared("meet", "prod", "livekit", dep.shared)

    assert out == {"st_meet_livekit_domain": "livekit.example.org"}
    assert "st_meet_livekit_api_key" not in out
    assert "st_meet_livekit_api_secret" not in out
    assert "st_meet_livekit_turn_domain" not in out


def test_recover_shared_nonexistent_unit_returns_empty(repo):
    meta = appmeta.load_app("meet")
    dep = next(d for d in meta.dependencies if d.of == "meet" and d.on == "livekit")
    assert recover.recover_shared("meet", "prod", "livekit", dep.shared) == {}


def test_recover_shared_vault_key_ref_recovered_from_vars_verbatim(repo):
    """Check that a vault_key rule's ref is recovered from vars.yml with no
    decryption."""
    seed_creds(repo)
    save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("messages", "prod", "mpa", "managed")]
        )
    )
    data = CommentedMap()
    data["st_messages_mpa_auth_bearer"] = "{{ vault_mpa_auth_bearer }}"
    tree.save_vars("messages", "prod", "mpa", data)

    meta = appmeta.load_app("messages")
    dep = next(d for d in meta.dependencies if d.of == "messages" and d.on == "mpa")
    out = recover.recover_shared("messages", "prod", "mpa", dep.shared)
    assert out["st_messages_mpa_auth_bearer"] == "{{ vault_mpa_auth_bearer }}"
    assert "st_messages_mpa_rspamd_controller_password" not in out


def test_recover_shared_survives_undecryptable_vault(repo, monkeypatch):
    """Check that an undecryptable vault.yml omits the affected keys instead of
    raising."""
    seed_livekit_provider(repo)
    # remove the vault password so vault.decrypt_to_dict raises StCliError
    (paths.repo_root() / ".vault-pass").unlink()

    meta = appmeta.load_app("meet")
    dep = next(d for d in meta.dependencies if d.of == "meet" and d.on == "livekit")
    out = recover.recover_shared("meet", "prod", "livekit", dep.shared)

    assert "st_meet_livekit_api_key" not in out
    # the plain vars.yml scalars are still recovered
    assert out["st_meet_livekit_domain"] == "livekit.example.org"


def test_recover_shared_empty_shared_list(repo):
    seed_meet_unit(repo)
    assert recover.recover_shared("meet", "prod", "meet", []) == {}
