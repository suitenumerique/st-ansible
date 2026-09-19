"""Acceptance tests for the rebootstrap flow (`st_cli.cmd.bootstrap`).

`accept_defaults` presses Enter on every unscripted prompt but does not prove
one was skipped; use `assert not sq.asked(...)` for that.
"""

from __future__ import annotations

import shutil

import pytest
from helpers import (
    ACCEPT_DEFAULT,
    accept_defaults,
    docs_first_run_script,
    drive_first_run_script,
    livekit_script,
    meet_first_run_script,
    messages_first_run_script,
    projects_first_run_script,
    script_questionary,
    seed_creds,
    seed_external_livekit_with_leftover_tree,
    seed_hashi_livekit_provider,
    seed_livekit_provider,
    seed_meet_egress_unit,
    seed_meet_unit,
    set_flags,
    with_answers,
)
from ruamel.yaml.scalarstring import LiteralScalarString

from st_cli import __version__
from st_cli.cmd import bootstrap
from st_cli.core import appmeta, envrender, manifest, paths, tree, upgrades, vault
from st_cli.core.errors import StCliError
from st_cli.core.secretbackend import AnsibleVaultBackend, HashiVaultBackend


def _meet_first_run_script_fully_recoverable(with_livekit: bool = False) -> list[tuple]:
    """`meet_first_run_script(smtp=False)`, so a later SILENT replay has nothing left to
    ask.

    `with_livekit` adds a co-located livekit and egress deploy, to also exercise
    the unflagged dependency reuse path.
    """
    if with_livekit:
        return meet_first_run_script(smtp=False, livekit=None) + livekit_script(
            ask_now=True
        )
    return meet_first_run_script(smtp=False)


