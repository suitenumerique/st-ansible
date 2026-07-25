"""Tests for st_cli.core.writer — pure writers for the committed config tree."""

from __future__ import annotations

import stat

import pytest
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

from helpers import seed_creds, seed_livekit_provider


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


def test_apply_component_vars_keeps_committed_value_when_answer_is_missing():
    """A recovery gap must never downgrade a committed value to "{PLACEHOLDER}".

    Regression guard for the merge behaviour of write_core: apply_component_vars
    falls back to the literal template when an answer is missing, which was
    harmless while every write started from an empty map. Now that a rebootstrap
    merges into the committed file, that fallback would overwrite a perfectly
    good st_drive_public_host with the literal string "{DOMAIN}" whenever
    recovery failed to reproduce DOMAIN — silent config corruption on exactly
    the path the rebootstrap flow adds.
    """
    meta = appmeta.load_app("drive")
    data = CommentedMap()
    data["st_drive_public_host"] = "drive.example.org"

    writer.apply_component_vars(data, meta, meta.core(), {})  # no DOMAIN recovered

    assert data["st_drive_public_host"] == "drive.example.org"


def test_apply_component_vars_writes_literal_when_nothing_to_preserve():
    """The literal fallback still applies for an ABSENT key — writing
    "{DOMAIN}" for the operator to fix beats writing nothing at all. Only an
    already-committed value is protected (see the test above)."""
    meta = appmeta.load_app("drive")
    data = CommentedMap()

    writer.apply_component_vars(data, meta, meta.core(), {})

    assert data["st_drive_public_host"] == "{DOMAIN}"


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


# --------------------------------------------------------------------------- write_core merge / rebootstrap


def _write_meet_core(answers, backend=None, hosts=("10.0.0.5",)):
    meta = appmeta.load_app("meet")
    writer.write_core(
        meta, answers, backend or AnsibleVaultBackend(), list(hosts), [], "prod"
    )


def test_write_core_fresh_unit_behaves_as_before(repo):
    """No pre-existing vars.yml: a fresh write_core call must look exactly like
    the pre-merge behaviour — component vars rendered, a single header comment,
    no marker/merge artefacts anywhere."""
    seed_creds(repo)
    _write_meet_core({"DOMAIN": "meet.example.org"})

    data = tree.load_vars("meet", "prod", "meet")
    assert data["st_meet_public_host"] == "meet.example.org"
    assert bool(data.ca.comment)  # header was stamped

    text = paths.vars_path("meet", "prod", "meet").read_text(encoding="utf-8")
    assert text.count("safe to edit by hand") == 1
    assert f"# added by st-cli {__version__}" not in text  # nothing to merge yet


def test_write_core_enter_through_rebootstrap_is_byte_identical(repo):
    """The safety property the whole rebootstrap feature rests on: re-running
    write_core with the SAME answers over an existing unit must reproduce the
    exact same vars.yml, byte for byte."""
    seed_creds(repo)
    answers = {"DOMAIN": "meet.example.org", "DJANGO_ALLOWED_HOSTS": "meet.example.org"}
    _write_meet_core(dict(answers))

    path = paths.vars_path("meet", "prod", "meet")
    before = path.read_bytes()

    _write_meet_core(dict(answers), backend=AnsibleVaultBackend())

    assert path.read_bytes() == before


def test_write_core_merge_preserves_custom_var_comment_and_env_line(repo):
    """A rebootstrap must never destroy an operator's hand-edits: a custom
    st_* var (with its own comment) and a custom KEY=value line stuffed inside
    an *_env blob must both survive an Enter-through rerun, and the header must
    not be duplicated."""
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
    """A changed answer must update the existing line IN PLACE (not duplicate
    it); a key the renderer emits but the committed blob is missing (as if it
    predates that key) must be appended once, under the marker."""
    seed_creds(repo)
    answers = {"DOMAIN": "meet.example.org", "DJANGO_ALLOWED_HOSTS": "meet.example.org"}
    _write_meet_core(dict(answers))

    # Simulate a blob that predates OIDC_RP_CLIENT_ID (an operator-committed
    # file missing a key the current templates always render).
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


# --------------------------------------------------------------------------- write_vault merge / rebootstrap


def test_write_vault_noop_on_empty_buffer_leaves_existing_vault_untouched(repo):
    """An empty component_secrets buffer must be a total no-op — even when
    vault.yml already exists — so a rebootstrap that introduces no new secret
    never touches the file (mtime + bytes both unchanged)."""
    seed_livekit_provider(repo)
    path = paths.vault_path("meet", "prod", "livekit")
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    backend = _StubBackend()  # component_secrets("livekit") == {}
    writer.write_vault("meet", "prod", "livekit", backend)

    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime


def test_write_vault_merges_new_secret_preserving_existing(repo):
    """A rebootstrap's buffer holds only NEWLY prompted secrets; write_vault
    must merge them over the existing decrypted mapping rather than replacing
    it, so every previously-committed secret survives."""
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
    """Re-mirroring an UNCHANGED secret must not rewrite the file.

    ansible-vault salts each encryption, so re-encrypting the same mapping
    yields different ciphertext and the file would churn in `git diff` on every
    rebootstrap. The reuse-a-livekit-provider path re-mirrors its api key/secret
    into the meet core's vault on every run, so this is the common case.
    """
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
    """A vault.yml that can't be decrypted (missing/wrong .vault-pass, corrupt
    file) must raise StCliError BEFORE anything is written — no partial write,
    no leftover .tmp, the original bytes untouched."""
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


# --------------------------------------------------------------------------- ensure_vault_readable


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
