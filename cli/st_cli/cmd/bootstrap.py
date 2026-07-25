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

**Rebootstrap.** Re-running this questionnaire over an ``(app, env, component)``
that already has a committed ``vars.yml`` is a *rebootstrap*, not a destructive
rebuild: every prompt is pre-filled from what is already on disk
(:mod:`st_cli.core.recover`) so pressing Enter through the whole thing
reproduces the current config byte-for-byte (the property the whole feature
rests on — see ``core/recover.py``'s module docstring). Three mechanisms make
that possible, used throughout this module:

* :func:`_recall` — the pre-fill (``default=``) for an ordinary text prompt.
* :func:`_ask_secret` — a secret is **never** re-prompted or regenerated once
  a value for its key already sits in ``answers`` (a recovered ``{{ vault_x
  }}``/hashi-lookup ref is exactly what the next render needs — asking again,
  or worse regenerating, would silently rotate a live credential).
* Conditional gates (the SMTP confirm, the blobs-offload confirm, the
  DATABASE_URL/discrete select, the direct/relay select, the OIDC provider
  select, and the per-dependency deploy/reuse/external select) derive their
  *default* from recovered state instead of a hardcoded first-run default —
  otherwise an Enter-through rebootstrap would silently tear out working
  configuration (see each gate's own comment for the reasoning).

:func:`core.writer.write_core` already merges rather than replaces
``vars.yml`` (comments/hand-edits survive) and :func:`core.writer.write_vault`
already merges rather than replaces ``vault.yml`` (and no-ops when nothing new
was prompted) — this module's job is only to feed both of those the same
answers a from-scratch run would have produced, so neither ever sees a reason
to touch what is already correct.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ruamel.yaml.comments import CommentedMap

from .. import __version__
from ..core import (
    appmeta,
    envrender,
    manifest,
    paths,
    recover,
    secrets,
    tree,
    ui,
    vault,
    writer,
)
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
_EMAIL_APPS = {"drive", "meet"}

# Inverse of _ask_keycloak's "jdbc:postgresql://host:port/name" composition, so
# the 3 separate DB prompts can be pre-filled from the single recovered
# KC_DB_URL (kept here, not core/recover.py, which is deliberately app-agnostic).
_KC_DB_URL_RE = re.compile(
    r"^jdbc:postgresql://(?P<host>[^:/]+):(?P<port>\d+)/(?P<name>.+)$"
)

# Inverse of MESSAGES_BLOBS_ENCRYPT_KEYS' JSON composition (see
# _ask_messages_storage), so the single generated secret embedded inside it can
# be recovered without re-parsing/round-tripping JSON.
_ENCRYPT_KEY_RE = re.compile(r'"secret":\s*"([^"]*)"')

# Inverse of the drive/collabora shared rule's "https://{value}/hosting/discovery"
# consumer_format (see apps/drive.yml) — that rule has no `var`, so
# core.recover.recover_shared cannot recover it; this reconstructs the plain
# domain from the core's own already-recovered WOPI_COLLABORA_DISCOVERY_URL.
_COLLABORA_URL_RE = re.compile(r"^https://(?P<domain>.+)/hosting/discovery$")


# --------------------------------------------------------------------------- #
# rebootstrap helpers
# --------------------------------------------------------------------------- #
def _recall(answers: dict, key: str, fallback: str = "") -> str:
    """The pre-fill (``default=``) for an ordinary text prompt.

    Use as ``_ask("DB_HOST", _recall(answers, "DB_HOST"))``. When a call site
    used to pass a first-run default (``_ask("DB_PORT", "5432")``), pass that
    same value as ``fallback`` (``_recall(answers, "DB_PORT", "5432")``) so a
    recovered value still wins over it. When a call site used a ``placeholder=``
    instead, leave ``fallback`` empty and pass the placeholder through
    unchanged: :func:`core.prompts._text_question` already ignores
    ``placeholder`` whenever ``default`` is non-empty, so a recovered value
    silently drops the ghost hint on its own (see ``core/prompts.py:36-45``) —
    nothing extra to do here.
    """
    value = answers.get(key)
    return str(value) if value is not None else fallback


def _recall_bool(answers: dict, key: str, fallback: bool) -> bool:
    """Tolerant boolean pre-fill for a ``_confirm`` gate's ``default=``.

    Mirrors :func:`core.recover.recover_cadvisor`'s tolerant string parsing
    (a recovered value may be a real bool, or a string like ``"1"``/``"true"``
    from an env blob or a hand-edited ``vars.yml``). Absence degrades to
    ``fallback`` — the historical first-run default — not ``False``.
    """
    value = answers.get(key)
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "on", "1")


def _ask_secret(
    answers: dict,
    backend: SecretBackend,
    key: str,
    component: str,
    label: str | None = None,
    gen=None,
) -> None:
    """Prompt for (or generate) a secret and route it through the backend —
    unless ``answers`` already holds a value for ``key``, in which case this
    is a no-op.

    This is the single place enforcing "never re-prompt, never rotate an
    already-decided secret": a recovered secret is the literal ``{{ vault_x
    }}`` ref (or a hashi lookup ref) parsed verbatim out of the committed env
    blob by :func:`core.recover.recover` — exactly the string the next render
    needs. Returning immediately here leaves it untouched in ``answers`` AND
    leaves the backend's per-component vault buffer empty for this key, so
    :func:`core.writer.write_vault` merges over the existing ``vault.yml``
    instead of clobbering it (see that function's docstring) — a secret field
    has no editable ``default=`` (unlike a text prompt) precisely because it is
    hidden input, so "skip the prompt entirely" is the only way to avoid
    re-asking it.
    """
    if key in answers:
        return
    if gen is not None:
        value = gen() if backend.prompts_values() else None
    else:
        value = _password(label or key) if backend.prompts_values() else None
    backend.env_secret(answers, key, component=component, value=value)


def _cadvisor_default(app: str, env: str, component: str) -> bool:
    """``recover.recover_cadvisor``'s ``None`` (absent/never bootstrapped) falls
    back to the historical first-run default of ``True``."""
    recovered = recover.recover_cadvisor(app, env, component)
    return True if recovered is None else recovered


# --------------------------------------------------------------------------- #
# manifest + local bootstrap
# --------------------------------------------------------------------------- #
def _ensure_manifest() -> StCliManifest:
    """Load ``.st-cli.yml`` or create a fresh one pinned to this CLI version."""
    if paths.manifest_path().exists():
        return manifest.load_manifest()

    return StCliManifest(
        collection_version=__version__, cli_version=__version__, units=[]
    )


# --------------------------------------------------------------------------- #
# identity provider / OIDC + core Django answers
# --------------------------------------------------------------------------- #
def _ask_oidc(answers: dict, backend: SecretBackend, component: str) -> None:
    """Choose an identity provider; fill OIDC answers (client secret → backend).

    The provider itself is never stored anywhere in the tree — the committed
    ``OIDC_OP_*`` endpoints (already recovered into ``answers`` by the time
    this runs) ARE the provider choice, so ``core.recover.recover_oidc``
    infers it back from them and pre-selects the same choice (and pre-fills
    the keycloak base-url/realm follow-ups) on a rebootstrap.
    """
    recovered_provider, recovered_base, recovered_realm = recover.recover_oidc(answers)
    provider = _ask_select(
        "Identity provider:", _OIDC_PROVIDERS, default=recovered_provider
    )
    base_url = realm = None
    answers["OIDC_PROVIDER"] = provider
    # The recovered base-url/realm only apply when the operator kept the SAME
    # provider as before — if they picked a different one this run, prefilling
    # them would silently mix state from an unrelated provider.
    same_provider = provider == recovered_provider
    if provider == "keycloak":
        base_url = _ask(
            "Keycloak base URL",
            recovered_base if same_provider else "",
            placeholder="https://idp.example.org",
        )
        realm = _ask(
            "Keycloak realm", (recovered_realm if same_provider else "") or "master"
        )
    elif provider == "custom":
        base_url = _ask(
            "Custom OIDC issuer base URL (optional)",
            recovered_base if same_provider else "",
            required=False,
        )
    answers.update(envrender.oidc_endpoints(provider, base_url, realm))
    answers["OIDC_RP_CLIENT_ID"] = _ask(
        "OIDC_RP_CLIENT_ID", _recall(answers, "OIDC_RP_CLIENT_ID")
    )
    _ask_secret(answers, backend, "OIDC_RP_CLIENT_SECRET", component)


def _ask_email(answers: dict, backend: SecretBackend, component: str, app: str) -> None:
    """Prompt Django transactional email (SMTP) settings for drive / meet.

    Skipped entirely for ``messages`` (no ``DJANGO_EMAIL_*`` upstream). The SMTP
    password is a secret routed through the backend like the other env secrets;
    optional fields are only written into ``answers`` when filled in so template
    guards stay clean.

    The confirm gate's default is derived from whether SMTP was already
    configured (``DJANGO_EMAIL_HOST`` recovered) — hardcoding ``default=False``
    here would mean an Enter-through rebootstrap silently DROPS a working SMTP
    configuration (the confirm declines, none of the fields below are asked,
    and the whole block is omitted from the next render).
    """
    if app not in _EMAIL_APPS:
        return
    if not _confirm(
        "Configure transactional email (SMTP) settings?",
        default=bool(_recall(answers, "DJANGO_EMAIL_HOST")),
    ):
        return
    answers["DJANGO_EMAIL_HOST"] = _ask(
        "DJANGO_EMAIL_HOST",
        _recall(answers, "DJANGO_EMAIL_HOST"),
        placeholder="smtp.example.org",
    )
    answers["DJANGO_EMAIL_PORT"] = _ask(
        "DJANGO_EMAIL_PORT", _recall(answers, "DJANGO_EMAIL_PORT", "587")
    )
    host_user = _ask(
        "DJANGO_EMAIL_HOST_USER (optional)",
        _recall(answers, "DJANGO_EMAIL_HOST_USER"),
        required=False,
    )
    if host_user:
        answers["DJANGO_EMAIL_HOST_USER"] = host_user
    _ask_secret(answers, backend, "DJANGO_EMAIL_HOST_PASSWORD", component)
    answers["DJANGO_EMAIL_USE_TLS"] = (
        "true"
        if _confirm(
            "DJANGO_EMAIL_USE_TLS?",
            default=_recall_bool(answers, "DJANGO_EMAIL_USE_TLS", True),
        )
        else "false"
    )
    answers["DJANGO_EMAIL_USE_SSL"] = (
        "true"
        if _confirm(
            "DJANGO_EMAIL_USE_SSL?",
            default=_recall_bool(answers, "DJANGO_EMAIL_USE_SSL", False),
        )
        else "false"
    )
    answers["DJANGO_EMAIL_FROM"] = _ask(
        "DJANGO_EMAIL_FROM",
        _recall(answers, "DJANGO_EMAIL_FROM"),
        placeholder="noreply@example.org",
    )
    brand_name = _ask(
        "DJANGO_EMAIL_BRAND_NAME (optional)",
        _recall(answers, "DJANGO_EMAIL_BRAND_NAME"),
        required=False,
    )
    if brand_name:
        answers["DJANGO_EMAIL_BRAND_NAME"] = brand_name


def _ask_cadvisor(label: str, default: bool = True) -> bool:
    """Prompt whether to enable the cadvisor monitoring sidecar for a component.

    ``default`` is the historical first-run default (``True``) unless the
    caller passes a recovered value (see :func:`_cadvisor_default`) — a
    rebootstrap must offer the operator's CURRENT choice, not silently flip a
    disabled monitor back on (or vice versa) on every Enter-through rerun.
    """
    return _confirm(
        f"Enable cadvisor container monitoring for {label}?", default=default
    )


def _ask_db(answers: dict, backend: SecretBackend, component: str, app: str) -> None:
    """Prompt database connection: a DATABASE_URL or discrete DB_* vars.

    The mode select's default is derived from which shape was actually
    recovered (``DB_HOST`` present ⇒ discrete; otherwise the natural
    first-listed "DATABASE_URL" choice already matches) — a rebootstrap must
    not silently flip an operator from discrete DB_* vars to DATABASE_URL (or
    back) just because the select defaults to its first option.
    """
    default_mode = "discrete (DB_*)" if "DB_HOST" in answers else None
    mode = _ask_select(
        "Database configuration:",
        ["DATABASE_URL", "discrete (DB_*)"],
        default=default_mode,
    )
    if mode.startswith("DATABASE_URL"):
        # DATABASE_URL is itself the secret (it may embed a password) — never
        # re-prompt it once recovered: unlike DB_PASSWORD, there is no separate
        # plaintext field to recall a default from, so re-prompting would mean
        # retyping the whole URL, and pre-filling with the recovered
        # `{{ vault_database_url }}` ref would corrupt vault.yml (see
        # _ask_secret's docstring for why a secret field has no `default=`).
        if "DATABASE_URL" not in answers:
            value = _ask("DATABASE_URL") if backend.prompts_values() else None
            backend.env_secret(
                answers, "DATABASE_URL", component=component, value=value
            )
        return
    answers["DB_HOST"] = _ask("DB_HOST", _recall(answers, "DB_HOST"))
    answers["DB_NAME"] = _ask("DB_NAME", _recall(answers, "DB_NAME", app))
    answers["DB_USER"] = _ask("DB_USER", _recall(answers, "DB_USER", app))
    _ask_secret(answers, backend, "DB_PASSWORD", component)
    answers["DB_PORT"] = _ask("DB_PORT", _recall(answers, "DB_PORT", "5432"))


def _ask_keycloak(meta, backend: SecretBackend, answers: dict | None = None) -> dict:
    """Collect the keycloak core answers → the ``st_keycloak_env`` blob.

    Keycloak is not a Django app: its role consumes a single free-form
    ``st_keycloak_env`` blob (no DOMAIN/Redis/S3/OIDC/email questionnaire). The
    ``messages-keycloak`` image bakes in ``KC_DB=postgres`` + features/metrics/health
    at build time, so we only prompt for what the operator must supply at runtime:
    the DB connection, the public hostname, and the admin bootstrap credentials.
    Passwords route through the secret backend exactly like the Django apps'
    ``DB_PASSWORD`` (``{{ vault_* }}`` ref in the blob, real value in vault.yml).

    ``answers`` (rebootstrap) is the dict recovered by
    :func:`core.recover.recover` for the keycloak core unit — every prompt
    below pre-fills from it. ``KC_DB_URL`` is recovered as a single composed
    string (``jdbc:postgresql://host:port/name``); it is decomposed back into
    the 3 separate prompts by :data:`_KC_DB_URL_RE` (the exact inverse of the
    f-string composition below) since there is no other single source for the
    individual host/port/name values.
    """
    core_key = meta.core().key
    answers = dict(answers) if answers else {}
    domain = _ask(
        "Public domain for keycloak",
        _recall(answers, "DOMAIN") or _recall(answers, "KC_HOSTNAME"),
        placeholder="idp.example.org",
    )
    # DOMAIN feeds _print_summary; KC_HOSTNAME is the actual env key.
    answers["DOMAIN"] = domain
    answers["KC_HOSTNAME"] = domain

    db_url_match = _KC_DB_URL_RE.match(_recall(answers, "KC_DB_URL"))
    db_host = _ask(
        "Database host",
        db_url_match.group("host") if db_url_match else "",
        placeholder="db.example.org",
    )
    db_port = _ask(
        "Database port", (db_url_match.group("port") if db_url_match else "") or "5432"
    )
    db_name = _ask(
        "Database name",
        (db_url_match.group("name") if db_url_match else "") or "keycloak",
    )
    answers["KC_DB_URL"] = f"jdbc:postgresql://{db_host}:{db_port}/{db_name}"
    answers["KC_DB_USERNAME"] = _ask(
        "Database user", _recall(answers, "KC_DB_USERNAME", "keycloak")
    )
    _ask_secret(answers, backend, "KC_DB_PASSWORD", core_key)

    answers["KC_BOOTSTRAP_ADMIN_USERNAME"] = _ask(
        "Bootstrap admin username",
        _recall(answers, "KC_BOOTSTRAP_ADMIN_USERNAME", "admin"),
    )
    _ask_secret(answers, backend, "KC_BOOTSTRAP_ADMIN_PASSWORD", core_key)
    return answers


def _ask_core(meta, backend: SecretBackend, answers: dict | None = None) -> dict:
    """Collect the core component answers (domain, db, redis, s3, secrets, OIDC).

    ``answers`` (rebootstrap) is the dict recovered by
    :func:`core.recover.recover` for the core unit — copied (never mutated in
    place, the caller's dict is disposable but this keeps the function pure)
    and used to pre-fill every prompt below via :func:`_recall`/:func:`_ask_secret`.
    """
    app = meta.app
    core_key = meta.core().key
    answers = dict(answers) if answers else {}
    # DOMAIN itself is only a committed `st_*` var for meet/drive (see
    # apps/*.yml's `st_<app>_public_host: "{DOMAIN}"`) — recover() recovers it
    # directly for those two via the component-var inversion. messages has no
    # such var, so DOMAIN never comes back that way; it falls back to
    # DJANGO_ALLOWED_HOSTS, which for every app EXCEPT meet (which overrides it
    # to the "{{ st_meet_public_host }}" indirection) is emitted as the literal
    # domain string — exactly what was typed here originally.
    domain = _ask(
        f"Public domain for {app}",
        _recall(answers, "DOMAIN") or _recall(answers, "DJANGO_ALLOWED_HOSTS"),
        placeholder=f"{app}.example.org",
    )

    answers.update(
        {
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
    )
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
    _ask_secret(
        answers,
        backend,
        "DJANGO_SECRET_KEY",
        core_key,
        gen=secrets.gen_secret,
    )

    _ask_db(answers, backend, core_key, app)

    # REDIS_URL can embed a password (redis://user:password@host) so it is
    # routed through the secret backend like DATABASE_URL — and, like
    # DATABASE_URL, must never be re-prompted once recovered (see _ask_db's
    # comment: pre-filling a `default=` from the recovered `{{ vault_x }}` ref
    # would store that ref string AS the secret value, corrupting vault.yml).
    # CELERY_BROKER_URL mirrors the same broker, so it references the same
    # secret (one vault entry / one OpenBao lookup) rather than prompting again.
    if "REDIS_URL" not in answers:
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
        if app == "drive":
            # drive's committed AWS_S3_ENDPOINT_URL/AWS_STORAGE_BUCKET_NAME hold
            # the `{{ st_drive_s3_* }}` indirection (set below), NOT the real
            # endpoint/bucket the operator originally typed — recover() cannot
            # invert that. Reconstruct the prompt pre-fills instead from the
            # recovered S3_PROTOCOL/S3_HOST/S3_BUCKET component vars (see
            # apps/drive.yml's `st_drive_s3_*: "{S3_*}"` mapping — an exact
            # single-placeholder template, so core.recover.recover's
            # component-var inversion DOES recover those directly).
            s3_protocol = _recall(answers, "S3_PROTOCOL")
            s3_host = _recall(answers, "S3_HOST")
            endpoint_default = f"{s3_protocol}://{s3_host}" if s3_host else ""
            bucket_default = _recall(answers, "S3_BUCKET")
        else:
            # meet/other apps keep the literal endpoint/bucket in the blob (no
            # indirection), so the standard recall applies unchanged.
            endpoint_default = _recall(answers, "AWS_S3_ENDPOINT_URL")
            bucket_default = _recall(answers, "AWS_STORAGE_BUCKET_NAME")

        endpoint = _ask(
            "AWS_S3_ENDPOINT_URL",
            endpoint_default,
            placeholder="https://s3.fr-par.scw.cloud",
        )
        answers["AWS_S3_ACCESS_KEY_ID"] = _ask(
            "AWS_S3_ACCESS_KEY_ID", _recall(answers, "AWS_S3_ACCESS_KEY_ID")
        )
        _ask_secret(answers, backend, "AWS_S3_SECRET_ACCESS_KEY", core_key)
        bucket = _ask("AWS_STORAGE_BUCKET_NAME", bucket_default)
        answers["AWS_S3_REGION_NAME"] = _ask(
            "AWS_S3_REGION_NAME (optional)",
            _recall(answers, "AWS_S3_REGION_NAME"),
            required=False,
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
            # The collabora dependency's shared "domain" prompt rule has no
            # `var` (see apps/drive.yml), so core.recover.recover_shared cannot
            # recover it — the deps loop instead falls back to
            # answers.get(rule["answer_key"]) (COLLABORA_DOMAIN). Reconstruct
            # it here from the already-recovered WOPI_COLLABORA_DISCOVERY_URL
            # (the inverse of the rule's consumer_format).
            m = _COLLABORA_URL_RE.match(
                _recall(answers, "WOPI_COLLABORA_DISCOVERY_URL")
            )
            if m:
                answers.setdefault("COLLABORA_DOMAIN", m.group("domain"))
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
        else:
            answers["AWS_S3_ENDPOINT_URL"] = endpoint
            answers["AWS_STORAGE_BUCKET_NAME"] = bucket

    if app == "messages":
        # MDA_API_SECRET is a messages-core secret (mta-in is only a consumer).
        # Generate it here so it exists whenever messages is bootstrapped —
        # independent of whether mta-in is deployed / skipped / external.
        _ask_secret(
            answers, backend, "MDA_API_SECRET", core_key, gen=secrets.gen_secret
        )
        # SALT_KEY: django-fernet-encrypted-fields key (DKIM keys, channel secrets).
        # Required in practice — an empty value makes encrypted-field writes raise.
        _ask_secret(answers, backend, "SALT_KEY", core_key, gen=secrets.gen_secret)
        _ask_messages_storage(answers, backend, core_key)
        # OPENSEARCH_URL is mandatory: the in-app default points at a non-existent
        # `opensearch` host, so search silently breaks unless it is set here.
        answers["OPENSEARCH_URL"] = _ask(
            "OPENSEARCH_URL",
            _recall(answers, "OPENSEARCH_URL"),
            placeholder="http://opensearch:9200",
        )
        # MESSAGES_TECHNICAL_DOMAIN backs the MX/SPF/DKIM DNS records
        # (get_expected_dns_records substitutes it into MESSAGES_DNS_RECORDS) and the
        # exporter noreply@ address. The in-app default `localhost` breaks real mail,
        # so prompt for it.
        answers["MESSAGES_TECHNICAL_DOMAIN"] = _ask(
            "MESSAGES_TECHNICAL_DOMAIN",
            _recall(answers, "MESSAGES_TECHNICAL_DOMAIN"),
            placeholder="mail.example.org",
        )

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
            "MX public hostname (MYHOSTNAME) for mta-in",
            _recall(answers, "MYHOSTNAME"),
            placeholder="mx.example.org",
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
            "PROXY_EXTERNAL (socks-proxy egress interface)",
            _recall(answers, "PROXY_EXTERNAL", "eth0"),
        )
        port = _ask(
            "PROXY_INTERNAL_PORT", _recall(answers, "PROXY_INTERNAL_PORT", "50405")
        )
        answers["PROXY_INTERNAL_PORT"] = port
        if "PROXY_USERS" in answers:
            pass  # already recovered/decided this run — never rotate it
        elif backend.prompts_values():  # ansible-vault: mint the credential + mirror it
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

    The blobs-offload confirm's default is derived from the recovered
    ``MESSAGES_BLOBS_OFFLOAD_ENABLED`` flag — hardcoding ``default=False`` would
    silently drop a configured offload bucket on an Enter-through rebootstrap.
    ``MESSAGES_BLOBS_ENCRYPT_KEY`` (the raw secret) is never itself emitted by
    any template — only the composed ``MESSAGES_BLOBS_ENCRYPT_KEYS`` JSON is —
    so it is recovered by extracting it back out of that JSON
    (:data:`_ENCRYPT_KEY_RE`) before :func:`_ask_secret` is asked to skip
    prompting/generating it: without this, a rebootstrap would mint a BRAND NEW
    key every time (the key itself was never "in answers" to begin with).
    """
    # --- imports bucket (always) ---
    answers["STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL"] = _ask(
        "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL",
        _recall(answers, "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL"),
        placeholder="https://s3.fr-par.scw.cloud",
    )
    answers["STORAGE_MESSAGE_IMPORTS_BUCKET_NAME"] = _ask(
        "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME",
        _recall(answers, "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME"),
        placeholder="msg-imports",
    )
    answers["STORAGE_MESSAGE_IMPORTS_ACCESS_KEY"] = _ask(
        "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY",
        _recall(answers, "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY"),
    )
    _ask_secret(answers, backend, "STORAGE_MESSAGE_IMPORTS_SECRET_KEY", core_key)
    region = _ask(
        "STORAGE_MESSAGE_IMPORTS_REGION_NAME (optional)",
        _recall(answers, "STORAGE_MESSAGE_IMPORTS_REGION_NAME"),
        required=False,
    )
    if region:
        answers["STORAGE_MESSAGE_IMPORTS_REGION_NAME"] = region
    answers["STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY"] = _ask(
        "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY",
        _recall(answers, "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
    )

    # --- blobs offload bucket (optional) ---
    if not _confirm(
        "Enable blobs offloading to S3 (pg→ S3)?",
        default=_recall_bool(answers, "MESSAGES_BLOBS_OFFLOAD_ENABLED", False),
    ):
        return
    answers["MESSAGES_BLOBS_OFFLOAD_ENABLED"] = "1"
    answers["STORAGE_MESSAGE_BLOBS_ENDPOINT_URL"] = _ask(
        "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL",
        _recall(answers, "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL"),
        placeholder="https://s3.fr-par.scw.cloud",
    )
    answers["STORAGE_MESSAGE_BLOBS_BUCKET_NAME"] = _ask(
        "STORAGE_MESSAGE_BLOBS_BUCKET_NAME",
        _recall(answers, "STORAGE_MESSAGE_BLOBS_BUCKET_NAME"),
        placeholder="msg-blobs",
    )
    answers["STORAGE_MESSAGE_BLOBS_ACCESS_KEY"] = _ask(
        "STORAGE_MESSAGE_BLOBS_ACCESS_KEY",
        _recall(answers, "STORAGE_MESSAGE_BLOBS_ACCESS_KEY"),
    )
    _ask_secret(answers, backend, "STORAGE_MESSAGE_BLOBS_SECRET_KEY", core_key)
    region = _ask(
        "STORAGE_MESSAGE_BLOBS_REGION_NAME (optional)",
        _recall(answers, "STORAGE_MESSAGE_BLOBS_REGION_NAME"),
        required=False,
    )
    if region:
        answers["STORAGE_MESSAGE_BLOBS_REGION_NAME"] = region
    # encryption key: generate (ansible-vault) / lookup (hashi); only the secret is
    # dynamic. Recover it out of the composed JSON first (see docstring above) so
    # _ask_secret can see it was already decided.
    if "MESSAGES_BLOBS_ENCRYPT_KEY" not in answers:
        m = _ENCRYPT_KEY_RE.search(_recall(answers, "MESSAGES_BLOBS_ENCRYPT_KEYS"))
        if m and m.group(1):
            answers["MESSAGES_BLOBS_ENCRYPT_KEY"] = m.group(1)
    _ask_secret(
        answers, backend, "MESSAGES_BLOBS_ENCRYPT_KEY", core_key, gen=secrets.gen_token
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
    the secret backend) and suppresses the socks-proxy prompt (see the deps loop).

    ``MTA_OUT_MODE`` is only ever recovered as ``"relay"`` (direct mode never
    sets it — see the ``return`` below): so the select's default is only set
    explicitly for relay, overriding the natural first-listed "direct" choice;
    a recovered direct mode needs no override since "direct" IS the first
    (naturally highlighted) option already.
    """
    choices = [
        "direct: send from the messages host / socks-proxy",
        "relay: send via an external SMTP server",
    ]
    default_choice = choices[1] if answers.get("MTA_OUT_MODE") == "relay" else None
    choice = _ask_select(
        "Outbound mail mode (MTA_OUT_MODE):", choices, default=default_choice
    )
    if not choice.startswith("relay"):
        return
    answers["MTA_OUT_MODE"] = "relay"
    answers["MTA_OUT_RELAY_HOST"] = _ask(
        "MTA_OUT_RELAY_HOST",
        _recall(answers, "MTA_OUT_RELAY_HOST"),
        placeholder="smtp.example.org:587",
    )
    user = _ask(
        "MTA_OUT_RELAY_USERNAME (optional, blank = no auth)",
        _recall(answers, "MTA_OUT_RELAY_USERNAME"),
        required=False,
    )
    if user:
        answers["MTA_OUT_RELAY_USERNAME"] = user
        _ask_secret(answers, backend, "MTA_OUT_RELAY_PASSWORD", core_key)


def _ensure_meet_domain(answers: dict, recovered_domain: str = "") -> None:
    """meet/livekit: the livekit unit's st_meet_public_host component var is
    built from DOMAIN in apply_component_vars. DOMAIN is already collected in a
    full bootstrap; for a standalone `bootstrap -c livekit` run answers is empty,
    so prompt it — pre-filled with ``recovered_domain`` (the livekit unit's OWN
    committed DOMAIN, via ``recover.recover(app, env, "livekit")``'s
    component-var inversion of its ``st_meet_public_host: "{DOMAIN}"``) on a
    standalone rebootstrap, so re-running `-c livekit` doesn't force the
    operator to retype the domain every single time."""
    if not answers.get("DOMAIN"):
        answers["DOMAIN"] = _ask(
            "Public domain for meet (for the LiveKit recording webhook)",
            recovered_domain,
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


def _ask_egress_hosts(
    meta, env: str, livekit_hosts: list[str]
) -> tuple[list[str], bool]:
    """meet/livekit: ask the egress hosts (blank ⇒ co-locate on the livekit hosts)
    right after the livekit hosts prompt (Q2 — BEFORE the LiveKit domain/TURN
    prompts) and decide the livekit↔egress redis topology up front so the redis
    prompt (if any) can be asked later, after the livekit cadvisor confirm.
    Returns (egress_hosts, valkey_enabled).

    The pre-fill (rebootstrap) is egress's own recovered hosts, if any — the
    ``or list(livekit_hosts)`` fallback below is unaffected either way (a
    recovered co-located egress equals ``livekit_hosts`` already)."""
    egress_hosts = _ask_hosts(
        "egress (leave blank to co-locate on the livekit hosts)",
        allow_empty=True,
        default=recover.recover_hosts(meta.app, env, "egress"),
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
    writer.expand_var_markers(ev, backend)
    ev[writer.cadvisor_var(meta.app)] = _ask_cadvisor(
        "egress", _cadvisor_default(meta.app, env, "egress")
    )
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
    writer.expand_var_markers(ev, backend)
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
def _prompt_shared(rule: dict, default: str = "") -> str:
    """Prompt for a shared value described by ``rule``.

    ``default`` pre-fills a NON-secret prompt (a secret field has no editable
    default — see :func:`_ask_secret`'s docstring for why). Callers pass
    ``answers.get(rule["answer_key"], "")`` when the rule has an ``answer_key``
    — the only recovery path available for a rule with no ``var`` (e.g.
    drive's collabora domain), since :func:`core.recover.recover_shared` can
    only recover rules that declare one.
    """
    if writer.rule_is_secret(rule):
        return _password(writer.rule_label(rule))
    return _ask(writer.rule_label(rule), default)


def _shared_default(answers: dict, rule: dict) -> str:
    """The best pre-fill available for a shared-rule prompt with no ``var``
    (see :func:`_prompt_shared`)."""
    key = rule.get("answer_key")
    if not key:
        return ""
    value = answers.get(key)
    return str(value) if value is not None else ""


def _dep_default_choice(
    m: StCliManifest, app: str, env: str, dep_on: str, options: dict[str, str]
) -> str | None:
    """Pre-select the dependency mode already recorded in ``.st-cli.yml`` for
    ``dep_on``, so an Enter-through rebootstrap of a full app naturally keeps
    an already-managed provider on "Reuse" — leaving its vars.yml/vault.yml
    completely untouched — instead of defaulting to a fresh "Yes — bootstrap
    now" deploy that would regenerate its secrets. ``options`` maps the
    displayed label to its internal mode ("reuse"/"deploy"/"skip"/"external");
    returns the label whose mode matches the recorded ``UnitState.mode`` when
    that label is actually offered this run (a since-removed option silently
    degrades to no pre-selection, mirroring ``_ask_select``'s own default
    handling).

    ``UnitState.mode`` only ever stores "managed" or "external" (never
    "reuse"/"deploy"/"skip" — those are ``_handle_dependency``'s internal
    vocabulary), so "managed" maps to "reuse" here: a unit is only ever
    recorded once it exists on disk, at which point "Reuse existing in the
    repo" is the option that keeps it as-is.
    """
    unit = next(
        (u for u in m.units if u.app == app and u.env == env and u.component == dep_on),
        None,
    )
    if unit is None:
        return None
    wanted_mode = "reuse" if unit.mode == "managed" else unit.mode
    for label, mode in options.items():
        if mode == wanted_mode:
            return label
    return None


def _handle_dependency(
    meta,
    dep,
    answers,
    backend: SecretBackend,
    env,
    m: StCliManifest,
    wire_only: bool = False,
    assume_deploy: bool = False,
) -> str:
    """Run the dependency prompt for one dependency; wire shared vars. Returns mode.

    Asks "Bootstrap <dep> now?" with up to four choices: REUSE (if the provider
    tree already exists in this repo), "Yes — bootstrap now", "No — bootstrap later"
    (returns "skip" — registers no unit), and "Already deployed (enter URL +
    keys)" (external). Optional deps surface only as a hint on the
    "Bootstrapping …" line and via the "bootstrap later" choice — there is no
    separate optional confirm anymore. The select's default is the mode already
    recorded for this unit in ``.st-cli.yml`` (:func:`_dep_default_choice`) — the
    trap this guards against: without it, an Enter-through rebootstrap of the
    whole app would default to re-deploying (and regenerating the secrets of)
    every already-managed dependency instead of leaving it alone.

    With ``wire_only=True`` (core-only bootstrap) the "Yes — bootstrap now" option
    is omitted — only REUSE (if the provider tree exists) / "No — bootstrap later"
    / EXTERNAL remain — so the consumer's env refs are wired without deploying
    any provider. The reuse and external paths are unchanged; the deploy path
    is simply unreachable.

    With ``assume_deploy=True`` (direct provider-target bootstrap, e.g.
    ``bootstrap -c livekit``) the select is skipped entirely and
    ``choice = "deploy"`` is assumed — the user explicitly asked to bootstrap
    that provider, so the "Bootstrap <dep> now?" question is redundant. This is
    the one path where the deploy branch's own rebootstrap machinery actually
    matters (there is no "Reuse" fallback to fall back on): hosts, cadvisor,
    and every ``shared`` rule with a ``var`` are pre-filled/recovered via
    :func:`core.recover.recover_hosts`/:func:`_cadvisor_default`/
    :func:`core.recover.recover_shared` instead of the old destructive
    overwrite-confirm ("Overwrite the existing <dep> unit…") that used to gate
    this branch — a rebootstrap supersedes that confirm entirely.
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
        choice = options[
            _ask_select(
                f"Bootstrap {dep.on} now?",
                list(options),
                default=_dep_default_choice(m, meta.app, env, dep.on, options),
            )
        ]

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
                value = _prompt_shared(rule, _shared_default(answers, rule))
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
                value = (
                    str(value)
                    if value is not None
                    else _prompt_shared(rule, _shared_default(answers, rule))
                )
            if rule.get("answer_key") and value is not None:
                answers[rule["answer_key"]] = value
            writer.inject_consumer(rule, value, answers, backend, core.key)
        if meta.app == "meet" and provider.key == "livekit" and not wire_only:
            _reuse_egress(meta, answers, backend, env)
        ui.info(f"{dep.on}: reuse — kept existing unit (still deployed).")
        return "managed"

    # deploy: create + manage this unit as part of the deployment. A rebootstrap
    # (has_existing) supersedes the old overwrite-confirm — every prompt below
    # pre-fills from what is already on disk instead.
    existing_hosts = recover.recover_hosts(meta.app, env, provider.key)
    hosts = _ask_hosts(dep.on, default=existing_hosts)
    egress_hosts = valkey_enabled = None
    if meta.app == "meet" and provider.key == "livekit":
        egress_hosts, valkey_enabled = _ask_egress_hosts(meta, env, hosts)  # Q2
    # Merge, not replace (mirrors write_core's rationale): loading the existing
    # vars.yml means a hand-edited/custom key on this provider survives, and a
    # shared-rule var recovered below (never re-set) is simply left as-is.
    pvars = tree.load_vars(meta.app, env, provider.key)
    existing_shared = recover.recover_shared(meta.app, env, provider.key, dep.shared)
    for rule in dep.shared:
        var = rule.get("var")
        consumer_key = rule.get("consumer_env_key")
        is_secret = writer.rule_is_secret(rule)
        recovered = existing_shared.get(var) if var else None

        if is_secret and recovered is not None:
            # Already decided on a previous run — NEVER regenerate/re-prompt a
            # secret (the guard against rotating a live LiveKit api key/secret
            # on a standalone `-c livekit` rebootstrap; a secret field has no
            # editable default — see _ask_secret's docstring for why that
            # means "skip the prompt entirely" rather than "pre-fill it").
            # `pvars` already holds the provider-side value verbatim (loaded
            # from disk above), so only the CONSUMER side (this run's
            # `answers`, which for a standalone provider-only rerun may not
            # have it yet) needs re-injecting, using the raw value
            # `recover_shared` resolved for us.
            if consumer_key:
                backend.env_secret(
                    answers, consumer_key, component=core.key, value=recovered
                )
            if rule.get("answer_key"):
                answers[rule["answer_key"]] = recovered
            continue

        if is_secret:
            if rule.get("generate"):
                if backend.prompts_values():  # ansible-vault mints it
                    value = writer.gen_value(rule)
                    ui.info(f"{dep.on}: generated {consumer_key or var or 'value'}.")
                else:  # hashi_vault references an existing secret
                    value = None
            else:
                # prompted secret — only prompt the value in ansible-vault mode
                # (hashi_vault mode prompts a lookup term in var_secret/env_secret).
                value = _prompt_shared(rule) if backend.prompts_values() else None
        else:
            # non-secret: unlike a secret, this DOES get re-asked every time —
            # just pre-filled from the recovered value (or the answer_key
            # fallback for a rule with no `var`, e.g. drive's collabora
            # domain) so accepting it is a no-op and editing it still works.
            default = (
                recovered if recovered is not None else _shared_default(answers, rule)
            )
            value = _prompt_shared(rule, default)
        if is_secret and var and consumer_key:
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
                if is_secret:
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
        _ensure_meet_domain(
            answers, recover.recover(meta.app, env, provider.key).get("DOMAIN", "")
        )
        # egress hosts already asked (Q2); redis+egress write happens AFTER the
        # livekit cadvisor confirm below.
    elif meta.app == "meet" and provider.key == "egress":
        _standalone_egress(meta, pvars, answers, backend, env)
    writer.apply_component_vars(pvars, meta, provider, answers)
    writer.expand_var_markers(pvars, backend)
    pvars[writer.cadvisor_var(meta.app)] = _ask_cadvisor(
        dep.on, _cadvisor_default(meta.app, env, provider.key)
    )  # Q7 livekit cadvisor
    if meta.app == "meet" and provider.key == "livekit":
        # Q8 redis (address/username/password, only when NOT co-located) + Q9
        # egress cadvisor; runs before save_vars so the redis vars land in pvars.
        _bundle_egress(meta, pvars, answers, backend, env, egress_hosts, valkey_enabled)
    if not pvars.ca.comment:
        # Only stamp the header when the file has no start comment already — a
        # rebootstrap over an existing header must not stack a duplicate one
        # (mirrors write_core's same guard).
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

    No flag (``component=None``) runs today's full bootstrap: all deps
    (deploy/reuse/external) + the core + an optional worker.

    **Rebootstrap.** Whether the core (or the single targeted component) ALREADY
    has a committed ``vars.yml`` is detected up front (``core_exists`` /
    ``has_existing`` inside ``_handle_dependency``) and drives three things,
    every one of them BEFORE any prompt is shown:

    1. ``writer.ensure_vault_readable`` is called against every unit already
       registered for ``(app, env)`` — an unreadable ``vault.yml`` aborts here,
       not 40 questions into the questionnaire.
    2. The pre-questionnaire intro + readiness gate (``_print_bootstrap_intro``)
       is replaced by a short "every answer is pre-filled" notice.
    3. ``recover.recover(...)`` seeds the core's ``answers`` (and
       ``recover.recover_hosts``/``_cadvisor_default`` seed the hosts/cadvisor
       prompts) so the questionnaire that follows is a REPLAY, not a
       from-scratch rebuild — the old "Re-bootstrap the '<core>' component?"
       overwrite-confirm is gone; a rebootstrap supersedes it outright. Every
       unit upserted below is stamped ``bootstrapped_with=__version__``
       (``core/models.UnitState``) regardless of whether this run was a fresh
       bootstrap or a rebootstrap — it records that THIS questionnaire ran for
       that unit, on this CLI version.
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

    core_or_worker = {core.key} | (
        {worker.key} if worker and worker.implemented else set()
    )

    m = _ensure_manifest()
    # A rebootstrap is detected purely from what's already committed: the
    # core's vars.yml existing means this run replays the questionnaire with
    # every answer pre-filled instead of starting fresh.
    core_exists = paths.vars_path(app, env, core.key).exists()
    is_rebootstrap = core_exists and (component is None or component in core_or_worker)

    # Fail fast: an unreadable vault.yml must abort BEFORE the (potentially
    # 40+ question) questionnaire runs, not partway through it. Checked
    # against every unit already registered for this (app, env) regardless of
    # which ones this particular invocation will touch — ensure_vault_readable
    # is a no-op for a component with no vault.yml (fresh unit, hashi_vault).
    writer.ensure_vault_readable(
        app, env, [u.component for u in manifest.units_for(m, app, env)]
    )

    # Pre-questionnaire guidance for a full/core/workers bootstrap (an
    # architecture-docs pointer + a requirements checklist gated behind a
    # 'press Enter when ready' acknowledgement) — replaced by a short
    # rebootstrap notice when the unit already exists. Provider-only runs
    # (`-c <provider>`) skip both.
    if component is None or component in core_or_worker:
        if is_rebootstrap:
            ui.note(
                f"Rebootstrapping {app}/{env} — every answer is pre-filled from "
                "your current config; press Enter to keep it.",
                title="Rebootstrap",
            )
        else:
            _print_bootstrap_intro(meta)

    ui.note(
        "This questionnaire only scaffolds your config files.\n"
        "If you mistype an answer, don't start over: finish "
        "the questionnaire, then edit the generated files directly under "
        "<app>/<env>/<component>/."
    )
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
        # to the deploy path (rebootstrap pre-fills + shared-value prompts stay).
        deps = [d for d in meta.dependencies if d.on == component]
        wire_only, upsert_providers = False, True
        assume_deploy = True

    answers: dict = {}
    core_hosts: list[str] = []
    worker_hosts: list[str] = []
    core_cadvisor = True

    # Core: always runs the questionnaire when targeted — fresh (nothing to
    # recover) or a rebootstrap (every prompt pre-filled from `recover.recover`).
    if target_core:
        seed = recover.recover(app, env, core.key) if core_exists else {}
        core_hosts_default = (
            recover.recover_hosts(app, env, core.key) if core_exists else []
        )
        core_hosts = _ask_hosts(core.key, default=core_hosts_default)  # hosts first
        # Optional worker IPs: blank ⇒ workers co-locate on the core hosts (the
        # default). Meet has no workers implementation, so it is never prompted.
        if worker and worker.implemented:
            worker_hosts_default = (
                tree.read_hosts(app, env, core.key, group=worker.app_name)
                if core_exists
                else []
            )
            worker_hosts = _ask_hosts(
                f"workers (leave blank to run on the {core.key} hosts)",
                allow_empty=True,
                default=worker_hosts_default,
            )
        # keycloak is not a Django app — it takes its own (raw-env) questionnaire.
        answers = (
            _ask_keycloak(meta, backend, seed)
            if app == "keycloak"
            else _ask_core(meta, backend, seed)
        )
        core_cadvisor = _ask_cadvisor(
            core.key, _cadvisor_default(app, env, core.key)
        )  # last core question

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
            m,
            wire_only=wire_only,
            assume_deploy=assume_deploy,
        )
        if upsert_providers and mode != "skip":
            manifest.upsert_unit(
                m,
                UnitState(
                    app=app,
                    env=env,
                    component=dep.on,
                    mode=mode,
                    bootstrapped_with=__version__,
                ),
            )
            if app == "meet" and dep.on == "livekit" and answers.get("_egress_bundled"):
                manifest.upsert_unit(
                    m,
                    UnitState(
                        app=app,
                        env=env,
                        component="egress",
                        mode=answers["_egress_bundled"],
                        bootstrapped_with=__version__,
                    ),
                )

    if target_core:
        writer.write_core(
            meta, answers, backend, core_hosts, worker_hosts, env, core_cadvisor
        )
        manifest.upsert_unit(
            m,
            UnitState(
                app=app,
                env=env,
                component=core.key,
                mode="managed",
                bootstrapped_with=__version__,
            ),
        )
    # workers own no files — they reuse the core unit's vars/vault and only flip
    # st_<app>_workers_enabled. A [workers] inventory group is written (in the
    # core's hosts file) only when worker IPs were entered; otherwise the worker
    # falls back to the core group. Meet has no workers implementation, so it is
    # neither prompted nor registered.
    if target_worker:
        manifest.upsert_unit(
            m,
            UnitState(
                app=app,
                env=env,
                component=worker.key,
                mode="managed",
                bootstrapped_with=__version__,
            ),
        )

    units = manifest.units_for(m, app, env)
    manifest.save_manifest(m)
    _print_summary(app, env, answers, units, component)
