"""Tests for st_cli.core.writer — pure writers for the committed config tree."""

from __future__ import annotations

import pytest
from helpers import file_mode, seed_creds, seed_livekit_provider
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import LiteralScalarString

from st_cli import __version__
from st_cli.core import appmeta, paths, tree, vault, writer
from st_cli.core.errors import StCliError
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
    """st_meet_public_host is `{DOMAIN}`, the single source of truth for the public meet
    domain."""
    meta = appmeta.load_app("meet")
    assert meta.component_vars("meet")["st_meet_public_host"] == "{DOMAIN}"
    data = CommentedMap()
    writer.apply_component_vars(data, meta, meta.core(), {"DOMAIN": "meet.example.org"})
    assert data["st_meet_public_host"] == "meet.example.org"


def test_drive_has_no_legacy_s3_component_vars():
    """A legacy st_drive_s3_host from a pre-0.4.0 unit survives a replay,
    because apply_component_vars never deletes a committed key."""
    meta = appmeta.load_app("drive")
    cvars = meta.component_vars("drive")
    assert "st_drive_s3_protocol" not in cvars
    assert "st_drive_s3_host" not in cvars
    assert "st_drive_s3_bucket" not in cvars

    data = CommentedMap()
    data["st_drive_s3_host"] = "minio.example.org:9000"

    writer.apply_component_vars(
        data, meta, meta.core(), {"DOMAIN": "drive.example.org"}
    )

    assert data["st_drive_public_host"] == "drive.example.org"
    assert data["st_drive_s3_host"] == "minio.example.org:9000"


def test_apply_component_vars_keeps_committed_value_when_answer_is_missing():
    """Check that a missing answer does not downgrade a committed value to a literal
    placeholder."""
    meta = appmeta.load_app("drive")
    data = CommentedMap()
    data["st_drive_public_host"] = "drive.example.org"

    writer.apply_component_vars(data, meta, meta.core(), {})  # no DOMAIN recovered

    assert data["st_drive_public_host"] == "drive.example.org"


def test_apply_component_vars_writes_literal_when_nothing_to_preserve():
    """Check that the literal placeholder is still written when no value is
    committed."""
    meta = appmeta.load_app("drive")
    data = CommentedMap()

    writer.apply_component_vars(data, meta, meta.core(), {})

    assert data["st_drive_public_host"] == "{DOMAIN}"


def test_backend_run_migrations_gated_to_first_host():
    """drive/meet/messages backends scaffold st_<app>_backend_run_migrations to a
    literal Ansible expression that runs migrations only on the first play host."""
    expr = "{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}"
    for app in ("drive", "meet", "messages", "docs", "conversations"):
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


class _StubBackend(SecretBackend):
    """Minimal backend stand-in: yields one secret for the core component."""

    def component_secrets(self, component):
        return {"vault_django_secret_key": "s3cr3t"} if component == "meet" else {}


def test_write_vault_tmp_file_created_at_0600(repo, mocker):
    """write_vault writes plaintext secrets to a `<vault>.tmp` file before
    ansible-vault encrypts it in place."""
    (repo / ".vault-pass").write_text(
        "testpass\n", encoding="utf-8"
    )  # for encrypt_file

    captured = {}

    def _record_tmp_mode(path):
        captured["mode"] = file_mode(path)

    mocker.patch.object(writer.vault, "encrypt_file", side_effect=_record_tmp_mode)

    writer.write_vault("meet", "prod", "meet", _StubBackend())

    assert captured.get("mode") == 0o600
    # os.replace preserves the source inode's mode: the final vault.yml is 0600 too
    assert file_mode(paths.vault_path("meet", "prod", "meet")) == 0o600


