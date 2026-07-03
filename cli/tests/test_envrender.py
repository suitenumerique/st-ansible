"""Tests for st_cli.core.envrender — per-component env blob + OIDC endpoint rendering."""

from __future__ import annotations

from st_cli.core import envrender


def test_oidc_keycloak_derivation():
    ep = envrender.oidc_endpoints("keycloak", "https://id.example.org", "realm1")
    base = "https://id.example.org/realms/realm1/protocol/openid-connect"
    assert ep["OIDC_OP_TOKEN_ENDPOINT"] == f"{base}/token"


def test_proconnect_presets():
    prod = envrender.oidc_endpoints("proconnect-prod", None, None)
    assert prod["OIDC_OP_URL"] == "https://auth.agentconnect.gouv.fr/api/v2"
    assert prod["OIDC_OP_AUTHORIZATION_ENDPOINT"].endswith("/api/v2/authorize")
    assert prod["OIDC_OP_JWKS_ENDPOINT"].endswith("/api/v2/jwks")
    integ = envrender.oidc_endpoints("proconnect-integ", None, None)
    assert integ["OIDC_OP_URL"] == "https://fca.integ01.dev-agentconnect.fr/api/v2"
    assert integ["OIDC_OP_LOGOUT_ENDPOINT"].endswith("/session/end")
    assert "TODO" not in "".join(prod.values()) + "".join(integ.values())


def test_render_meet_backend_keeps_vault_refs():
    blobs = envrender.render_env(
        "meet",
        "meet",
        {
            "DJANGO_SECRET_KEY": "{{ vault_django_secret_key }}",
            "REDIS_URL": "redis://r/1",
            "LIVEKIT_API_KEY": "{{ vault_livekit_api_key }}",
        },
    )
    body = blobs["st_meet_backend_env"]
    assert "DJANGO_SECRET_KEY={{ vault_django_secret_key }}" in body
    assert "LIVEKIT_API_KEY={{ vault_livekit_api_key }}" in body


def test_render_drive_backend_env_references_s3_vars():
    blobs = envrender.render_env(
        "drive",
        "drive",
        {
            "AWS_S3_ENDPOINT_URL": "{{ st_drive_s3_protocol }}://{{ st_drive_s3_host }}",
            "AWS_STORAGE_BUCKET_NAME": "{{ st_drive_s3_bucket }}",
        },
    )
    body = blobs["st_drive_backend_env"]
    assert (
        "AWS_S3_ENDPOINT_URL={{ st_drive_s3_protocol }}://{{ st_drive_s3_host }}"
        in body
    )
    assert "AWS_STORAGE_BUCKET_NAME={{ st_drive_s3_bucket }}" in body


def test_render_meet_backend_env_concrete_s3_values():
    blobs = envrender.render_env(
        "meet",
        "meet",
        {
            "AWS_S3_ENDPOINT_URL": "https://s3.amazonaws.com",
            "AWS_STORAGE_BUCKET_NAME": "meet-media",
        },
    )
    body = blobs["st_meet_backend_env"]
    assert "AWS_S3_ENDPOINT_URL=https://s3.amazonaws.com" in body
    assert "AWS_STORAGE_BUCKET_NAME=meet-media" in body
    assert "st_drive_s3" not in body


def test_render_messages_backend_env_omits_aws_s3():
    """messages uses STORAGE_MESSAGE_* for object storage and does NOT emit the
    django-lasuite generic AWS_S3_* block — even if a stray AWS_S3 answer sneaks
    in, the base template only renders the block when AWS_S3_ENDPOINT_URL is set,
    which messages never populates."""
    blobs = envrender.render_env(
        "messages",
        "messages",
        {
            "DJANGO_SECRET_KEY": "{{ vault_django_secret_key }}",
            "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL": "https://s3.example.org",
            "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME": "msg-imports",
        },
    )
    body = blobs["st_messages_backend_env"]
    assert "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL=https://s3.example.org" in body
    assert "AWS_S3" not in body
    assert "AWS_STORAGE_BUCKET_NAME" not in body


def test_render_messages_backend_env_forces_oidc_create_user():
    """messages (unlike meet/drive) does not auto-create the local user on OIDC
    login by default; without OIDC_CREATE_USER=true a first-time ProConnect
    login is silently rejected. The setting is statically injected into the
    messages backend overlay (not base.django.env.j2) so meet/drive stay
    unaffected."""
    messages_body = envrender.render_env("messages", "messages", {})[
        "st_messages_backend_env"
    ]
    assert "OIDC_CREATE_USER=true" in messages_body
    assert "USE_X_FORWARDED_FOR=true" in messages_body

    meet_body = envrender.render_env("meet", "meet", {})["st_meet_backend_env"]
    assert "OIDC_CREATE_USER" not in meet_body
    assert "USE_X_FORWARDED_FOR" not in meet_body


