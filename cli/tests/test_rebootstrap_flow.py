"""Acceptance tests for the rebootstrap flow (`st_cli.cmd.bootstrap`).

The property the whole feature rests on: an Enter-through rebootstrap of an
already-committed unit must leave its config tree byte-identical. These tests
exercise that end to end (through the real `bootstrap.bootstrap()` entry
point, scripted via `ScriptedQuestionary`/`ACCEPT_DEFAULT`) rather than unit
by unit — see `test_bootstrap.py` for the narrower unit tests this complements.

`ACCEPT_DEFAULT` (see `helpers.py`) scripts "press Enter" on whatever
`default=` a prompt call was given. It only works where the prompt call
actually carries an explicit `default=` kwarg (a recovered value, or a
`_confirm` gate — which always carries one). A handful of `_ask_select` calls
have NO recoverable default in some states (e.g. the DB mode select when
`DATABASE_URL` was used, since ScriptedQuestionary cannot simulate a real
`questionary.select`'s implicit first-highlighted-choice) — those are scripted
with the literal matching choice string instead, which is an equally valid way
to assert "the same answer was given twice", just spelled out rather than
inferred.
"""

from __future__ import annotations

import shutil

import pytest
from helpers import (
    ACCEPT_DEFAULT,
    script_questionary,
    seed_creds,
    seed_external_livekit_with_leftover_tree,
    seed_hashi_livekit_provider,
    seed_livekit_provider,
    seed_meet_egress_unit,
    seed_meet_unit,
)
from ruamel.yaml.scalarstring import LiteralScalarString

from st_cli import __version__
from st_cli.cmd import bootstrap
from st_cli.core import appmeta, envrender, manifest, paths, tree, upgrades, vault
from st_cli.core.errors import StCliError
from st_cli.core.secretbackend import AnsibleVaultBackend, HashiVaultBackend


# --------------------------------------------------------------------------- #
# shared scripts: a minimal full `meet` bootstrap (no livekit — kept out so
# these tests focus on the CORE questionnaire's rebootstrap behaviour) with an
# SMTP on/off toggle (used by the round-trip + "gate must stay on" test).
# --------------------------------------------------------------------------- #
def _meet_first_run_script(smtp: bool) -> list[tuple]:
    script = [
        ("select", "Secret backend:", "ansible-vault"),
        ("text", "meet host(s)", "10.0.0.5"),
        ("text", "Public domain for meet", "meet.example.org"),
        ("select", "Database configuration:", "discrete (DB_*)"),
        ("text", "DB_HOST", "db.example.org"),
        ("text", "DB_NAME", "meetdb"),
        ("text", "DB_USER", "meetuser"),
        ("password", "DB_PASSWORD", "dbpass123"),
        ("text", "DB_PORT", "5432"),
        ("text", "REDIS_URL", "redis://redis:6379/0"),
        ("text", "AWS_S3_ENDPOINT_URL", "https://s3.example.org"),
        ("text", "AWS_S3_ACCESS_KEY_ID", "accesskey"),
        ("password", "AWS_S3_SECRET_ACCESS_KEY", "secretkey"),
        ("text", "AWS_STORAGE_BUCKET_NAME", "meet-media"),
        ("text", "AWS_S3_REGION_NAME (optional)", ""),
        ("select", "Identity provider:", "keycloak"),
        ("text", "Keycloak base URL", "https://idp.example.org"),
        ("text", "Keycloak realm", "master"),
        ("text", "OIDC_RP_CLIENT_ID", "meet-client-id"),
        ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
    ]
    if smtp:
        script += [
            ("confirm", "Configure transactional email (SMTP) settings?", True),
            ("text", "DJANGO_EMAIL_HOST", "smtp.example.org"),
            ("text", "DJANGO_EMAIL_PORT", "587"),
            ("text", "DJANGO_EMAIL_HOST_USER (optional)", "smtpuser"),
            ("password", "DJANGO_EMAIL_HOST_PASSWORD", "smtppass"),
            ("confirm", "DJANGO_EMAIL_USE_TLS?", True),
            ("confirm", "DJANGO_EMAIL_USE_SSL?", False),
            ("text", "DJANGO_EMAIL_FROM", "noreply@example.org"),
            ("text", "DJANGO_EMAIL_BRAND_NAME (optional)", "MeetBrand"),
        ]
    else:
        script.append(
            ("confirm", "Configure transactional email (SMTP) settings?", False)
        )
    script += [
        ("confirm", "cadvisor", True),
        ("select", "Bootstrap livekit now?", "No — bootstrap later"),
    ]
    return script


def _meet_accept_through_script(smtp_enabled: bool) -> list[tuple]:
    """An Enter-through re-run of `_meet_first_run_script` — every value
    pre-filled and accepted via `ACCEPT_DEFAULT`, except the two selects with
    no recoverable default in this state (see module docstring)."""
    script = [
        ("text", "meet host(s)", ACCEPT_DEFAULT),
        ("text", "Public domain for meet", ACCEPT_DEFAULT),
        ("select", "Database configuration:", ACCEPT_DEFAULT),
        ("text", "DB_HOST", ACCEPT_DEFAULT),
        ("text", "DB_NAME", ACCEPT_DEFAULT),
        ("text", "DB_USER", ACCEPT_DEFAULT),
        ("text", "DB_PORT", ACCEPT_DEFAULT),
        ("text", "AWS_S3_ENDPOINT_URL", ACCEPT_DEFAULT),
        ("text", "AWS_S3_ACCESS_KEY_ID", ACCEPT_DEFAULT),
        ("text", "AWS_STORAGE_BUCKET_NAME", ACCEPT_DEFAULT),
        ("text", "AWS_S3_REGION_NAME (optional)", ACCEPT_DEFAULT),
        ("select", "Identity provider:", ACCEPT_DEFAULT),
        ("text", "Keycloak base URL", ACCEPT_DEFAULT),
        ("text", "Keycloak realm", ACCEPT_DEFAULT),
        ("text", "OIDC_RP_CLIENT_ID", ACCEPT_DEFAULT),
        (
            "confirm",
            "SMTP is configured — review its settings?"
            if smtp_enabled
            else "Configure transactional email (SMTP) settings?",
            ACCEPT_DEFAULT,
        ),
    ]
    if smtp_enabled:
        script += [
            ("text", "DJANGO_EMAIL_HOST", ACCEPT_DEFAULT),
            ("text", "DJANGO_EMAIL_PORT", ACCEPT_DEFAULT),
            ("text", "DJANGO_EMAIL_HOST_USER (optional)", ACCEPT_DEFAULT),
            ("confirm", "DJANGO_EMAIL_USE_TLS?", ACCEPT_DEFAULT),
            ("confirm", "DJANGO_EMAIL_USE_SSL?", ACCEPT_DEFAULT),
            ("text", "DJANGO_EMAIL_FROM", ACCEPT_DEFAULT),
            ("text", "DJANGO_EMAIL_BRAND_NAME (optional)", ACCEPT_DEFAULT),
        ]
    script += [
        ("confirm", "cadvisor", ACCEPT_DEFAULT),
        ("select", "Bootstrap livekit now?", "No — bootstrap later"),
    ]
    return script


def _meet_first_run_script_fully_recoverable(with_livekit: bool = False) -> list[tuple]:
    """`_meet_first_run_script(smtp=False)`, so a later SILENT replay has
    genuinely nothing left to ask: a blank optional (e.g. AWS_S3_REGION_NAME)
    auto-accepts too, since silent mode never re-prompts a `required=False`
    field regardless of whether it was recovered (see `core/prompts.py`'s
    `_ask` docstring). ``with_livekit`` also deploys livekit (co-located
    egress), so a fully empty-script SILENT replay exercises the
    existing-unflagged dependency reuse path too.
    """
    script = _meet_first_run_script(smtp=False)
    if with_livekit:
        script = script[:-1] + [  # drop the "bootstrap later" select
            ("select", "Bootstrap livekit now?", "Yes — bootstrap now"),
            ("text", "livekit host(s)", "10.0.0.1"),
            ("text", "egress (leave blank", ""),
            (
                "text",
                "LiveKit domain (e.g. livekit.example.org)",
                "livekit.example.org",
            ),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", "turn.example.org"),
            ("confirm", "livekit", True),
            ("confirm", "egress", True),
        ]
    return script


# --------------------------------------------------------------------------- #
# the round trip (meet + drive + messages)
# --------------------------------------------------------------------------- #
def test_meet_round_trip_byte_identical_and_smtp_gate_stays_on(repo, monkeypatch):
    """Bootstrap meet (SMTP configured), snapshot vars.yml/vault.yml, rebootstrap
    accepting every default: both files are byte-identical, and — the gate
    trap this whole feature exists to close — SMTP is STILL configured
    afterwards (an Enter-through run must not silently decline the SMTP
    confirm and drop the whole block).

    This is the one round trip that goes through the default ``replay=ASK``
    3-way select (every other round-trip test passes an explicit
    ``replay=MODIFY`` to skip it and stay focused on its own concern) —
    picking "Modify" here must behave exactly like the old unconditional
    replay.
    """
    seed_creds(repo)
    sq1 = script_questionary(monkeypatch, _meet_first_run_script(smtp=True))
    bootstrap.bootstrap("meet", "prod")
    assert not sq1._scripts, f"unconsumed scripts: {sq1._scripts}"

    core_vars_before = (repo / "meet/prod/meet/vars.yml").read_text()
    core_vault_before = (repo / "meet/prod/meet/vault.yml").read_bytes()
    assert "DJANGO_EMAIL_HOST=smtp.example.org" in core_vars_before

    m = manifest.load_manifest()
    unit = next(u for u in m.units if u.component == "meet")
    assert unit.bootstrapped_with == __version__

    sq2 = script_questionary(
        monkeypatch,
        [
            (
                "select",
                "meet/prod is already bootstrapped — what do you want to do?",
                "Modify — replay the questionnaire (answers pre-filled)",
            ),
        ]
        + _meet_accept_through_script(smtp_enabled=True),
    )
    bootstrap.bootstrap("meet", "prod")
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    core_vars_after = (repo / "meet/prod/meet/vars.yml").read_text()
    core_vault_after = (repo / "meet/prod/meet/vault.yml").read_bytes()
    assert core_vars_after == core_vars_before
    assert core_vault_after == core_vault_before
    assert "DJANGO_EMAIL_HOST=smtp.example.org" in core_vars_after