def test_meet_round_trip_byte_identical_and_smtp_gate_stays_on(repo, monkeypatch):
    """An Enter-through meet rebootstrap with SMTP on keeps vars.yml and vault.yml
    byte-identical, and SMTP on."""
    seed_creds(repo)
    sq1 = script_questionary(monkeypatch, meet_first_run_script(smtp=True))
    bootstrap.bootstrap("meet", "prod")
    assert not sq1._scripts, f"unconsumed scripts: {sq1._scripts}"

    core_vars_before = (repo / "meet/prod/meet/vars.yml").read_text()
    core_vault_before = (repo / "meet/prod/meet/vault.yml").read_bytes()
    assert "DJANGO_EMAIL_HOST=smtp.example.org" in core_vars_before

    m = manifest.load_manifest()
    unit = next(u for u in m.units if u.component == "meet")
    assert unit.bootstrapped_with == __version__

    sq2 = accept_defaults(
        monkeypatch,
        [
            (
                "select",
                "meet/prod is already bootstrapped — what do you want to do?",
                "Modify — replay the questionnaire (answers pre-filled)",
            ),
            ("select", "Bootstrap livekit now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("meet", "prod")
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    core_vars_after = (repo / "meet/prod/meet/vars.yml").read_text()
    core_vault_after = (repo / "meet/prod/meet/vault.yml").read_bytes()
    assert core_vars_after == core_vars_before
    assert core_vault_after == core_vault_before
    assert "DJANGO_EMAIL_HOST=smtp.example.org" in core_vars_after


def test_meet_with_livekit_round_trip_leaves_core_vault_untouched(repo, monkeypatch):
    """An Enter-through meet+livekit rebootstrap does not rewrite core vault.yml or
    rotate livekit's shared secrets."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        meet_first_run_script(smtp=False, livekit=None)
        + [
            ("select", "Bootstrap livekit now?", "Yes — bootstrap now"),
            ("text", "livekit host(s)", "10.0.0.1"),
            ("text", "egress (leave blank", ""),  # blank means co-located with livekit
            (
                "text",
                "LiveKit domain (e.g. livekit.example.org)",
                "livekit.example.org",
            ),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", "turn.example.org"),
            ("confirm", "livekit", True),
            ("confirm", "egress", True),
        ],
    )
    bootstrap.bootstrap("meet", "prod")

    core_vault_before = (repo / "meet/prod/meet/vault.yml").read_bytes()
    core_vars_before = (repo / "meet/prod/meet/vars.yml").read_text()
    lk_vault_before = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    assert "LIVEKIT_API_KEY={{ vault_livekit_api_key }}" in core_vars_before

    sq2 = accept_defaults(monkeypatch)
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    # the reuse branch was pre-selected from .st-cli.yml, not re-decided
    assert any("Bootstrap livekit now?" in msg for msg, _ in sq2.select_calls), (
        "the dependency select should still be offered on a rebootstrap"
    )

    assert (repo / "meet/prod/meet/vars.yml").read_text() == core_vars_before
    # the actual regression guard: no phantom re-encryption
    assert (repo / "meet/prod/meet/vault.yml").read_bytes() == core_vault_before
    # and the shared secrets were not rotated
    assert (
        vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
        == lk_vault_before
    )


def test_drive_round_trip_byte_identical(repo, monkeypatch):
    """An Enter-through drive+collabora rebootstrap touches no file."""
    seed_creds(repo)
    sq1 = script_questionary(
        monkeypatch,
        drive_first_run_script()[:-1]  # drop the "bootstrap later" select
        + [
            ("select", "Bootstrap collabora now?", "Yes — bootstrap now"),
            ("text", "collabora host(s)", "10.0.0.11"),
            ("text", "Collabora domain", "collabora.example.org"),
            ("confirm", "cadvisor", True),
        ],
    )
    bootstrap.bootstrap("drive", "prod")
    assert not sq1._scripts, f"unconsumed scripts: {sq1._scripts}"

    core_vars_before = (repo / "drive/prod/drive/vars.yml").read_text()
    core_vault_before = (repo / "drive/prod/drive/vault.yml").read_bytes()
    collabora_vars_before = (repo / "drive/prod/collabora/vars.yml").read_text()
    # collabora's only shared rule is a non-secret prompt, so it never gets a
    # vault.yml at all (write_vault no-ops on an empty secret buffer).
    assert not paths.vault_path("drive", "prod", "collabora").exists()
    # sanity: the backend blob holds the literal endpoint/bucket, and the
    # caddy blob holds the split-out S3 host; no legacy indirection left.
    assert "AWS_S3_ENDPOINT_URL=https://s3.fr-par.scw.cloud" in core_vars_before
    assert "CADDY_S3_HOST=s3.fr-par.scw.cloud" in core_vars_before
    assert "st_drive_s3_" not in core_vars_before

    sq2 = accept_defaults(
        monkeypatch, [("select", "Database configuration:", "DATABASE_URL")]
    )
    bootstrap.bootstrap("drive", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    assert (repo / "drive/prod/drive/vars.yml").read_text() == core_vars_before
    assert (repo / "drive/prod/drive/vault.yml").read_bytes() == core_vault_before
    assert (repo / "drive/prod/collabora/vars.yml").read_text() == collabora_vars_before
    assert not paths.vault_path("drive", "prod", "collabora").exists()


def test_drive_legacy_s3_indirection_replay_migrates_to_caddy(repo, monkeypatch):
    """A pre-0.4.0 drive unit's st_drive_s3_* indirection replay rewrites
    AWS_S3_ENDPOINT_URL and AWS_STORAGE_BUCKET_NAME to literal values, adds
    st_drive_caddy_env, and keeps the legacy lines untouched."""
    seed_creds(repo)
    script_questionary(monkeypatch, drive_first_run_script())
    bootstrap.bootstrap("drive", "prod")

    data = tree.load_vars("drive", "prod", "drive")
    data["st_drive_s3_protocol"] = "https"
    data["st_drive_s3_host"] = "s3.fr-par.scw.cloud"
    data["st_drive_s3_bucket"] = "drive-media"
    blob = str(data["st_drive_backend_env"])
    blob = blob.replace(
        "AWS_S3_ENDPOINT_URL=https://s3.fr-par.scw.cloud",
        "AWS_S3_ENDPOINT_URL={{ st_drive_s3_protocol }}://{{ st_drive_s3_host }}",
    ).replace(
        "AWS_STORAGE_BUCKET_NAME=drive-media",
        "AWS_STORAGE_BUCKET_NAME={{ st_drive_s3_bucket }}",
    )
    data["st_drive_backend_env"] = LiteralScalarString(blob)
    del data["st_drive_caddy_env"]
    tree.save_vars("drive", "prod", "drive", data)

    core_vault_before = (repo / "drive/prod/drive/vault.yml").read_bytes()

    collabora_script = [
        ("select", "Database configuration:", "DATABASE_URL"),
        ("select", "Bootstrap collabora now?", "No — bootstrap later"),
    ]
    sq = accept_defaults(monkeypatch, collabora_script)
    bootstrap.bootstrap("drive", "prod", replay=bootstrap.ReplayAction.MODIFY)

    new_data = tree.load_vars("drive", "prod", "drive")
    backend_blob = str(new_data["st_drive_backend_env"])
    assert "AWS_S3_ENDPOINT_URL=https://s3.fr-par.scw.cloud" in backend_blob
    assert "AWS_STORAGE_BUCKET_NAME=drive-media" in backend_blob
    caddy_blob = str(new_data["st_drive_caddy_env"])
    assert "CADDY_S3_PROTOCOL=https" in caddy_blob
    assert "CADDY_S3_HOST=s3.fr-par.scw.cloud" in caddy_blob
    assert "CADDY_S3_BUCKET=drive-media" in caddy_blob
    # the legacy lines survive; the merge/apply_component_vars never delete.
    assert new_data["st_drive_s3_protocol"] == "https"
    assert new_data["st_drive_s3_host"] == "s3.fr-par.scw.cloud"
    assert new_data["st_drive_s3_bucket"] == "drive-media"
    assert (repo / "drive/prod/drive/vault.yml").read_bytes() == core_vault_before
    assert sq.asked("text", "AWS_S3_ENDPOINT_URL")

    vars_after_first_replay = (repo / "drive/prod/drive/vars.yml").read_text()
    accept_defaults(monkeypatch, collabora_script)
    bootstrap.bootstrap("drive", "prod", replay=bootstrap.ReplayAction.MODIFY)

    assert (repo / "drive/prod/drive/vars.yml").read_text() == vars_after_first_replay


def test_workers_only_run_over_existing_core_skips_3way_select(repo, monkeypatch):
    """A `-c workers` re-run over an existing core skips the 3-way select and
    re-registers with no prompts."""
    seed_creds(repo)
    script_questionary(monkeypatch, drive_first_run_script())
    bootstrap.bootstrap("drive", "prod")
    script_questionary(monkeypatch, [])
    bootstrap.bootstrap("drive", "prod", component="workers")

    m = manifest.load_manifest()
    assert any(u.component == "workers" for u in m.units)

    # a second `-c workers` run over the now-existing core must not offer the
    # 3-way select; it must just re-register the unit with no prompts.
    sq = script_questionary(monkeypatch, [])
    bootstrap.bootstrap("drive", "prod", component="workers")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert not any("is already bootstrapped" in msg for msg, _ in sq.select_calls)


@pytest.mark.parametrize(
    "replay", [bootstrap.ReplayAction.REUSE, bootstrap.ReplayAction.OVERRIDE]
)
def test_workers_only_run_rejects_reuse_and_override(repo, monkeypatch, replay):
    """A `-c workers` run with `replay=REUSE`/`OVERRIDE` raises: those apply to the core
    path only."""
    seed_creds(repo)
    script_questionary(monkeypatch, drive_first_run_script())
    bootstrap.bootstrap("drive", "prod")
    script_questionary(monkeypatch, [])
    bootstrap.bootstrap("drive", "prod", component="workers")

    with pytest.raises(StCliError, match="applies to the core path only"):
        bootstrap.bootstrap("drive", "prod", component="workers", replay=replay)


def test_wire_only_core_run_rejects_override_and_omits_it_from_select(
    repo, monkeypatch
):
    """A wire-only core run rejects `replay=OVERRIDE` and omits Override from the 3-way
    select."""
    seed_creds(repo)
    script_questionary(monkeypatch, drive_first_run_script())
    bootstrap.bootstrap("drive", "prod")

    script_questionary(monkeypatch, [])
    with pytest.raises(StCliError, match="wire-only"):
        bootstrap.bootstrap(
            "drive", "prod", component="drive", replay=bootstrap.ReplayAction.OVERRIDE
        )

    sq = script_questionary(
        monkeypatch,
        [
            (
                "select",
                "is already bootstrapped",
                "Reuse — keep everything as-is (skip the questionnaire)",
            )
        ],
    )
    bootstrap.bootstrap("drive", "prod", component="drive")
    select_choices = [
        choices for msg, choices in sq.select_calls if "is already bootstrapped" in msg
    ]
    assert select_choices, "the 3-way select did not appear"
    assert not any("Override" in c for c in select_choices[0])


def test_messages_round_trip_byte_identical(repo, monkeypatch):
    """An Enter-through messages rebootstrap with blobs offload and relay outbound
    leaves vars.yml and vault.yml byte-identical."""
    seed_creds(repo)
    sq1 = script_questionary(
        monkeypatch,
        messages_first_run_script(
            db_mode="discrete", blobs_offload=True, outbound="relay"
        )
        + [
            ("select", "Bootstrap pymta now?", "No — bootstrap later"),
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("messages", "prod")
    assert not sq1._scripts, f"unconsumed scripts: {sq1._scripts}"

    core_vars_before = (repo / "messages/prod/messages/vars.yml").read_text()
    core_vault_before = (repo / "messages/prod/messages/vault.yml").read_bytes()

    sq2 = accept_defaults(
        monkeypatch,
        [
            ("select", "Bootstrap pymta now?", "No — bootstrap later"),
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("messages", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    assert (repo / "messages/prod/messages/vars.yml").read_text() == core_vars_before
    assert (repo / "messages/prod/messages/vault.yml").read_bytes() == core_vault_before


def test_projects_round_trip_byte_identical(repo, monkeypatch):
    """An Enter-through projects rebootstrap of the Sails app leaves vars.yml and
    vault.yml byte-identical."""
    seed_creds(repo)
    sq1 = script_questionary(monkeypatch, projects_first_run_script())
    bootstrap.bootstrap("projects", "prod")
    assert not sq1._scripts, f"unconsumed scripts: {sq1._scripts}"

    core_vars_before = (repo / "projects/prod/projects/vars.yml").read_text()
    core_vault_before = (repo / "projects/prod/projects/vault.yml").read_bytes()
    decrypted_before = vault.decrypt_to_dict(
        paths.vault_path("projects", "prod", "projects")
    )

    sq2 = accept_defaults(monkeypatch)
    bootstrap.bootstrap("projects", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    assert (repo / "projects/prod/projects/vars.yml").read_text() == core_vars_before
    assert (repo / "projects/prod/projects/vault.yml").read_bytes() == core_vault_before
    assert (
        vault.decrypt_to_dict(paths.vault_path("projects", "prod", "projects"))
        == decrypted_before
    )


def test_docs_round_trip_byte_identical(repo, monkeypatch):
    """An Enter-through docs+yprovider rebootstrap leaves vars.yml/vault.yml
    byte-identical for both units."""
    seed_creds(repo)
    sq1 = script_questionary(
        monkeypatch,
        docs_first_run_script(smtp=True, yprovider="Yes — bootstrap now")
        + [
            ("text", "yprovider host(s)", "10.0.0.9"),
            ("confirm", "cadvisor", True),  # yprovider cadvisor
        ],
    )
    bootstrap.bootstrap("docs", "prod")
    assert not sq1._scripts, f"unconsumed scripts: {sq1._scripts}"

    core_vars_before = (repo / "docs/prod/docs/vars.yml").read_text()
    core_vault_before = (repo / "docs/prod/docs/vault.yml").read_bytes()
    yp_vars_before = (repo / "docs/prod/yprovider/vars.yml").read_text()
    yp_vault_before = (repo / "docs/prod/yprovider/vault.yml").read_bytes()
    docs_decrypted_before = vault.decrypt_to_dict(
        paths.vault_path("docs", "prod", "docs")
    )
    yp_decrypted_before = vault.decrypt_to_dict(
        paths.vault_path("docs", "prod", "yprovider")
    )
    assert "CADDY_YPROVIDER_ENDPOINTS=10.0.0.9:50601" in core_vars_before

    sq2 = accept_defaults(
        monkeypatch,
        [
            ("select", "Database configuration:", "DATABASE_URL"),
            (
                "select",
                "Bootstrap yprovider now?",
                "Modify (replay the questionnaire)",
            ),
        ],
    )
    bootstrap.bootstrap("docs", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    assert (repo / "docs/prod/docs/vars.yml").read_text() == core_vars_before
    assert (repo / "docs/prod/yprovider/vars.yml").read_text() == yp_vars_before
    assert (repo / "docs/prod/docs/vault.yml").read_bytes() == core_vault_before
    assert (repo / "docs/prod/yprovider/vault.yml").read_bytes() == yp_vault_before
    assert (
        vault.decrypt_to_dict(paths.vault_path("docs", "prod", "docs"))
        == docs_decrypted_before
    )
    assert (
        vault.decrypt_to_dict(paths.vault_path("docs", "prod", "yprovider"))
        == yp_decrypted_before
    )
    core_vars_after = (repo / "docs/prod/docs/vars.yml").read_text()
    assert "CADDY_YPROVIDER_ENDPOINTS=10.0.0.9:50601" in core_vars_after


def test_docs_kept_external_yprovider_round_trip(repo, monkeypatch):
    """An Enter-through docs rebootstrap with an external yprovider keeps its secrets
    and writes no yprovider tree."""
    seed_creds(repo)
    sq1 = script_questionary(
        monkeypatch,
        docs_first_run_script(
            smtp=False, yprovider="Already deployed (enter URL + keys)"
        )
        + [
            (
                "text",
                "yprovider endpoints (host:port, comma-separated)",
                "10.0.0.9:50601, 10.0.0.10:50601",
            ),
            (
                "text",
                "Y_PROVIDER_API_BASE_URL (backend-only conversion API)",
                "http://yprovider.internal:50601/api/",
            ),
            ("password", "COLLABORATION_SERVER_SECRET", "ext-collab-secret"),
            ("password", "Y_PROVIDER_API_KEY", "ext-yprovider-key"),
        ],
    )
    bootstrap.bootstrap("docs", "prod")
    assert not sq1._scripts, f"unconsumed scripts: {sq1._scripts}"

    core_vars_before = (repo / "docs/prod/docs/vars.yml").read_text()
    core_vault_before = (repo / "docs/prod/docs/vault.yml").read_bytes()
    core_decrypted_before = vault.decrypt_to_dict(
        paths.vault_path("docs", "prod", "docs")
    )
    assert core_decrypted_before["vault_collaboration_server_secret"] == (
        "ext-collab-secret"
    )
    assert core_decrypted_before["vault_y_provider_api_key"] == "ext-yprovider-key"
    assert not paths.vars_path("docs", "prod", "yprovider").exists()

    sq2 = accept_defaults(
        monkeypatch, [("select", "Database configuration:", "DATABASE_URL")]
    )
    bootstrap.bootstrap("docs", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    assert (repo / "docs/prod/docs/vars.yml").read_text() == core_vars_before
    assert (repo / "docs/prod/docs/vault.yml").read_bytes() == core_vault_before
    core_decrypted_after = vault.decrypt_to_dict(
        paths.vault_path("docs", "prod", "docs")
    )
    assert core_decrypted_after == core_decrypted_before
    assert core_decrypted_after["vault_collaboration_server_secret"] == (
        "ext-collab-secret"
    )
    assert core_decrypted_after["vault_y_provider_api_key"] == "ext-yprovider-key"
    assert not paths.vars_path("docs", "prod", "yprovider").exists()


def test_hand_edits_survive_rebootstrap(repo, monkeypatch):
    """A hand-added custom var, comment, and env line all survive an Enter-through
    rebootstrap untouched."""
    seed_creds(repo)
    script_questionary(monkeypatch, meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")

    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_my_custom_var"] = "custom-value"
    data.yaml_set_comment_before_after_key("st_meet_my_custom_var", before="my comment")
    blob = str(data["st_meet_backend_env"])
    data["st_meet_backend_env"] = LiteralScalarString(blob + "MY_VAR=1\n")
    tree.save_vars("meet", "prod", "meet", data)

    accept_defaults(
        monkeypatch, [("select", "Bootstrap livekit now?", "No — bootstrap later")]
    )
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)

    new_text = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "st_meet_my_custom_var: custom-value" in new_text
    assert "my comment" in new_text
    assert "MY_VAR=1" in new_text


def test_socks_proxy_replay_never_rotates_or_clobbers(repo, monkeypatch):
    """A standalone socks-proxy Enter-through replay does not mint PROXY_USERS or touch
    its committed files."""
    seed_creds(repo)
    sq1 = script_questionary(
        monkeypatch,
        messages_first_run_script()
        + [
            ("select", "Bootstrap pymta now?", "No — bootstrap later"),
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
            ("select", "Bootstrap socks-proxy now?", "Yes — bootstrap now"),
            ("text", "socks-proxy host(s)", "10.0.0.6"),
            ("text", "PROXY_EXTERNAL", "eth1"),
            ("text", "PROXY_INTERNAL_PORT", "51000"),
            ("confirm", "cadvisor", True),  # socks-proxy cadvisor
        ],
    )
    bootstrap.bootstrap("messages", "prod")
    assert not sq1._scripts, f"unconsumed scripts: {sq1._scripts}"

    sp_vars_before = (repo / "messages/prod/socks-proxy/vars.yml").read_text()
    sp_vault_before = paths.vault_path("messages", "prod", "socks-proxy").read_bytes()
    core_vault_before = paths.vault_path("messages", "prod", "messages").read_bytes()

    sq2 = accept_defaults(monkeypatch)
    bootstrap.bootstrap("messages", "prod", component="socks-proxy")
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    sp_vars_after = (repo / "messages/prod/socks-proxy/vars.yml").read_text()
    assert sp_vars_after == sp_vars_before
    assert "PROXY_EXTERNAL=eth1" in sp_vars_after
    assert "PROXY_INTERNAL_PORT=51000" in sp_vars_after
    assert (
        paths.vault_path("messages", "prod", "socks-proxy").read_bytes()
        == sp_vault_before
    )
    assert (
        paths.vault_path("messages", "prod", "messages").read_bytes()
        == core_vault_before
    )


def test_socks_proxy_standalone_mint_then_full_replay_repairs_core_vault(
    repo, monkeypatch
):
    """A full replay after a standalone socks-proxy mint repairs the core vault's
    vault_proxy_users ref."""
    seed_creds(repo)
    sq1 = script_questionary(
        monkeypatch,
        messages_first_run_script()
        + [
            ("select", "Bootstrap pymta now?", "No — bootstrap later"),
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
            ("select", "Bootstrap socks-proxy now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("messages", "prod")
    assert not sq1._scripts, f"unconsumed scripts: {sq1._scripts}"

    core_vault_path = paths.vault_path("messages", "prod", "messages")
    assert "vault_proxy_users" not in vault.decrypt_to_dict(core_vault_path)

    sq2 = script_questionary(
        monkeypatch,
        [
            ("text", "socks-proxy host(s)", "10.0.0.6"),
            ("text", "PROXY_EXTERNAL", "eth1"),
            ("text", "PROXY_INTERNAL_PORT", "51000"),
            ("confirm", "cadvisor", True),
        ],
    )
    bootstrap.bootstrap("messages", "prod", component="socks-proxy")
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    provider_vault_before = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "socks-proxy")
    )
    assert "vault_proxy_users" in provider_vault_before
    # the mint's core-side mirror never reached disk; this run wrote only
    # the provider unit.
    assert "vault_proxy_users" not in vault.decrypt_to_dict(core_vault_path)

    sq3 = accept_defaults(
        monkeypatch,
        [
            ("select", "Database configuration:", "DATABASE_URL"),
            ("select", "Outbound mail mode", "direct"),
            ("select", "Bootstrap pymta now?", "No — bootstrap later"),
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
            (
                "select",
                "Bootstrap socks-proxy now?",
                "Modify (replay the questionnaire)",
            ),
        ],
    )
    bootstrap.bootstrap("messages", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq3._scripts, f"unconsumed scripts: {sq3._scripts}"

    core_vault_after = vault.decrypt_to_dict(core_vault_path)
    provider_vault_after = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "socks-proxy")
    )
    assert (
        core_vault_after["vault_proxy_users"]
        == provider_vault_before["vault_proxy_users"]
    )
    assert provider_vault_after == provider_vault_before


def test_edited_answer_propagates(repo, monkeypatch):
    """Changing a value on the rebootstrap, instead of accepting the default, lands in
    the committed tree."""
    seed_creds(repo)
    script_questionary(monkeypatch, meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")
    assert "DB_HOST=db.example.org" in (repo / "meet/prod/meet/vars.yml").read_text()

    accept_defaults(
        monkeypatch,
        [
            ("text", "DB_HOST", "new-db.example.org"),
            ("select", "Bootstrap livekit now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)

    new_text = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "DB_HOST=new-db.example.org" in new_text
    assert "DB_HOST=db.example.org" not in new_text


def test_new_question_is_asked_and_merged_in(repo, monkeypatch):
    """A genuinely new SMTP answer on the rebootstrap is asked for, and merged in behind
    the marker comment."""
    seed_creds(repo)
    script_questionary(monkeypatch, meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")
    before = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "DJANGO_EMAIL_HOST" not in before

    accept_defaults(
        monkeypatch,
        [
            ("confirm", "Configure transactional email (SMTP) settings?", True),
            ("text", "DJANGO_EMAIL_HOST", "new-smtp.example.org"),
            ("text", "DJANGO_EMAIL_PORT", "587"),
            ("text", "DJANGO_EMAIL_HOST_USER (optional)", ""),
            ("password", "DJANGO_EMAIL_HOST_PASSWORD", "newsmtppass"),
            ("confirm", "DJANGO_EMAIL_USE_TLS?", True),
            ("confirm", "DJANGO_EMAIL_USE_SSL?", False),
            ("text", "DJANGO_EMAIL_FROM", "noreply@example.org"),
            ("text", "DJANGO_EMAIL_BRAND_NAME (optional)", ""),
            ("select", "Bootstrap livekit now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)

    after = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "DJANGO_EMAIL_HOST=new-smtp.example.org" in after
    # the new keys were appended behind the merge marker, not interleaved
    assert "# added by st-cli" in after


def test_recovered_secrets_never_reprompted(repo, monkeypatch):
    """An Enter-through rebootstrap never issues a single `password` prompt: every
    secret was recovered."""
    seed_creds(repo)
    script_questionary(monkeypatch, meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")

    sq = accept_defaults(
        monkeypatch, [("select", "Bootstrap livekit now?", "No — bootstrap later")]
    )
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert not any(kind == "password" for kind, _ in sq.prompts)


def test_livekit_shared_secret_not_rotated_on_standalone_rebootstrap(repo, monkeypatch):
    """A standalone `-c livekit` rebootstrap recovers the LiveKit api key/secret instead
    of regenerating them."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            *livekit_script(public_domain=True),
        ],
    )
    bootstrap.bootstrap("meet", "prod", component="livekit")
    lv_before = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    ev_before = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))

    sq = accept_defaults(monkeypatch)
    bootstrap.bootstrap("meet", "prod", component="livekit")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    lv_after = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    ev_after = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert lv_after["st_meet_livekit_api_key"] == lv_before["st_meet_livekit_api_key"]
    assert (
        lv_after["st_meet_livekit_api_secret"]
        == lv_before["st_meet_livekit_api_secret"]
    )
    assert ev_after["st_meet_livekit_api_key"] == ev_before["st_meet_livekit_api_key"]
    assert (
        ev_after["st_meet_livekit_api_secret"]
        == ev_before["st_meet_livekit_api_secret"]
    )

    m = manifest.load_manifest()
    unit = next(u for u in m.units if u.component == "livekit")
    assert unit.bootstrapped_with == __version__