def test_write_vault_is_noop_when_no_secrets(repo):
    """An empty component_secrets writes no file at all."""
    backend = _StubBackend()  # yields {} for non-core components

    writer.write_vault("meet", "prod", "livekit", backend)

    assert not paths.vault_path("meet", "prod", "livekit").exists()


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
    """expand_var_markers walks every string leaf and expands @openbao()/@vault()
    markers via the backend; already-rendered refs stay untouched."""
    data = _sample_map()
    backend = HashiVaultBackend("x")

    writer.expand_var_markers(data, backend)

    assert data["st_x_host"] == "db-" + hashi_lookup_ref("kv/data/x:host")
    # multi-line value stays a LiteralScalarString (readable `|` block preserved)
    assert isinstance(data["st_x_env"], LiteralScalarString)
    assert "KEY=" + hashi_lookup_ref("kv/data/x:pw") in str(data["st_x_env"])
    assert "PLAIN=1" in str(data["st_x_env"])
    assert "OTHER=2" in str(data["st_x_env"])
    # already-rendered lookup ref carries no marker, so it stays unchanged
    assert data["st_x_ref"] == (
        "{{ lookup('community.hashi_vault.hashi_vault', 'kv/data/x:tok') }}"
    )
    # non-string leaf is untouched
    assert data["st_x_flag"] is True


def test_expand_var_markers_ansible_vault_is_noop():
    """ansible-vault has no OpenBao, so expand_var_markers leaves the map unchanged."""
    data = _sample_map()
    before = {k: str(v) for k, v in data.items()}
    backend = AnsibleVaultBackend()

    writer.expand_var_markers(data, backend)

    assert {k: str(v) for k, v in data.items()} == before
    assert isinstance(data["st_x_env"], LiteralScalarString)


def test_vars_header_secrets_line_matches_the_backend():
    """The header must describe the backend actually in use, so it never points
    the operator at a vault.yml or ref that does not exist."""
    from st_cli.core import appmeta, writer
    from st_cli.core.secretbackend import AnsibleVaultBackend, HashiVaultBackend

    meta = appmeta.load_app("projects")
    comp = meta.core()

    vault_hdr = writer.vars_header("projects", meta, comp, AnsibleVaultBackend())
    assert "{{ vault_* }}" in vault_hdr
    assert "vault.yml" in vault_hdr and "OpenBao" not in vault_hdr

    hashi_hdr = writer.vars_header(
        "projects", meta, comp, HashiVaultBackend("projects")
    )
    assert "OpenBao" in hashi_hdr and "no vault.yml" in hashi_hdr
    assert "{{ vault_* }}" not in hashi_hdr

    # no backend passed, so wording stays the ansible-vault default
    assert "{{ vault_* }}" in writer.vars_header("projects", meta, comp)


def test_write_core_success_message_reflects_whether_vault_written(repo, capfd):
    """write_core reports `vault.yml` only when the backend actually buffers
    secrets for the core."""
    from st_cli.core import appmeta, writer
    from st_cli.core.secretbackend import AnsibleVaultBackend, HashiVaultBackend

    meta = appmeta.load_app("projects")
    (repo / ".vault-pass").write_text("pw\n", encoding="utf-8")  # for encrypt_file

    # ansible-vault with a buffered secret writes and reports vault.yml
    av = AnsibleVaultBackend()
    answers: dict = {}
    av.env_secret(answers, "SECRET_KEY", component="projects", value="x")
    writer.write_core(meta, answers, av, ["10.0.0.1"], [], "prod")
    assert "wrote vars.yml + vault.yml + hosts" in capfd.readouterr().out

    # hashi_vault buffers nothing and writes no vault.yml, so don't name it
    writer.write_core(meta, {}, HashiVaultBackend("projects"), ["10.0.0.2"], [], "pp")
    out = capfd.readouterr().out
    assert "wrote vars.yml + hosts" in out
    assert "vault.yml" not in out


def _write_meet_core(answers, backend=None, hosts=("10.0.0.5",)):
    meta = appmeta.load_app("meet")
    writer.write_core(
        meta, answers, backend or AnsibleVaultBackend(), list(hosts), [], "prod"
    )