def test_meet_with_livekit_round_trip_leaves_core_vault_untouched(repo, monkeypatch):
    """The realistic meet deployment — WITH livekit — must also round-trip.

    The other meet round-trip keeps livekit out to isolate the core
    questionnaire, but every real meet install has one, and reusing a livekit
    provider re-mirrors its api key/secret into the meet core's own vault on
    every run. Since ansible-vault salts each encryption, re-encrypting that
    unchanged mapping would rewrite core vault.yml with fresh ciphertext on
    every single rebootstrap — a permanent phantom entry in `git diff` that
    trains operators to ignore vault changes, hiding the reruns that really did
    rotate a secret. write_vault skips a write when the merge is a no-op; this
    pins that end-to-end, and checks the decrypted values are genuinely
    unchanged rather than merely re-encrypted.
    """
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        _meet_first_run_script(smtp=False)[:-1]  # drop the "bootstrap later" select
        + [
            ("select", "Bootstrap livekit now?", "Yes — bootstrap now"),
            ("text", "livekit host(s)", "10.0.0.1"),
            ("text", "egress (leave blank", ""),  # blank → co-located with livekit
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

    sq2 = script_questionary(
        monkeypatch,
        _meet_accept_through_script(smtp_enabled=False)[:-1]
        + [("select", "Bootstrap livekit now?", ACCEPT_DEFAULT)],
    )
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


def test_drive_round_trip_byte_identical_s3_reconstruction(repo, monkeypatch):
    """Bootstrap drive + collabora, snapshot every file, rebootstrap accepting
    every default: drive exercises the S3 endpoint RECONSTRUCTION (its
    committed AWS_S3_ENDPOINT_URL holds the `{{ st_drive_s3_* }}` indirection,
    not the real endpoint — the pre-fill has to be rebuilt from the recovered
    S3_PROTOCOL/S3_HOST/S3_BUCKET component vars). collabora's shared "domain"
    rule has no `var` at all, so it exercises the answer_key-based fallback
    recovery instead of `recover_shared`. Neither drive's nor collabora's
    files are touched by the rebootstrap (collabora is a non-secret provider,
    so — unlike meet/livekit — reusing it doesn't even re-encrypt anything)."""
    seed_creds(repo)
    sq1 = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "drive host(s)", "10.0.0.10"),
            ("text", "workers (leave blank", ""),
            ("text", "Public domain for drive", "drive.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://drive"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            ("text", "AWS_S3_ENDPOINT_URL", "https://s3.fr-par.scw.cloud"),
            ("text", "AWS_S3_ACCESS_KEY_ID", "driveaccess"),
            ("password", "AWS_S3_SECRET_ACCESS_KEY", "drivesecretkey"),
            ("text", "AWS_STORAGE_BUCKET_NAME", "drive-media"),
            ("text", "AWS_S3_REGION_NAME (optional)", "fr-par"),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "drive-client-id"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            ("confirm", "Configure transactional email (SMTP) settings?", False),
            ("confirm", "cadvisor", True),
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
    # collabora's only shared rule is a non-secret prompt — it never gets a
    # vault.yml at all (write_vault no-ops on an empty secret buffer).
    assert not paths.vault_path("drive", "prod", "collabora").exists()
    # sanity: the committed endpoint is the indirection, NOT the real one typed above
    assert "{{ st_drive_s3_protocol }}://{{ st_drive_s3_host }}" in core_vars_before

    sq2 = script_questionary(
        monkeypatch,
        [
            ("text", "drive host(s)", ACCEPT_DEFAULT),
            ("text", "workers (leave blank", ACCEPT_DEFAULT),
            ("text", "Public domain for drive", ACCEPT_DEFAULT),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "AWS_S3_ENDPOINT_URL", ACCEPT_DEFAULT),
            ("text", "AWS_S3_ACCESS_KEY_ID", ACCEPT_DEFAULT),
            ("text", "AWS_STORAGE_BUCKET_NAME", ACCEPT_DEFAULT),
            ("text", "AWS_S3_REGION_NAME (optional)", ACCEPT_DEFAULT),
            ("select", "Identity provider:", ACCEPT_DEFAULT),
            ("text", "Keycloak base URL", ACCEPT_DEFAULT),
            ("text", "Keycloak realm", ACCEPT_DEFAULT),
            ("text", "OIDC_RP_CLIENT_ID", ACCEPT_DEFAULT),
            (
                "confirm",
                "Configure transactional email (SMTP) settings?",
                ACCEPT_DEFAULT,
            ),
            ("confirm", "cadvisor", ACCEPT_DEFAULT),
            ("select", "Bootstrap collabora now?", ACCEPT_DEFAULT),
            ("text", "Collabora domain", ACCEPT_DEFAULT),
        ],
    )
    bootstrap.bootstrap("drive", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    assert (repo / "drive/prod/drive/vars.yml").read_text() == core_vars_before
    assert (repo / "drive/prod/drive/vault.yml").read_bytes() == core_vault_before
    assert (repo / "drive/prod/collabora/vars.yml").read_text() == collabora_vars_before
    assert not paths.vault_path("drive", "prod", "collabora").exists()


def _drive_first_run_script() -> list[tuple]:
    return [
        ("select", "Secret backend:", "ansible-vault"),
        ("text", "drive host(s)", "10.0.0.10"),
        ("text", "workers (leave blank", ""),
        ("text", "Public domain for drive", "drive.example.org"),
        ("select", "Database configuration:", "DATABASE_URL"),
        ("text", "DATABASE_URL", "postgres://drive"),
        ("text", "REDIS_URL", "redis://redis:6379/0"),
        ("text", "AWS_S3_ENDPOINT_URL", "https://s3.fr-par.scw.cloud"),
        ("text", "AWS_S3_ACCESS_KEY_ID", "driveaccess"),
        ("password", "AWS_S3_SECRET_ACCESS_KEY", "drivesecretkey"),
        ("text", "AWS_STORAGE_BUCKET_NAME", "drive-media"),
        ("text", "AWS_S3_REGION_NAME (optional)", "fr-par"),
        ("select", "Identity provider:", "keycloak"),
        ("text", "Keycloak base URL", "https://idp.example.org"),
        ("text", "Keycloak realm", "master"),
        ("text", "OIDC_RP_CLIENT_ID", "drive-client-id"),
        ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
        ("confirm", "Configure transactional email (SMTP) settings?", False),
        ("confirm", "cadvisor", True),
        ("select", "Bootstrap collabora now?", "No — bootstrap later"),
    ]


def test_workers_only_run_over_existing_core_skips_3way_select(repo, monkeypatch):
    """M8 regression: `-c workers` over an existing core used to hit the same
    3-way Modify/Reuse/Override select as a core-targeting run, even though
    `target_core` is False for it — Override would confirm destructively but
    destroy nothing (workers own no files of their own), and Reuse would
    return before ever registering the unit. A `-c workers` re-run must skip
    the select entirely and keep registering the unit (today's MODIFY
    behavior), with no "is already bootstrapped" select consumed."""
    seed_creds(repo)
    script_questionary(monkeypatch, _drive_first_run_script())
    bootstrap.bootstrap("drive", "prod")
    script_questionary(monkeypatch, [])
    bootstrap.bootstrap("drive", "prod", component="workers")

    m = manifest.load_manifest()
    assert any(u.component == "workers" for u in m.units)

    # a second `-c workers` run over the now-existing core must not offer the
    # 3-way select — it must just re-register the unit with no prompts.
    sq = script_questionary(monkeypatch, [])
    bootstrap.bootstrap("drive", "prod", component="workers")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert not any("is already bootstrapped" in msg for msg, _ in sq.select_calls)


@pytest.mark.parametrize(
    "replay", [bootstrap.ReplayAction.REUSE, bootstrap.ReplayAction.OVERRIDE]
)
def test_workers_only_run_rejects_reuse_and_override(repo, monkeypatch, replay):
    """M8 follow-up: `-c workers` with an explicit `replay=REUSE`/`OVERRIDE`
    must raise naming the limitation, the same as a dependency provider
    target (M7) — REUSE/OVERRIDE apply to the core path only."""
    seed_creds(repo)
    script_questionary(monkeypatch, _drive_first_run_script())
    bootstrap.bootstrap("drive", "prod")
    script_questionary(monkeypatch, [])
    bootstrap.bootstrap("drive", "prod", component="workers")

    with pytest.raises(StCliError, match="applies to the core path only"):
        bootstrap.bootstrap("drive", "prod", component="workers", replay=replay)


def test_wire_only_core_run_rejects_override_and_omits_it_from_select(
    repo, monkeypatch
):
    """A wire-only run (`-c <core>`) never touches a provider, so it cannot
    force-replay the kept providers that rebuild the core-side wiring — an
    Override there would silently drop the constructed values. The 3-way
    select must omit the Override choice, and an explicit `replay=OVERRIDE`
    must raise before any prompt."""
    seed_creds(repo)
    script_questionary(monkeypatch, _drive_first_run_script())
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
    """Bootstrap messages (blobs offload + relay outbound enabled, mta-in/mpa
    left for later), snapshot, rebootstrap accepting every default: byte
    identical. Exercises messages' DOMAIN recovery fallback (no `{DOMAIN}`
    component var exists for messages — it falls back to the recovered
    DJANGO_ALLOWED_HOSTS), the blobs-offload gate, the relay/direct outbound
    gate, and MESSAGES_BLOBS_ENCRYPT_KEY's regex-based recovery out of the
    composed MESSAGES_BLOBS_ENCRYPT_KEYS JSON."""
    seed_creds(repo)
    sq1 = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "messages host(s)", "10.0.0.4"),
            ("text", "workers (leave blank", ""),
            ("text", "Public domain for messages", "messages.example.org"),
            ("select", "Database configuration:", "discrete (DB_*)"),
            ("text", "DB_HOST", "msgdb.example.org"),
            ("text", "DB_NAME", "messagesdb"),
            ("text", "DB_USER", "messagesuser"),
            ("password", "DB_PASSWORD", "msgdbpass"),
            ("text", "DB_PORT", "5432"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", "msg-imports"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", "impkey"),
            ("password", "STORAGE_MESSAGE_IMPORTS_SECRET_KEY", "impsecret"),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ""),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
            ("confirm", "Enable blobs offloading", True),
            ("text", "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_BLOBS_BUCKET_NAME", "msg-blobs"),
            ("text", "STORAGE_MESSAGE_BLOBS_ACCESS_KEY", "blobkey"),
            ("password", "STORAGE_MESSAGE_BLOBS_SECRET_KEY", "blobsecret"),
            ("text", "STORAGE_MESSAGE_BLOBS_REGION_NAME", ""),
            ("text", "OPENSEARCH_URL", "http://opensearch:9200"),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", "mail.example.org"),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "messages-client-id"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            ("select", "Outbound mail mode", "relay"),
            ("text", "MTA_OUT_RELAY_HOST", "smtp.example.org:587"),
            ("text", "MTA_OUT_RELAY_USERNAME", "relayuser"),
            ("password", "MTA_OUT_RELAY_PASSWORD", "relaypass"),
            ("confirm", "cadvisor", True),
            ("select", "Bootstrap mta-in now?", "No — bootstrap later"),
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("messages", "prod")
    assert not sq1._scripts, f"unconsumed scripts: {sq1._scripts}"

    core_vars_before = (repo / "messages/prod/messages/vars.yml").read_text()
    core_vault_before = (repo / "messages/prod/messages/vault.yml").read_bytes()

    sq2 = script_questionary(
        monkeypatch,
        [
            ("text", "messages host(s)", ACCEPT_DEFAULT),
            ("text", "workers (leave blank", ACCEPT_DEFAULT),
            ("text", "Public domain for messages", ACCEPT_DEFAULT),
            ("select", "Database configuration:", ACCEPT_DEFAULT),
            ("text", "DB_HOST", ACCEPT_DEFAULT),
            ("text", "DB_NAME", ACCEPT_DEFAULT),
            ("text", "DB_USER", ACCEPT_DEFAULT),
            ("text", "DB_PORT", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", ACCEPT_DEFAULT),
            (
                "confirm",
                "Blobs offloading is enabled — review its settings?",
                ACCEPT_DEFAULT,
            ),
            ("text", "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_BLOBS_BUCKET_NAME", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_BLOBS_ACCESS_KEY", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_BLOBS_REGION_NAME", ACCEPT_DEFAULT),
            ("text", "OPENSEARCH_URL", ACCEPT_DEFAULT),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", ACCEPT_DEFAULT),
            ("select", "Identity provider:", ACCEPT_DEFAULT),
            ("text", "Keycloak base URL", ACCEPT_DEFAULT),
            ("text", "Keycloak realm", ACCEPT_DEFAULT),
            ("text", "OIDC_RP_CLIENT_ID", ACCEPT_DEFAULT),
            ("select", "Outbound mail mode", ACCEPT_DEFAULT),
            ("text", "MTA_OUT_RELAY_HOST", ACCEPT_DEFAULT),
            ("text", "MTA_OUT_RELAY_USERNAME", ACCEPT_DEFAULT),
            ("confirm", "cadvisor", ACCEPT_DEFAULT),
            ("select", "Bootstrap mta-in now?", "No — bootstrap later"),
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("messages", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    assert (repo / "messages/prod/messages/vars.yml").read_text() == core_vars_before
    assert (repo / "messages/prod/messages/vault.yml").read_bytes() == core_vault_before


# --------------------------------------------------------------------------- #
# targeted acceptance scenarios
# --------------------------------------------------------------------------- #
def test_hand_edits_survive_rebootstrap(repo, monkeypatch):
    """A custom `st_*` var, a `# comment`, and a custom `MY_VAR=1` line hand-added
    to the committed tree after the first bootstrap all survive an
    Enter-through rebootstrap untouched (core.writer.write_core's merge +
    core.envblob.merge's "operator-only key stays verbatim, in place" rule)."""
    seed_creds(repo)
    script_questionary(monkeypatch, _meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")

    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_my_custom_var"] = "custom-value"
    data.yaml_set_comment_before_after_key("st_meet_my_custom_var", before="my comment")
    blob = str(data["st_meet_backend_env"])
    data["st_meet_backend_env"] = LiteralScalarString(blob + "MY_VAR=1\n")
    tree.save_vars("meet", "prod", "meet", data)

    script_questionary(monkeypatch, _meet_accept_through_script(smtp_enabled=False))
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)

    new_text = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "st_meet_my_custom_var: custom-value" in new_text
    assert "my comment" in new_text
    assert "MY_VAR=1" in new_text


def test_socks_proxy_replay_never_rotates_or_clobbers(repo, monkeypatch):
    """A standalone `-c socks-proxy` Enter-through replay must not mint a new
    PROXY_USERS credential, must not re-mirror it into the messages core
    vault, and must not touch either committed vars.yml or vault.yml: the
    dotenv inversion (`core.recover.recover`) and the provider-leg seed
    (`_ask_messages_provider`) must pre-fill PROXY_EXTERNAL/PROXY_INTERNAL_PORT/
    PROXY_USERS from the committed unit, so every prompt sees them already
    decided."""
    seed_creds(repo)
    sq1 = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
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
            ("confirm", "cadvisor", True),  # core cadvisor
            ("select", "Bootstrap mta-in now?", "No — bootstrap later"),
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

    sq2 = script_questionary(
        monkeypatch,
        [
            ("text", "socks-proxy host(s)", ACCEPT_DEFAULT),
            ("text", "PROXY_EXTERNAL", ACCEPT_DEFAULT),
            ("text", "PROXY_INTERNAL_PORT", ACCEPT_DEFAULT),
            ("confirm", "cadvisor", ACCEPT_DEFAULT),
        ],
    )
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
    """A standalone `-c socks-proxy` mint (run before the core ever wired
    direct outbound to it) writes PROXY_USERS into the provider's OWN vault
    only: the mint mirrors the value into the core's IN-MEMORY answers buffer
    too, but that standalone run never writes the core unit, so the mirror
    never reaches disk. A later full Enter-through replay (socks-proxy
    "Modify") must repair that: the core vault gains vault_proxy_users,
    matching the provider's value exactly — no dangling
    ``{{ vault_proxy_users }}`` ref, and no rotation of the provider's own
    value."""
    seed_creds(repo)
    sq1 = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
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
            ("confirm", "cadvisor", True),  # core cadvisor
            ("select", "Bootstrap mta-in now?", "No — bootstrap later"),
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
    # the mint's core-side mirror never reached disk — this run wrote only
    # the provider unit.
    assert "vault_proxy_users" not in vault.decrypt_to_dict(core_vault_path)

    sq3 = script_questionary(
        monkeypatch,
        [
            ("text", "messages host(s)", ACCEPT_DEFAULT),
            ("text", "workers (leave blank", ACCEPT_DEFAULT),
            ("text", "Public domain for messages", ACCEPT_DEFAULT),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", ACCEPT_DEFAULT),
            ("confirm", "Enable blobs offloading", ACCEPT_DEFAULT),
            ("text", "OPENSEARCH_URL", ACCEPT_DEFAULT),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", ACCEPT_DEFAULT),
            ("select", "Identity provider:", ACCEPT_DEFAULT),
            ("text", "Keycloak base URL", ACCEPT_DEFAULT),
            ("text", "Keycloak realm", ACCEPT_DEFAULT),
            ("text", "OIDC_RP_CLIENT_ID", ACCEPT_DEFAULT),
            ("select", "Outbound mail mode", "direct"),
            ("confirm", "cadvisor", ACCEPT_DEFAULT),
            ("select", "Bootstrap mta-in now?", "No — bootstrap later"),
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
            (
                "select",
                "Bootstrap socks-proxy now?",
                "Modify (replay the questionnaire)",
            ),
            ("text", "socks-proxy host(s)", ACCEPT_DEFAULT),
            ("text", "PROXY_EXTERNAL", ACCEPT_DEFAULT),
            ("text", "PROXY_INTERNAL_PORT", ACCEPT_DEFAULT),
            ("confirm", "cadvisor", ACCEPT_DEFAULT),
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
    """Changing a value on the rebootstrap (instead of accepting the default)
    lands in the committed tree — a rebootstrap is a real replay, not a
    no-op."""
    seed_creds(repo)
    script_questionary(monkeypatch, _meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")
    assert "DB_HOST=db.example.org" in (repo / "meet/prod/meet/vars.yml").read_text()

    script = _meet_accept_through_script(smtp_enabled=False)
    # replace the DB_HOST entry with an explicit, DIFFERENT value
    idx = next(i for i, s in enumerate(script) if s[1] == "DB_HOST")
    script[idx] = ("text", "DB_HOST", "new-db.example.org")
    script_questionary(monkeypatch, script)
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)

    new_text = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "DB_HOST=new-db.example.org" in new_text
    assert "DB_HOST=db.example.org" not in new_text


def test_new_question_is_asked_and_merged_in(repo, monkeypatch):
    """SMTP was never configured on the first run; enabling it on the
    rebootstrap is a genuinely NEW answer, not a recovered one — the confirm's
    default is still False (nothing recovered), so it must be explicitly
    answered, and the resulting new env lines are appended (with the
    `envblob.merge` marker comment) rather than silently ignored."""
    seed_creds(repo)
    script_questionary(monkeypatch, _meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")
    before = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "DJANGO_EMAIL_HOST" not in before

    script = _meet_accept_through_script(smtp_enabled=False)
    # flip the SMTP gate on with a real (non-recovered) answer instead of
    # ACCEPT_DEFAULT, and answer the newly-unlocked fields for real.
    idx = next(
        i
        for i, s in enumerate(script)
        if s[1] == "Configure transactional email (SMTP) settings?"
    )
    script[idx : idx + 1] = [
        ("confirm", "Configure transactional email (SMTP) settings?", True),
        ("text", "DJANGO_EMAIL_HOST", "new-smtp.example.org"),
        ("text", "DJANGO_EMAIL_PORT", "587"),
        ("text", "DJANGO_EMAIL_HOST_USER (optional)", ""),
        ("password", "DJANGO_EMAIL_HOST_PASSWORD", "newsmtppass"),
        ("confirm", "DJANGO_EMAIL_USE_TLS?", True),
        ("confirm", "DJANGO_EMAIL_USE_SSL?", False),
        ("text", "DJANGO_EMAIL_FROM", "noreply@example.org"),
        ("text", "DJANGO_EMAIL_BRAND_NAME (optional)", ""),
    ]
    script_questionary(monkeypatch, script)
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)

    after = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "DJANGO_EMAIL_HOST=new-smtp.example.org" in after
    # the new keys were appended behind the merge marker, not interleaved
    assert "# added by st-cli" in after


def test_recovered_secrets_never_reprompted(repo, monkeypatch):
    """An Enter-through rebootstrap never issues a single `password` prompt —
    every secret (DB_PASSWORD, AWS_S3_SECRET_ACCESS_KEY, OIDC_RP_CLIENT_SECRET,
    the generated DJANGO_SECRET_KEY) was already recovered. The script below
    deliberately contains NO `password` entries: if any secret were re-prompted,
    ScriptedQuestionary would raise on the unscripted prompt instead of this
    test quietly passing."""
    seed_creds(repo)
    script_questionary(monkeypatch, _meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")

    sq = script_questionary(
        monkeypatch, _meet_accept_through_script(smtp_enabled=False)
    )
    assert not any(kind == "password" for kind, *_ in sq._scripts)
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"


def test_livekit_shared_secret_not_rotated_on_standalone_rebootstrap(repo, monkeypatch):
    """`bootstrap meet prod -c livekit` re-run over an existing livekit unit
    (the "deploy" branch's own rebootstrap machinery, since there is no
    "Reuse" option to fall back on for a direct provider-target run): the
    generated LiveKit api key/secret must be recovered — via
    `core.recover.recover_shared` — not regenerated, or every consumer already
    wired to the old value would silently break."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "livekit host(s)", "10.0.0.1"),
            ("text", "egress (leave blank", ""),
            (
                "text",
                "LiveKit domain (e.g. livekit.example.org)",
                "livekit.example.org",
            ),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", "turn.example.org"),
            (
                "text",
                "Public domain for meet (for the LiveKit recording webhook)",
                "meet.example.org",
            ),
            ("confirm", "livekit", True),
            ("confirm", "egress", True),
        ],
    )
    bootstrap.bootstrap("meet", "prod", component="livekit")
    lv_before = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    ev_before = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))

    sq = script_questionary(
        monkeypatch,
        [
            ("text", "livekit host(s)", ACCEPT_DEFAULT),
            ("text", "egress (leave blank", ACCEPT_DEFAULT),
            ("text", "LiveKit domain (e.g. livekit.example.org)", ACCEPT_DEFAULT),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", ACCEPT_DEFAULT),
            (
                "text",
                "Public domain for meet (for the LiveKit recording webhook)",
                ACCEPT_DEFAULT,
            ),
            ("confirm", "livekit", ACCEPT_DEFAULT),
            ("confirm", "egress", ACCEPT_DEFAULT),
        ],
    )
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
    """M7 regression: `-c <provider>` with `replay=REUSE`/`OVERRIDE` used to be
    silently downgraded to MODIFY (neither branch handling `action` applies
    to a provider target) — it must instead raise, naming the limitation,
    rather than quietly replaying a different action than the caller asked
    for."""
    seed_livekit_provider(repo)
    with pytest.raises(StCliError, match="applies to the core path only"):
        bootstrap.bootstrap("meet", "prod", component="livekit", replay=replay)


def _colocated_livekit_first_run_script(host: str = "10.0.0.1") -> list[tuple]:
    """A fresh `-c livekit` bootstrap, egress left blank (co-located)."""
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


def _colocated_livekit_replay_script(host=ACCEPT_DEFAULT) -> list[tuple]:
    """An Enter-through `-c livekit` replay of the script above (egress hosts
    stay blank — the co-located default — unless ``host`` types a move)."""
    return [
        ("text", "livekit host(s)", host),
        ("text", "egress (leave blank", ACCEPT_DEFAULT),
        ("text", "LiveKit domain (e.g. livekit.example.org)", ACCEPT_DEFAULT),
        ("text", "LiveKit TURN domain (e.g. turn.example.org)", ACCEPT_DEFAULT),
        (
            "text",
            "Public domain for meet (for the LiveKit recording webhook)",
            ACCEPT_DEFAULT,
        ),
        ("confirm", "livekit", ACCEPT_DEFAULT),
        ("confirm", "egress", ACCEPT_DEFAULT),
    ]


def test_livekit_replay_prefills_external_redis_and_never_rotates(repo, monkeypatch):
    """`-c livekit` replayed over an existing EXTERNAL-redis livekit + standalone
    egress unit (seeded, not bundled — different hosts, so co-location never
    fires): every prompt is Enter-through, INCLUDING the redis address/username,
    and NO password prompt fires at all — the password is recovered from the
    on-disk livekit vault (topology was already external), never re-asked. Both
    vaults stay byte-identical (write_vault's no-change check), proving the
    recovered redis password was re-mirrored, not rotated or dropped."""
    seed_livekit_provider(repo)
    seed_meet_egress_unit(repo, hosts=("10.0.0.2",))
    lk_vault_before = (repo / "meet/prod/livekit/vault.yml").read_bytes()
    ev_vault_before = (repo / "meet/prod/egress/vault.yml").read_bytes()

    sq = script_questionary(
        monkeypatch,
        [
            ("text", "livekit host(s)", ACCEPT_DEFAULT),
            ("text", "egress (leave blank", ACCEPT_DEFAULT),
            ("text", "LiveKit domain (e.g. livekit.example.org)", ACCEPT_DEFAULT),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", ACCEPT_DEFAULT),
            (
                "text",
                "Public domain for meet (for the LiveKit recording webhook)",
                "meet.example.org",
            ),
            ("confirm", "livekit", ACCEPT_DEFAULT),
            ("text", "Redis address shared by livekit and egress", ACCEPT_DEFAULT),
            ("text", "Redis username shared by livekit and egress", ACCEPT_DEFAULT),
            # NO "password" script entry — a re-prompt would raise here.
            ("confirm", "egress", ACCEPT_DEFAULT),
        ],
    )
    bootstrap.bootstrap("meet", "prod", component="livekit")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    lv = tree.load_vars("meet", "prod", "livekit")
    assert lv["st_meet_livekit_redis_address"] == "livekit-redis.example:6379"
    assert (repo / "meet/prod/livekit/vault.yml").read_bytes() == lk_vault_before
    assert (repo / "meet/prod/egress/vault.yml").read_bytes() == ev_vault_before


def test_egress_hand_edits_survive_livekit_replay(repo, monkeypatch):
    """A co-located `-c livekit` bootstrap bundles egress's vars.yml; a hand-added
    comment + custom var on that file must survive a later Enter-through `-c
    livekit` replay (the C3 load-mutate-save fix), and the file must not gain a
    second stamped header."""
    seed_creds(repo)
    script_questionary(monkeypatch, _colocated_livekit_first_run_script())
    bootstrap.bootstrap("meet", "prod", component="livekit")

    egress_vars = repo / "meet/prod/egress/vars.yml"
    hand_edited = (
        egress_vars.read_text() + "\n# hand note: keep this\nmy_custom_var: keepme\n"
    )
    egress_vars.write_text(hand_edited)

    sq = script_questionary(monkeypatch, _colocated_livekit_replay_script())
    bootstrap.bootstrap("meet", "prod", component="livekit")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    final = egress_vars.read_text()
    assert "my_custom_var: keepme" in final
    assert "# hand note: keep this" in final
    assert final.count("st-cli config for meet/egress") == 1


def test_colocated_egress_follows_livekit_host_move(repo, monkeypatch):
    """A co-located egress unit follows livekit when the operator retypes the
    livekit hosts on a replay (egress hosts left blank — the new co-located
    default per C1): both hosts files move to the new host, and neither keeps
    the old one. A further Enter-through replay (hosts unchanged) then leaves
    egress's hosts file byte-identical — the move is not repeated every run."""
    seed_creds(repo)
    script_questionary(monkeypatch, _colocated_livekit_first_run_script("10.0.0.1"))
    bootstrap.bootstrap("meet", "prod", component="livekit")

    sq = script_questionary(
        monkeypatch, _colocated_livekit_replay_script(host="10.0.0.9")
    )
    bootstrap.bootstrap("meet", "prod", component="livekit")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    lk_hosts = (repo / "meet/prod/livekit/hosts").read_text()
    ev_hosts = (repo / "meet/prod/egress/hosts").read_text()
    assert "10.0.0.9" in lk_hosts and "10.0.0.1" not in lk_hosts
    assert "10.0.0.9" in ev_hosts and "10.0.0.1" not in ev_hosts

    ev_hosts_before = (repo / "meet/prod/egress/hosts").read_bytes()
    sq2 = script_questionary(monkeypatch, _colocated_livekit_replay_script())
    bootstrap.bootstrap("meet", "prod", component="livekit")
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"
    assert (repo / "meet/prod/egress/hosts").read_bytes() == ev_hosts_before


def test_undecryptable_vault_aborts_before_any_prompt(repo, monkeypatch):
    """An unreadable vault.yml (wrong `.vault-pass`) aborts BEFORE the
    questionnaire runs a single prompt, and leaves the committed tree exactly
    as it was — the script below is intentionally EMPTY, so any prompt at all
    would raise inside ScriptedQuestionary instead of this test's expected
    StCliError."""
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


# --------------------------------------------------------------------------- #
# forced-replay / reuse-modify menu (the bug this rework closes: the old
# "Reuse" choice replayed no provider questionnaire yet still restamped the
# unit, silently clearing a pending rebootstrap flag)
# --------------------------------------------------------------------------- #
def _set_flags(monkeypatch, tmp_path, flags: list[dict]):
    """Seed a tmp upgrade-flag file and point core.upgrades at it —
    mirrors ``tests/test_upgrades.py``'s own ``_set_flags`` helper."""
    p = tmp_path / "upgrades.yml"
    y = tree.yaml()
    with p.open("w", encoding="utf-8") as fh:
        y.dump(flags, fh)
    monkeypatch.setattr(upgrades, "_RESOURCE", p)
    return p


def test_flagged_existing_dependency_skips_select_and_replays(
    repo, monkeypatch, tmp_path
):
    """A dependency provider with an outstanding rebootstrap flag must not
    offer the reuse/modify select at all — it forces a replay of its own
    questionnaire directly. Before this fix, "Reuse" replayed nothing yet
    still restamped the unit, silently clearing the flag."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        _meet_first_run_script(smtp=False)[:-1]  # drop the "bootstrap later" select
        + [
            ("select", "Bootstrap livekit now?", "Yes — bootstrap now"),
            ("text", "livekit host(s)", "10.0.0.1"),
            ("text", "egress (leave blank", ""),
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

    # a flag well above the version this unit was just stamped with
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "999.0.0", "apps": "all", "reason": "test flag", "link": ""}],
    )

    sq = script_questionary(
        monkeypatch,
        _meet_accept_through_script(smtp_enabled=False)[:-1]
        + [
            ("text", "livekit host(s)", ACCEPT_DEFAULT),
            ("text", "egress (leave blank", ACCEPT_DEFAULT),
            ("text", "LiveKit domain (e.g. livekit.example.org)", ACCEPT_DEFAULT),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", ACCEPT_DEFAULT),
            ("confirm", "livekit", ACCEPT_DEFAULT),
            ("confirm", "egress", ACCEPT_DEFAULT),
        ],
    )
    bootstrap.bootstrap("meet", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    # the select was never offered — the replay was forced, not chosen
    assert not any("Bootstrap livekit now?" in msg for msg, _ in sq.select_calls)

    m = manifest.load_manifest()
    unit = next(u for u in m.units if u.component == "livekit")
    assert unit.bootstrapped_with == __version__


def test_silent_flagged_existing_dependency_replays_without_prompt(
    repo, monkeypatch, tmp_path
):
    """M1 regression: a flagged EXISTING dependency forces the deploy branch
    (not reuse), so its non-secret shared-rule defaults (`recovered` at
    `_handle_dependency`'s deploy tail) must be wrapped in `Recovered`, or a
    silent replay stalls asking for the LiveKit domain/TURN domain the
    operator already answered. Seeds meet+livekit, flags `apps: ["meet"]`
    (which also flags the livekit unit, not only the core), rolls every
    stamp back, then replays with `ReplayAction.SILENT` and an EMPTY script:
    the flagged livekit dep must complete with zero prompts, and the tree
    stays byte-identical apart from the stamps."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        _meet_first_run_script(smtp=False)[:-1]  # drop the "bootstrap later" select
        + [
            ("select", "Bootstrap livekit now?", "Yes — bootstrap now"),
            ("text", "livekit host(s)", "10.0.0.1"),
            ("text", "egress (leave blank", ""),
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

    _set_flags(
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
    """An existing, unflagged dependency provider offers exactly two choices —
    "Reuse existing in the repo" (the default) or "Modify (replay the
    questionnaire)" — never the fresh-unit "No — bootstrap later"/"Already
    deployed" options, which would abandon or disown a unit that is already
    live."""
    seed_livekit_provider(repo)
    sq = script_questionary(
        monkeypatch,
        [
            ("text", "meet host(s)", "10.0.0.5"),
            ("text", "Public domain for meet", "meet.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://meet"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            ("text", "AWS_S3_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "AWS_S3_ACCESS_KEY_ID", "accesskey"),
            ("password", "AWS_S3_SECRET_ACCESS_KEY", "secretkey"),
            ("text", "AWS_STORAGE_BUCKET_NAME", "meet-media"),
            ("text", "AWS_S3_REGION_NAME (optional)", ""),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "meet-client-id"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            ("confirm", "Configure transactional email (SMTP) settings?", False),
            ("confirm", "cadvisor", True),
            ("select", "Bootstrap livekit now?", "Reuse existing in the repo"),
            ("confirm", "egress", True),  # egress cadvisor (created on reuse)
        ],
    )
    bootstrap.bootstrap("meet", "prod")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    dep_offers = [c for msg, c in sq.select_calls if "Bootstrap livekit now?" in msg]
    assert dep_offers == [
        ["Reuse existing in the repo", "Modify (replay the questionnaire)"]
    ]


# --------------------------------------------------------------------------- #
# the top-level 3-way select (`replay=ASK`, the CLI default, over an already-
# bootstrapped unit): Reuse and Override. The Modify branch is already
# covered — it is the select answer taken by the round-trip test above and by
# every other rebootstrap test via an explicit `replay=MODIFY`.
# --------------------------------------------------------------------------- #
def test_reuse_writes_nothing_and_warns_pending_flag(
    repo, monkeypatch, tmp_path, mocker
):
    """Picking "Reuse" on the top-level select returns before a single write:
    the stamp and every committed file stay exactly as they are, no
    vault-password prompt fires (Reuse returns before secret-backend setup
    even runs), and a pending rebootstrap flag on the targeted unit is warned
    about — Reuse can never clear it, only a real replay can."""
    seed_meet_unit(repo)
    _set_flags(
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
    """M10 regression: `_ask_rebootstrap_action` used to print pending flags
    for only the run's own targeted component(s) (core/workers) — a
    dependency provider's pending flag (e.g. livekit) never printed, so an
    operator picking Reuse never saw it was left pending. It must warn for
    EVERY flagged component of `(app, env)`, before the select fires."""
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
    """Picking "Override" then declining the destructive confirm raises and
    touches nothing — not even the questionnaire's first prompt runs."""
    seed_creds(repo)
    script_questionary(monkeypatch, _meet_first_run_script(smtp=False))
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
    """Accepting "Override" rebuilds meet/prod's core from an empty tree: a
    freshly generated DJANGO_SECRET_KEY (never the old one), a hand-added
    `st_*` var gone (the merge that would have kept it never runs), and the
    unit stamped like any other successful bootstrap run."""
    seed_creds(repo)
    script_questionary(monkeypatch, _meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")

    vault_before = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "meet"))

    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_my_custom_var"] = "custom-value"
    tree.save_vars("meet", "prod", "meet", data)

    # The secret-backend choice is already persisted, so — same as every other
    # rebootstrap script in this file — the "Secret backend:" select is not
    # asked again; the rest of the fresh questionnaire is identical to a
    # from-scratch run since OVERRIDE seeds nothing.
    override_script = _meet_first_run_script(smtp=False)[1:]
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
    """M2 regression: overriding the messages core, then landing on mta-in/mpa/
    socks-proxy (all already deployed), must NOT offer the ordinary
    reuse/modify select — an Override wipes the core's vars.yml/vault.yml
    (`fresh=True`/`replace=True`), and a plain "Reuse" only re-injects
    `shared` rules with a `consumer_env_key`; SPAM_CONFIG,
    MTA_OUT_DIRECT_PROXIES, `vault_mpa_auth_bearer`, and `vault_proxy_users`
    are constructed only by `_ask_messages_provider` on the deploy branch.
    With the fix, `override_core` forces each provider straight to that
    branch (no select), so the same run that wipes the core also rebuilds its
    wiring — using the providers' own still-intact, RECOVERED values, never
    rotating them."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
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
            ("select", "Bootstrap mta-in now?", "Yes — bootstrap now"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            ("text", "MYHOSTNAME", "mx.example.org"),
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

    mtain_vault_before = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "mta-in")
    )
    mpa_vault_before = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "mpa")
    )
    sp_vault_before = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "socks-proxy")
    )

    # OVERRIDE seeds nothing, so the core questionnaire is fresh (no defaults
    # to accept) — same values as the first run, just retyped. Every
    # dependency, in contrast, is pre-filled from its own (untouched)
    # committed tree — ACCEPT_DEFAULT — and offers NO select at all.
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
            # mta-in: no select — forced straight to the deploy branch.
            ("text", "mta-in host(s)", ACCEPT_DEFAULT),
            ("text", "MYHOSTNAME", ACCEPT_DEFAULT),
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
    mtain_vault_after = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "mta-in")
    )
    mpa_vault_after = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "mpa"))
    sp_vault_after = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "socks-proxy")
    )
    assert mpa_vault_after == mpa_vault_before
    assert sp_vault_after == sp_vault_before
    # mta-in's own MDA_API_SECRET DOES change: it mirrors the core's, and the
    # core's is a generated secret the override regenerates.
    assert mtain_vault_after != mtain_vault_before
    assert (
        mtain_vault_after["vault_mda_api_secret"]
        == core_vault_after["vault_mda_api_secret"]
    )


def test_override_core_recorded_external_dep_reprompts_constructed_value(
    repo, monkeypatch
):
    """M2 follow-up: does "Keep external (recorded)" silently drop a recorded
    external dependency's constructed value under Override, the same hole the
    managed-provider case had? Verified here rather than assumed: an OVERRIDE
    seeds NO core answers at all (`seed = {}`), so `only_missing and
    answers.get(key)` is always False for the messages/mpa SPAM_CONFIG
    special-case — "Keep external (recorded)" ends up re-prompting SPAM_CONFIG
    regardless, exactly like "Re-enter external values" would. No separate
    fix needed; this pins that behaviour."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
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
            ("select", "Bootstrap mta-in now?", "No — bootstrap later"),
            (
                "select",
                "Bootstrap mpa now?",
                "Already deployed (enter URL + keys)",
            ),
            (
                "password",
                "SPAM_CONFIG (JSON for the external mpa)",
                '{"rspamd_url": "https://ext-mpa.example.org", '
                '"rspamd_auth": "Bearer extbearer", "inbound_auth": "rspamd"}',
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
            ("select", "Bootstrap mta-in now?", "No — bootstrap later"),
            # mpa is recorded external — the default is "Keep external
            # (recorded)", picked via ACCEPT_DEFAULT.
            ("select", "Bootstrap mpa now?", ACCEPT_DEFAULT),
            (
                "password",
                "SPAM_CONFIG (JSON for the external mpa)",
                '{"rspamd_url": "https://ext-mpa-2.example.org", '
                '"rspamd_auth": "Bearer newbearer", "inbound_auth": "rspamd"}',
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
    """`replay=SILENT` never offers the top-level Modify/Reuse/Override select
    (the caller already decided) and prints the "kept/asked" stats line at the
    end. Every individual prompt auto-accepts its recovered default (see
    `core/prompts.py`'s `Recovered` marker), so an EMPTY script proves the
    select is skipped and every prompt is auto-accepted, then the stats
    print."""
    seed_creds(repo)
    script_questionary(monkeypatch, _meet_first_run_script(smtp=False))
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


# --------------------------------------------------------------------------- #
# Phase D1 — a recovered shared secret under hashi_vault is reused verbatim,
# never re-prompted (env_secret would ignore the value and re-prompt a fresh
# lookup term, repointing the committed ref).
# --------------------------------------------------------------------------- #
def test_hashi_recovered_shared_secret_not_reprompted(repo, monkeypatch):
    """`-c livekit` replayed under hashi_vault over a co-located unit: the
    committed LIVEKIT_API_KEY/SECRET lookup refs must be reused as-is — the
    script below has no "LIVEKIT_API_KEY"/"LIVEKIT_API_SECRET" text entry, so
    ScriptedQuestionary would raise if either fired a fresh term prompt."""
    seed_hashi_livekit_provider(repo)
    ref_key = tree.load_vars("meet", "prod", "livekit")["st_meet_livekit_api_key"]
    ref_secret = tree.load_vars("meet", "prod", "livekit")["st_meet_livekit_api_secret"]

    sq = script_questionary(
        monkeypatch,
        [
            ("text", "livekit host(s)", ACCEPT_DEFAULT),
            ("text", "egress (leave blank", ACCEPT_DEFAULT),
            ("text", "LiveKit domain (e.g. livekit.example.org)", ACCEPT_DEFAULT),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", ACCEPT_DEFAULT),
            (
                "text",
                "Public domain for meet (for the LiveKit recording webhook)",
                "meet.example.org",
            ),
            ("confirm", "livekit", ACCEPT_DEFAULT),
            ("confirm", "egress", ACCEPT_DEFAULT),
        ],
    )
    bootstrap.bootstrap("meet", "prod", component="livekit")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    lk_vars = tree.load_vars("meet", "prod", "livekit")
    assert lk_vars["st_meet_livekit_api_key"] == ref_key
    assert lk_vars["st_meet_livekit_api_secret"] == ref_secret


# --------------------------------------------------------------------------- #
# Phase D2 — a unit recorded "external" wins over a leftover local tree: the
# menu offers 3 options (keep / re-enter / manage locally), a wire-only run
# never offers a menu at all, and "keep" never re-prompts an already-recovered
# value.
# --------------------------------------------------------------------------- #
def test_external_recorded_unit_stays_external_on_full_replay(repo, monkeypatch):
    """A full `meet prod` Enter-through replay over a unit recorded external
    (with a leftover local livekit tree from before the flip): the select
    offers exactly the 3 new options defaulting to "Keep external (recorded)",
    no LIVEKIT_* value prompt fires (every consumer key was already recovered
    from the core's own committed env blob), the manifest keeps mode
    "external", and the leftover tree is untouched."""
    seed_external_livekit_with_leftover_tree(repo)
    lk_vars_before = (repo / "meet/prod/livekit/vars.yml").read_bytes()
    lk_vault_before = (repo / "meet/prod/livekit/vault.yml").read_bytes()
    lk_hosts_before = (repo / "meet/prod/livekit/hosts").read_bytes()

    sq = script_questionary(
        monkeypatch,
        _meet_accept_through_script(smtp_enabled=False)[:-1]
        + [("select", "Bootstrap livekit now?", ACCEPT_DEFAULT)],
    )
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
    """`-c meet` (wire-only) over the same recorded-external unit: no select is
    offered at all (wire-only can never deploy a provider, so "external" is
    taken directly), and the core round-trips byte-identical."""
    seed_external_livekit_with_leftover_tree(repo)
    core_vars_before = (repo / "meet/prod/meet/vars.yml").read_text()
    core_vault_before = (repo / "meet/prod/meet/vault.yml").read_bytes()

    sq = script_questionary(
        monkeypatch, _meet_accept_through_script(smtp_enabled=False)[:-1]
    )
    bootstrap.bootstrap(
        "meet", "prod", component="meet", replay=bootstrap.ReplayAction.MODIFY
    )
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    assert not any("Bootstrap livekit now?" in msg for msg, _ in sq.select_calls)
    assert (repo / "meet/prod/meet/vars.yml").read_text() == core_vars_before
    assert (repo / "meet/prod/meet/vault.yml").read_bytes() == core_vault_before


def test_external_redo_reprompts_values(repo, monkeypatch):
    """Choosing "Re-enter external values (URL + keys)" re-asks every shared
    rule regardless of what was already recovered, and keeps the unit
    recorded external (only its VALUES change, not its mode)."""
    seed_external_livekit_with_leftover_tree(repo)

    sq = script_questionary(
        monkeypatch,
        _meet_accept_through_script(smtp_enabled=False)[:-1]
        + [
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
    """A first run answers "No — bootstrap later" for livekit: the core commits
    LIVEKIT_API_KEY=/LIVEKIT_API_SECRET=/LIVEKIT_API_URL= as EMPTY lines (no
    consumer value was ever injected for a skipped dependency), and
    `recover()` brings each one back as ``""``. A later "Already deployed
    (enter URL + keys)" adopt must not read that recovered ``""`` as an
    already-decided answer — the skip check needs a truthy test, not
    ``key in answers``, or the adopt silently skips every prompt and commits
    the unit with the same three empty lines."""
    seed_creds(repo)
    script_questionary(monkeypatch, _meet_first_run_script(smtp=False))
    bootstrap.bootstrap("meet", "prod")

    core_vars_before = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "LIVEKIT_API_KEY=" in core_vars_before
    assert "LIVEKIT_API_SECRET=" in core_vars_before
    assert "LIVEKIT_API_URL=" in core_vars_before
    core_vault_before = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "meet"))
    assert "vault_livekit_api_key" not in core_vault_before

    sq = script_questionary(
        monkeypatch,
        _meet_accept_through_script(smtp_enabled=False)[:-1]
        + [
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


# --------------------------------------------------------------------------- #
# honest gate wording + mode-switch warnings — unit-level (not full round
# trips): each targets one gate/warning in isolation.
# --------------------------------------------------------------------------- #
def test_smtp_confirm_uses_review_wording_when_recovered(monkeypatch):
    """Once DJANGO_EMAIL_HOST is recovered, the confirm asks to review the
    existing config instead of asking as if SMTP were never configured."""
    answers = {"DJANGO_EMAIL_HOST": "smtp.example.org"}
    backend = AnsibleVaultBackend()
    sq = script_questionary(
        monkeypatch,
        [("confirm", "SMTP is configured — review its settings?", False)],
    )
    bootstrap._ask_email(answers, backend, "meet", "meet")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"


def test_smtp_confirm_keeps_first_run_wording_when_unconfigured(monkeypatch):
    """No recovered DJANGO_EMAIL_HOST: the confirm keeps asking as a fresh
    setup question, not a review of something that doesn't exist yet."""
    answers: dict = {}
    backend = AnsibleVaultBackend()
    sq = script_questionary(
        monkeypatch,
        [("confirm", "Configure transactional email (SMTP) settings?", False)],
    )
    bootstrap._ask_email(answers, backend, "meet", "meet")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"


def test_blobs_confirm_uses_review_wording_when_recovered(monkeypatch):
    """Once MESSAGES_BLOBS_OFFLOAD_ENABLED is recovered as truthy, the confirm
    asks to review the existing config instead of asking as if offload were
    still off."""
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
    """Recovered relay, operator switches to direct: envblob.merge never
    deletes a line, so the relay lines stay in the committed tree until an
    operator removes them by hand — warn about that instead of silently
    leaving a still-relay config."""
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
    """No recovered relay mode: staying on direct is a no-op, not a switch —
    no cleanup warning is warranted."""
    answers: dict = {}
    backend = AnsibleVaultBackend()
    script_questionary(monkeypatch, [("select", "Outbound mail mode", "direct")])
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")

    bootstrap._ask_messages_outbound(answers, backend, "messages")

    warn_spy.assert_not_called()


def test_blank_relay_username_clears_password_and_warns(monkeypatch, mocker):
    """Blanking a recovered MTA_OUT_RELAY_USERNAME must also drop the
    recovered MTA_OUT_RELAY_PASSWORD — leaving the password key behind while
    the username is gone would commit a half-active auth config. Both the
    ``_ask_optional`` clear (the username) and this function's own clear (the
    password) must warn — once each."""
    answers = {
        "MTA_OUT_MODE": "relay",
        "MTA_OUT_RELAY_HOST": "smtp.example.org:587",
        "MTA_OUT_RELAY_USERNAME": "relayuser",
        "MTA_OUT_RELAY_PASSWORD": "{{ vault_mta_out_relay_password }}",
    }
    backend = AnsibleVaultBackend()
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Outbound mail mode", ACCEPT_DEFAULT),
            ("text", "MTA_OUT_RELAY_HOST", ACCEPT_DEFAULT),
            ("text", "MTA_OUT_RELAY_USERNAME (optional, blank = no auth)", ""),
        ],
    )
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")

    bootstrap._ask_messages_outbound(answers, backend, "messages")

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert "MTA_OUT_RELAY_USERNAME" not in answers
    assert "MTA_OUT_RELAY_PASSWORD" not in answers
    assert warn_spy.call_count == 2


def test_db_mode_switch_warns_both_directions(monkeypatch, mocker):
    """Switching DB_* <-> DATABASE_URL leaves the old shape's lines (and, for a
    switch away from DATABASE_URL, its vault entry) committed — warn in both
    directions instead of leaving two conflicting DB configs in place."""
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
    """Keeping the same DB mode (nothing to switch away from) never warns."""
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
    """M6 regression: a total DB recovery gap (neither `DATABASE_URL` nor
    `DB_HOST` recovered) is a genuine new question, not a mode switch — the
    select must surface even under `silent_replay()`, not silently default to
    "DATABASE_URL" and let the later required `DATABASE_URL` prompt fire on a
    maybe-discrete unit."""
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


# --------------------------------------------------------------------------- #
# _resolve_egress_redis_password: never re-prompt a DECIDED secret, but an
# empty legacy store or a moved redis address must still reach the prompt.
# --------------------------------------------------------------------------- #
def test_egress_redis_password_blank_legacy_store_is_reprompted(repo, monkeypatch):
    """A legacy on-disk livekit vault with an EMPTY stored redis password
    (``st_meet_livekit_redis_password: ""``) must not read as a decided
    secret — an empty stored value is a blank-auth artifact, not a real
    password, so the prompt stays reachable and the freshly typed value is
    returned."""
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
    """``reuse_disk=False`` (the operator typed a NEW redis address on this
    replay) must not silently carry the OLD server's on-disk password
    forward — the prompt fires instead of reusing a password that belonged
    to a different address."""
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


# --------------------------------------------------------------------------- #
# messages DOMAIN pre-fill comma guard
# --------------------------------------------------------------------------- #
class _Stop(Exception):
    """Raised by the fake _ask below to short-circuit _ask_core once the
    domain prompt's pre-fill has been captured — the rest of the (long) core
    questionnaire is irrelevant to this test."""


def _capture_domain_default(monkeypatch, captured: dict):
    real_ask = bootstrap._ask

    def _fake_ask(prompt, default="", **kwargs):
        if prompt.startswith("Public domain"):
            captured["default"] = default
            raise _Stop
        return real_ask(prompt, default, **kwargs)

    monkeypatch.setattr(bootstrap, "_ask", _fake_ask)


def test_domain_prefill_skips_comma_separated_allowed_hosts(monkeypatch):
    """A hand-edited, comma-separated DJANGO_ALLOWED_HOSTS must not become the
    DOMAIN pre-fill — it would poison every value derived from DOMAIN
    (DJANGO_CSRF_TRUSTED_ORIGINS, etc.)."""
    meta = appmeta.load_app("messages")
    backend = AnsibleVaultBackend()
    seed = {"DJANGO_ALLOWED_HOSTS": "messages.example.org,other.example.org"}
    captured: dict = {}
    _capture_domain_default(monkeypatch, captured)

    with pytest.raises(_Stop):
        bootstrap._ask_core(meta, backend, seed)

    assert captured["default"] == ""


def test_domain_prefill_keeps_single_host_allowed_hosts(monkeypatch):
    """A single-host (no comma) DJANGO_ALLOWED_HOSTS still pre-fills DOMAIN —
    the comma guard must not degrade the ordinary recovery path."""
    meta = appmeta.load_app("messages")
    backend = AnsibleVaultBackend()
    seed = {"DJANGO_ALLOWED_HOSTS": "messages.example.org"}
    captured: dict = {}
    _capture_domain_default(monkeypatch, captured)

    with pytest.raises(_Stop):
        bootstrap._ask_core(meta, backend, seed)

    assert captured["default"] == "messages.example.org"


# --------------------------------------------------------------------------- #
# Phase B1 — MESSAGES_BLOBS_ENCRYPT_KEYS: recovered verbatim, rotation slots
# survive (never rebuilt from a single freshly-generated key).
# --------------------------------------------------------------------------- #
def test_blobs_encrypt_keys_multislot_survives_replay(repo, monkeypatch):
    """An operator who hand-appends a second key slot to the committed
    MESSAGES_BLOBS_ENCRYPT_KEYS JSON (a key-rotation slot) must see it survive
    an Enter-through replay byte-identical: the JSON is recovered verbatim and
    reused, not rebuilt from a single freshly-composed slot."""
    seed_creds(repo)
    sq1 = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
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
            ("confirm", "Enable blobs offloading", True),
            ("text", "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_BLOBS_BUCKET_NAME", "msg-blobs"),
            ("text", "STORAGE_MESSAGE_BLOBS_ACCESS_KEY", "blobkey"),
            ("password", "STORAGE_MESSAGE_BLOBS_SECRET_KEY", "blobsecret"),
            ("text", "STORAGE_MESSAGE_BLOBS_REGION_NAME", ""),
            ("text", "OPENSEARCH_URL", "http://opensearch:9200"),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", "mail.example.org"),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "messages-client-id"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            ("select", "Outbound mail mode", "direct"),
            ("confirm", "cadvisor", True),
            ("select", "Bootstrap mta-in now?", "No — bootstrap later"),
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

    sq2 = script_questionary(
        monkeypatch,
        [
            ("text", "messages host(s)", ACCEPT_DEFAULT),
            ("text", "workers (leave blank", ACCEPT_DEFAULT),
            ("text", "Public domain for messages", ACCEPT_DEFAULT),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", ACCEPT_DEFAULT),
            (
                "confirm",
                "Blobs offloading is enabled — review its settings?",
                ACCEPT_DEFAULT,
            ),
            ("text", "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_BLOBS_BUCKET_NAME", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_BLOBS_ACCESS_KEY", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_BLOBS_REGION_NAME", ACCEPT_DEFAULT),
            ("text", "OPENSEARCH_URL", ACCEPT_DEFAULT),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", ACCEPT_DEFAULT),
            ("select", "Identity provider:", ACCEPT_DEFAULT),
            ("text", "Keycloak base URL", ACCEPT_DEFAULT),
            ("text", "Keycloak realm", ACCEPT_DEFAULT),
            ("text", "OIDC_RP_CLIENT_ID", ACCEPT_DEFAULT),
            ("select", "Outbound mail mode", "direct"),
            ("confirm", "cadvisor", ACCEPT_DEFAULT),
            ("select", "Bootstrap mta-in now?", "No — bootstrap later"),
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
            ("select", "Bootstrap socks-proxy now?", "No — bootstrap later"),
        ],
    )
    bootstrap.bootstrap("messages", "prod", replay=bootstrap.ReplayAction.MODIFY)
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    assert core_vars_path.read_text() == core_vars_before
    assert (repo / "messages/prod/messages/vault.yml").read_bytes() == core_vault_before


def test_hashi_recovered_blobs_encrypt_keys_not_reprompted(monkeypatch):
    """Under the hashi backend, a recovered MESSAGES_BLOBS_ENCRYPT_KEYS (a
    composed JSON embedding a lookup ref) must not trigger a fresh lookup-term
    prompt: the script below has NO "MESSAGES_BLOBS_ENCRYPT_KEY" text entry, so
    ScriptedQuestionary would raise on an unscripted prompt if one fired."""
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
    sq = script_questionary(
        monkeypatch,
        [
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME (optional)", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", ACCEPT_DEFAULT),
            (
                "confirm",
                "Blobs offloading is enabled — review its settings?",
                True,
            ),
            ("text", "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_BLOBS_BUCKET_NAME", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_BLOBS_ACCESS_KEY", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_BLOBS_REGION_NAME (optional)", ACCEPT_DEFAULT),
        ],
    )
    bootstrap._ask_messages_storage(answers, backend, "messages")
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert answers["MESSAGES_BLOBS_ENCRYPT_KEYS"] == encrypt_keys_json


# --------------------------------------------------------------------------- #
# Phase B2 — OIDC endpoints: a recovered "custom" provider's hand-edited
# OIDC_OP_* endpoints are never blanked; switching TO custom warns and keeps
# the old provider's committed endpoints in place.
# --------------------------------------------------------------------------- #
def test_custom_oidc_endpoints_survive_replay(monkeypatch):
    """A "custom" provider has no per-endpoint prompt — the operator hand-edits
    each OIDC_OP_* line directly in vars.yml. envrender.oidc_endpoints only
    ever derives OIDC_OP_URL for "custom", so an unconditional answers.update
    used to blank every other hand-edited endpoint back to "" on replay."""
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
    script_questionary(
        monkeypatch,
        [
            ("select", "Identity provider:", "custom"),
            ("text", "Custom OIDC issuer base URL (optional)", ACCEPT_DEFAULT),
            ("text", "OIDC_RP_CLIENT_ID", ACCEPT_DEFAULT),
        ],
    )
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
    """Switching the identity provider to "custom" FROM a different recovered
    provider (keycloak here) must warn that the committed OIDC_OP_* lines stay
    in place and need hand-editing — and must not blank them: oidc_endpoints
    for "custom" with no base URL returns nothing, and the update filters
    empty values out."""
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
    """A keycloak-shaped recovered OIDC_OP_JWKS_ENDPOINT lets recover_oidc infer
    the provider/base/realm; an Enter-through replay (same base + realm) must
    not overwrite a hand-edited OIDC_OP_TOKEN_ENDPOINT — ``setdefault`` only
    fills a key missing from ``answers``, so the hand edit wins over the
    recomputed derived default."""
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
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Identity provider:", ACCEPT_DEFAULT),
            ("text", "Keycloak base URL", ACCEPT_DEFAULT),
            ("text", "Keycloak realm", ACCEPT_DEFAULT),
            ("text", "OIDC_RP_CLIENT_ID", ACCEPT_DEFAULT),
        ],
    )

    bootstrap._ask_oidc(answers, backend, "meet")

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert answers["OIDC_OP_TOKEN_ENDPOINT"] == "https://proxy.internal/token"


def test_keycloak_changed_base_recomputes_all_endpoints(monkeypatch):
    """Typing a NEW keycloak base URL on the replay must recompute every
    OIDC_OP_* endpoint — a hand-edited OIDC_OP_TOKEN_ENDPOINT from the OLD
    base is not silently carried forward onto the new one."""
    old_base = "https://idp.example.org/realms/master/protocol/openid-connect"
    answers = {
        "OIDC_OP_URL": "https://idp.example.org/realms/master",
        "OIDC_OP_JWKS_ENDPOINT": f"{old_base}/certs",
        "OIDC_OP_TOKEN_ENDPOINT": "https://proxy.internal/token",
        "OIDC_RP_CLIENT_ID": "client-id",
        "OIDC_RP_CLIENT_SECRET": "{{ vault_oidc_rp_client_secret }}",
    }
    backend = AnsibleVaultBackend()
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Identity provider:", ACCEPT_DEFAULT),
            ("text", "Keycloak base URL", "https://new-idp.example.org"),
            ("text", "Keycloak realm", ACCEPT_DEFAULT),
            ("text", "OIDC_RP_CLIENT_ID", ACCEPT_DEFAULT),
        ],
    )

    bootstrap._ask_oidc(answers, backend, "meet")

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert answers["OIDC_OP_TOKEN_ENDPOINT"] == (
        "https://new-idp.example.org/realms/master/protocol/openid-connect/token"
    )


def test_custom_oidc_trailing_slash_survives_byte_identical(monkeypatch):
    """A recovered custom-provider OIDC_OP_URL with a trailing slash must
    survive an Enter-through replay byte-identically — ``oidc_endpoints``
    always strips the trailing slash when it recomputes OIDC_OP_URL, so only
    a ``setdefault`` (never an unconditional update) keeps the exact
    recovered string on an unchanged replay."""
    answers = {
        "OIDC_OP_URL": "https://idp.example.org/",
        "OIDC_OP_JWKS_ENDPOINT": "https://idp.example.org/jwks",
        "OIDC_RP_CLIENT_ID": "client-id",
        "OIDC_RP_CLIENT_SECRET": "{{ vault_oidc_rp_client_secret }}",
    }
    backend = AnsibleVaultBackend()
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Identity provider:", ACCEPT_DEFAULT),
            ("text", "Custom OIDC issuer base URL (optional)", ACCEPT_DEFAULT),
            ("text", "OIDC_RP_CLIENT_ID", ACCEPT_DEFAULT),
        ],
    )

    bootstrap._ask_oidc(answers, backend, "meet")

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert answers["OIDC_OP_URL"] == "https://idp.example.org/"


# --------------------------------------------------------------------------- #
# Phase B3 — DOMAIN-derived keys: kept domain preserves hand-edits (setdefault),
# changed domain fully recomputes (update), and an unrecoverable DOMAIN (the
# messages multi-host case) never forces a recompute.
# --------------------------------------------------------------------------- #
def _drive_core_seed(domain: str) -> dict:
    return {
        "DOMAIN": domain,
        "DJANGO_ALLOWED_HOSTS": domain,
        "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{domain}",
        "DJANGO_CORS_ALLOWED_ORIGINS": "https://custom-cors.example.org",
        "DATABASE_URL": "{{ vault_database_url }}",
        "REDIS_URL": "{{ vault_redis_url }}",
        "S3_PROTOCOL": "https",
        "S3_HOST": "s3.example.org",
        "S3_BUCKET": "drive-media",
        "AWS_S3_ACCESS_KEY_ID": "accesskey",
        "AWS_S3_SECRET_ACCESS_KEY": "{{ vault_aws_s3_secret_access_key }}",
        "OIDC_OP_URL": "https://idp.example.org/realms/master",
        "OIDC_OP_JWKS_ENDPOINT": (
            "https://idp.example.org/realms/master/protocol/openid-connect/certs"
        ),
        "OIDC_RP_CLIENT_ID": "drive-client-id",
        "OIDC_RP_CLIENT_SECRET": "{{ vault_oidc_rp_client_secret }}",
    }


def _drive_core_script(domain_answer) -> list[tuple]:
    return [
        ("text", "Public domain for drive", domain_answer),
        ("select", "Database configuration:", "DATABASE_URL"),
        ("text", "AWS_S3_ENDPOINT_URL", ACCEPT_DEFAULT),
        ("text", "AWS_S3_ACCESS_KEY_ID", ACCEPT_DEFAULT),
        ("text", "AWS_STORAGE_BUCKET_NAME", ACCEPT_DEFAULT),
        ("text", "AWS_S3_REGION_NAME (optional)", ACCEPT_DEFAULT),
        ("select", "Identity provider:", ACCEPT_DEFAULT),
        ("text", "Keycloak base URL", ACCEPT_DEFAULT),
        ("text", "Keycloak realm", ACCEPT_DEFAULT),
        ("text", "OIDC_RP_CLIENT_ID", ACCEPT_DEFAULT),
        ("confirm", "Configure transactional email (SMTP) settings?", ACCEPT_DEFAULT),
    ]


def test_kept_domain_preserves_hand_edited_derived_keys(monkeypatch):
    """Retyping the SAME recovered DOMAIN must not recompute the derived
    keys — a hand-edited DJANGO_CORS_ALLOWED_ORIGINS survives via setdefault
    instead of being overwritten by the recomputed default."""
    meta = appmeta.load_app("drive")
    backend = AnsibleVaultBackend()
    seed = _drive_core_seed("drive.example.org")
    script_questionary(monkeypatch, _drive_core_script(ACCEPT_DEFAULT))

    answers = bootstrap._ask_core(meta, backend, seed)

    assert answers["DOMAIN"] == "drive.example.org"
    assert answers["DJANGO_CORS_ALLOWED_ORIGINS"] == "https://custom-cors.example.org"
    # a key that was NOT already recovered still gets the computed default —
    # setdefault only protects a key already present.
    assert answers["LOGIN_REDIRECT_URL"] == "https://{{ st_drive_public_host }}/"


def test_changed_domain_recomputes_derived_keys(monkeypatch):
    """Typing a NEW domain on a rebootstrap fully recomputes every
    DOMAIN-derived key — a stale hand-edited DJANGO_CORS_ALLOWED_ORIGINS from
    the OLD domain is not silently carried forward onto the new one."""
    meta = appmeta.load_app("drive")
    backend = AnsibleVaultBackend()
    seed = _drive_core_seed("old.example.org")
    script_questionary(monkeypatch, _drive_core_script("new.example.org"))

    answers = bootstrap._ask_core(meta, backend, seed)

    assert answers["DOMAIN"] == "new.example.org"
    assert answers["DJANGO_ALLOWED_HOSTS"] == "new.example.org"
    assert answers["DJANGO_CSRF_TRUSTED_ORIGINS"] == "https://new.example.org"
    assert answers["DJANGO_CORS_ALLOWED_ORIGINS"] == "https://new.example.org"
    assert answers["LOGIN_REDIRECT_URL"] == "https://{{ st_drive_public_host }}/"


def test_multihost_allowed_hosts_survive_unrecoverable_domain_replay(monkeypatch):
    """messages has no DOMAIN component var, so a comma-separated (hand-edited
    multi-host) DJANGO_ALLOWED_HOSTS makes DOMAIN unrecoverable (see the comma
    guard above `_ask_core`). Retyping a domain in that state must not force a
    full recompute — the recovered multi-host DJANGO_ALLOWED_HOSTS survives via
    setdefault even though the retyped DOMAIN differs from any single host in
    it."""
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
    script_questionary(
        monkeypatch,
        [
            ("text", "Public domain for messages", "messages.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME (optional)", ACCEPT_DEFAULT),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", ACCEPT_DEFAULT),
            ("confirm", "Enable blobs offloading", False),
            ("text", "OPENSEARCH_URL", ACCEPT_DEFAULT),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", ACCEPT_DEFAULT),
            ("select", "Identity provider:", ACCEPT_DEFAULT),
            ("text", "Keycloak base URL", ACCEPT_DEFAULT),
            ("text", "Keycloak realm", ACCEPT_DEFAULT),
            ("text", "OIDC_RP_CLIENT_ID", ACCEPT_DEFAULT),
            ("select", "Outbound mail mode", "direct"),
        ],
    )
    answers = bootstrap._ask_core(meta, backend, seed)

    assert answers["DOMAIN"] == "messages.example.org"
    assert answers["DJANGO_ALLOWED_HOSTS"] == "messages.example.org,other.example.org"


# --------------------------------------------------------------------------- #
# Phase E — _ask_optional: a blank answer over a recovered value pops the key
# and warns; an Enter-through replay keeps the value with no warn.
# --------------------------------------------------------------------------- #
def test_ask_optional_blank_clears_recovered_value_and_warns(monkeypatch, mocker):
    """Blanking a recovered DJANGO_EMAIL_HOST_USER pops the key from ``answers``
    and warns that the committed line must be removed by hand (the merge never
    deletes it on its own)."""
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
    """Accepting the recovered default (Enter-through) keeps the value in
    ``answers`` and never warns."""
    answers = {"DJANGO_EMAIL_HOST_USER": "smtpuser"}
    script_questionary(
        monkeypatch,
        [("text", "DJANGO_EMAIL_HOST_USER (optional)", ACCEPT_DEFAULT)],
    )
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")

    bootstrap._ask_optional(
        answers, "DJANGO_EMAIL_HOST_USER", "DJANGO_EMAIL_HOST_USER (optional)"
    )

    assert answers["DJANGO_EMAIL_HOST_USER"] == "smtpuser"
    warn_spy.assert_not_called()


# --------------------------------------------------------------------------- #
# Work item 2 — silent-replay call-site wiring (`Recovered`, the two select
# defaults, `_handle_dependency`'s fresh/silent dispatch, the hashi reuse
# guard). An EMPTY `ScriptedQuestionary` script is the no-prompt proof: any
# unscripted prompt raises inside `ScriptedQuestionary` instead of the test
# quietly passing.
# --------------------------------------------------------------------------- #
def test_silent_enter_through_meet_with_livekit_byte_identical_and_dep_reused(
    repo, monkeypatch
):
    """A fully-recoverable meet unit (livekit deployed, co-located egress),
    replayed with `replay=SILENT` and an EMPTY script: every prompt
    auto-accepts its `Recovered` default (or, for the two selects with no
    recoverable default, the explicit "DATABASE_URL"/"direct" default this
    wave adds), the existing-unflagged livekit dependency auto-reuses through
    its own default with NO select actually consumed, the whole tree
    (core+livekit+egress) stays byte-identical, and every unit's stamp
    advances back to the current version."""
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
    """A fully-recoverable messages unit (relay outbound + blobs offload, every
    optional field answered non-blank), replayed with `replay=SILENT` and an
    EMPTY script: the storage/outbound gates all auto-accept, and the two
    still-fresh optional deps (mta-in, mpa — never bootstrapped) skip quietly
    with no select and register no unit (socks-proxy is excluded from the
    deps loop entirely while outbound stays relay, so it needs no assertion)."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "messages host(s)", "10.0.0.4"),
            ("text", "workers (leave blank", ""),
            ("text", "Public domain for messages", "messages.example.org"),
            ("select", "Database configuration:", "discrete (DB_*)"),
            ("text", "DB_HOST", "msgdb.example.org"),
            ("text", "DB_NAME", "messagesdb"),
            ("text", "DB_USER", "messagesuser"),
            ("password", "DB_PASSWORD", "msgdbpass"),
            ("text", "DB_PORT", "5432"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", "msg-imports"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", "impkey"),
            ("password", "STORAGE_MESSAGE_IMPORTS_SECRET_KEY", "impsecret"),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", "fr-par"),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
            ("confirm", "Enable blobs offloading", True),
            ("text", "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_BLOBS_BUCKET_NAME", "msg-blobs"),
            ("text", "STORAGE_MESSAGE_BLOBS_ACCESS_KEY", "blobkey"),
            ("password", "STORAGE_MESSAGE_BLOBS_SECRET_KEY", "blobsecret"),
            ("text", "STORAGE_MESSAGE_BLOBS_REGION_NAME", "fr-par"),
            ("text", "OPENSEARCH_URL", "http://opensearch:9200"),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", "mail.example.org"),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "messages-client-id"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            ("select", "Outbound mail mode", "relay"),
            ("text", "MTA_OUT_RELAY_HOST", "smtp.example.org:587"),
            ("text", "MTA_OUT_RELAY_USERNAME", "relayuser"),
            ("password", "MTA_OUT_RELAY_PASSWORD", "relaypass"),
            ("confirm", "cadvisor", True),
            ("select", "Bootstrap mta-in now?", "No — bootstrap later"),
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
    assert not any(u.component in ("mta-in", "mpa") for u in m2.units)
    unit = next(u for u in m2.units if u.component == "messages")
    assert unit.bootstrapped_with == __version__


def test_silent_dep_dispatch_offered_component_shows_menu_then_declines(
    repo, monkeypatch, tmp_path
):
    """A flag naming a component in `new_components` turns the usual quiet
    auto-skip into a real menu: the offer (version/reason/link) is printed,
    the fresh-provider select fires for real (scripted, decline), and
    declining registers no unit and prints the `-c livekit` hint. The flagged
    EXISTING units (the meet core) still get silently replayed and restamped
    in the same run, so a follow-up `new_component_offers` call against the
    saved manifest is empty — the offer goes quiet on its own."""
    seed_creds(repo)
    script_questionary(
        monkeypatch, _meet_first_run_script_fully_recoverable(with_livekit=False)
    )
    bootstrap.bootstrap("meet", "prod")

    m = manifest.load_manifest()
    for u in m.units:
        u.bootstrapped_with = "0.0.1"
    manifest.save_manifest(m)

    _set_flags(
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
    """The offer's version/reason/link are actually printed via `ui.info`
    before the fresh menu — not just inferred from the select firing."""
    seed_creds(repo)
    script_questionary(
        monkeypatch, _meet_first_run_script_fully_recoverable(with_livekit=False)
    )
    bootstrap.bootstrap("meet", "prod")

    m = manifest.load_manifest()
    for u in m.units:
        u.bootstrapped_with = "0.0.1"
    manifest.save_manifest(m)

    _set_flags(
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
    # path — that path's own message must not appear alongside it.
    assert not any("not bootstrapped" in msg for msg in printed)


def test_silent_dep_dispatch_offered_component_external_choice_asks_for_real(
    repo, monkeypatch
):
    """M4 regression: before the fix, only the "deploy" branch of an offered
    fresh dependency's post-menu handling ran inside `suspend_silent()` — the
    "external" branch stayed under the OUTER silent context, so a shared
    rule's `_shared_default` (which wraps an already-decided `answers[key]` in
    `Recovered`) would auto-accept it with no prompt. This calls
    `_handle_dependency` directly for `drive`'s `collabora` dependency (its
    one shared rule has an `answer_key`, `COLLABORA_DOMAIN`) with that key
    pre-seeded as `Recovered` in `answers` (simulating an earlier step of the
    same replay having already decided it) — a fresh, offered dependency must
    still ask, never silently reuse a value from elsewhere in the run."""
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
    """Simulates a release that adds one new mandatory setting: a patched env
    template gains a new key, and a patched `_ask_core` collects it via the
    ordinary `_ask` primitive with a plain (non-`Recovered`) fallback. Under a
    SILENT replay it is the only prompt that fires (the script holds exactly
    one entry) — everything else auto-accepts from the recovered tree — and
    the new key is appended behind the `envblob.merge` marker, leaving every
    other committed line untouched."""
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
    """A hashi_vault-backed meet+livekit unit, replayed SILENTLY with an EMPTY
    script: the reuse branch's guard (`_handle_dependency` skips
    `writer.inject_consumer` for a secret shared rule whose consumer key is
    already truthy in `answers`) is what makes this possible. Without it,
    `HashiVaultBackend.env_secret` would unconditionally prompt a fresh
    LIVEKIT_API_KEY/LIVEKIT_API_SECRET lookup term even though both were
    already recovered from the core's own committed env blob — the script
    below has NO such entry, so `ScriptedQuestionary` would raise on it."""
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