@pytest.mark.parametrize(
    "replay", [bootstrap.ReplayAction.REUSE, bootstrap.ReplayAction.OVERRIDE]
)
def test_provider_only_run_rejects_reuse_and_override(repo, monkeypatch, replay):
    """A `-c <provider>` run with `replay=REUSE`/`OVERRIDE` raises instead of silently
    downgrading to MODIFY."""
    seed_livekit_provider(repo)
    with pytest.raises(StCliError, match="applies to the core path only"):
        bootstrap.bootstrap("meet", "prod", component="livekit", replay=replay)


def _colocated_livekit_first_run_script(host: str = "10.0.0.1") -> list[tuple]:
    """A fresh `-c livekit` bootstrap, with egress left blank so it is co-located."""
    return [
        ("select", "Secret backend:", "ansible-vault"),
        ("text", "livekit host(s)", host),
        ("text", "egress (leave blank", ""),
        ("text", "LiveKit domain (e.g. livekit.example.org)", "livekit.example.org"),
        ("text", "LiveKit TURN domain (e.g. turn.example.org)", "turn.example.org"),
        (
            "text",
            "Public domain for meet (for the LiveKit recording webhook)",
            "meet.example.org",
        ),
        ("confirm", "livekit", True),
        ("confirm", "egress", True),
    ]


def test_livekit_replay_prefills_external_redis_and_never_rotates(repo, monkeypatch):
    """A `-c livekit` replay over an external-redis unit prefills the redis address,
    never re-prompts the password."""
    seed_livekit_provider(repo)
    seed_meet_egress_unit(repo, hosts=("10.0.0.2",))
    lk_vault_before = (repo / "meet/prod/livekit/vault.yml").read_bytes()
    ev_vault_before = (repo / "meet/prod/egress/vault.yml").read_bytes()

    sq = accept_defaults(
        monkeypatch,
        [
            (
                "text",
                "Public domain for meet (for the LiveKit recording webhook)",
                "meet.example.org",
            ),
        ],
    )
    bootstrap.bootstrap("meet", "prod", component="livekit")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert sq.asked("text", "Redis address shared by livekit and egress")
    assert sq.asked("text", "Redis username shared by livekit and egress")

    lv = tree.load_vars("meet", "prod", "livekit")
    assert lv["st_meet_livekit_redis_address"] == "livekit-redis.example:6379"
    assert (repo / "meet/prod/livekit/vault.yml").read_bytes() == lk_vault_before
    assert (repo / "meet/prod/egress/vault.yml").read_bytes() == ev_vault_before


def test_egress_hand_edits_survive_livekit_replay(repo, monkeypatch):
    """A hand-added comment and custom var on egress's bundled vars.yml survive a later
    Enter-through `-c livekit` replay."""
    seed_creds(repo)
    script_questionary(monkeypatch, _colocated_livekit_first_run_script())
    bootstrap.bootstrap("meet", "prod", component="livekit")

    egress_vars = repo / "meet/prod/egress/vars.yml"
    hand_edited = (
        egress_vars.read_text() + "\n# hand note: keep this\nmy_custom_var: keepme\n"
    )
    egress_vars.write_text(hand_edited)

    sq = accept_defaults(monkeypatch)
    bootstrap.bootstrap("meet", "prod", component="livekit")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    final = egress_vars.read_text()
    assert "my_custom_var: keepme" in final
    assert "# hand note: keep this" in final
    assert final.count("st-cli config for meet/egress") == 1


def test_colocated_egress_follows_livekit_host_move(repo, monkeypatch):
    """A co-located egress unit follows livekit's host on a replay, then stays
    byte-identical on a further replay."""
    seed_creds(repo)
    script_questionary(monkeypatch, _colocated_livekit_first_run_script("10.0.0.1"))
    bootstrap.bootstrap("meet", "prod", component="livekit")

    sq = accept_defaults(monkeypatch, [("text", "livekit host(s)", "10.0.0.9")])
    bootstrap.bootstrap("meet", "prod", component="livekit")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    lk_hosts = (repo / "meet/prod/livekit/hosts").read_text()
    ev_hosts = (repo / "meet/prod/egress/hosts").read_text()
    assert "10.0.0.9" in lk_hosts and "10.0.0.1" not in lk_hosts
    assert "10.0.0.9" in ev_hosts and "10.0.0.1" not in ev_hosts

    ev_hosts_before = (repo / "meet/prod/egress/hosts").read_bytes()
    sq2 = accept_defaults(monkeypatch)
    bootstrap.bootstrap("meet", "prod", component="livekit")
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"
    assert (repo / "meet/prod/egress/hosts").read_bytes() == ev_hosts_before


def test_undecryptable_vault_aborts_before_any_prompt(repo, monkeypatch):
    """An unreadable vault.yml aborts before any prompt runs, and leaves the committed
    tree untouched."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "keycloak host(s)", "10.0.0.9"),
            ("text", "Public domain for keycloak", "idp.example.org"),
            ("text", "Database host", "db.example.org"),
            ("text", "Database port", "5432"),
            ("text", "Database name", "keycloak"),
            ("text", "Database user", "keycloak"),
            ("password", "KC_DB_PASSWORD", "dbsecret"),
            ("text", "Bootstrap admin username", "admin"),
            ("password", "KC_BOOTSTRAP_ADMIN_PASSWORD", "adminsecret"),
            ("confirm", "cadvisor", True),
        ],
    )
    bootstrap.bootstrap("keycloak", "prod")

    vars_before = (repo / "keycloak/prod/keycloak/vars.yml").read_text()
    vault_before = (repo / "keycloak/prod/keycloak/vault.yml").read_bytes()

    # corrupt the vault password so decryption fails
    (repo / ".vault-pass").write_text("totally-wrong-password\n")

    sq = script_questionary(monkeypatch, [])
    with pytest.raises(StCliError):
        bootstrap.bootstrap("keycloak", "prod")
    assert not sq.select_calls  # not even the first prompt was reached

    assert (repo / "keycloak/prod/keycloak/vars.yml").read_text() == vars_before
    assert (repo / "keycloak/prod/keycloak/vault.yml").read_bytes() == vault_before


def test_flagged_existing_dependency_skips_select_and_replays(
    repo, monkeypatch, tmp_path
):
    """A flagged dependency provider skips the reuse/modify select and replays its own
    questionnaire directly."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        meet_first_run_script(smtp=False, livekit=None) + livekit_script(ask_now=True),
    )
    bootstrap.bootstrap("meet", "prod")

    # a flag well above the version this unit was just stamped with
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "999.0.0", "apps": "all", "reason": "test flag", "link": ""}],
    )

    sq = accept_defaults(monkeypatch)
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    # the select was never offered; the replay was forced, not chosen
    assert not any("Bootstrap livekit now?" in msg for msg, _ in sq.select_calls)

    m = manifest.load_manifest()
    unit = next(u for u in m.units if u.component == "livekit")
    assert unit.bootstrapped_with == __version__