def test_render_messages_backend_env_emits_technical_domain():
    """MESSAGES_TECHNICAL_DOMAIN backs the MX/SPF/DKIM DNS records
    (get_expected_dns_records substitutes it into MESSAGES_DNS_RECORDS) and the
    exporter noreply@ address. The in-app default `localhost` breaks real mail,
    so the backend env emits it when set."""
    body = envrender.render_env(
        "messages", "messages", {"MESSAGES_TECHNICAL_DOMAIN": "mail.example.org"}
    )["st_messages_backend_env"]
    assert "MESSAGES_TECHNICAL_DOMAIN=mail.example.org" in body


def test_render_messages_frontend_env_points_at_backend_container():
    """messages frontend (Caddy) overlay emits the backend server the frontend
    container proxies /api,/admin,/static to — in the split-container setup the
    backend is reachable at the ``messages-backend`` container_name on port 8000,
    not the default ``localhost:8000``."""
    blobs = envrender.render_env("messages", "messages", {})
    body = blobs["st_messages_frontend_env"]
    assert "MESSAGES_FRONTEND_BACKEND_SERVER=messages-backend:8000" in body
    assert "DJANGO_SECRET_KEY" not in body  # frontend overlay, no base include


def test_render_drive_backend_email_block_keeps_vault_ref():
    """drive backend emits the SHARED email keys and the SMTP password keeps its
    {{ vault_... }} ref verbatim through rendering."""
    blobs = envrender.render_env(
        "drive",
        "drive",
        {
            "DJANGO_EMAIL_HOST": "smtp.example.org",
            "DJANGO_EMAIL_HOST_PASSWORD": "{{ vault_django_email_host_password }}",
        },
    )
    body = blobs["st_drive_backend_env"]
    assert "DJANGO_EMAIL_HOST=smtp.example.org" in body
    assert "DJANGO_EMAIL_HOST_PASSWORD={{ vault_django_email_host_password }}" in body


def test_render_meet_backend_email_extras():
    """meet backend emits the meet-only email extras (DOMAIN / APP_BASE_URL) when set."""
    blobs = envrender.render_env(
        "meet",
        "meet",
        {
            "DJANGO_EMAIL_DOMAIN": "meet.example.org",
            "DJANGO_EMAIL_APP_BASE_URL": "https://meet.example.org",
        },
    )
    body = blobs["st_meet_backend_env"]
    assert "DJANGO_EMAIL_DOMAIN=meet.example.org" in body
    assert "DJANGO_EMAIL_APP_BASE_URL=https://meet.example.org" in body


def test_render_keycloak_env_keeps_vault_refs_and_omits_baked_keys():
    """keycloak renders the KC_* runtime keys with vault refs for the secrets, and
    does NOT emit KC_DB (baked into the image) or any Django keys."""
    blobs = envrender.render_env(
        "keycloak",
        "keycloak",
        {
            "KC_DB_URL": "jdbc:postgresql://db.example.org:5432/keycloak",
            "KC_DB_USERNAME": "keycloak",
            "KC_DB_PASSWORD": "{{ vault_kc_db_password }}",
            "KC_HOSTNAME": "idp.example.org",
            "KC_BOOTSTRAP_ADMIN_USERNAME": "admin",
            "KC_BOOTSTRAP_ADMIN_PASSWORD": "{{ vault_kc_bootstrap_admin_password }}",
        },
    )
    body = blobs["st_keycloak_env"]
    assert "KC_DB_URL=jdbc:postgresql://db.example.org:5432/keycloak" in body
    assert "KC_HOSTNAME=idp.example.org" in body
    assert "KC_DB_PASSWORD={{ vault_kc_db_password }}" in body
    assert "KC_BOOTSTRAP_ADMIN_PASSWORD={{ vault_kc_bootstrap_admin_password }}" in body
    assert "KC_HTTP_ENABLED=true" in body
    assert "KC_DB=" not in body  # KC_DB is baked into the image at build time
    assert "DJANGO_" not in body  # not a Django app


def test_render_email_block_absent_when_unconfigured():
    """When no DJANGO_EMAIL_* answers are set, the email block is omitted entirely
    for both drive and messages (messages has no such settings upstream)."""
    for app, component in (("drive", "drive"), ("messages", "messages")):
        blobs = envrender.render_env(app, component, {})
        body = blobs[f"st_{app}_backend_env"]
        assert "DJANGO_EMAIL_" not in body