def test_write_core_fresh_unit_behaves_as_before(repo):
    """Check that write_core on a fresh unit renders vars with one header and no merge
    artifacts."""
    seed_creds(repo)
    _write_meet_core({"DOMAIN": "meet.example.org"})

    data = tree.load_vars("meet", "prod", "meet")
    assert data["st_meet_public_host"] == "meet.example.org"
    assert bool(data.ca.comment)  # header was stamped

    text = paths.vars_path("meet", "prod", "meet").read_text(encoding="utf-8")
    assert text.count("safe to edit by hand") == 1
    assert f"# added by st-cli {__version__}" not in text  # nothing to merge yet


def test_write_core_enter_through_rebootstrap_is_byte_identical(repo):
    """Check that re-running write_core with the same answers reproduces a
    byte-identical vars.yml."""
    seed_creds(repo)
    answers = {"DOMAIN": "meet.example.org", "DJANGO_ALLOWED_HOSTS": "meet.example.org"}
    _write_meet_core(dict(answers))

    path = paths.vars_path("meet", "prod", "meet")
    before = path.read_bytes()

    _write_meet_core(dict(answers), backend=AnsibleVaultBackend())

    assert path.read_bytes() == before


def test_write_core_merge_preserves_custom_var_comment_and_env_line(repo):
    """Check that a rebootstrap preserves a custom var, its comment, and a custom
    env-blob line."""
    seed_creds(repo)
    answers = {"DOMAIN": "meet.example.org"}
    _write_meet_core(dict(answers))

    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_something"] = "custom-value"
    data.yaml_set_comment_before_after_key(
        "st_meet_something", before="an operator's own comment"
    )
    blob = str(data["st_meet_backend_env"])
    data["st_meet_backend_env"] = LiteralScalarString(blob + "MY_VAR=1\n")
    tree.save_vars("meet", "prod", "meet", data)

    _write_meet_core(dict(answers), backend=AnsibleVaultBackend())

    text = paths.vars_path("meet", "prod", "meet").read_text(encoding="utf-8")
    assert "st_meet_something: custom-value" in text
    assert "# an operator's own comment" in text
    assert "MY_VAR=1" in text
    assert text.count("safe to edit by hand") == 1  # header not stacked


def test_write_core_rebootstrap_updates_value_in_place_and_appends_new_key(repo):
    """Check that a changed answer updates in place and a missing rendered key is
    appended once."""
    seed_creds(repo)
    answers = {"DOMAIN": "meet.example.org", "DJANGO_ALLOWED_HOSTS": "meet.example.org"}
    _write_meet_core(dict(answers))

    # simulate a blob that predates OIDC_RP_CLIENT_ID
    data = tree.load_vars("meet", "prod", "meet")
    lines = [
        ln
        for ln in str(data["st_meet_backend_env"]).splitlines()
        if not ln.startswith("OIDC_RP_CLIENT_ID=")
    ]
    data["st_meet_backend_env"] = LiteralScalarString("\n".join(lines) + "\n")
    tree.save_vars("meet", "prod", "meet", data)

    answers2 = dict(answers, DJANGO_ALLOWED_HOSTS="changed.example.org")
    _write_meet_core(answers2, backend=AnsibleVaultBackend())

    blob = str(tree.load_vars("meet", "prod", "meet")["st_meet_backend_env"])
    assert blob.count("DJANGO_ALLOWED_HOSTS=") == 1
    assert "DJANGO_ALLOWED_HOSTS=changed.example.org" in blob

    marker = f"# added by st-cli {__version__}"
    assert blob.count(marker) == 1
    blob_lines = blob.splitlines()
    marker_idx = blob_lines.index(marker)
    assert any(ln.startswith("OIDC_RP_CLIENT_ID=") for ln in blob_lines[marker_idx:])