def test_silent_flagged_existing_dependency_replays_without_prompt(
    repo, monkeypatch, tmp_path
):
    """A flagged existing dependency's silent replay completes with zero prompts and a
    byte-identical tree."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        meet_first_run_script(smtp=False, livekit=None) + livekit_script(ask_now=True),
    )
    bootstrap.bootstrap("meet", "prod")

    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": __version__, "apps": ["meet"], "reason": "test flag", "link": ""}],
    )
    m = manifest.load_manifest()
    for u in m.units:
        u.bootstrapped_with = "0.0.1"
    manifest.save_manifest(m)

    core_vars_before = (repo / "meet/prod/meet/vars.yml").read_text()
    core_vault_before = (repo / "meet/prod/meet/vault.yml").read_bytes()
    lk_vars_before = (repo / "meet/prod/livekit/vars.yml").read_text()
    lk_vault_before = (repo / "meet/prod/livekit/vault.yml").read_bytes()
    eg_vars_before = (repo / "meet/prod/egress/vars.yml").read_text()

    sq = script_questionary(monkeypatch, [])
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.SILENT)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert not sq.select_calls, f"a select fired: {sq.select_calls}"

    assert (repo / "meet/prod/meet/vars.yml").read_text() == core_vars_before
    assert (repo / "meet/prod/meet/vault.yml").read_bytes() == core_vault_before
    assert (repo / "meet/prod/livekit/vars.yml").read_text() == lk_vars_before
    assert (repo / "meet/prod/livekit/vault.yml").read_bytes() == lk_vault_before
    assert (repo / "meet/prod/egress/vars.yml").read_text() == eg_vars_before

    m2 = manifest.load_manifest()
    for component in ("meet", "livekit", "egress"):
        unit = next(u for u in m2.units if u.component == component)
        assert unit.bootstrapped_with == __version__


def test_unflagged_existing_dependency_offers_reuse_modify_menu(repo, monkeypatch):
    """An existing, unflagged dependency offers only Reuse or Modify, never the
    fresh-unit options."""
    seed_livekit_provider(repo)
    sq = script_questionary(
        monkeypatch,
        meet_first_run_script(
            smtp=False,
            db_mode="url",
            secret_backend=False,
            livekit="Reuse existing in the repo",
        )
        + [("confirm", "egress", True)],  # egress cadvisor (created on reuse)
    )
    bootstrap.bootstrap("meet", "prod")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    dep_offers = [c for msg, c in sq.select_calls if "Bootstrap livekit now?" in msg]
    assert dep_offers == [
        ["Reuse existing in the repo", "Modify (replay the questionnaire)"]
    ]


def test_reuse_writes_nothing_and_warns_pending_flag(
    repo, monkeypatch, tmp_path, mocker
):
    """Picking Reuse on the top-level select writes nothing, keeps the stamp, and warns
    about a pending flag."""
    seed_meet_unit(repo)
    set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": "999.0.0",
                "apps": "all",
                "reason": "test flag",
                "link": "https://example.org/flag",
            }
        ],
    )

    vars_before = (repo / "meet/prod/meet/vars.yml").read_bytes()
    hosts_before = (repo / "meet/prod/meet/hosts").read_bytes()
    stamp_before = next(
        u for u in manifest.load_manifest().units if u.component == "meet"
    ).bootstrapped_with

    warn_spy = mocker.patch.object(bootstrap.ui, "warn")
    sq = script_questionary(
        monkeypatch,
        [
            (
                "select",
                "meet/prod is already bootstrapped — what do you want to do?",
                "Reuse — keep everything as-is (skip the questionnaire)",
            ),
        ],
    )
    bootstrap.bootstrap("meet", "prod")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    assert (repo / "meet/prod/meet/vars.yml").read_bytes() == vars_before
    assert (repo / "meet/prod/meet/hosts").read_bytes() == hosts_before
    stamp_after = next(
        u for u in manifest.load_manifest().units if u.component == "meet"
    ).bootstrapped_with
    assert stamp_after == stamp_before

    warned = [c.args[0] for c in warn_spy.call_args_list]
    assert any("still pending" in msg and "deploy" in msg for msg in warned)


def test_ask_rebootstrap_action_warns_dependency_flags_too(monkeypatch, mocker):
    """`_ask_rebootstrap_action` warns about every flagged component of `(app, env)`,
    not only the targeted one."""
    from st_cli.core.models import UpgradeNeed

    flagged = {
        "meet": UpgradeNeed("meet", "prod", "meet", "0.3.0", "core reason", "l1"),
        "livekit": UpgradeNeed("meet", "prod", "livekit", "0.4.0", "dep reason", "l2"),
    }
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")
    script_questionary(
        monkeypatch,
        [
            (
                "select",
                "meet/prod is already bootstrapped — what do you want to do?",
                "Reuse — keep everything as-is (skip the questionnaire)",
            ),
        ],
    )
    action = bootstrap._ask_rebootstrap_action("meet", "prod", flagged)
    assert action == bootstrap.ReplayAction.REUSE

    warned = [c.args[0] for c in warn_spy.call_args_list]
    assert any("livekit" in msg and "dep reason" in msg for msg in warned)
    assert any("meet/prod/meet" in msg and "core reason" in msg for msg in warned)


def test_override_declined_leaves_tree_untouched(repo, monkeypatch):
    """Picking Override then declining the destructive confirm raises and touches
    nothing."""
    seed_creds(repo)
    script_questionary(monkeypatch, meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")

    vars_before = (repo / "meet/prod/meet/vars.yml").read_text()
    vault_before = (repo / "meet/prod/meet/vault.yml").read_bytes()

    sq = script_questionary(
        monkeypatch,
        [
            (
                "select",
                "meet/prod is already bootstrapped — what do you want to do?",
                "Override — rebuild from scratch (DESTRUCTIVE: regenerates secrets)",
            ),
            (
                "confirm",
                "Override meet/prod: this rebuilds the core from scratch",
                False,
            ),
        ],
    )
    with pytest.raises(StCliError, match="override cancelled"):
        bootstrap.bootstrap("meet", "prod")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    assert (repo / "meet/prod/meet/vars.yml").read_text() == vars_before
    assert (repo / "meet/prod/meet/vault.yml").read_bytes() == vault_before


def test_override_accepted_regenerates_secrets_and_drops_hand_edits(repo, monkeypatch):
    """Accepting Override rebuilds the core from an empty tree: a fresh secret key and
    hand-added vars dropped."""
    seed_creds(repo)
    script_questionary(monkeypatch, meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")

    vault_before = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "meet"))

    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_my_custom_var"] = "custom-value"
    tree.save_vars("meet", "prod", "meet", data)

    # The secret-backend choice is already persisted, so, same as every other
    # rebootstrap script in this file, the "Secret backend:" select is not
    # asked again; the rest of the fresh questionnaire is identical to a
    # from-scratch run since OVERRIDE seeds nothing.
    override_script = meet_first_run_script(smtp=False)[1:]
    sq = script_questionary(
        monkeypatch,
        [
            (
                "select",
                "meet/prod is already bootstrapped — what do you want to do?",
                "Override — rebuild from scratch (DESTRUCTIVE: regenerates secrets)",
            ),
            (
                "confirm",
                "Override meet/prod: this rebuilds the core from scratch",
                True,
            ),
        ]
        + override_script,
    )
    bootstrap.bootstrap("meet", "prod")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    vault_after = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "meet"))
    assert (
        vault_after["vault_django_secret_key"]
        != vault_before["vault_django_secret_key"]
    )

    vars_after = tree.load_vars("meet", "prod", "meet")
    assert "st_meet_my_custom_var" not in vars_after

    m = manifest.load_manifest()
    unit = next(u for u in m.units if u.component == "meet")
    assert unit.bootstrapped_with == __version__


def test_override_core_forces_dependency_replay_keeps_constructed_values(
    repo, monkeypatch
):
    """Overriding the messages core forces every dependency straight to a replay,
    rebuilding its wiring."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        messages_first_run_script()
        + [
            ("select", "Bootstrap pymta now?", "Yes — bootstrap now"),
            ("text", "pymta host(s)", "10.0.0.7"),
            ("text", "PYMTA_SMTP_HOSTNAME", "mx.example.org"),
            ("confirm", "cadvisor", True),
            ("select", "Bootstrap mpa now?", "Yes — bootstrap now"),
            ("text", "mpa host(s)", "10.0.0.8"),
            ("confirm", "cadvisor", True),
            ("select", "Bootstrap socks-proxy now?", "Yes — bootstrap now"),
            ("text", "socks-proxy host(s)", "10.0.0.6"),
            ("text", "PROXY_EXTERNAL", "eth0"),
            ("text", "PROXY_INTERNAL_PORT", "50405"),
            ("confirm", "cadvisor", True),
        ],
    )
    bootstrap.bootstrap("messages", "prod")

    pymta_vault_before = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "pymta")
    )
    mpa_vault_before = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "mpa")
    )
    sp_vault_before = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "socks-proxy")
    )

    # OVERRIDE seeds nothing, so the core questionnaire is fresh (no defaults
    # to accept), same values as the first run, just retyped. Every
    # dependency, in contrast, is pre-filled from its own (untouched)
    # committed tree, using ACCEPT_DEFAULT, and offers NO select at all.
    sq = script_questionary(
        monkeypatch,
        [
            (
                "select",
                "messages/prod is already bootstrapped — what do you want to do?",
                "Override — rebuild from scratch (DESTRUCTIVE: regenerates secrets)",
            ),
            (
                "confirm",
                "Override messages/prod: this rebuilds the core from scratch",
                True,
            ),
            ("text", "messages host(s)", "10.0.0.4"),
            ("text", "workers (leave blank", ""),
            ("text", "Public domain for messages", "messages.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://messages"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", "msg-imports"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", "impkey"),
            ("password", "STORAGE_MESSAGE_IMPORTS_SECRET_KEY", "impsecret"),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ""),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
            ("confirm", "Enable blobs offloading", False),
            ("text", "OPENSEARCH_URL", "http://opensearch:9200"),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", "mail.example.org"),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "messages-client-id"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            ("select", "Outbound mail mode", "direct"),
            ("confirm", "cadvisor", True),
            # pymta: no select; forced straight to the deploy branch.
            ("text", "pymta host(s)", ACCEPT_DEFAULT),
            ("text", "PYMTA_SMTP_HOSTNAME", ACCEPT_DEFAULT),
            ("confirm", "cadvisor", ACCEPT_DEFAULT),
            # mpa: no select either.
            ("text", "mpa host(s)", ACCEPT_DEFAULT),
            ("confirm", "cadvisor", ACCEPT_DEFAULT),
            # socks-proxy: no select either.
            ("text", "socks-proxy host(s)", ACCEPT_DEFAULT),
            ("text", "PROXY_EXTERNAL", ACCEPT_DEFAULT),
            ("text", "PROXY_INTERNAL_PORT", ACCEPT_DEFAULT),
            ("confirm", "cadvisor", ACCEPT_DEFAULT),
        ],
    )
    bootstrap.bootstrap("messages", "prod")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    core_vars = (repo / "messages/prod/messages/vars.yml").read_text()
    assert (
        "MTA_OUT_DIRECT_PROXIES=socks5s://{{ vault_proxy_users }}@10.0.0.6:50405"
        in core_vars
    )
    assert (
        'SPAM_CONFIG={"rspamd_url": "http://10.0.0.8:{{ st_messages_mpa_caddy_port }}", '
        '"rspamd_auth": "Bearer {{ vault_mpa_auth_bearer }}", '
        '"inbound_auth": "rspamd"}' in core_vars
    )

    core_vault_after = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "messages")
    )
    assert "vault_mpa_auth_bearer" in core_vault_after
    assert (
        core_vault_after["vault_mpa_auth_bearer"]
        == mpa_vault_before["vault_mpa_auth_bearer"]
    )
    assert "vault_proxy_users" in core_vault_after
    assert core_vault_after["vault_proxy_users"] == sp_vault_before["vault_proxy_users"]

    # the provider secrets themselves are never rotated by the override.
    pymta_vault_after = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "pymta")
    )
    mpa_vault_after = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "mpa"))
    sp_vault_after = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "socks-proxy")
    )
    assert mpa_vault_after == mpa_vault_before
    assert sp_vault_after == sp_vault_before
    # pymta's own MDA_API_SECRET DOES change: it mirrors the core's, and the
    # core's is a generated secret the override regenerates.
    assert pymta_vault_after != pymta_vault_before
    assert (
        pymta_vault_after["vault_mda_api_secret"]
        == core_vault_after["vault_mda_api_secret"]
    )


