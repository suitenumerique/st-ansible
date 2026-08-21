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
    # file upload (custom call backgrounds) is on by default for meet
    assert "FILE_UPLOAD_ENABLED=True" in body


def test_render_meet_caddy_env_s3_values():
    """meet's Caddy ingress proxies media straight to S3 via CADDY_S3_* container
    env vars (fed through the caddy_env file), not st_meet_s3_* ansible vars —
    mirrors test_render_meet_backend_env_concrete_s3_values but for the caddy
    env_render layer (see apps/meet.yml's caddy layer / meet.caddy.env.j2)."""
    blobs = envrender.render_env(
        "meet",
        "meet",
        {
            "CADDY_S3_PROTOCOL": "https",
            "CADDY_S3_HOST": "minio.example.org:9000",
            "CADDY_S3_BUCKET": "meet-media",
        },
    )
    body = blobs["st_meet_caddy_env"]
    assert "CADDY_S3_PROTOCOL=https" in body
    assert "CADDY_S3_HOST=minio.example.org:9000" in body
    assert "CADDY_S3_BUCKET=meet-media" in body


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


def test_render_meet_backend_env_emits_recording_block_when_enabled():
    """meet backend emits the RECORDING_* block when RECORDING_ENABLE is set: LiveKit
    egress uploads to the backend's existing AWS_S3_* bucket; RECORDING_STORAGE_EVENT_ENABLE
    is pinned False so completion is signalled via the LiveKit webhook. RECORDING_DOWNLOAD_BASE_URL
    references the st_meet_public_host ansible var (set by _ask_core to the same
    single-source-of-truth value DJANGO_ALLOWED_HOSTS / the redirects reference);
    the {{ }} lands verbatim in the env blob and ANSIBLE resolves it at deploy from
    the core vars.yml (DOMAIN still feeds the {DOMAIN} component var). RECORDING_OUTPUT_FOLDER
    is optional."""
    blobs = envrender.render_env(
        "meet",
        "meet",
        {
            "DOMAIN": "meet.example.org",
            "RECORDING_ENABLE": "True",
            "RECORDING_DOWNLOAD_BASE_URL": "https://{{ st_meet_public_host }}/recording",
            "RECORDING_OUTPUT_FOLDER": "recordings",
        },
    )
    body = blobs["st_meet_backend_env"]
    assert "RECORDING_ENABLE=True" in body
    assert "RECORDING_STORAGE_EVENT_ENABLE=False" in body
    assert "RECORDING_OUTPUT_FOLDER=recordings" in body
    assert (
        "RECORDING_DOWNLOAD_BASE_URL=https://{{ st_meet_public_host }}/recording"
        in body
    )


def test_render_meet_backend_env_omits_recording_block_when_disabled():
    """Without RECORDING_ENABLE, none of the RECORDING_* lines appear in the rendered
    meet backend env — mirrors the AWS_S3 guard test for messages
    (test_render_messages_backend_env_omits_aws_s3)."""
    blobs = envrender.render_env("meet", "meet", {"DOMAIN": "meet.example.org"})
    body = blobs["st_meet_backend_env"]
    assert "RECORDING_" not in body


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


# --------------------------------------------------------------------------- docs


def test_render_docs_backend_references_public_host_and_collaboration_urls():
    """docs backend emits OIDC_REDIRECT_ALLOWED_HOSTS + the collaboration URLs
    verbatim (the {{ st_docs_public_host }} ref travels through the answer value
    unresolved — the jinja2 env template just prints the string; ANSIBLE resolves
    it at deploy from the core vars.yml, same pattern as meet's public-host refs)."""
    blobs = envrender.render_env(
        "docs",
        "docs",
        {
            "OIDC_REDIRECT_ALLOWED_HOSTS": '["https://{{ st_docs_public_host }}"]',
            "COLLABORATION_WS_URL": "wss://{{ st_docs_public_host }}/collaboration/ws/",
            "COLLABORATION_API_URL": (
                "https://{{ st_docs_public_host }}/collaboration/api/"
            ),
            "COLLABORATION_SERVER_SECRET": "{{ vault_collaboration_server_secret }}",
            "Y_PROVIDER_API_BASE_URL": "http://10.0.0.9:50601/api/",
            "Y_PROVIDER_API_KEY": "{{ vault_y_provider_api_key }}",
        },
    )
    body = blobs["st_docs_backend_env"]
    assert 'OIDC_REDIRECT_ALLOWED_HOSTS=["https://{{ st_docs_public_host }}"]' in body
    assert (
        "COLLABORATION_WS_URL=wss://{{ st_docs_public_host }}/collaboration/ws/" in body
    )
    assert (
        "COLLABORATION_API_URL=https://{{ st_docs_public_host }}/collaboration/api/"
        in body
    )
    assert "COLLABORATION_SERVER_SECRET={{ vault_collaboration_server_secret }}" in body
    assert "Y_PROVIDER_API_BASE_URL=http://10.0.0.9:50601/api/" in body
    assert "Y_PROVIDER_API_KEY={{ vault_y_provider_api_key }}" in body


def test_render_docs_backend_optional_keys_absent_when_unset():
    """Y_PROVIDER_API_BASE_URL and the frontend theme keys are guarded — absent
    from the rendered body when not set in answers."""
    body = envrender.render_env("docs", "docs", {"DOMAIN": "docs.example.org"})[
        "st_docs_backend_env"
    ]
    assert "Y_PROVIDER_API_BASE_URL" not in body
    assert "FRONTEND_THEME" not in body
    assert "FRONTEND_CSS_URL" not in body
    assert "DJANGO_EMAIL_URL_APP" not in body


def test_render_docs_backend_theme_customization():
    body = envrender.render_env(
        "docs",
        "docs",
        {"FRONTEND_THEME": "custom", "FRONTEND_CSS_URL": "https://cdn/theme.css"},
    )["st_docs_backend_env"]
    assert "FRONTEND_THEME=custom" in body
    assert "FRONTEND_CSS_URL=https://cdn/theme.css" in body


def test_render_docs_caddy_env_s3_and_yprovider_values():
    """docs' Caddy ingress proxies /media/* straight to S3 via CADDY_S3_* container
    env vars and /collaboration/* to the y-provider upstreams via
    CADDY_YPROVIDER_ENDPOINTS (a space-separated host:port list caddy expands at
    parse time), all fed through the caddy_env file."""
    blobs = envrender.render_env(
        "docs",
        "docs",
        {
            "CADDY_S3_PROTOCOL": "https",
            "CADDY_S3_HOST": "minio.example.org:9000",
            "CADDY_S3_BUCKET": "docs-media",
            "CADDY_YPROVIDER_ENDPOINTS": "10.0.0.9:50601 10.0.0.10:50601",
        },
    )
    body = blobs["st_docs_caddy_env"]
    assert "CADDY_S3_PROTOCOL=https" in body
    assert "CADDY_S3_HOST=minio.example.org:9000" in body
    assert "CADDY_S3_BUCKET=docs-media" in body
    assert "CADDY_YPROVIDER_ENDPOINTS=10.0.0.9:50601 10.0.0.10:50601" in body