def test_write_core_reports_wrote_vs_updated(repo, capfd):
    """ui.success reflects whether the unit was fresh or already existed."""
    seed_creds(repo)
    _write_meet_core({"DOMAIN": "meet.example.org"})
    assert "wrote vars.yml" in capfd.readouterr().out

    _write_meet_core({"DOMAIN": "meet.example.org"}, backend=AnsibleVaultBackend())
    assert "updated vars.yml" in capfd.readouterr().out


def test_write_vault_noop_on_empty_buffer_leaves_existing_vault_untouched(repo):
    """Check that an empty component_secrets buffer leaves an existing vault.yml
    untouched."""
    seed_livekit_provider(repo)
    path = paths.vault_path("meet", "prod", "livekit")
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    backend = _StubBackend()  # component_secrets("livekit") == {}
    writer.write_vault("meet", "prod", "livekit", backend)

    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime


def test_write_vault_merges_new_secret_preserving_existing(repo):
    """Check that write_vault merges a new secret into the existing mapping without
    losing old ones."""
    seed_livekit_provider(repo)
    path = paths.vault_path("meet", "prod", "livekit")

    class _NewSecretBackend(SecretBackend):
        def component_secrets(self, component):
            return {"vault_meet_livekit_new_secret": "brand-new"}

    writer.write_vault("meet", "prod", "livekit", _NewSecretBackend())

    merged = writer.vault.decrypt_to_dict(path)
    assert merged["st_meet_livekit_api_key"] == "real-token"
    assert merged["st_meet_livekit_api_secret"] == "real-secret"
    assert merged["st_meet_livekit_redis_password"] == "real-redis-pass"
    assert merged["vault_meet_livekit_new_secret"] == "brand-new"


def test_write_vault_noop_when_merge_changes_nothing(repo):
    """Check that re-mirroring the same secret value does not rewrite vault.yml, since
    salting would change the diff each run."""
    seed_creds(repo)
    backend = AnsibleVaultBackend()
    backend.var_secret(CommentedMap(), "vault_api_key", "same-value", component="meet")
    writer.write_vault("meet", "prod", "meet", backend)

    path = paths.vault_path("meet", "prod", "meet")
    before = path.read_bytes()

    # a second run mirroring the identical value must leave the bytes alone
    backend2 = AnsibleVaultBackend()
    backend2.var_secret(CommentedMap(), "vault_api_key", "same-value", component="meet")
    writer.write_vault("meet", "prod", "meet", backend2)

    assert path.read_bytes() == before

    # ...but a genuinely changed value still gets written
    backend3 = AnsibleVaultBackend()
    backend3.var_secret(CommentedMap(), "vault_api_key", "rotated", component="meet")
    writer.write_vault("meet", "prod", "meet", backend3)

    assert path.read_bytes() != before
    assert vault.decrypt_to_dict(path)["vault_api_key"] == "rotated"


def test_write_vault_undecryptable_raises_and_leaves_file_untouched(repo):
    """Check that an undecryptable vault.yml raises StCliError before any write
    happens."""
    seed_creds(repo)
    path = paths.vault_path("meet", "prod", "livekit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a real ansible-vault file\n", encoding="utf-8")
    before = path.read_bytes()

    class _Backend(SecretBackend):
        def component_secrets(self, component):
            return {"vault_x": "y"}

    with pytest.raises(StCliError):
        writer.write_vault("meet", "prod", "livekit", _Backend())

    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".tmp").exists()


def test_ensure_vault_readable_noop_when_absent(repo):
    seed_creds(repo)
    writer.ensure_vault_readable(
        "meet", "prod", ["meet", "livekit"]
    )  # nothing to check


def test_ensure_vault_readable_passes_for_good_vault(repo):
    seed_livekit_provider(repo)
    writer.ensure_vault_readable("meet", "prod", ["livekit"])  # must not raise


def test_ensure_vault_readable_raises_for_bad_vault(repo):
    seed_creds(repo)
    path = paths.vault_path("meet", "prod", "livekit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage\n", encoding="utf-8")

    with pytest.raises(StCliError):
        writer.ensure_vault_readable("meet", "prod", ["livekit"])