def test_override_core_recorded_external_dep_reprompts_constructed_value(
    repo, monkeypatch
):
    """Overriding the core re-prompts a recorded external dependency's constructed
    value, such as SPAM_CONFIG."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        messages_first_run_script()
        + [
            ("select", "Bootstrap pymta now?", "No — bootstrap later"),
            (
                "select",
                "Bootstrap mpa now?",
                "Already deployed (enter URL + keys)",
            ),
            (
                "password",
                "SPAM_CONFIG (JSON for the external mpa)",
                '{"rspamd_url": "https://ext-mpa.example.org", "rspamd_auth": "Bearer extbearer", "inbound_auth": "rspamd"}',
            ),
            ("select", "Bootstrap socks-proxy now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("messages", "prod")

    m = manifest.load_manifest()
    mpa_unit = next(u for u in m.units if u.component == "mpa")
    assert mpa_unit.mode == "external"

    sq = script_questionary(
        monkeypatch,
        [
            (
                "select",
                "messages/prod is already bootstrapped — what do you want to do?",
                "Override — rebuild from scratch (DESTRUCTIVE: regenerates secrets)",
            ),
            (
                "confirm",
                "Override messages/prod: this rebuilds the core from scratch",
                True,
            ),
            ("text", "messages host(s)", "10.0.0.4"),
            ("text", "workers (leave blank", ""),
            ("text", "Public domain for messages", "messages.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://messages"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", "msg-imports"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", "impkey"),
            ("password", "STORAGE_MESSAGE_IMPORTS_SECRET_KEY", "impsecret"),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ""),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
            ("confirm", "Enable blobs offloading", False),
            ("text", "OPENSEARCH_URL", "http://opensearch:9200"),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", "mail.example.org"),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "messages-client-id"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            ("select", "Outbound mail mode", "direct"),
            ("confirm", "cadvisor", True),
            ("select", "Bootstrap pymta now?", "No — bootstrap later"),
            # mpa is recorded external; the default is "Keep external
            # (recorded)", picked via ACCEPT_DEFAULT.
            ("select", "Bootstrap mpa now?", ACCEPT_DEFAULT),
            (
                "password",
                "SPAM_CONFIG (JSON for the external mpa)",
                '{"rspamd_url": "https://ext-mpa-2.example.org", "rspamd_auth": "Bearer newbearer", "inbound_auth": "rspamd"}',
            ),
            ("select", "Bootstrap socks-proxy now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("messages", "prod")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    core_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "messages"))
    assert core_vault["vault_spam_config"] == (
        '{"rspamd_url": "https://ext-mpa-2.example.org", '
        '"rspamd_auth": "Bearer newbearer", "inbound_auth": "rspamd"}'
    )


def test_silent_replay_skips_top_level_select_and_prints_stats(
    repo, monkeypatch, mocker
):
    """`replay=SILENT` skips the top-level select, auto-accepts every prompt, and prints
    the stats line."""
    seed_creds(repo)
    script_questionary(monkeypatch, meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")

    info_spy = mocker.patch.object(bootstrap.ui, "info")
    sq = script_questionary(monkeypatch, [])
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.SILENT)

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert not any("is already bootstrapped" in msg for msg, _ in sq.select_calls)
    stats_lines = [
        c.args[0] for c in info_spy.call_args_list if "recovered answer" in c.args[0]
    ]
    assert len(stats_lines) == 1


def test_hashi_recovered_shared_secret_not_reprompted(repo, monkeypatch):
    """A `-c livekit` replay under hashi_vault reuses the committed lookup refs, never
    asking a fresh term."""
    seed_hashi_livekit_provider(repo)
    ref_key = tree.load_vars("meet", "prod", "livekit")["st_meet_livekit_api_key"]
    ref_secret = tree.load_vars("meet", "prod", "livekit")["st_meet_livekit_api_secret"]

    sq = accept_defaults(
        monkeypatch,
        [
            (
                "text",
                "Public domain for meet (for the LiveKit recording webhook)",
                "meet.example.org",
            ),
        ],
    )
    bootstrap.bootstrap("meet", "prod", component="livekit")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert not sq.asked("text", "st_meet_livekit_api_key")
    assert not sq.asked("text", "st_meet_livekit_api_secret")

    lk_vars = tree.load_vars("meet", "prod", "livekit")
    assert lk_vars["st_meet_livekit_api_key"] == ref_key
    assert lk_vars["st_meet_livekit_api_secret"] == ref_secret


def test_external_recorded_unit_stays_external_on_full_replay(repo, monkeypatch):
    """A full Enter-through replay over a recorded-external unit keeps it external and
    its tree untouched."""
    seed_external_livekit_with_leftover_tree(repo)
    lk_vars_before = (repo / "meet/prod/livekit/vars.yml").read_bytes()
    lk_vault_before = (repo / "meet/prod/livekit/vault.yml").read_bytes()
    lk_hosts_before = (repo / "meet/prod/livekit/hosts").read_bytes()

    sq = accept_defaults(monkeypatch)
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    dep_offers = [c for msg, c in sq.select_calls if "Bootstrap livekit now?" in msg]
    assert dep_offers == [
        [
            "Keep external (recorded)",
            "Re-enter external values (URL + keys)",
            "Bootstrap now (manage locally)",
        ]
    ]

    m = manifest.load_manifest()
    unit = next(u for u in m.units if u.component == "livekit")
    assert unit.mode == "external"

    assert (repo / "meet/prod/livekit/vars.yml").read_bytes() == lk_vars_before
    assert (repo / "meet/prod/livekit/vault.yml").read_bytes() == lk_vault_before
    assert (repo / "meet/prod/livekit/hosts").read_bytes() == lk_hosts_before


def test_external_recorded_unit_wire_only_skips_menu_and_stale_tree(repo, monkeypatch):
    """A wire-only `-c meet` run over a recorded-external unit offers no select and
    round-trips byte-identical."""
    seed_external_livekit_with_leftover_tree(repo)
    core_vars_before = (repo / "meet/prod/meet/vars.yml").read_text()
    core_vault_before = (repo / "meet/prod/meet/vault.yml").read_bytes()

    sq = accept_defaults(monkeypatch)
    bootstrap.bootstrap(
        "meet", "prod", component="meet", replay=bootstrap.ReplayAction.MODIFY
    )
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    assert not any("Bootstrap livekit now?" in msg for msg, _ in sq.select_calls)
    assert (repo / "meet/prod/meet/vars.yml").read_text() == core_vars_before
    assert (repo / "meet/prod/meet/vault.yml").read_bytes() == core_vault_before


def test_external_redo_reprompts_values(repo, monkeypatch):
    """Choosing "Re-enter external values" re-asks every shared rule and keeps the unit
    recorded external."""
    seed_external_livekit_with_leftover_tree(repo)

    sq = accept_defaults(
        monkeypatch,
        [
            (
                "select",
                "Bootstrap livekit now?",
                "Re-enter external values (URL + keys)",
            ),
            ("password", "LIVEKIT_API_KEY", "new-external-key"),
            ("password", "LIVEKIT_API_SECRET", "new-external-secret"),
            (
                "text",
                "LiveKit domain (e.g. livekit.example.org)",
                "new-external-livekit.example.org",
            ),
        ],
    )
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    m = manifest.load_manifest()
    unit = next(u for u in m.units if u.component == "livekit")
    assert unit.mode == "external"

    core_vars = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "LIVEKIT_API_URL=wss://new-external-livekit.example.org" in core_vars

    core_vault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "meet"))
    assert core_vault["vault_livekit_api_key"] == "new-external-key"
    assert core_vault["vault_livekit_api_secret"] == "new-external-secret"


def test_skip_then_external_adopt_reprompts_empty_recovered_shared_values(
    repo, monkeypatch
):
    """Adopting a skipped, empty-recovered livekit dependency as external still asks
    every shared value."""
    seed_creds(repo)
    script_questionary(monkeypatch, meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")

    core_vars_before = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "LIVEKIT_API_KEY=" in core_vars_before
    assert "LIVEKIT_API_SECRET=" in core_vars_before
    assert "LIVEKIT_API_URL=" in core_vars_before
    core_vault_before = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "meet"))
    assert "vault_livekit_api_key" not in core_vault_before

    sq = accept_defaults(
        monkeypatch,
        [
            (
                "select",
                "Bootstrap livekit now?",
                "Already deployed (enter URL + keys)",
            ),
            ("password", "LIVEKIT_API_KEY", "adopted-key"),
            ("password", "LIVEKIT_API_SECRET", "adopted-secret"),
            (
                "text",
                "LiveKit domain (e.g. livekit.example.org)",
                "adopted-livekit.example.org",
            ),
        ],
    )
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    m = manifest.load_manifest()
    unit = next(u for u in m.units if u.component == "livekit")
    assert unit.mode == "external"

    core_vars_after = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "LIVEKIT_API_URL=wss://adopted-livekit.example.org" in core_vars_after

    core_vault_after = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "meet"))
    assert core_vault_after["vault_livekit_api_key"] == "adopted-key"
    assert core_vault_after["vault_livekit_api_secret"] == "adopted-secret"


def test_smtp_confirm_uses_review_wording_when_recovered(monkeypatch):
    """A recovered DJANGO_EMAIL_HOST makes the SMTP confirm ask to review, not to set up
    from scratch."""
    answers = {"DJANGO_EMAIL_HOST": "smtp.example.org"}
    backend = AnsibleVaultBackend()
    sq = script_questionary(
        monkeypatch,
        [("confirm", "SMTP is configured — review its settings?", False)],
    )
    bootstrap._ask_email(answers, backend, "meet", "meet")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"


def test_smtp_confirm_keeps_first_run_wording_when_unconfigured(monkeypatch):
    """With no recovered DJANGO_EMAIL_HOST, the SMTP confirm keeps asking as a fresh
    setup question."""
    answers: dict = {}
    backend = AnsibleVaultBackend()
    sq = script_questionary(
        monkeypatch,
        [("confirm", "Configure transactional email (SMTP) settings?", False)],
    )
    bootstrap._ask_email(answers, backend, "meet", "meet")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"


def test_blobs_confirm_uses_review_wording_when_recovered(monkeypatch):
    """A recovered truthy MESSAGES_BLOBS_OFFLOAD_ENABLED makes the confirm ask to
    review, not set up offload."""
    answers = {"MESSAGES_BLOBS_OFFLOAD_ENABLED": "1"}
    backend = AnsibleVaultBackend()
    sq = script_questionary(
        monkeypatch,
        [
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", "msg-imports"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", "impkey"),
            ("password", "STORAGE_MESSAGE_IMPORTS_SECRET_KEY", "impsecret"),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ""),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
            ("confirm", "Blobs offloading is enabled — review its settings?", False),
        ],
    )
    bootstrap._ask_messages_storage(answers, backend, "messages")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"


def test_relay_to_direct_switch_warns_about_leftover_relay_lines(monkeypatch, mocker):
    """Switching a recovered relay config to direct warns about the leftover relay lines
    it cannot delete."""
    answers = {"MTA_OUT_MODE": "relay"}
    backend = AnsibleVaultBackend()
    script_questionary(monkeypatch, [("select", "Outbound mail mode", "direct")])
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")

    bootstrap._ask_messages_outbound(answers, backend, "messages")

    warn_spy.assert_called_once()
    msg = warn_spy.call_args[0][0]
    assert "MTA_OUT_MODE" in msg
    assert "MTA_OUT_RELAY_*" in msg
    assert "st_messages_env" in msg


def test_direct_to_direct_switch_does_not_warn(monkeypatch, mocker):
    """Staying on direct outbound mode with no recovered relay is a no-op: no cleanup
    warning fires."""
    answers: dict = {}
    backend = AnsibleVaultBackend()
    script_questionary(monkeypatch, [("select", "Outbound mail mode", "direct")])
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")

    bootstrap._ask_messages_outbound(answers, backend, "messages")

    warn_spy.assert_not_called()


def test_blank_relay_username_clears_password_and_warns(monkeypatch, mocker):
    """Blanking a recovered relay username also drops the recovered relay password,
    warning once each."""
    answers = {
        "MTA_OUT_MODE": "relay",
        "MTA_OUT_RELAY_HOST": "smtp.example.org:587",
        "MTA_OUT_RELAY_USERNAME": "relayuser",
        "MTA_OUT_RELAY_PASSWORD": "{{ vault_mta_out_relay_password }}",
    }
    backend = AnsibleVaultBackend()
    sq = accept_defaults(
        monkeypatch,
        [("text", "MTA_OUT_RELAY_USERNAME (optional, blank = no auth)", "")],
    )
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")

    bootstrap._ask_messages_outbound(answers, backend, "messages")

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert "MTA_OUT_RELAY_USERNAME" not in answers
    assert "MTA_OUT_RELAY_PASSWORD" not in answers
    assert warn_spy.call_count == 2


def test_db_mode_switch_warns_both_directions(monkeypatch, mocker):
    """Switching DB_* to DATABASE_URL, or back, warns about the old shape's leftover
    committed lines."""
    backend = AnsibleVaultBackend()

    # discrete -> DATABASE_URL
    answers = {"DB_HOST": "db.example.org"}
    script_questionary(
        monkeypatch,
        [
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://meet"),
        ],
    )
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")
    bootstrap._ask_db(answers, backend, "meet", "meet")
    warn_spy.assert_called_once()
    msg = warn_spy.call_args[0][0]
    assert "DB_HOST" in msg
    assert "DB_PASSWORD" in msg

    # DATABASE_URL -> discrete
    answers2 = {"DATABASE_URL": "{{ vault_database_url }}"}
    script_questionary(
        monkeypatch,
        [
            ("select", "Database configuration:", "discrete (DB_*)"),
            ("text", "DB_HOST", "db2.example.org"),
            ("text", "DB_NAME", "meet"),
            ("text", "DB_USER", "meet"),
            ("password", "DB_PASSWORD", "pw"),
            ("text", "DB_PORT", "5432"),
        ],
    )
    warn_spy2 = mocker.patch.object(bootstrap.ui, "warn")
    bootstrap._ask_db(answers2, backend, "meet", "meet")
    warn_spy2.assert_called_once()
    msg2 = warn_spy2.call_args[0][0]
    assert "DATABASE_URL" in msg2


def test_db_mode_kept_same_does_not_warn(monkeypatch, mocker):
    """Keeping the same DB mode never warns."""
    backend = AnsibleVaultBackend()
    answers = {"DB_HOST": "db.example.org"}
    script_questionary(
        monkeypatch,
        [
            ("select", "Database configuration:", "discrete (DB_*)"),
            ("text", "DB_HOST", "db.example.org"),
            ("text", "DB_NAME", "meet"),
            ("text", "DB_USER", "meet"),
            ("password", "DB_PASSWORD", "pw"),
            ("text", "DB_PORT", "5432"),
        ],
    )
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")
    bootstrap._ask_db(answers, backend, "meet", "meet")
    warn_spy.assert_not_called()


def test_db_mode_total_gap_surfaces_select_even_in_silent_mode(monkeypatch):
    """A total DB recovery gap surfaces the mode select even under a silent replay,
    instead of defaulting silently."""
    from st_cli.core import prompts

    backend = AnsibleVaultBackend()
    answers: dict = {}
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Database configuration:", "discrete (DB_*)"),
            ("text", "DB_HOST", "db.example.org"),
            ("text", "DB_NAME", "meet"),
            ("text", "DB_USER", "meet"),
            ("password", "DB_PASSWORD", "pw"),
            ("text", "DB_PORT", "5432"),
        ],
    )
    with prompts.silent_replay():
        bootstrap._ask_db(answers, backend, "meet", "meet")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert answers["DB_HOST"] == "db.example.org"


def test_egress_redis_password_blank_legacy_store_is_reprompted(repo, monkeypatch):
    """An empty stored legacy redis password is not read as decided: the prompt stays
    reachable."""
    seed_creds(repo)
    meta = appmeta.load_app("meet")
    vp = paths.vault_path("meet", "prod", "livekit")
    vp.parent.mkdir(parents=True, exist_ok=True)
    with vp.open("w", encoding="utf-8") as fh:
        tree.yaml().dump({"st_meet_livekit_redis_password": ""}, fh)
    vault.encrypt_file(vp)

    backend = AnsibleVaultBackend()
    sq = script_questionary(
        monkeypatch,
        [
            (
                "password",
                "Redis password shared by livekit and egress",
                "freshly-typed-pass",
            ),
        ],
    )
    result = bootstrap._resolve_egress_redis_password(
        meta, "prod", backend, reuse_disk=True
    )

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert result == "freshly-typed-pass"


def test_egress_redis_password_not_reused_when_reuse_disk_false(repo, monkeypatch):
    """`reuse_disk=False` does not carry the old server's on-disk redis password
    forward: the prompt fires."""
    seed_creds(repo)
    meta = appmeta.load_app("meet")
    vp = paths.vault_path("meet", "prod", "livekit")
    vp.parent.mkdir(parents=True, exist_ok=True)
    with vp.open("w", encoding="utf-8") as fh:
        tree.yaml().dump({"st_meet_livekit_redis_password": "old-server-pass"}, fh)
    vault.encrypt_file(vp)

    backend = AnsibleVaultBackend()
    sq = script_questionary(
        monkeypatch,
        [
            (
                "password",
                "Redis password shared by livekit and egress",
                "new-server-pass",
            ),
        ],
    )
    result = bootstrap._resolve_egress_redis_password(
        meta, "prod", backend, reuse_disk=False
    )

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert result == "new-server-pass"


class _Stop(Exception):
    """Raised by the fake _ask below to short-circuit _ask_core once the
    domain prompt's default is captured."""


