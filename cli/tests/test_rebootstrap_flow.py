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

import pytest
from ruamel.yaml.scalarstring import LiteralScalarString

from st_cli import __version__
from st_cli.cmd import bootstrap
from st_cli.core import manifest, paths, tree, vault
from st_cli.core.errors import StCliError

from helpers import ACCEPT_DEFAULT, seed_creds, script_questionary


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
        ("confirm", "Configure transactional email (SMTP) settings?", ACCEPT_DEFAULT),
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


# --------------------------------------------------------------------------- #
# the round trip (meet + drive + messages)
# --------------------------------------------------------------------------- #
def test_meet_round_trip_byte_identical_and_smtp_gate_stays_on(repo, monkeypatch):
    """Bootstrap meet (SMTP configured), snapshot vars.yml/vault.yml, rebootstrap
    accepting every default: both files are byte-identical, and — the gate
    trap this whole feature exists to close — SMTP is STILL configured
    afterwards (an Enter-through run must not silently decline the SMTP
    confirm and drop the whole block)."""
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
        monkeypatch, _meet_accept_through_script(smtp_enabled=True)
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
    bootstrap.bootstrap("meet", "prod")
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
    bootstrap.bootstrap("drive", "prod")
    assert not sq2._scripts, f"unconsumed scripts: {sq2._scripts}"

    assert (repo / "drive/prod/drive/vars.yml").read_text() == core_vars_before
    assert (repo / "drive/prod/drive/vault.yml").read_bytes() == core_vault_before
    assert (repo / "drive/prod/collabora/vars.yml").read_text() == collabora_vars_before
    assert not paths.vault_path("drive", "prod", "collabora").exists()


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
            ("confirm", "Enable blobs offloading", ACCEPT_DEFAULT),
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
    bootstrap.bootstrap("messages", "prod")
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
    bootstrap.bootstrap("meet", "prod")

    new_text = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "st_meet_my_custom_var: custom-value" in new_text
    assert "my comment" in new_text
    assert "MY_VAR=1" in new_text


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
    bootstrap.bootstrap("meet", "prod")

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
    bootstrap.bootstrap("meet", "prod")

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
    bootstrap.bootstrap("meet", "prod")
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
