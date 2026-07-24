"""Tests for st_cli.core.writer — pure writers for the committed config tree."""

from __future__ import annotations

import stat

from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import LiteralScalarString

from st_cli.core import appmeta, paths, writer
from st_cli.core.secretbackend import (
    AnsibleVaultBackend,
    HashiVaultBackend,
    SecretBackend,
    hashi_lookup_ref,
)


def test_drive_public_host_from_domain():
    meta = appmeta.load_app("drive")
    assert meta.component_vars("drive")["st_drive_public_host"] == "{DOMAIN}"
    data = CommentedMap()
    writer.apply_component_vars(
        data, meta, meta.core(), {"DOMAIN": "drive.example.org"}
    )
    assert data["st_drive_public_host"] == "drive.example.org"


def test_meet_public_host_from_domain():
    """Mirrors test_drive_public_host_from_domain: the meet core exposes
    st_meet_public_host (=="{DOMAIN}") as the single source of truth for the
    public meet domain, so every public-facing env var references it verbatim
    (the role derives DJANGO_ALLOWED_HOSTS, redirects, the LiveKit recording
    webhook URL and the recordings download base from it). apply_component_vars
    renders {DOMAIN} → the literal DOMAIN answer (written into the core vars.yml),
    and the livekit component reuses the SAME var (see
    test_meet_and_livekit_component_vars_carry_public_host)."""
    meta = appmeta.load_app("meet")
    assert meta.component_vars("meet")["st_meet_public_host"] == "{DOMAIN}"
    data = CommentedMap()
    writer.apply_component_vars(data, meta, meta.core(), {"DOMAIN": "meet.example.org"})
    assert data["st_meet_public_host"] == "meet.example.org"


def test_drive_s3_component_vars_from_answers():
    meta = appmeta.load_app("drive")
    cvars = meta.component_vars("drive")
    assert cvars["st_drive_public_host"] == "{DOMAIN}"
    assert cvars["st_drive_s3_protocol"] == "{S3_PROTOCOL}"
    assert cvars["st_drive_s3_host"] == "{S3_HOST}"
    assert cvars["st_drive_s3_bucket"] == "{S3_BUCKET}"

    data = CommentedMap()
    writer.apply_component_vars(
        data,
        meta,
        meta.core(),
        {
            "DOMAIN": "drive.example.org",
            "S3_PROTOCOL": "https",
            "S3_HOST": "minio.example.org:9000",
            "S3_BUCKET": "drive-media",
        },
    )
    assert data["st_drive_s3_host"] == "minio.example.org:9000"
    assert data["st_drive_s3_protocol"] == "https"
    assert data["st_drive_s3_bucket"] == "drive-media"


def test_backend_run_migrations_gated_to_first_host():
    """drive/meet/messages backends scaffold st_<app>_backend_run_migrations to a
    literal Ansible expression that runs migrations only on the first play host
    (plays are serial:1, so the role's run_once task would otherwise fire per host).
    The manifest quadruples the braces so str.format emits real {{ }}."""
    expr = "{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}"
    for app in ("drive", "meet", "messages"):
        meta = appmeta.load_app(app)
        var = f"st_{app}_backend_run_migrations"
        # manifest stores the escaped (quadrupled-brace) template …
        assert meta.component_vars(meta.core().key)[var] == (
            "{{{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}}}"
        )
        # … which renders to the real single-{{ }} Ansible expression.
        data = CommentedMap()
        writer.apply_component_vars(
            data, meta, meta.core(), {"DOMAIN": "x.example.org"}
        )
        assert data[var] == expr


def test_collabora_env_and_wiring():
    meta = appmeta.load_app("drive")
    data = CommentedMap()
    writer.apply_component_vars(
        data,
        meta,
        meta.component("collabora"),
        {"COLLABORA_DOMAIN": "collabora.example.org"},
    )
    env = str(data["st_drive_collabora_env"])
    assert 'server_name="collabora.example.org"' in env
    assert "DONT_GEN_SSL_CERT=true" in env
    assert "ssl.termination=true" in env

    dep = meta.dependencies[0]  # drive -> collabora
    assert "st_drive_collabora_port" not in {r.get("var") for r in dep.shared}
    rule = dep.shared[0]
    assert rule["answer_key"] == "COLLABORA_DOMAIN"
    assert rule["consumer_format"] == "https://{value}/hosting/discovery"