def _capture_domain_default(monkeypatch, captured: dict):
    real_ask = bootstrap._ask

    def _fake_ask(prompt, default="", **kwargs):
        if prompt.startswith("Public domain"):
            captured["default"] = default
            raise _Stop
        return real_ask(prompt, default, **kwargs)

    monkeypatch.setattr(bootstrap, "_ask", _fake_ask)


def test_domain_prefill_skips_comma_separated_allowed_hosts(monkeypatch):
    """A hand-edited, comma-separated DJANGO_ALLOWED_HOSTS is never used as the DOMAIN
    pre-fill."""
    meta = appmeta.load_app("messages")
    backend = AnsibleVaultBackend()
    seed = {"DJANGO_ALLOWED_HOSTS": "messages.example.org,other.example.org"}
    captured: dict = {}
    _capture_domain_default(monkeypatch, captured)

    with pytest.raises(_Stop):
        bootstrap._ask_core(meta, backend, seed)

    assert captured["default"] == ""


def test_domain_prefill_keeps_single_host_allowed_hosts(monkeypatch):
    """A single-host DJANGO_ALLOWED_HOSTS, with no comma, still pre-fills DOMAIN."""
    meta = appmeta.load_app("messages")
    backend = AnsibleVaultBackend()
    seed = {"DJANGO_ALLOWED_HOSTS": "messages.example.org"}
    captured: dict = {}
    _capture_domain_default(monkeypatch, captured)

    with pytest.raises(_Stop):
        bootstrap._ask_core(meta, backend, seed)

    assert captured["default"] == "messages.example.org"


