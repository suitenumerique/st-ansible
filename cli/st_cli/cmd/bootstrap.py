"""Interactive bootstrap for an ``(app, env)`` deployment.

Flow: backend choice → ssh/vault setup → core host IPs → identity provider +
core Django answers → per-dependency 3-state prompt (deploy / reuse / external)
→ write the committed tree and record units in ``.st-cli.yml``.

Secret handling is routed through a :mod:`st_cli.core.secretbackend` strategy
chosen per (app, env) at bootstrap:

* ``ansible-vault`` (the default, unchanged): ``<app>/<env>/<component>/vars.yml``
  is **plaintext / diffable**. Secrets inside an env blob are Jinja refs, e.g.
  ``DJANGO_SECRET_KEY={{ vault_django_secret_key }}``.
  ``<app>/<env>/<component>/vault.yml`` is a **whole-file ansible-vault** encrypted
  mapping holding the real values.
* ``hashi_vault`` (OpenBao, reference-only): no ``vault.yml`` is written — the
  env blob carries ``{{ lookup('community.hashi_vault.hashi_vault', '<term>') }}``
  refs to existing OpenBao entries and the real values live in OpenBao. st-cli
  never generates secrets and never writes to OpenBao in this mode.

The generated playbook loads ``vars.yml`` + (if present) ``vault.yml`` via
``vars_files`` and ansible resolves the refs. Hosts live only in the ``hosts``
ini (not duplicated in ``.st-cli.yml``).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ruamel.yaml.comments import CommentedMap

from ..core import appmeta, envrender, manifest, paths, secrets, tree, ui, vault, writer
from ..core.errors import StCliError
from ..core.models import StCliManifest, UnitState
from ..core.prompts import (
    _ask,
    _ask_hosts,
    _ask_select,
    _confirm,
    _password,
    _confirm_ready,
)
from ..core.secretbackend import SecretBackend, setup_backend

__all__ = ["bootstrap"]

_OIDC_PROVIDERS = ["keycloak", "proconnect-prod", "proconnect-integ", "custom"]

# Apps that carry upstream DJANGO_EMAIL_* settings; messages is skipped (no such
# settings upstream) so its questionnaire never prompts for SMTP config.
_EMAIL_APPS = {"drive", "meet", "transfers"}


# --------------------------------------------------------------------------- #
# manifest + local bootstrap
# --------------------------------------------------------------------------- #
def _ensure_manifest() -> StCliManifest:
    """Load ``.st-cli.yml`` or create a fresh one pinned to this CLI version."""
    if paths.manifest_path().exists():
        return manifest.load_manifest()
    from .. import __version__

    return StCliManifest(
        collection_version=__version__, cli_version=__version__, units=[]
    )


# --------------------------------------------------------------------------- #
# identity provider / OIDC + core Django answers
# --------------------------------------------------------------------------- #
def _ask_oidc(answers: dict, backend: SecretBackend, component: str) -> None:
    """Choose an identity provider; fill OIDC answers (client secret → backend)."""
    provider = _ask_select("Identity provider:", _OIDC_PROVIDERS)
    base_url = realm = None
    answers["OIDC_PROVIDER"] = provider
    if provider == "keycloak":
        base_url = _ask("Keycloak base URL", placeholder="https://idp.example.org")
        realm = _ask("Keycloak realm", "master")
    elif provider == "custom":
        base_url = _ask("Custom OIDC issuer base URL (optional)", required=False)
    answers.update(envrender.oidc_endpoints(provider, base_url, realm))
    answers["OIDC_RP_CLIENT_ID"] = _ask("OIDC_RP_CLIENT_ID")
    value = _password("OIDC_RP_CLIENT_SECRET") if backend.prompts_values() else None
    backend.env_secret(
        answers, "OIDC_RP_CLIENT_SECRET", component=component, value=value
    )


def _ask_email(answers: dict, backend: SecretBackend, component: str, app: str) -> None:
    """Prompt Django transactional email (SMTP) settings for drive / meet.

    Skipped entirely for ``messages`` (no ``DJANGO_EMAIL_*`` upstream). The SMTP
    password is a secret routed through the backend like the other env secrets;
    optional fields are only written into ``answers`` when filled in so template
    guards stay clean.
    """
    if app not in _EMAIL_APPS:
        return
    if not _confirm("Configure transactional email (SMTP) settings?", default=False):
        return
    answers["DJANGO_EMAIL_HOST"] = _ask(
        "DJANGO_EMAIL_HOST", placeholder="smtp.example.org"
    )
    answers["DJANGO_EMAIL_PORT"] = _ask("DJANGO_EMAIL_PORT", "587")
    host_user = _ask("DJANGO_EMAIL_HOST_USER (optional)", required=False)
    if host_user:
        answers["DJANGO_EMAIL_HOST_USER"] = host_user
    value = (
        _password("DJANGO_EMAIL_HOST_PASSWORD") if backend.prompts_values() else None
    )
    backend.env_secret(
        answers, "DJANGO_EMAIL_HOST_PASSWORD", component=component, value=value
    )
    answers["DJANGO_EMAIL_USE_TLS"] = (
        "true" if _confirm("DJANGO_EMAIL_USE_TLS?", default=True) else "false"
    )
    answers["DJANGO_EMAIL_USE_SSL"] = (
        "true" if _confirm("DJANGO_EMAIL_USE_SSL?", default=False) else "false"
    )
    answers["DJANGO_EMAIL_FROM"] = _ask(
        "DJANGO_EMAIL_FROM", placeholder="noreply@example.org"
    )
    brand_name = _ask("DJANGO_EMAIL_BRAND_NAME (optional)", required=False)
    if brand_name:
        answers["DJANGO_EMAIL_BRAND_NAME"] = brand_name


def _ask_cadvisor(label: str) -> bool:
    """Prompt whether to enable the cadvisor monitoring sidecar for a component."""
    return _confirm(f"Enable cadvisor container monitoring for {label}?", default=True)


def _ask_db(answers: dict, backend: SecretBackend, component: str, app: str) -> None:
    """Prompt database connection: a DATABASE_URL or discrete DB_* vars."""
    mode = _ask_select("Database configuration:", ["DATABASE_URL", "discrete (DB_*)"])
    if mode.startswith("DATABASE_URL"):
        value = _ask("DATABASE_URL") if backend.prompts_values() else None
        backend.env_secret(answers, "DATABASE_URL", component=component, value=value)
        return
    answers["DB_HOST"] = _ask("DB_HOST")
    answers["DB_NAME"] = _ask("DB_NAME", app)
    answers["DB_USER"] = _ask("DB_USER", app)
    value = _password("DB_PASSWORD") if backend.prompts_values() else None
    backend.env_secret(answers, "DB_PASSWORD", component=component, value=value)
    answers["DB_PORT"] = _ask("DB_PORT", "5432")


def _ask_keycloak(meta, backend: SecretBackend) -> dict:
    """Collect the keycloak core answers → the ``st_keycloak_env`` blob.

    Keycloak is not a Django app: its role consumes a single free-form
    ``st_keycloak_env`` blob (no DOMAIN/Redis/S3/OIDC/email questionnaire). The
    ``messages-keycloak`` image bakes in ``KC_DB=postgres`` + features/metrics/health
    at build time, so we only prompt for what the operator must supply at runtime:
    the DB connection, the public hostname, and the admin bootstrap credentials.
    Passwords route through the secret backend exactly like the Django apps'
    ``DB_PASSWORD`` (``{{ vault_* }}`` ref in the blob, real value in vault.yml).
    """
    core_key = meta.core().key
    domain = _ask("Public domain for keycloak", placeholder="idp.example.org")
    # DOMAIN feeds _print_summary; KC_HOSTNAME is the actual env key.
    answers: dict = {"DOMAIN": domain, "KC_HOSTNAME": domain}

    db_host = _ask("Database host", placeholder="db.example.org")
    db_port = _ask("Database port", "5432")
    db_name = _ask("Database name", "keycloak")
    answers["KC_DB_URL"] = f"jdbc:postgresql://{db_host}:{db_port}/{db_name}"
    answers["KC_DB_USERNAME"] = _ask("Database user", "keycloak")
    value = _password("KC_DB_PASSWORD") if backend.prompts_values() else None
    backend.env_secret(answers, "KC_DB_PASSWORD", component=core_key, value=value)

    answers["KC_BOOTSTRAP_ADMIN_USERNAME"] = _ask("Bootstrap admin username", "admin")
    value = (
        _password("KC_BOOTSTRAP_ADMIN_PASSWORD") if backend.prompts_values() else None
    )
    backend.env_secret(
        answers, "KC_BOOTSTRAP_ADMIN_PASSWORD", component=core_key, value=value
    )
    return answers


def _ask_core(meta, backend: SecretBackend) -> dict:
    """Collect the core component answers (domain, db, redis, s3, secrets, OIDC)."""
    app = meta.app
    core_key = meta.core().key
    domain = _ask(f"Public domain for {app}", placeholder=f"{app}.example.org")

    answers: dict = {
        "DOMAIN": domain,
        "DJANGO_SETTINGS_MODULE": f"{app}.settings",
        "DJANGO_CONFIGURATION": "Production",
        "DJANGO_ALLOWED_HOSTS": domain,
        "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{domain}",
        "DJANGO_CORS_ALLOWED_ORIGINS": f"https://{domain}",
        "LOGIN_REDIRECT_URL": f"https://{domain}/",
        "LOGIN_REDIRECT_URL_FAILURE": f"https://{domain}/",
        "LOGOUT_REDIRECT_URL": f"https://{domain}/",
    }
    if app == "meet":
        # single source of truth: every public-domain env var references the
        # st_meet_public_host ansible var (written into the core vars.yml from
        # DOMAIN), so the operator changes the domain in one place. Mirrors drive's
        # st_drive_public_host redirect-url override. The answer VALUE is the literal
        # "https://{{ st_meet_public_host }}" string — the env template emits it
        # via answers.SOMEKEY so the {{ }} lands verbatim in the env file and ANSIBLE
        # resolves it at deploy (do NOT put {{ st_meet_public_host }} directly in a
        # jinja env template line — jinja2 would resolve it and emit empty).
        host = "{{ st_meet_public_host }}"
        answers["DJANGO_ALLOWED_HOSTS"] = host
        answers["DJANGO_CSRF_TRUSTED_ORIGINS"] = f"https://{host}"
        answers["DJANGO_CORS_ALLOWED_ORIGINS"] = f"https://{host}"
        answers["LOGIN_REDIRECT_URL"] = f"https://{host}/"
        answers["LOGIN_REDIRECT_URL_FAILURE"] = f"https://{host}/"
        answers["LOGOUT_REDIRECT_URL"] = f"https://{host}/"
    if app == "transfers":
        # The app's Python package is `transferts` (French spelling), so the
        # Django settings module differs from the st-cli app name `transfers`.
        answers["DJANGO_SETTINGS_MODULE"] = "transferts.settings"
    backend.env_secret(
        answers,
        "DJANGO_SECRET_KEY",
        component=core_key,
        value=secrets.gen_secret() if backend.prompts_values() else None,
    )

    _ask_db(answers, backend, core_key, app)

    # REDIS_URL can embed a password (redis://user:password@host) so it is
    # routed through the secret backend like DATABASE_URL. CELERY_BROKER_URL
    # mirrors the same broker, so it references the same secret (one vault
    # entry / one OpenBao lookup) rather than prompting again.
    redis_url = (
        _ask(
            "REDIS_URL (redis://[user:password@]host:port/db)",
            "redis://redis:6379/0",
        )
        if backend.prompts_values()
        else None
    )
    backend.env_secret(answers, "REDIS_URL", component=core_key, value=redis_url)
    answers["CELERY_BROKER_URL"] = answers["REDIS_URL"]

    # messages does NOT use the django-lasuite default S3 storage (AWS_S3_*) — it
    # uses STORAGE_MESSAGE_* instead (see _ask_messages_storage), so skip the S3
    # questionnaire entirely for it.
    if app != "messages":
        endpoint = _ask(
            "AWS_S3_ENDPOINT_URL", placeholder="https://s3.fr-par.scw.cloud"
        )
        answers["AWS_S3_ACCESS_KEY_ID"] = _ask("AWS_S3_ACCESS_KEY_ID")
        value = (
            _password("AWS_S3_SECRET_ACCESS_KEY") if backend.prompts_values() else None
        )
        backend.env_secret(
            answers,
            "AWS_S3_SECRET_ACCESS_KEY",
            component=core_key,
            value=value,
        )
        bucket = _ask("AWS_STORAGE_BUCKET_NAME")
        answers["AWS_S3_REGION_NAME"] = _ask(
            "AWS_S3_REGION_NAME (optional)", required=False
        )

        if app == "drive":
            # drive's nginx proxies media straight to S3 via st_drive_s3_* vars;
            # derive them here and point the backend env at the same vars (single
            # source of truth). See apps/drive.yml's st_drive_s3_protocol/host/bucket
            # component vars.
            parts = urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}")
            answers["S3_PROTOCOL"] = parts.scheme or "https"
            answers["S3_HOST"] = parts.netloc
            answers["S3_BUCKET"] = bucket
            answers["AWS_S3_ENDPOINT_URL"] = (
                "{{ st_drive_s3_protocol }}://{{ st_drive_s3_host }}"
            )
            answers["AWS_STORAGE_BUCKET_NAME"] = "{{ st_drive_s3_bucket }}"

            # WOPI (collabora) wiring — WOPI_SRC_BASE_URL points at the same
            # st_drive_public_host var the role already sets (resolved at deploy).
            answers["WOPI_CLIENTS"] = "collabora"
            answers["WOPI_SRC_BASE_URL"] = "https://{{ st_drive_public_host }}"
            # public-facing URLs point at st_drive_public_host (resolved at deploy),
            # matching the role's healthcheck Host var rather than the raw domain.
            answers["LOGIN_REDIRECT_URL"] = "https://{{ st_drive_public_host }}/"
            answers["LOGIN_REDIRECT_URL_FAILURE"] = (
                "https://{{ st_drive_public_host }}/"
            )
            answers["LOGOUT_REDIRECT_URL"] = "https://{{ st_drive_public_host }}/"
            answers["MEDIA_BASE_URL"] = "https://{{ st_drive_public_host }}"
        elif app == "meet":
            # meet's in-compose Caddy ingress proxies media straight to S3 via
            # CADDY_S3_* container env vars fed through a caddy_env file (not
            # st_meet_s3_* ansible vars) — see apps/meet.yml's caddy env_render
            # layer and roles/meet/templates/meet/Caddyfile.j2. The backend env
            # keeps the real literal endpoint/bucket values (no indirection).
            parts = urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}")
            answers["CADDY_S3_PROTOCOL"] = parts.scheme or "https"
            answers["CADDY_S3_HOST"] = parts.netloc
            answers["CADDY_S3_BUCKET"] = bucket
            answers["AWS_S3_ENDPOINT_URL"] = endpoint
            answers["AWS_STORAGE_BUCKET_NAME"] = bucket
        elif app == "transfers":
            # transfers uses the django-lasuite default AWS_STORAGE_BUCKET_NAME (via
            # base), plus a few extras: uploads/downloads use presigned URLs straight
            # to S3, so the frontend Caddy CSP must allow the S3 origin; derive it from
            # the endpoint (single source of truth).
            parts = urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}")
            answers["AWS_S3_ENDPOINT_URL"] = endpoint
            answers["AWS_STORAGE_BUCKET_NAME"] = bucket
            answers["AWS_S3_SIGNATURE_VERSION"] = "s3v4"
            answers["TRANSFERTS_FRONTEND_S3_ORIGIN"] = (
                f"{parts.scheme or 'https'}://{parts.netloc}"
            )
            # transfers sits behind the frontend Caddy, which sets X-Forwarded-For;
            # enable request-IP logging from that proxy header.
            answers["USE_X_FORWARDED_FOR"] = "true"
        else:
            answers["AWS_S3_ENDPOINT_URL"] = endpoint
            answers["AWS_STORAGE_BUCKET_NAME"] = bucket

    if app == "messages":
        # MDA_API_SECRET is a messages-core secret (mta-in is only a consumer).
        # Generate it here so it exists whenever messages is bootstrapped —
        # independent of whether mta-in is deployed / skipped / external.
        backend.env_secret(
            answers,
            "MDA_API_SECRET",
            component=core_key,
            value=secrets.gen_secret() if backend.prompts_values() else None,
        )
        # SALT_KEY: django-fernet-encrypted-fields key (DKIM keys, channel secrets).
        # Required in practice — an empty value makes encrypted-field writes raise.
        backend.env_secret(
            answers,
            "SALT_KEY",
            component=core_key,
            value=secrets.gen_secret() if backend.prompts_values() else None,
        )
        _ask_messages_storage(answers, backend, core_key)
        # OPENSEARCH_URL is mandatory: the in-app default points at a non-existent
        # `opensearch` host, so search silently breaks unless it is set here.
        answers["OPENSEARCH_URL"] = _ask(
            "OPENSEARCH_URL", placeholder="http://opensearch:9200"
        )
        # MESSAGES_TECHNICAL_DOMAIN backs the MX/SPF/DKIM DNS records
        # (get_expected_dns_records substitutes it into MESSAGES_DNS_RECORDS) and the
        # exporter noreply@ address. The in-app default `localhost` breaks real mail,
        # so prompt for it.
        answers["MESSAGES_TECHNICAL_DOMAIN"] = _ask(
            "MESSAGES_TECHNICAL_DOMAIN", placeholder="mail.example.org"
        )

    if app == "transfers":
        # Optional Drive integration (file picker). Left unset → integration off.
        drive_url = _ask(
            "DRIVE_BASE_URL — enable the Drive file picker (optional)",
            required=False,
        )
        if drive_url:
            answers["DRIVE_BASE_URL"] = drive_url

    _ask_oidc(answers, backend, core_key)
    _ask_email(answers, backend, core_key, app)
    if app == "messages":
        _ask_messages_outbound(answers, backend, core_key)
    if app == "meet":
        _set_meet_recording(answers)
    return answers


def _ask_messages_provider(
    provider_key, answers, backend, hosts, core_key, app, env, pvars
):
    """messages mta-in / socks-proxy / mpa: collect provider-local env values + route
    their secrets so apply_component_vars can render the st_messages_<comp>_env blob,
    (socks-proxy) build the computed MTA_OUT_DIRECT_PROXIES consumer value, and
    (mpa) build the computed SPAM_CONFIG consumer JSON — always constructed, never
    prompted: the auth bearer is a {{ vault_mpa_auth_bearer }} ref (mirrored into the
    messages vault) under ansible-vault, or the self-contained OpenBao lookup ref
    taken from pvars under hashi_vault."""
    if provider_key == "mta-in":
        # DOMAIN feeds MDA_API_BASE_URL; present in full bootstrap, prompt if standalone.
        if not answers.get("DOMAIN"):
            answers["DOMAIN"] = _ask(
                "Public domain for messages (for MDA_API_BASE_URL)",
                placeholder="messages.example.org",
            )
        answers["MYHOSTNAME"] = _ask(
            "MX public hostname (MYHOSTNAME) for mta-in", placeholder="mx.example.org"
        )
        # MDA_API_SECRET is owned by the messages core (generated in _ask_core). Mirror
        # the core-owned value into mta-in's own vault so its env blob ref resolves. In a
        # full run the core buffer holds it; for a standalone `bootstrap -c mta-in` read it
        # from the messages vault on disk. If neither is available (standalone run BEFORE
        # the core is bootstrapped), prompt the operator for the core's existing value —
        # skipping it would leave answers[MDA_API_SECRET] unset and apply_component_vars
        # would emit a literal '{MDA_API_SECRET}' placeholder into the committed vars.yml.
        if backend.prompts_values():
            v = backend.component_secrets(core_key).get("vault_mda_api_secret")
            if v is None:
                mvp = paths.vault_path(app, env, core_key)
                if mvp.exists():
                    v = vault.decrypt_to_dict(mvp).get("vault_mda_api_secret")
            if v is None:
                v = _password(
                    "MDA_API_SECRET (shared with the messages core — must match it)"
                )
            backend.env_secret(
                answers, "MDA_API_SECRET", component=provider_key, value=v
            )
        elif not answers.get("MDA_API_SECRET"):
            # hashi standalone: no core-set ref in answers → prompt a lookup term.
            backend.env_secret(
                answers, "MDA_API_SECRET", component=provider_key, value=None
            )
        # hashi full run: answers[MDA_API_SECRET] already holds the lookup ref → reuse.
    elif provider_key == "socks-proxy":
        answers["PROXY_EXTERNAL"] = _ask(
            "PROXY_EXTERNAL (socks-proxy egress interface)", "eth0"
        )
        port = _ask("PROXY_INTERNAL_PORT", "50405")
        answers["PROXY_INTERNAL_PORT"] = port
        if backend.prompts_values():  # ansible-vault: mint the credential + mirror it
            v = "messages:" + secrets.gen_password()
            backend.env_secret(answers, "PROXY_USERS", component=provider_key, value=v)
            # mirror the same secret into the messages core vault so the
            # {{ vault_proxy_users }} ref embedded below resolves there too.
            backend.env_secret(answers, "PROXY_USERS", component=core_key, value=v)
        else:  # hashi reference-only: one lookup term for PROXY_USERS
            backend.env_secret(
                answers, "PROXY_USERS", component=provider_key, value=None
            )
        # MTA_OUT_DIRECT_PROXIES is a messages-core value computed from the proxy
        # hosts + port, embedding whatever PROXY_USERS ref the backend produced (a
        # {{ vault_proxy_users }} ref under ansible-vault, a self-contained OpenBao
        # lookup under hashi_vault). Never prompted.
        answers["MTA_OUT_DIRECT_PROXIES"] = ",".join(
            "socks5s://" + answers["PROXY_USERS"] + "@" + h + ":" + port for h in hosts
        )
    elif provider_key == "mpa":
        # rspamd_url: a single mpa host → derive it from the host + caddy port; a
        # load-balanced (multi-host) mpa → prompt the LB URL. Shared by both backends.
        if len(hosts) == 1:
            rspamd_url = "http://" + hosts[0] + ":{{ st_messages_mpa_caddy_port }}"
        else:
            rspamd_url = _ask(
                "rspamd URL for SPAM_CONFIG (mpa load balancer)",
                placeholder="https://mpa.example.org",
            )
        # SPAM_CONFIG is a messages-core env var (mpa is only its provider): always
        # CONSTRUCTED, never prompted. Only the rspamd auth bearer is a secret, and it
        # is already stored as st_messages_mpa_auth_bearer in the mpa pvars. The two
        # backends differ only in how that bearer is referenced.
        if backend.prompts_values():  # ansible-vault: a {{ vault_* }} ref
            token = backend.component_secrets(provider_key).get("vault_mpa_auth_bearer")
            if token is not None:
                # mirror the bearer into the messages vault under the same vault_mpa_*
                # name so the {{ vault_mpa_auth_bearer }} ref in SPAM_CONFIG resolves there.
                backend.var_secret(
                    CommentedMap(), "vault_mpa_auth_bearer", token, component=core_key
                )
            bearer_ref = "{{ vault_mpa_auth_bearer }}"
        else:  # hashi_vault: reuse the self-contained OpenBao lookup ref from pvars
            bearer_ref = pvars.get("st_messages_mpa_auth_bearer")
            if bearer_ref is None:
                raise StCliError(
                    "mpa auth bearer ref missing — st_messages_mpa_auth_bearer was "
                    "not set before building SPAM_CONFIG."
                )
        answers["SPAM_CONFIG"] = (
            '{"rspamd_url": "' + rspamd_url + '", '
            '"rspamd_auth": "Bearer ' + bearer_ref + '", '
            '"inbound_auth": "rspamd"}'
        )


def _ask_messages_storage(answers: dict, backend: SecretBackend, core_key: str) -> None:
    """messages-only S3: the imports bucket (always) + optional blobs offload bucket.

    messages does not use the django-lasuite generic AWS_S3_* default storage at all
    (those are not prompted for it) — STORAGE_MESSAGE_* is its only object storage.
    Secret keys route through the backend; the blobs encrypt key is generated
    (ansible-vault) or looked up (hashi) and embedded into the
    MESSAGES_BLOBS_ENCRYPT_KEYS JSON.
    """
    # --- imports bucket (always) ---
    answers["STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL"] = _ask(
        "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL",
        placeholder="https://s3.fr-par.scw.cloud",
    )
    answers["STORAGE_MESSAGE_IMPORTS_BUCKET_NAME"] = _ask(
        "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", placeholder="msg-imports"
    )
    answers["STORAGE_MESSAGE_IMPORTS_ACCESS_KEY"] = _ask(
        "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY"
    )
    value = (
        _password("STORAGE_MESSAGE_IMPORTS_SECRET_KEY")
        if backend.prompts_values()
        else None
    )
    backend.env_secret(
        answers, "STORAGE_MESSAGE_IMPORTS_SECRET_KEY", component=core_key, value=value
    )
    region = _ask("STORAGE_MESSAGE_IMPORTS_REGION_NAME (optional)", required=False)
    if region:
        answers["STORAGE_MESSAGE_IMPORTS_REGION_NAME"] = region
    answers["STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY"] = _ask(
        "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"
    )

    # --- blobs offload bucket (optional) ---
    if not _confirm("Enable blobs offloading to S3 (pg→ S3)?", default=False):
        return
    answers["MESSAGES_BLOBS_OFFLOAD_ENABLED"] = "1"
    answers["STORAGE_MESSAGE_BLOBS_ENDPOINT_URL"] = _ask(
        "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL", placeholder="https://s3.fr-par.scw.cloud"
    )
    answers["STORAGE_MESSAGE_BLOBS_BUCKET_NAME"] = _ask(
        "STORAGE_MESSAGE_BLOBS_BUCKET_NAME", placeholder="msg-blobs"
    )
    answers["STORAGE_MESSAGE_BLOBS_ACCESS_KEY"] = _ask(
        "STORAGE_MESSAGE_BLOBS_ACCESS_KEY"
    )
    value = (
        _password("STORAGE_MESSAGE_BLOBS_SECRET_KEY")
        if backend.prompts_values()
        else None
    )
    backend.env_secret(
        answers, "STORAGE_MESSAGE_BLOBS_SECRET_KEY", component=core_key, value=value
    )
    region = _ask("STORAGE_MESSAGE_BLOBS_REGION_NAME (optional)", required=False)
    if region:
        answers["STORAGE_MESSAGE_BLOBS_REGION_NAME"] = region
    # encryption key: generate (ansible-vault) / lookup (hashi); only the secret is dynamic.
    keyval = secrets.gen_token() if backend.prompts_values() else None
    backend.env_secret(
        answers, "MESSAGES_BLOBS_ENCRYPT_KEY", component=core_key, value=keyval
    )
    answers["MESSAGES_BLOBS_ENCRYPT_KEYS"] = (
        '{"1": {"algo": "aes-gcm", "secret": "'
        + answers["MESSAGES_BLOBS_ENCRYPT_KEY"]
        + '", "active": true}}'
    )


def _ask_messages_outbound(
    answers: dict, backend: SecretBackend, core_key: str
) -> None:
    """messages outbound mode: DIRECT (send from messages host / socks-proxy) or
    RELAY (external SMTP smarthost). Direct leaves MTA_OUT_MODE unset (the app
    default) and lets the socks-proxy dependency prompt handle egress; relay
    collects the smarthost host + optional credentials (password routed through
    the secret backend) and suppresses the socks-proxy prompt (see the deps loop)."""
    choice = _ask_select(
        "Outbound mail mode (MTA_OUT_MODE):",
        [
            "direct: send from the messages host / socks-proxy",
            "relay: send via an external SMTP server",
        ],
    )
    if not choice.startswith("relay"):
        return
    answers["MTA_OUT_MODE"] = "relay"
    answers["MTA_OUT_RELAY_HOST"] = _ask(
        "MTA_OUT_RELAY_HOST", placeholder="smtp.example.org:587"
    )
    user = _ask("MTA_OUT_RELAY_USERNAME (optional, blank = no auth)", required=False)
    if user:
        answers["MTA_OUT_RELAY_USERNAME"] = user
        value = (
            _password("MTA_OUT_RELAY_PASSWORD") if backend.prompts_values() else None
        )
        backend.env_secret(
            answers, "MTA_OUT_RELAY_PASSWORD", component=core_key, value=value
        )


def _ensure_meet_domain(answers: dict) -> None:
    """meet/livekit: the livekit unit's st_meet_public_host component var is
    built from DOMAIN in apply_component_vars. DOMAIN is already collected in a
    full bootstrap; for a standalone `bootstrap -c livekit` run answers is empty,
    so prompt it."""
    if not answers.get("DOMAIN"):
        answers["DOMAIN"] = _ask(
            "Public domain for meet (for the LiveKit recording webhook)",
            placeholder="meet.example.org",
        )


def _set_meet_recording(answers: dict) -> None:
    """meet-only: always enable LiveKit egress recording (uploads to the backend's
    existing AWS_S3_* bucket; completion is signalled via the LiveKit webhook) —
    never prompted.

    This used to be a confirm (default No). It isn't one anymore because the
    egress recorder is bundled into the livekit bootstrap step UNCONDITIONALLY
    (see ``_bundle_egress`` / ``_standalone_egress`` / ``_reuse_egress`` below,
    none of which check a recording flag): the recorder's infrastructure is
    deployed either way. A confirm here would only let the operator switch OFF
    an *app-level* feature (whether the meet backend advertises/serves
    recordings) while the process that produces them keeps running regardless —
    that's not a meaningful choice, just a footgun (a deployed-but-unused
    recorder, or worse, a silently confusing half-wired stack). So recording is
    unconditionally on.

    RECORDING_OUTPUT_FOLDER is fixed at "recordings" — the value the old prompt
    defaulted to. It's an S3 key prefix, not something most operators need to
    change; one who does can edit RECORDING_OUTPUT_FOLDER directly in the core
    <app>/<env>/<core>/vars.yml after bootstrap.
    """
    answers["RECORDING_ENABLE"] = "True"
    answers["RECORDING_OUTPUT_FOLDER"] = "recordings"
    # SINGULAR /recording — matches the meet frontend SPA route (upstream default
    # is RECORDING_DOWNLOAD_BASE_URL=http://localhost:3000/recording). Do not
    # pluralize: that would 404 the emailed recording-ready link. Unrelated to
    # RECORDING_OUTPUT_FOLDER above, which is legitimately plural (an S3 folder
    # prefix, not a URL path).
    answers["RECORDING_DOWNLOAD_BASE_URL"] = (
        "https://{{ st_meet_public_host }}/recording"
    )


# --------------------------------------------------------------------------- #
# egress component (bundled into the livekit bootstrap step, or standalone -c egress)
# --------------------------------------------------------------------------- #
def _redis_topology(
    livekit_hosts: list[str], egress_hosts: list[str]
) -> tuple[bool, str | None]:
    """(valkey_enabled, redis_address|None). Single co-located node → local valkey
    (``127.0.0.1:6379``); otherwise the operator must supply a shared redis url —
    the caller prompts it (NO format validation: any non-empty string is accepted)."""
    single = sorted(livekit_hosts) == sorted(egress_hosts) and len(livekit_hosts) == 1
    return (True, "127.0.0.1:6379") if single else (False, None)


def _ask_egress_hosts(livekit_hosts: list[str]) -> tuple[list[str], bool]:
    """meet/livekit: ask the egress hosts (blank ⇒ co-locate on the livekit hosts)
    right after the livekit hosts prompt (Q2 — BEFORE the LiveKit domain/TURN
    prompts) and decide the livekit↔egress redis topology up front so the redis
    prompt (if any) can be asked later, after the livekit cadvisor confirm.
    Returns (egress_hosts, valkey_enabled)."""
    egress_hosts = _ask_hosts(
        "egress (leave blank to co-locate on the livekit hosts)", allow_empty=True
    ) or list(livekit_hosts)
    valkey_enabled, _ = _redis_topology(livekit_hosts, egress_hosts)
    return egress_hosts, valkey_enabled


def _mirror_livekit_creds_to_egress(
    backend,
    meta,
    env: str,
    ev,
    lk_vars,
    names=("st_meet_livekit_api_key", "st_meet_livekit_api_secret"),
) -> None:
    """Mirror livekit's already-decided secrets (api key/secret, plus the redis
    password when the caller adds it to ``names``) into egress's own vault — raw
    under the same ``st_meet_livekit_*`` var name egress reuses, exactly how
    livekit stores its own (the role reads them directly from vault.yml). Full run
    → read the live buffer; standalone ``-c egress`` → read livekit's on-disk
    vault. hashi mode reuses livekit's ALREADY-DECIDED lookup refs from
    ``lk_vars`` directly in ``ev`` — no fresh prompt (the old code prompted fresh
    lookup terms into a throwaway map, discarding them; egress lost the ref and
    re-asked). Both branches now fail fast (``StCliError``) on a missing
    secret/ref instead of silently skipping it — a silent skip would leave
    egress's vars.yml without a required var, only to blow up much later at
    deploy time as an undefined variable."""
    if (
        backend.prompts_values()
    ):  # ansible-vault: copy raw values into egress vault buffer
        src = backend.component_secrets("livekit")
        disk = None
        for name in names:
            val = src.get(name)
            if val is None:
                if disk is None:
                    lvp = paths.vault_path(meta.app, env, "livekit")
                    disk = vault.decrypt_to_dict(lvp) if lvp.exists() else {}
                val = disk.get(name)
            if val is None:
                raise StCliError(
                    f"livekit secret {name} missing — cannot mirror it to egress; "
                    "re-bootstrap livekit."
                )
            backend.var_secret(CommentedMap(), name, val, component="egress")
    else:  # hashi: reuse livekit's lookup refs directly in egress vars.yml (NO re-prompt)
        for name in names:
            ref = lk_vars.get(name)
            if ref is None:
                raise StCliError(
                    f"livekit lookup ref {name} missing — cannot mirror it to egress; "
                    "re-bootstrap livekit."
                )
            ev[name] = ref


def _bundle_egress(
    meta, lk_pvars, answers, backend, env, egress_hosts, valkey_enabled
) -> None:
    """Called from the livekit deploy tail, AFTER ``apply_component_vars``/the
    livekit cadvisor confirm but BEFORE livekit's tail writes its vars.yml (so the
    redis vars set here on ``lk_pvars`` are persisted with the livekit unit).
    Egress hosts + the livekit↔egress redis topology were already decided
    (``_ask_egress_hosts``, right after the livekit hosts prompt) — this only
    prompts the redis address/username/password when NOT co-located, sets the
    livekit unit's valkey/redis vars, mirrors livekit's generated api creds (+ the
    redis password when external) into egress's own vault, and writes the
    egress's own unit (vars.yml/vault.yml/hosts) — egress is bundled into the
    livekit bootstrap step so the core env stays the only meet component that
    references livekit. Records ``answers["_egress_bundled"]`` so the deps loop
    registers the egress unit."""
    if valkey_enabled:
        addr, username, pw = "127.0.0.1:6379", "", None
    else:
        addr = _ask("Redis address shared by livekit and egress (host:port)")
        username = _ask(
            "Redis username shared by livekit and egress (leave blank if none)",
            required=False,
        )
        pw = (
            _password(
                "Redis password shared by livekit and egress (leave blank if none)",
                required=False,
            )
            if backend.prompts_values()
            else None
        )
    lk_pvars["st_meet_livekit_valkey_enabled"] = valkey_enabled
    lk_pvars["st_meet_livekit_redis_address"] = addr
    mirror_names = ["st_meet_livekit_api_key", "st_meet_livekit_api_secret"]
    if not valkey_enabled:
        if username:
            lk_pvars["st_meet_livekit_redis_username"] = username
        backend.var_secret(
            lk_pvars, "st_meet_livekit_redis_password", pw, component="livekit"
        )
        mirror_names.append("st_meet_livekit_redis_password")
    ev = CommentedMap()
    ev["st_meet_livekit_domain"] = lk_pvars["st_meet_livekit_domain"]
    ev["st_meet_livekit_redis_address"] = addr
    if not valkey_enabled and username:
        ev["st_meet_livekit_redis_username"] = username
    _mirror_livekit_creds_to_egress(backend, meta, env, ev, lk_pvars, mirror_names)
    writer.apply_component_vars(ev, meta, meta.component("egress"), answers)
    ev[writer.cadvisor_var(meta.app)] = _ask_cadvisor("egress")
    ev.yaml_set_start_comment(
        writer.vars_header(meta.app, meta, meta.component("egress"))
    )
    tree.save_vars(meta.app, env, "egress", ev)
    writer.write_vault(meta.app, env, "egress", backend)
    tree.write_hosts(
        meta.app, env, "egress", meta.component("egress").app_name, egress_hosts
    )
    answers["_egress_bundled"] = "managed"


def _standalone_egress(meta, ev_pvars, answers, backend, env) -> None:
    """Called from a ``provider.key == "egress"`` branch in ``_handle_dependency``: a
    standalone ``bootstrap -c egress`` run. The generic dep tail then writes the
    egress unit's vars/vault/hosts, so this ONLY adopts livekit's already-decided
    domain + redis topology (it does NOT re-prompt or re-decide topology —
    guarantees egress shares the same redis the livekit unit was bootstrapped
    with), including the redis username (plaintext) and — when the livekit unit
    is on an EXTERNAL (non-valkey) redis — mirroring the redis password alongside
    the api key/secret."""
    lvp = paths.vars_path(meta.app, env, "livekit")
    if not lvp.exists():
        raise StCliError(
            "bootstrap livekit first — egress adopts livekit's redis topology "
            "and ws domain, so the livekit unit must already exist."
        )
    lk = tree.load_vars(meta.app, env, "livekit")
    ev_pvars["st_meet_livekit_domain"] = lk["st_meet_livekit_domain"]
    ev_pvars["st_meet_livekit_redis_address"] = lk.get(
        "st_meet_livekit_redis_address", "127.0.0.1:6379"
    )
    username = lk.get("st_meet_livekit_redis_username")
    if username:
        ev_pvars["st_meet_livekit_redis_username"] = username
    external = not lk.get("st_meet_livekit_valkey_enabled", True)
    mirror_names = ["st_meet_livekit_api_key", "st_meet_livekit_api_secret"]
    if external:
        mirror_names.append("st_meet_livekit_redis_password")
    _mirror_livekit_creds_to_egress(backend, meta, env, ev_pvars, lk, mirror_names)


def _reuse_egress(meta, answers, backend, env) -> None:
    """livekit REUSE: keep egress in the deployment. If the egress tree already
    exists (bundled when livekit was first deployed), just re-register it. If it is
    missing (livekit predates egress bundling), create it from livekit's on-disk
    redis topology + ws domain, co-located on the livekit hosts."""
    if paths.vars_path(meta.app, env, "egress").exists():
        answers["_egress_bundled"] = "managed"  # keep as-is, re-register
        return
    ev = CommentedMap()
    _standalone_egress(meta, ev, answers, backend, env)
    writer.apply_component_vars(ev, meta, meta.component("egress"), answers)
    ev[writer.cadvisor_var(meta.app)] = _ask_cadvisor("egress")
    ev.yaml_set_start_comment(
        writer.vars_header(meta.app, meta, meta.component("egress"))
    )
    tree.save_vars(meta.app, env, "egress", ev)
    writer.write_vault(meta.app, env, "egress", backend)
    egress_hosts = tree.read_hosts(meta.app, env, "livekit")
    tree.write_hosts(
        meta.app, env, "egress", meta.component("egress").app_name, egress_hosts
    )
    answers["_egress_bundled"] = "managed"


# --------------------------------------------------------------------------- #
# dependency handling
# --------------------------------------------------------------------------- #
def _prompt_shared(rule: dict) -> str:
    """Prompt for a shared value described by ``rule``."""
    if writer.rule_is_secret(rule):
        return _password(writer.rule_label(rule))
    return _ask(writer.rule_label(rule))


def _handle_dependency(
    meta,
    dep,
    answers,
    backend: SecretBackend,
    env,
    wire_only: bool = False,
    assume_deploy: bool = False,
) -> str:
    """Run the dependency prompt for one dependency; wire shared vars. Returns mode.

    Asks "Bootstrap <dep> now?" with up to four choices: REUSE (if the provider
    tree already exists in this repo), "Yes — bootstrap now", "No — bootstrap later"
    (returns "skip" — registers no unit), and "Already deployed (enter URL +
    keys)" (external). Optional deps surface only as a hint on the
    "Bootstrapping …" line and via the "bootstrap later" choice — there is no
    separate optional confirm anymore.

    With ``wire_only=True`` (core-only bootstrap) the "Yes — bootstrap now" option
    is omitted — only REUSE (if the provider tree exists) / "No — bootstrap later"
    / EXTERNAL remain — so the consumer's env refs are wired without deploying
    any provider. The reuse and external paths are unchanged; the deploy path
    is simply unreachable.

    With ``assume_deploy=True`` (direct provider-target bootstrap, e.g.
    ``bootstrap -c livekit``) the select is skipped entirely and
    ``choice = "deploy"`` is assumed — the user explicitly asked to bootstrap
    that provider, so the "Bootstrap <dep> now?" question is redundant.
    ``assume_deploy`` and ``wire_only`` are mutually exclusive; the existing
    ``has_existing`` overwrite guard, host prompt, and shared-value prompts all
    stay as is.
    """
    provider = meta.component(dep.on)
    core = meta.core()

    ui.console.print()
    optional_hint = "[bold]optional[/bold] " if dep.optional else ""
    ui.info(f"Bootstrapping {dep.on}/{env} ({optional_hint}dependency of {meta.app}).")

    has_existing = paths.vars_path(meta.app, env, provider.key).exists()
    if assume_deploy:
        choice = "deploy"
    else:
        options: dict[str, str] = {}
        if has_existing:
            options["Reuse existing in the repo"] = "reuse"
        # For an optional dep, offer "No — bootstrap later" before "Yes — bootstrap
        # now" so the highlighted default leans towards skipping it.
        if dep.optional:
            options["No — bootstrap later"] = "skip"
            if not wire_only:
                options["Yes — bootstrap now"] = "deploy"
        else:
            if not wire_only:
                options["Yes — bootstrap now"] = "deploy"
            options["No — bootstrap later"] = "skip"
        options["Already deployed (enter URL + keys)"] = "external"
        choice = options[_ask_select(f"Bootstrap {dep.on} now?", list(options))]

    if choice == "skip":
        ui.info(
            f"{dep.on}: bootstrap later — add it with "
            f"`st-cli bootstrap {meta.app} {env} -c {dep.on}`."
        )
        return "skip"

    if choice == "external":
        for rule in dep.shared:
            if not rule.get("consumer_env_key"):
                continue
            if writer.rule_is_secret(rule):
                value = _prompt_shared(rule) if backend.prompts_values() else None
            else:
                value = _prompt_shared(rule)
            if rule.get("answer_key") and value is not None:
                answers[rule["answer_key"]] = value
            writer.inject_consumer(rule, value, answers, backend, core.key)
        if meta.app == "messages" and dep.on == "socks-proxy":
            value = (
                _password("MTA_OUT_DIRECT_PROXIES (socks5s://user:pass@host:port,...)")
                if backend.prompts_values()
                else None
            )
            backend.env_secret(
                answers, "MTA_OUT_DIRECT_PROXIES", component=core.key, value=value
            )
        if meta.app == "messages" and dep.on == "mpa":
            value = (
                _password("SPAM_CONFIG (JSON for the external mpa)")
                if backend.prompts_values()
                else None
            )
            backend.env_secret(answers, "SPAM_CONFIG", component=core.key, value=value)
        ui.info(f"{dep.on}: external — values prompted, not deployed.")
        return "external"

    if choice == "reuse":
        # reuse is a bootstrap behaviour (keep the existing unit as-is) — the unit
        # is still *managed*, so it deploys with the app. The provider's stored
        # values are reused; only the consumer ref is re-injected.
        pvars = tree.load_vars(meta.app, env, provider.key)
        # decrypt the existing vault ONLY in ansible-vault mode (hashi_vault
        # mode prompts a fresh lookup term for each consumer ref instead).
        pvault = (
            vault.decrypt_to_dict(paths.vault_path(meta.app, env, provider.key))
            if backend.prompts_values()
            else {}
        )
        for rule in dep.shared:
            if not rule.get("consumer_env_key"):
                continue
            var = rule.get("var")
            if writer.rule_is_secret(rule):
                if backend.prompts_values():
                    value = pvault.get(var) if var else None
                    value = str(value) if value is not None else _prompt_shared(rule)
                else:
                    # hashi_vault: value is not needed — env_secret prompts a
                    # fresh lookup term for the consumer ref.
                    value = None
            else:
                value = pvars.get(var) if var else None
                value = str(value) if value is not None else _prompt_shared(rule)
            if rule.get("answer_key") and value is not None:
                answers[rule["answer_key"]] = value
            writer.inject_consumer(rule, value, answers, backend, core.key)
        if meta.app == "meet" and provider.key == "livekit" and not wire_only:
            _reuse_egress(meta, answers, backend, env)
        ui.info(f"{dep.on}: reuse — kept existing unit (still deployed).")
        return "managed"

    # deploy: create + manage this unit as part of the deployment
    if has_existing and not _confirm(
        f"Overwrite the existing {dep.on} unit (vars.yml/vault.yml regenerated)?",
        default=False,
    ):
        raise StCliError(f"aborted — {dep.on} left untouched.")
    hosts = _ask_hosts(dep.on)
    egress_hosts = valkey_enabled = None
    if meta.app == "meet" and provider.key == "livekit":
        egress_hosts, valkey_enabled = _ask_egress_hosts(hosts)  # Q2
    pvars = CommentedMap()  # enabled flag is injected on the deploy task, not here
    for rule in dep.shared:
        if rule.get("generate"):
            if backend.prompts_values():  # ansible-vault mints it
                value = writer.gen_value(rule)
                ui.info(
                    f"{dep.on}: generated {rule.get('consumer_env_key') or rule.get('var') or 'value'}."
                )
            else:  # hashi_vault references an existing secret
                value = None
        elif writer.rule_is_secret(rule):
            # prompted secret — only prompt the value in ansible-vault mode
            # (hashi_vault mode prompts a lookup term in var_secret/env_secret).
            value = _prompt_shared(rule) if backend.prompts_values() else None
        else:
            value = _prompt_shared(rule)
        var = rule.get("var")
        consumer_key = rule.get("consumer_env_key")
        if writer.rule_is_secret(rule) and var and consumer_key:
            # same secret on both sides — store once, ref it from both (in
            # hashi_vault mode a single OpenBao location; ansible-vault keeps its
            # historical two-vault behaviour via the default implementation).
            backend.shared_provider_secret(
                pvars,
                answers,
                var,
                consumer_key,
                value,
                provider=provider.key,
                consumer=core.key,
            )
        else:
            if var:  # standalone scalar for the provider
                if writer.rule_is_secret(rule):
                    backend.var_secret(
                        pvars,
                        var,
                        value,
                        component=provider.key,
                        vault_key=rule.get("vault_key"),
                    )
                else:
                    pvars[var] = value
            writer.inject_consumer(rule, value, answers, backend, core.key)
        if (
            rule.get("answer_key") and value is not None
        ):  # expose the raw value to provider component_vars
            answers[rule["answer_key"]] = value
    if meta.app == "messages" and provider.key in ("mta-in", "mpa", "socks-proxy"):
        _ask_messages_provider(
            provider.key, answers, backend, hosts, core.key, meta.app, env, pvars
        )
    if meta.app == "meet" and provider.key == "livekit":
        _ensure_meet_domain(answers)
        # egress hosts already asked (Q2); redis+egress write happens AFTER the
        # livekit cadvisor confirm below.
    elif meta.app == "meet" and provider.key == "egress":
        _standalone_egress(meta, pvars, answers, backend, env)
    writer.apply_component_vars(pvars, meta, provider, answers)
    pvars[writer.cadvisor_var(meta.app)] = _ask_cadvisor(dep.on)  # Q7 livekit cadvisor
    if meta.app == "meet" and provider.key == "livekit":
        # Q8 redis (address/username/password, only when NOT co-located) + Q9
        # egress cadvisor; runs before save_vars so the redis vars land in pvars.
        _bundle_egress(meta, pvars, answers, backend, env, egress_hosts, valkey_enabled)
    pvars.yaml_set_start_comment(writer.vars_header(meta.app, meta, provider))
    tree.save_vars(meta.app, env, provider.key, pvars)
    writer.write_vault(meta.app, env, provider.key, backend)
    tree.write_hosts(meta.app, env, provider.key, provider.app_name, hosts)
    ui.success(f"{dep.on}: managed — wrote vars.yml + vault.yml + hosts.")
    return "managed"


# --------------------------------------------------------------------------- #
# summary (hosts read from the ini, not the manifest)
# --------------------------------------------------------------------------- #
def _print_summary(
    app: str,
    env: str,
    answers: dict,
    units: list[UnitState],
    component: str | None = None,
) -> None:
    ui.success(f"Bootstrapped {app}/{env}.")
    meta = appmeta.load_app(app)
    core_key = meta.core().key
    # When a single non-core component was bootstrapped, the core was not
    # (re)written — answers is empty — so skip the domain/provider lines and
    # narrow the listed units + the "Next" hint to that component.
    scoped = component is not None and component != core_key
    if not scoped:
        ui.info(f"  domain: {answers.get('DOMAIN', '?')}")
        if "OIDC_PROVIDER" in answers:  # keycloak (an IdP itself) has no OIDC provider
            ui.info(f"  OIDC provider: {answers['OIDC_PROVIDER']}")
    shown = [u for u in units if u.component == component] if scoped else units
    for u in shown:
        comp = meta.component(u.component)
        files_key = meta.files_component(u.component).key
        group = tree.effective_group(app, env, meta, comp)
        hosts = ", ".join(tree.read_hosts(app, env, files_key, group=group)) or "(none)"
        ui.info(f"  - {u.component:12s} [{u.mode:8s}] hosts={hosts}")
    # "Next steps" panel (reuses ui.note's boxed style). The .vault-pass backup
    # and `st-cli secrets` steps are ansible-vault only — skipped for hashi_vault
    # (no .vault-pass; secrets live in OpenBao).
    if scoped:
        review_root = f"{app}/{env}/{meta.files_component(component).key}"
        deploy_cmd = f"st-cli deploy {app} {env} -c {component}"
        secrets_cmd = f"st-cli secrets {app} {env} -c {component}"
    else:
        review_root = f"{app}/{env}/*"
        deploy_cmd = f"st-cli deploy {app} {env}"
        secrets_cmd = f"st-cli secrets {app} {env}"

    m = manifest.load_manifest()
    is_vault = manifest.secret_config_for(m, app, env).backend == "ansible-vault"

    steps: list[str] = []
    if is_vault:
        steps.append(
            "[bold]Back up and share .vault-pass with the other operators.[/bold]"
        )
    steps.append(f"Review {review_root}/vars.yml and {review_root}/hosts.")
    if is_vault:
        steps.append(f"Review secrets with `{secrets_cmd}`.")
    steps.append(f"Deploy with `{deploy_cmd}`.")

    body = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
    ui.note(body, title="Next steps")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def _print_bootstrap_intro(meta) -> None:
    """Pre-questionnaire guidance for a full/core/workers bootstrap: an
    architecture-docs pointer + a requirements checklist gated behind a
    yes/no readiness confirmation (declining or Ctrl+C aborts the CLI)."""
    if meta.arch_docs_url:
        ui.note(
            f"Read how [bold]{meta.app}[/bold] is architected before you start:\n"
            f"  {meta.arch_docs_url}",
            title="Bootstrap",
        )
    ui.note(
        "Depending on the app, make sure you've prepared:\n"
        "  • [bold]IP[/bold] or hostname of the VM(s)\n"
        "  • [bold]PostgreSQL[/bold] host and credentials\n"
        "  • [bold]Redis[/bold] host and credentials\n"
        "  • [bold]S3[/bold] endpoint, bucket and credentials\n"
        "  • [bold]Identity provider[/bold] URLs and credentials\n"
        "    (For ProConnect Integration environment: create an app at https://partenaires.proconnect.gouv.fr/)",
        title="Requirements",
    )
    _confirm_ready("Do you have all of the above ready to continue?")


def bootstrap(app: str, env: str, component: str | None = None) -> None:
    """Run the interactive bootstrap questionnaire for ``(app, env)``.

    With ``component`` set, scaffold only that component's unit so a provider
    can be rolled out before the core:

    * a dependency provider (e.g. ``livekit``) — bootstrap it standalone via
      ``_handle_dependency`` with ``assume_deploy=True``: the "Bootstrap <provider>
      now?" select is skipped and the deploy path is taken directly (the user
      explicitly asked to bootstrap it);
    * the core (e.g. ``meet``) — run the core questionnaire + ``writer.write_core``,
      wiring deps WITHOUT deploying any provider (wire-only: each dep offers
      Reuse / deploy-later / external, never "Yes — bootstrap now"); provider units
      are NOT registered in this mode;
    * a worker (only if implemented) — just register the worker unit; the core
      must already exist (workers reuse its vars/vault/hosts).

    No flag (``component=None``) runs today's full bootstrap, byte-for-byte
    unchanged: all deps (deploy/reuse/external) + the core + an optional worker.
    """
    meta = appmeta.load_app(app)
    core = meta.core()
    worker = meta.worker()

    # Validate the requested component against the valid target set:
    # {core.key} ∪ {dep.on} ∪ ({worker.key} if worker implemented).
    valid: set[str] = {core.key}
    valid |= {dep.on for dep in meta.dependencies}
    if worker and worker.implemented:
        valid.add(worker.key)
    if component is not None and component not in valid:
        raise StCliError(
            f"unknown component {component!r} for {app!r}; "
            f"valid targets: {', '.join(sorted(valid))}"
        )

    # Pre-questionnaire guidance for a full/core/workers bootstrap (an
    # architecture-docs pointer + a requirements checklist gated behind a
    # 'press Enter when ready' acknowledgement). Provider-only runs
    # (`-c <provider>`) skip it.
    core_or_worker = {core.key} | (
        {worker.key} if worker and worker.implemented else set()
    )
    if component is None or component in core_or_worker:
        _print_bootstrap_intro(meta)

    ui.note(
        "This questionnaire only scaffolds your config files.\n"
        "If you mistype an answer, don't start over: finish "
        "the questionnaire, then edit the generated files directly under "
        "<app>/<env>/<component>/."
    )
    m = _ensure_manifest()
    # Choose the secret backend (ansible-vault | hashi_vault) per (app, env).
    # The choice is persisted into .st-cli.yml; connection details for
    # hashi_vault go into <app>/<env>/common.yml.
    backend = setup_backend(m, app, env)
    if backend.kind == "ansible-vault":
        vault.ensure_vault_password(create=True)
    tree.ensure_common(app, env)
    tree.ensure_ssh_scaffold()

    ui.info(f"Bootstrapping {app}/{env}.")

    # Scope flags — gate the sections below so the no-flag path is unchanged.
    target_core = component in (None, core.key)
    target_worker = bool(
        worker and worker.implemented and component in (None, worker.key)
    )
    if component is None:
        deps, wire_only, upsert_providers = meta.dependencies, False, True
        assume_deploy = False
    elif component == core.key:
        deps, wire_only, upsert_providers = meta.dependencies, True, False
        assume_deploy = False
    elif worker is not None and component == worker.key:
        deps, wire_only, upsert_providers = [], False, False
        assume_deploy = False
    else:
        # component is a dependency provider (the only remaining valid target).
        # assume_deploy=True: the user explicitly asked to bootstrap this
        # provider, so skip the "Bootstrap <provider> now?" select and go straight
        # to the deploy path (overwrite guard + prompts remain).
        deps = [d for d in meta.dependencies if d.on == component]
        wire_only, upsert_providers = False, True
        assume_deploy = True

    answers: dict = {}
    core_hosts: list[str] = []
    worker_hosts: list[str] = []
    core_cadvisor = True

    # Each component proposes its own action. Core: fresh → ask; existing →
    # keep or re-bootstrap. Dependencies: the 3-state prompt (deploy/reuse/external).
    redo_core = False
    if target_core:
        redo_core = True
        if paths.vars_path(app, env, core.key).exists():
            redo_core = _confirm(
                f"Re-bootstrap the '{core.key}' component? (rewrites its vars.yml/vault.yml "
                "and regenerates its secrets)",
                default=False,
            )
        if redo_core:
            core_hosts = _ask_hosts(core.key)  # hosts first, then the env questionnaire
            # Optional worker IPs: blank ⇒ workers co-locate on the core hosts (the
            # default). Meet has no workers implementation, so it is never prompted.
            if worker and worker.implemented:
                worker_hosts = _ask_hosts(
                    f"workers (leave blank to run on the {core.key} hosts)",
                    allow_empty=True,
                )
            # keycloak is not a Django app — it takes its own (raw-env) questionnaire.
            answers = (
                _ask_keycloak(meta, backend)
                if app == "keycloak"
                else _ask_core(meta, backend)
            )
            core_cadvisor = _ask_cadvisor(core.key)  # last core question
        else:
            ui.info(f"{core.key}: kept existing.")

    # Worker-only bootstrap: the core must already exist (workers reuse its
    # vars/vault/hosts). In the full path the core was just (re)written above.
    if target_worker and worker is not None and component == worker.key:
        if not paths.vars_path(app, env, core.key).exists():
            raise StCliError(
                "bootstrap the core first — workers reuse its vars/vault/hosts."
            )

    for dep in deps:
        if (
            app == "messages"
            and dep.on == "socks-proxy"
            and answers.get("MTA_OUT_MODE") == "relay"
        ):
            continue
        if app == "meet" and dep.on == "egress" and component != "egress":
            continue  # egress is bundled into the livekit step, not a separate iteration
        mode = _handle_dependency(
            meta,
            dep,
            answers,
            backend,
            env,
            wire_only=wire_only,
            assume_deploy=assume_deploy,
        )
        if upsert_providers and mode != "skip":
            manifest.upsert_unit(
                m, UnitState(app=app, env=env, component=dep.on, mode=mode)
            )
            if app == "meet" and dep.on == "livekit" and answers.get("_egress_bundled"):
                manifest.upsert_unit(
                    m,
                    UnitState(
                        app=app,
                        env=env,
                        component="egress",
                        mode=answers["_egress_bundled"],
                    ),
                )

    if target_core and redo_core:
        writer.write_core(
            meta, answers, backend, core_hosts, worker_hosts, env, core_cadvisor
        )
    if target_core:
        manifest.upsert_unit(
            m, UnitState(app=app, env=env, component=core.key, mode="managed")
        )
    # workers own no files — they reuse the core unit's vars/vault and only flip
    # st_<app>_workers_enabled. A [workers] inventory group is written (in the
    # core's hosts file) only when worker IPs were entered; otherwise the worker
    # falls back to the core group. Meet has no workers implementation, so it is
    # neither prompted nor registered.
    if target_worker:
        manifest.upsert_unit(
            m, UnitState(app=app, env=env, component=worker.key, mode="managed")
        )

    units = manifest.units_for(m, app, env)
    manifest.save_manifest(m)
    if target_core and not redo_core and meta.dependencies:
        ui.warn(
            f"Kept {core.key} unchanged — if you changed a dependency's shared value, "
            f"re-bootstrap {core.key} so its env picks it up."
        )
    _print_summary(app, env, answers, units, component)