# --------------------------------------------------------------------------- write_vault tmp-file permissions


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class _StubBackend(SecretBackend):
    """Minimal backend stand-in: yields one secret for the core component."""

    def component_secrets(self, component):
        return {"vault_django_secret_key": "s3cr3t"} if component == "meet" else {}


def test_write_vault_tmp_file_created_at_0600(repo, mocker):
    """write_vault writes plaintext secrets to a ``<vault>.tmp`` file before
    ansible-vault encrypts it in place. The tmp must be created at 0600 (not the
    default 0644 under umask 022) so the plaintext is never world-readable during
    the encrypt subprocess. Captures the mode at encrypt_file time — before
    os.replace turns the tmp into the final vault.yml."""
    (repo / ".vault-pass").write_text(
        "testpass\n", encoding="utf-8"
    )  # for encrypt_file

    captured = {}

    def _record_tmp_mode(path):
        captured["mode"] = _mode(path)

    mocker.patch.object(writer.vault, "encrypt_file", side_effect=_record_tmp_mode)

    writer.write_vault("meet", "prod", "meet", _StubBackend())

    assert captured.get("mode") == 0o600
    # os.replace preserves the source inode's mode → the final vault.yml is 0600 too
    assert _mode(paths.vault_path("meet", "prod", "meet")) == 0o600


def test_write_vault_is_noop_when_no_secrets(repo):
    """An empty component_secrets (e.g. hashi_vault mode) writes no file at all."""
    backend = _StubBackend()  # yields {} for non-core components

    writer.write_vault("meet", "prod", "livekit", backend)

    assert not paths.vault_path("meet", "prod", "livekit").exists()


# --------------------------------------------------------------------------- expand_var_markers


def _sample_map() -> CommentedMap:
    data = CommentedMap()
    data["st_x_host"] = "db-@openbao(kv/data/x:host)"
    data["st_x_env"] = LiteralScalarString(
        "PLAIN=1\nKEY=@openbao(kv/data/x:pw)\nOTHER=2\n"
    )
    data["st_x_ref"] = (
        "{{ lookup('community.hashi_vault.hashi_vault', 'kv/data/x:tok') }}"
    )
    data["st_x_flag"] = True
    return data


def test_expand_var_markers_hashi_vault_expands_every_string_leaf():
    """expand_var_markers walks every string leaf (non-secret scalars AND
    multi-line env blobs) and expands @openbao()/@vault() markers via the
    backend; already-rendered refs and non-string leaves are left untouched."""
    data = _sample_map()
    backend = HashiVaultBackend("x")

    writer.expand_var_markers(data, backend)

    assert data["st_x_host"] == "db-" + hashi_lookup_ref("kv/data/x:host")
    # multi-line value stays a LiteralScalarString (readable `|` block preserved)
    assert isinstance(data["st_x_env"], LiteralScalarString)
    assert "KEY=" + hashi_lookup_ref("kv/data/x:pw") in str(data["st_x_env"])
    assert "PLAIN=1" in str(data["st_x_env"])
    assert "OTHER=2" in str(data["st_x_env"])
    # already-rendered lookup ref carries no marker → unchanged
    assert data["st_x_ref"] == (
        "{{ lookup('community.hashi_vault.hashi_vault', 'kv/data/x:tok') }}"
    )
    # non-string leaf is untouched
    assert data["st_x_flag"] is True


def test_expand_var_markers_ansible_vault_is_noop():
    """ansible-vault has no OpenBao: expand_var_markers must leave the map
    completely unchanged (markers preserved literally, byte-for-byte)."""
    data = _sample_map()
    before = {k: str(v) for k, v in data.items()}
    backend = AnsibleVaultBackend()

    writer.expand_var_markers(data, backend)

    assert {k: str(v) for k, v in data.items()} == before
    assert isinstance(data["st_x_env"], LiteralScalarString)