def test_blobs_encrypt_keys_multislot_survives_replay(repo, monkeypatch):
    """A hand-appended rotation key slot in MESSAGES_BLOBS_ENCRYPT_KEYS survives an
    Enter-through replay."""
    seed_creds(repo)
    sq1 = script_questionary(
        monkeypatch,
        messages_first_run_script(blobs_offload=True)
        + [
            ("select", "Bootstrap pymta now?", "No — bootstrap later"),
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
            ("select", "Bootstrap socks-proxy now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("messages", "prod")
    assert not sq1._scripts, f"unconsumed scripts: {sq1._scripts}"

    core_vars_path = repo / "messages/prod/messages/vars.yml"
    old_line = (
        'MESSAGES_BLOBS_ENCRYPT_KEYS={"1": {"algo": "aes-gcm", '
        '"secret": "{{ vault_messages_blobs_encrypt_key }}", "active": true}}'
    )
    text = core_vars_path.read_text()
    assert old_line in text
    new_line = (
        old_line[:-1] + ', "2": {"algo": "aes-gcm", '
        '"secret": "hand-added-rotation-key", "active": false}}'
    )
    core_vars_path.write_text(text.replace(old_line, new_line))

    core_vars_before = core_vars_path.read_text()
    core_vault_before = (repo / "messages/prod/messages/vault.yml").read_bytes()
    assert '"2": {"algo": "aes-gcm"' in core_vars_before

    sq2 = accept_defaults(
        monkeypatch,
        [
            ("select", "Database configuration:", "DATABASE_URL"),
            ("select", "Outbound mail mode", "direct"),
            ("select", "Bootstrap pymta now?", "No — bootstrap later"),
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
            ("select", "Bootstrap socks-proxy now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("messages", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    assert core_vars_path.read_text() == core_vars_before
    assert (repo / "messages/prod/messages/vault.yml").read_bytes() == core_vault_before


def test_hashi_recovered_blobs_encrypt_keys_not_reprompted(monkeypatch):
    """Under hashi_vault, a recovered MESSAGES_BLOBS_ENCRYPT_KEYS does not trigger a
    fresh lookup-term prompt."""
    encrypt_keys_json = (
        '{"1": {"algo": "aes-gcm", "secret": '
        "\"{{ lookup('community.hashi_vault.hashi_vault', "
        "'kv/data/messages:MESSAGES_BLOBS_ENCRYPT_KEY') }}\", "
        '"active": true}}'
    )
    answers = {
        "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL": "https://s3.example.org",
        "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME": "msg-imports",
        "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY": "impkey",
        "STORAGE_MESSAGE_IMPORTS_SECRET_KEY": (
            "{{ lookup('community.hashi_vault.hashi_vault', "
            "'kv/data/messages:STORAGE_MESSAGE_IMPORTS_SECRET_KEY') }}"
        ),
        "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY": "3600",
        "MESSAGES_BLOBS_OFFLOAD_ENABLED": "1",
        "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL": "https://s3.example.org",
        "STORAGE_MESSAGE_BLOBS_BUCKET_NAME": "msg-blobs",
        "STORAGE_MESSAGE_BLOBS_ACCESS_KEY": "blobkey",
        "STORAGE_MESSAGE_BLOBS_SECRET_KEY": (
            "{{ lookup('community.hashi_vault.hashi_vault', "
            "'kv/data/messages:STORAGE_MESSAGE_BLOBS_SECRET_KEY') }}"
        ),
        "MESSAGES_BLOBS_ENCRYPT_KEYS": encrypt_keys_json,
    }
    backend = HashiVaultBackend("messages")
    sq = accept_defaults(
        monkeypatch,
        [
            (
                "confirm",
                "Blobs offloading is enabled — review its settings?",
                True,
            ),
        ],
    )
    bootstrap._ask_messages_storage(answers, backend, "messages")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert not sq.asked("text", "MESSAGES_BLOBS_ENCRYPT_KEY")
    assert answers["MESSAGES_BLOBS_ENCRYPT_KEYS"] == encrypt_keys_json


def test_custom_oidc_endpoints_survive_replay(monkeypatch):
    """A recovered "custom" OIDC provider's hand-edited OIDC_OP_* endpoints survive a
    replay untouched."""
    answers = {
        "OIDC_OP_URL": "https://idp.example.org",
        "OIDC_OP_JWKS_ENDPOINT": "https://idp.example.org/jwks",
        "OIDC_OP_AUTHORIZATION_ENDPOINT": "https://idp.example.org/auth",
        "OIDC_OP_TOKEN_ENDPOINT": "https://idp.example.org/token",
        "OIDC_OP_USER_ENDPOINT": "https://idp.example.org/userinfo",
        "OIDC_OP_LOGOUT_ENDPOINT": "https://idp.example.org/logout",
        "OIDC_OP_INTROSPECTION_ENDPOINT": "https://idp.example.org/introspect",
        "OIDC_RP_CLIENT_ID": "client-id",
        "OIDC_RP_CLIENT_SECRET": "{{ vault_oidc_rp_client_secret }}",
    }
    backend = AnsibleVaultBackend()
    accept_defaults(monkeypatch, [("select", "Identity provider:", "custom")])
    bootstrap._ask_oidc(answers, backend, "meet")

    assert answers["OIDC_OP_URL"] == "https://idp.example.org"
    assert answers["OIDC_OP_JWKS_ENDPOINT"] == "https://idp.example.org/jwks"
    assert answers["OIDC_OP_AUTHORIZATION_ENDPOINT"] == "https://idp.example.org/auth"
    assert answers["OIDC_OP_TOKEN_ENDPOINT"] == "https://idp.example.org/token"
    assert answers["OIDC_OP_USER_ENDPOINT"] == "https://idp.example.org/userinfo"
    assert answers["OIDC_OP_LOGOUT_ENDPOINT"] == "https://idp.example.org/logout"
    assert (
        answers["OIDC_OP_INTROSPECTION_ENDPOINT"]
        == "https://idp.example.org/introspect"
    )


def test_switch_to_custom_warns_and_keeps_endpoints(monkeypatch, mocker):
    """Switching the identity provider to custom from keycloak warns and keeps the
    committed OIDC_OP_* lines."""
    answers = {
        "OIDC_OP_URL": "https://idp.example.org/realms/master",
        "OIDC_OP_JWKS_ENDPOINT": (
            "https://idp.example.org/realms/master/protocol/openid-connect/certs"
        ),
        "OIDC_OP_AUTHORIZATION_ENDPOINT": (
            "https://idp.example.org/realms/master/protocol/openid-connect/auth"
        ),
        "OIDC_RP_CLIENT_ID": "client-id",
        "OIDC_RP_CLIENT_SECRET": "{{ vault_oidc_rp_client_secret }}",
    }
    backend = AnsibleVaultBackend()
    script_questionary(
        monkeypatch,
        [
            ("select", "Identity provider:", "custom"),
            ("text", "Custom OIDC issuer base URL (optional)", ""),
            ("text", "OIDC_RP_CLIENT_ID", ACCEPT_DEFAULT),
        ],
    )
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")

    bootstrap._ask_oidc(answers, backend, "meet")

    warn_spy.assert_called_once()
    msg = warn_spy.call_args[0][0]
    assert "keycloak" in msg
    assert "custom" in msg
    assert answers["OIDC_OP_JWKS_ENDPOINT"] == (
        "https://idp.example.org/realms/master/protocol/openid-connect/certs"
    )
    assert answers["OIDC_OP_AUTHORIZATION_ENDPOINT"] == (
        "https://idp.example.org/realms/master/protocol/openid-connect/auth"
    )


def test_keycloak_hand_edited_token_endpoint_survives_enter_through(monkeypatch):
    """An Enter-through keycloak OIDC replay does not overwrite a hand-edited
    OIDC_OP_TOKEN_ENDPOINT."""
    base = "https://idp.example.org/realms/master/protocol/openid-connect"
    answers = {
        "OIDC_OP_URL": "https://idp.example.org/realms/master",
        "OIDC_OP_JWKS_ENDPOINT": f"{base}/certs",
        "OIDC_OP_AUTHORIZATION_ENDPOINT": f"{base}/auth",
        "OIDC_OP_TOKEN_ENDPOINT": "https://proxy.internal/token",
        "OIDC_OP_USER_ENDPOINT": f"{base}/userinfo",
        "OIDC_OP_LOGOUT_ENDPOINT": f"{base}/logout",
        "OIDC_OP_INTROSPECTION_ENDPOINT": f"{base}/token/introspect",
        "OIDC_RP_CLIENT_ID": "client-id",
        "OIDC_RP_CLIENT_SECRET": "{{ vault_oidc_rp_client_secret }}",
    }
    backend = AnsibleVaultBackend()
    sq = accept_defaults(monkeypatch)

    bootstrap._ask_oidc(answers, backend, "meet")

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert answers["OIDC_OP_TOKEN_ENDPOINT"] == "https://proxy.internal/token"


def test_keycloak_changed_base_recomputes_all_endpoints(monkeypatch):
    """A new keycloak base URL on the replay recomputes every OIDC_OP_* endpoint from
    the new base."""
    old_base = "https://idp.example.org/realms/master/protocol/openid-connect"
    answers = {
        "OIDC_OP_URL": "https://idp.example.org/realms/master",
        "OIDC_OP_JWKS_ENDPOINT": f"{old_base}/certs",
        "OIDC_OP_TOKEN_ENDPOINT": "https://proxy.internal/token",
        "OIDC_RP_CLIENT_ID": "client-id",
        "OIDC_RP_CLIENT_SECRET": "{{ vault_oidc_rp_client_secret }}",
    }
    backend = AnsibleVaultBackend()
    sq = accept_defaults(
        monkeypatch, [("text", "Keycloak base URL", "https://new-idp.example.org")]
    )

    bootstrap._ask_oidc(answers, backend, "meet")

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert answers["OIDC_OP_TOKEN_ENDPOINT"] == (
        "https://new-idp.example.org/realms/master/protocol/openid-connect/token"
    )


def test_custom_oidc_trailing_slash_survives_byte_identical(monkeypatch):
    """A recovered custom-provider OIDC_OP_URL with a trailing slash survives an
    Enter-through replay."""
    answers = {
        "OIDC_OP_URL": "https://idp.example.org/",
        "OIDC_OP_JWKS_ENDPOINT": "https://idp.example.org/jwks",
        "OIDC_RP_CLIENT_ID": "client-id",
        "OIDC_RP_CLIENT_SECRET": "{{ vault_oidc_rp_client_secret }}",
    }
    backend = AnsibleVaultBackend()
    sq = accept_defaults(monkeypatch)

    bootstrap._ask_oidc(answers, backend, "meet")

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert answers["OIDC_OP_URL"] == "https://idp.example.org/"


def _drive_core_seed(domain: str) -> dict:
    return {
        "DOMAIN": domain,
        "DJANGO_ALLOWED_HOSTS": domain,
        "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{domain}",
        "DJANGO_CORS_ALLOWED_ORIGINS": "https://custom-cors.example.org",
        "DATABASE_URL": "{{ vault_database_url }}",
        "REDIS_URL": "{{ vault_redis_url }}",
        "AWS_S3_ENDPOINT_URL": "https://s3.example.org",
        "AWS_STORAGE_BUCKET_NAME": "drive-media",
        "AWS_S3_ACCESS_KEY_ID": "accesskey",
        "AWS_S3_SECRET_ACCESS_KEY": "{{ vault_aws_s3_secret_access_key }}",
        "OIDC_OP_URL": "https://idp.example.org/realms/master",
        "OIDC_OP_JWKS_ENDPOINT": (
            "https://idp.example.org/realms/master/protocol/openid-connect/certs"
        ),
        "OIDC_RP_CLIENT_ID": "drive-client-id",
        "OIDC_RP_CLIENT_SECRET": "{{ vault_oidc_rp_client_secret }}",
    }


def test_kept_domain_preserves_hand_edited_derived_keys(monkeypatch):
    """Retyping the same recovered DOMAIN keeps a hand-edited
    DJANGO_CORS_ALLOWED_ORIGINS via setdefault."""
    meta = appmeta.load_app("drive")
    backend = AnsibleVaultBackend()
    seed = _drive_core_seed("drive.example.org")
    accept_defaults(
        monkeypatch, [("select", "Database configuration:", "DATABASE_URL")]
    )

    answers = bootstrap._ask_core(meta, backend, seed)

    assert answers["DOMAIN"] == "drive.example.org"
    assert answers["DJANGO_CORS_ALLOWED_ORIGINS"] == "https://custom-cors.example.org"
    # a key that was NOT already recovered still gets the computed default:
    # setdefault only protects a key already present.
    assert answers["LOGIN_REDIRECT_URL"] == "https://{{ st_drive_public_host }}/"


def test_changed_domain_recomputes_derived_keys(monkeypatch):
    """Typing a new domain on a rebootstrap fully recomputes every DOMAIN-derived
    key."""
    meta = appmeta.load_app("drive")
    backend = AnsibleVaultBackend()
    seed = _drive_core_seed("old.example.org")
    accept_defaults(
        monkeypatch,
        [
            ("text", "Public domain for drive", "new.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
        ],
    )

    answers = bootstrap._ask_core(meta, backend, seed)

    assert answers["DOMAIN"] == "new.example.org"
    assert answers["DJANGO_ALLOWED_HOSTS"] == "new.example.org"
    assert answers["DJANGO_CSRF_TRUSTED_ORIGINS"] == "https://new.example.org"
    assert answers["DJANGO_CORS_ALLOWED_ORIGINS"] == "https://new.example.org"
    assert answers["LOGIN_REDIRECT_URL"] == "https://{{ st_drive_public_host }}/"


def test_multihost_allowed_hosts_survive_unrecoverable_domain_replay(monkeypatch):
    """Retyping a domain with an unrecoverable multi-host DJANGO_ALLOWED_HOSTS keeps it
    via setdefault."""
    meta = appmeta.load_app("messages")
    backend = AnsibleVaultBackend()
    seed = {
        "DJANGO_ALLOWED_HOSTS": "messages.example.org,other.example.org",
        "DATABASE_URL": "{{ vault_database_url }}",
        "REDIS_URL": "{{ vault_redis_url }}",
        "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL": "https://s3.example.org",
        "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME": "msg-imports",
        "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY": "impkey",
        "STORAGE_MESSAGE_IMPORTS_SECRET_KEY": (
            "{{ vault_storage_message_imports_secret_key }}"
        ),
        "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY": "3600",
        "OPENSEARCH_URL": "http://opensearch:9200",
        "MESSAGES_TECHNICAL_DOMAIN": "mail.example.org",
        "OIDC_OP_URL": "https://idp.example.org/realms/master",
        "OIDC_OP_JWKS_ENDPOINT": (
            "https://idp.example.org/realms/master/protocol/openid-connect/certs"
        ),
        "OIDC_RP_CLIENT_ID": "messages-client-id",
        "OIDC_RP_CLIENT_SECRET": "{{ vault_oidc_rp_client_secret }}",
        "MDA_API_SECRET": "{{ vault_mda_api_secret }}",
        "SALT_KEY": "{{ vault_salt_key }}",
    }
    accept_defaults(
        monkeypatch,
        [
            ("text", "Public domain for messages", "messages.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("confirm", "Enable blobs offloading", False),
            ("select", "Outbound mail mode", "direct"),
        ],
    )
    answers = bootstrap._ask_core(meta, backend, seed)

    assert answers["DOMAIN"] == "messages.example.org"
    assert answers["DJANGO_ALLOWED_HOSTS"] == "messages.example.org,other.example.org"


def test_ask_optional_blank_clears_recovered_value_and_warns(monkeypatch, mocker):
    """Blanking a recovered optional value pops it from `answers` and warns to remove
    the committed line."""
    answers = {"DJANGO_EMAIL_HOST_USER": "smtpuser"}
    script_questionary(monkeypatch, [("text", "DJANGO_EMAIL_HOST_USER (optional)", "")])
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")

    bootstrap._ask_optional(
        answers, "DJANGO_EMAIL_HOST_USER", "DJANGO_EMAIL_HOST_USER (optional)"
    )

    assert "DJANGO_EMAIL_HOST_USER" not in answers
    warn_spy.assert_called_once()
    msg = warn_spy.call_args[0][0]
    assert "DJANGO_EMAIL_HOST_USER" in msg


def test_ask_optional_enter_through_keeps_value_without_warning(monkeypatch, mocker):
    """Accepting the recovered default for an optional value keeps it in `answers` and
    never warns."""
    answers = {"DJANGO_EMAIL_HOST_USER": "smtpuser"}
    accept_defaults(monkeypatch)
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")

    bootstrap._ask_optional(
        answers, "DJANGO_EMAIL_HOST_USER", "DJANGO_EMAIL_HOST_USER (optional)"
    )

    assert answers["DJANGO_EMAIL_HOST_USER"] == "smtpuser"
    warn_spy.assert_not_called()


def test_seed_drive_legacy_s3_rewrites_both_keys():
    seed = {
        "AWS_S3_ENDPOINT_URL": "{{ st_drive_s3_protocol }}://{{ st_drive_s3_host }}",
        "AWS_STORAGE_BUCKET_NAME": "{{ st_drive_s3_bucket }}",
    }
    data = {
        "st_drive_s3_protocol": "https",
        "st_drive_s3_host": "s3.example.org",
        "st_drive_s3_bucket": "drive-media",
    }

    bootstrap._seed_drive_legacy_s3(seed, data)

    assert seed["AWS_S3_ENDPOINT_URL"] == "https://s3.example.org"
    assert seed["AWS_STORAGE_BUCKET_NAME"] == "drive-media"


def test_seed_drive_legacy_s3_pops_key_when_legacy_var_missing(mocker):
    """A missing legacy var pops the answer instead of pre-filling a wrong value."""
    seed = {
        "AWS_S3_ENDPOINT_URL": "{{ st_drive_s3_protocol }}://{{ st_drive_s3_host }}"
    }
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")

    bootstrap._seed_drive_legacy_s3(seed, {})

    assert "AWS_S3_ENDPOINT_URL" not in seed
    warn_spy.assert_called_once()


def test_seed_drive_legacy_s3_leaves_a_0_4_0_seed_untouched():
    """A 0.4.0 seed, already holding the literal values, is left as-is."""
    seed = {
        "AWS_S3_ENDPOINT_URL": "https://s3.example.org",
        "AWS_STORAGE_BUCKET_NAME": "drive-media",
    }

    bootstrap._seed_drive_legacy_s3(seed, {})

    assert seed["AWS_S3_ENDPOINT_URL"] == "https://s3.example.org"
    assert seed["AWS_STORAGE_BUCKET_NAME"] == "drive-media"


def test_silent_enter_through_meet_with_livekit_byte_identical_and_dep_reused(
    repo, monkeypatch
):
    """A silent replay of a fully-recoverable meet+livekit unit is byte-identical and
    advances every stamp."""
    seed_creds(repo)
    script_questionary(
        monkeypatch, _meet_first_run_script_fully_recoverable(with_livekit=True)
    )
    bootstrap.bootstrap("meet", "prod")

    # roll every stamp back, so a genuine advance (not a no-op) is what's proven.
    m = manifest.load_manifest()
    for u in m.units:
        u.bootstrapped_with = "0.0.1"
    manifest.save_manifest(m)

    core_vars_before = (repo / "meet/prod/meet/vars.yml").read_text()
    core_vault_before = (repo / "meet/prod/meet/vault.yml").read_bytes()
    lk_vars_before = (repo / "meet/prod/livekit/vars.yml").read_text()
    lk_vault_before = (repo / "meet/prod/livekit/vault.yml").read_bytes()
    eg_vars_before = (repo / "meet/prod/egress/vars.yml").read_text()

    sq = script_questionary(monkeypatch, [])
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.SILENT)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert not sq.select_calls, f"a select fired: {sq.select_calls}"

    assert (repo / "meet/prod/meet/vars.yml").read_text() == core_vars_before
    assert (repo / "meet/prod/meet/vault.yml").read_bytes() == core_vault_before
    assert (repo / "meet/prod/livekit/vars.yml").read_text() == lk_vars_before
    assert (repo / "meet/prod/livekit/vault.yml").read_bytes() == lk_vault_before
    assert (repo / "meet/prod/egress/vars.yml").read_text() == eg_vars_before

    m2 = manifest.load_manifest()
    for component in ("meet", "livekit", "egress"):
        unit = next(u for u in m2.units if u.component == component)
        assert unit.bootstrapped_with == __version__


def test_silent_enter_through_messages_byte_identical_fresh_deps_skip_quietly(
    repo, monkeypatch
):
    """A silent replay of a fully-recoverable messages unit is byte-identical and skips
    fresh deps quietly."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        with_answers(
            messages_first_run_script(
                db_mode="discrete", blobs_offload=True, outbound="relay"
            ),
            {"REGION_NAME": "fr-par"},
        )
        + [
            ("select", "Bootstrap pymta now?", "No — bootstrap later"),
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("messages", "prod")

    m = manifest.load_manifest()
    for u in m.units:
        u.bootstrapped_with = "0.0.1"
    manifest.save_manifest(m)

    core_vars_before = (repo / "messages/prod/messages/vars.yml").read_text()
    core_vault_before = (repo / "messages/prod/messages/vault.yml").read_bytes()

    sq = script_questionary(monkeypatch, [])
    bootstrap.bootstrap("messages", "prod", replay=bootstrap.ReplayAction.SILENT)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert not sq.select_calls, f"a select fired: {sq.select_calls}"

    assert (repo / "messages/prod/messages/vars.yml").read_text() == core_vars_before
    assert (repo / "messages/prod/messages/vault.yml").read_bytes() == core_vault_before

    m2 = manifest.load_manifest()
    assert not any(u.component in ("pymta", "mpa") for u in m2.units)
    unit = next(u for u in m2.units if u.component == "messages")
    assert unit.bootstrapped_with == __version__


def test_silent_dep_dispatch_offered_component_shows_menu_then_declines(
    repo, monkeypatch, tmp_path
):
    """A flagged `new_components` offer shows a real menu under silent replay; declining
    registers no unit."""
    seed_creds(repo)
    script_questionary(
        monkeypatch, _meet_first_run_script_fully_recoverable(with_livekit=False)
    )
    bootstrap.bootstrap("meet", "prod")

    m = manifest.load_manifest()
    for u in m.units:
        u.bootstrapped_with = "0.0.1"
    manifest.save_manifest(m)

    set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": __version__,
                "apps": ["meet"],
                "reason": "livekit is now optional",
                "link": "https://example.org/livekit",
                "new_components": ["livekit"],
            }
        ],
    )

    sq = script_questionary(
        monkeypatch, [("select", "Bootstrap livekit now?", "No — bootstrap later")]
    )
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.SILENT)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    dep_offers = [c for msg, c in sq.select_calls if "Bootstrap livekit now?" in msg]
    assert dep_offers, (
        "the fresh menu must be offered when a new_components offer exists"
    )

    m2 = manifest.load_manifest()
    assert not any(u.component == "livekit" for u in m2.units)
    unit = next(u for u in m2.units if u.component == "meet")
    assert unit.bootstrapped_with == __version__

    assert upgrades.new_component_offers(m2, "meet", "prod") == []


def test_silent_dep_dispatch_offer_message_printed(repo, monkeypatch, tmp_path, mocker):
    """A `new_components` offer's version/reason/link are printed via `ui.info` before
    the fresh menu."""
    seed_creds(repo)
    script_questionary(
        monkeypatch, _meet_first_run_script_fully_recoverable(with_livekit=False)
    )
    bootstrap.bootstrap("meet", "prod")

    m = manifest.load_manifest()
    for u in m.units:
        u.bootstrapped_with = "0.0.1"
    manifest.save_manifest(m)

    set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": __version__,
                "apps": ["meet"],
                "reason": "livekit is now optional",
                "link": "https://example.org/livekit",
                "new_components": ["livekit"],
            }
        ],
    )

    info_spy = mocker.patch.object(bootstrap.ui, "info")
    script_questionary(
        monkeypatch, [("select", "Bootstrap livekit now?", "No — bootstrap later")]
    )
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.SILENT)

    printed = [c.args[0] for c in info_spy.call_args_list]
    assert any(
        "newly available" in msg and "livekit is now optional" in msg for msg in printed
    )
    # the decline went through the real (scripted) select, not the quiet-skip
    # path; that path's own message must not appear alongside it.
    assert not any("not bootstrapped" in msg for msg in printed)


def test_silent_dep_dispatch_offered_component_external_choice_asks_for_real(
    repo, monkeypatch
):
    """The "external" branch of an offered fresh dependency still asks, never reusing a
    stale `Recovered` value."""
    from st_cli.core import prompts
    from st_cli.core.models import NewComponentOffer, StCliManifest
    from st_cli.core.secretbackend import AnsibleVaultBackend

    meta = appmeta.load_app("drive")
    dep = next(d for d in meta.dependencies if d.on == "collabora")
    backend = AnsibleVaultBackend()
    m = StCliManifest(collection_version="0.0.0", cli_version=__version__, units=[])
    offer = NewComponentOffer(
        app="drive",
        env="prod",
        component="collabora",
        version=__version__,
        reason="test offer",
        link="",
    )
    answers = {"COLLABORA_DOMAIN": prompts.Recovered("stale.example.org")}

    sq = script_questionary(
        monkeypatch,
        [
            (
                "select",
                "Bootstrap collabora now?",
                "Already deployed (enter URL + keys)",
            ),
            (
                "text",
                "Collabora domain (e.g. collabora.example.org)",
                "typed.example.org",
            ),
        ],
    )
    with prompts.silent_replay():
        mode = bootstrap._handle_dependency(
            meta, dep, answers, backend, "prod", m, flagged={}, offer=offer
        )
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert mode == "external"
    # the prompt fired for real and its typed answer won, not the stale
    # Recovered default silently auto-accepted from elsewhere in the run.
    assert answers["COLLABORA_DOMAIN"] == "typed.example.org"


def test_silent_new_template_key_is_asked_and_merged_in(repo, monkeypatch, tmp_path):
    """A new mandatory setting is the only prompt a silent replay asks, and is merged in
    behind the marker."""
    seed_creds(repo)
    script_questionary(
        monkeypatch, _meet_first_run_script_fully_recoverable(with_livekit=False)
    )
    bootstrap.bootstrap("meet", "prod")

    before_data = tree.load_vars("meet", "prod", "meet")
    before_blob = str(before_data["st_meet_backend_env"])
    before_keys = set(before_data)
    core_vault_before = (repo / "meet/prod/meet/vault.yml").read_bytes()

    tpl_dir = tmp_path / "env_templates"
    shutil.copytree(envrender._TEMPLATES_DIR, tpl_dir)
    base_tpl = tpl_dir / "base.django.env.j2"
    base_tpl.write_text(
        base_tpl.read_text() + "NEW_RELEASE_SETTING={{ answers.NEW_RELEASE_SETTING }}\n"
    )
    monkeypatch.setattr(envrender, "_TEMPLATES_DIR", tpl_dir)

    real_ask_core = bootstrap._ask_core

    def patched_ask_core(meta, backend, answers=None):
        result = real_ask_core(meta, backend, answers)
        result["NEW_RELEASE_SETTING"] = bootstrap._ask(
            "NEW_RELEASE_SETTING",
            bootstrap._recall(result, "NEW_RELEASE_SETTING", "default-value"),
        )
        return result

    monkeypatch.setattr(bootstrap, "_ask_core", patched_ask_core)

    sq = script_questionary(monkeypatch, [("text", "NEW_RELEASE_SETTING", "new-value")])
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.SILENT)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    after_data = tree.load_vars("meet", "prod", "meet")
    after_blob = str(after_data["st_meet_backend_env"])
    assert "NEW_RELEASE_SETTING=new-value" in after_blob
    assert "# added by st-cli" in after_blob
    assert after_blob.startswith(before_blob.rstrip("\n"))
    assert set(after_data) == before_keys
    for key in before_keys:
        if key != "st_meet_backend_env":
            assert after_data[key] == before_data[key]
    assert (repo / "meet/prod/meet/vault.yml").read_bytes() == core_vault_before


def test_hashi_silent_replay_reuse_no_lookup_term_prompt(repo, monkeypatch):
    """A hashi_vault-backed meet+livekit unit's silent replay never prompts a fresh
    LiveKit lookup term."""
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "hashi_vault (OpenBao)"),
            ("text", "OpenBao / Vault URL", "https://vault.example:8200"),
            ("confirm", "Skip TLS verification?", False),
            ("text", "meet host(s)", "10.0.0.5"),
            ("text", "Public domain for meet", "meet.example.org"),
            ("text", "DJANGO_SECRET_KEY", "@openbao(kv/data/meet:django_secret_key)"),
            ("select", "Database configuration:", "discrete (DB_*)"),
            ("text", "DB_HOST", "db.example.org"),
            ("text", "DB_NAME", "meetdb"),
            ("text", "DB_USER", "meetuser"),
            ("text", "DB_PASSWORD", "@openbao(kv/data/meet:db_password)"),
            ("text", "DB_PORT", "5432"),
            ("text", "REDIS_URL", "@openbao(kv/data/meet:redis_url)"),
            ("text", "AWS_S3_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "AWS_S3_ACCESS_KEY_ID", "accesskey"),
            ("text", "AWS_S3_SECRET_ACCESS_KEY", "@openbao(kv/data/meet:s3_secret)"),
            ("text", "AWS_STORAGE_BUCKET_NAME", "meet-media"),
            ("text", "AWS_S3_REGION_NAME (optional)", "fr-par"),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "meet-client-id"),
            ("text", "OIDC_RP_CLIENT_SECRET", "@openbao(kv/data/meet:oidc_secret)"),
            ("confirm", "Configure transactional email (SMTP) settings?", False),
            ("confirm", "cadvisor", True),
            ("select", "Bootstrap livekit now?", "Yes — bootstrap now"),
            ("text", "livekit host(s)", "10.0.0.1"),
            ("text", "egress (leave blank", ""),
            (
                "text",
                "st_meet_livekit_api_key",
                "@openbao(kv/data/meet:livekit_api_key)",
            ),
            (
                "text",
                "st_meet_livekit_api_secret",
                "@openbao(kv/data/meet:livekit_api_secret)",
            ),
            (
                "text",
                "LiveKit domain (e.g. livekit.example.org)",
                "livekit.example.org",
            ),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", "turn.example.org"),
            ("confirm", "livekit", True),
            ("confirm", "egress", True),
        ],
    )
    bootstrap.bootstrap("meet", "prod")

    core_vars_before = (repo / "meet/prod/meet/vars.yml").read_text()
    lk_vars_before = (repo / "meet/prod/livekit/vars.yml").read_text()

    sq = script_questionary(monkeypatch, [])
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.SILENT)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    assert (repo / "meet/prod/meet/vars.yml").read_text() == core_vars_before
    assert (repo / "meet/prod/livekit/vars.yml").read_text() == lk_vars_before
