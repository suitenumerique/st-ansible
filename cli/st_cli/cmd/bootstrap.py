"""Interactive bootstrap for an ``(app, env)`` deployment.

Flow: backend choice, host IPs, identity provider and core answers, a
deploy / skip / external prompt per dependency, then write the tree.
"""

from __future__ import annotations

import enum
import re
from contextlib import nullcontext
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
    upgrades,
    vault,
    writer,
)
from ..core.errors import StCliError
from ..core.models import (
    BACKEND_ANSIBLE_VAULT,
    MODE_EXTERNAL,
    MODE_MANAGED,
    NewComponentOffer,
    StCliManifest,
    UnitState,
    UpgradeNeed,
)
from ..core.prompts import (
    Recovered,
    _ask,
    _ask_hosts,
    _ask_select,
    _confirm,
    _confirm_ready,
    _password,
    in_silent_replay,
    silent_replay,
    suspend_silent,
)
from ..core.secretbackend import (
    SecretBackend,
    setup_backend,
)

__all__ = ["ReplayAction", "bootstrap"]


class ReplayAction(str, enum.Enum):
    """What `bootstrap` does when the targeted unit already exists."""

    ASK = "ask"  # CLI default: 3-way select when the unit exists
    MODIFY = "modify"  # pre-filled interactive replay (current behaviour)
    OVERRIDE = "override"  # rebuild from scratch, regenerate secrets
    REUSE = "reuse"  # keep as-is, write nothing, never stamp
    SILENT = "silent"  # upgrade: auto-accept recovered answers


_OIDC_PROVIDERS = ["keycloak", "proconnect-prod", "proconnect-integ", "custom"]

_S3_ENDPOINT_PLACEHOLDER = "https://s3.fr-par.scw.cloud"
_IDP_PLACEHOLDER = "https://idp.example.org"
_SMTP_PLACEHOLDER = "smtp.example.org"
_DOCS_CORE_SECRETS = ("COLLABORATION_SERVER_SECRET", "Y_PROVIDER_API_KEY")

# Apps that carry upstream DJANGO_EMAIL_* settings; messages is skipped (no such
# settings upstream) so its questionnaire never prompts for SMTP config.
_EMAIL_APPS = {"drive", "meet", "docs", "transfers"}

# Inverse of _ask_keycloak's "jdbc:postgresql://host:port/name" composition, so
# the 3 separate DB prompts can be pre-filled from the single recovered
# KC_DB_URL (kept here, not core/recover.py, which is deliberately app-agnostic).
_KC_DB_URL_RE = re.compile(
    r"^jdbc:postgresql://(?P<host>[^:/]+):(?P<port>\d+)/(?P<name>.+)$"
)

# Inverse of the drive/collabora shared rule's "https://{value}/hosting/discovery"
# consumer_format (see apps/drive.yml). That rule has no `var`, so
# core.recover.recover_shared cannot recover it; this reconstructs the plain
# domain from the core's own already-recovered WOPI_COLLABORA_DISCOVERY_URL.
_COLLABORA_URL_RE = re.compile(r"^https://(?P<domain>.+)/hosting/discovery$")
_JINJA_EXPR_RE = re.compile(r"\{\{(.*?)\}\}")


def _jinja_literal(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def valid_s3_endpoint(text: str) -> bool | str:
    """questionary validator: a literal endpoint must carry its scheme.

    boto3 rejects an ``endpoint_url`` without one. A Jinja value passes as is.
    """
    text = (text or "").strip()
    if "{{" in text or text.startswith(("http://", "https://")):
        return True
    return "AWS_S3_ENDPOINT_URL must start with http:// or https://."


def caddy_s3_parts(endpoint: str) -> tuple[str, str]:
    """Return the ``(CADDY_S3_PROTOCOL, CADDY_S3_HOST)`` pair for ``endpoint``.

    An endpoint embedding a Jinja expression cannot be split before Ansible
    resolves it, so the pair becomes two Jinja ``urlsplit`` expressions instead.
    """
    if "{{" not in endpoint:
        parts = urlsplit(endpoint)
        return parts.scheme, parts.netloc
    pieces: list[str] = []
    pos = 0
    for m in _JINJA_EXPR_RE.finditer(endpoint):
        if m.start() > pos:
            pieces.append(_jinja_literal(endpoint[pos : m.start()]))
        pieces.append(f"({m.group(1).strip()})")
        pos = m.end()
    if pos < len(endpoint):
        pieces.append(_jinja_literal(endpoint[pos:]))
    expr = pieces[0] if len(pieces) == 1 else f"({' ~ '.join(pieces)})"
    return (
        f"{{{{ {expr} | urlsplit('scheme') }}}}",
        f"{{{{ {expr} | urlsplit('netloc') }}}}",
    )


def _recall(answers: dict, key: str, fallback: str = "") -> str:
    """The pre-fill for a text prompt: ``_ask(label, _recall(answers, key))``.

    Returns a `Recovered` marker when `key` is in `answers` (a silent replay
    auto-accepts it); otherwise the plain `fallback`, which still gets asked.
    """
    if key in answers:
        return Recovered(str(answers[key]))
    return fallback


def _recall_bool(answers: dict, key: str, fallback: bool) -> bool:
    """Tolerant boolean pre-fill for a `_confirm` gate. An absent key returns
    `fallback`."""
    value = answers.get(key)
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    return bool(recover.parse_bool(str(value)))


def _ask_secret(
    answers: dict,
    backend: SecretBackend,
    key: str,
    component: str,
    gen=None,
) -> None:
    """Prompt for (or generate) a secret and route it through the backend.

    A no-op when ``answers`` already holds a value for ``key``: never
    re-prompt or rotate an already-decided secret.
    """
    if key in answers:
        return
    if gen is not None:
        value = gen() if backend.prompts_values() else None
    else:
        value = _password(key) if backend.prompts_values() else None
    backend.env_secret(answers, key, component=component, value=value)


def _ask_optional(answers: dict, key: str, label: str) -> None:
    """Ask an optional text field.

    A blank answer over a recovered value pops the key and warns to remove
    the stale committed line by hand (``envblob.merge`` never deletes one).
    """
    value = _ask(label, _recall(answers, key), required=False)
    if value:
        answers[key] = value
    elif answers.pop(key, None):
        ui.warn(
            f"{key} cleared — the merge never deletes committed lines: "
            f"remove the {key}= line from vars.yml by hand."
        )


def _warn_cleared_password(backend: SecretBackend, key: str) -> None:
    """Warn that clearing a username also cleared its paired password.

    ``envblob.merge`` never deletes a line, so the operator must remove the
    stale ``KEY=`` line (and, under ansible-vault, its vault entry) by hand.
    """
    vault_hint = (
        f" and the vault_{key.lower()} entry from vault.yml"
        if backend.prompts_values()
        else ""
    )
    ui.warn(
        f"{key} cleared with the username: remove the {key}= "
        f"line from vars.yml{vault_hint} by hand."
    )


def _seed_drive_legacy_s3(seed: dict, data) -> None:
    """Replace a pre-0.4.0 drive seed's ``{{ st_drive_s3_* }}`` refs with the
    literal values read from ``data``, the committed core ``vars.yml``.
    """
    endpoint = str(seed.get("AWS_S3_ENDPOINT_URL", ""))
    if "{{ st_drive_s3_" in endpoint:
        host = data.get("st_drive_s3_host")
        if host:
            protocol = data.get("st_drive_s3_protocol") or "https"
            seed["AWS_S3_ENDPOINT_URL"] = f"{protocol}://{host}"
        else:
            seed.pop("AWS_S3_ENDPOINT_URL", None)
            ui.warn(
                "drive: st_drive_s3_host is absent. Enter AWS_S3_ENDPOINT_URL again."
            )

    bucket = str(seed.get("AWS_STORAGE_BUCKET_NAME", ""))
    if "{{ st_drive_s3_" in bucket:
        legacy_bucket = data.get("st_drive_s3_bucket")
        if legacy_bucket:
            seed["AWS_STORAGE_BUCKET_NAME"] = str(legacy_bucket)
        else:
            seed.pop("AWS_STORAGE_BUCKET_NAME", None)
            ui.warn(
                "drive: st_drive_s3_bucket is absent. Enter AWS_STORAGE_BUCKET_NAME again."
            )


def _cadvisor_default(app: str, env: str, component: str) -> bool:
    """Return the recovered cadvisor flag, or the first-run default `True` when
    absent."""
    recovered = recover.recover_cadvisor(app, env, component)
    return True if recovered is None else recovered


def _ensure_manifest() -> StCliManifest:
    """Load ``.st-cli.yml`` or create a fresh one pinned to this CLI version."""
    if paths.manifest_path().exists():
        return manifest.load_manifest()

    return StCliManifest(
        collection_version=__version__, cli_version=__version__, units=[]
    )


def _ask_oidc(answers: dict, backend: SecretBackend, component: str) -> None:
    """Choose an identity provider; fill OIDC answers (client secret routed to backend).

    The provider itself is never stored: ``core.recover.recover_oidc`` infers
    it back from the recovered ``OIDC_OP_*`` endpoints on a rebootstrap.
    """
    recovered_provider, recovered_base, recovered_realm = recover.recover_oidc(answers)
    provider = _ask_select(
        "Identity provider:", _OIDC_PROVIDERS, default=recovered_provider
    )
    base_url = realm = None
    answers["OIDC_PROVIDER"] = provider
    # The recovered base-url/realm only apply when the operator kept the SAME
    # provider as before. If they picked a different one this run, prefilling
    # them would silently mix state from an unrelated provider.
    same_provider = provider == recovered_provider
    # Recovered() wraps only the recovered value itself. "master" below is a
    # first-run fallback, not a recovered realm, so it must still be asked.
    base_default = Recovered(recovered_base) if same_provider and recovered_base else ""
    realm_default = (
        Recovered(recovered_realm) if same_provider and recovered_realm else "master"
    )
    if provider == "keycloak":
        base_url = _ask(
            "Keycloak base URL",
            base_default,
            placeholder=_IDP_PLACEHOLDER,
        )
        realm = _ask("Keycloak realm", realm_default)
    elif provider == "custom":
        base_url = _ask(
            "Custom OIDC issuer base URL (optional)",
            base_default,
            required=False,
        )
    # Never blank a committed OIDC_OP_* endpoint: an empty value here only means
    # this provider/base-url combination has nothing to say about that key
    # (e.g. "custom" with no base URL), not that the operator cleared it.
    endpoints = {
        k: v
        for k, v in envrender.oidc_endpoints(provider, base_url, realm).items()
        if v
    }
    unchanged = (
        same_provider
        and (base_url or "") == (recovered_base or "")
        and (realm or "") == (recovered_realm or "")
    )
    if unchanged:
        # Enter-through: a hand-edited committed endpoint wins over the derived one.
        for k, v in endpoints.items():
            answers.setdefault(k, v)
    else:
        answers.update(endpoints)
    if provider == "custom" and recovered_provider and not same_provider:
        ui.warn(
            f"You switched the identity provider from {recovered_provider} to "
            "custom. The committed OIDC_OP_* lines stay in the env blob. Edit "
            "them by hand to match the new provider."
        )
    answers["OIDC_RP_CLIENT_ID"] = _ask(
        "OIDC_RP_CLIENT_ID", _recall(answers, "OIDC_RP_CLIENT_ID")
    )
    _ask_secret(answers, backend, "OIDC_RP_CLIENT_SECRET", component)


def _ask_email(answers: dict, backend: SecretBackend, component: str, app: str) -> None:
    """Prompt Django transactional email (SMTP) settings for drive / meet / docs.

    Skipped for ``messages`` (no ``DJANGO_EMAIL_*`` upstream). The confirm
    gate defaults to whether SMTP is already configured, so an Enter-through
    rebootstrap never silently drops a working configuration.
    """
    if app not in _EMAIL_APPS:
        return
    smtp_configured = bool(_recall(answers, "DJANGO_EMAIL_HOST"))
    prompt = (
        "SMTP is configured — review its settings?"
        if smtp_configured
        else "Configure transactional email (SMTP) settings?"
    )
    if not _confirm(prompt, default=smtp_configured):
        return
    answers["DJANGO_EMAIL_HOST"] = _ask(
        "DJANGO_EMAIL_HOST",
        _recall(answers, "DJANGO_EMAIL_HOST"),
        placeholder=_SMTP_PLACEHOLDER,
    )
    answers["DJANGO_EMAIL_PORT"] = _ask(
        "DJANGO_EMAIL_PORT", _recall(answers, "DJANGO_EMAIL_PORT", "587")
    )
    _ask_optional(
        answers, "DJANGO_EMAIL_HOST_USER", "DJANGO_EMAIL_HOST_USER (optional)"
    )
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
    _ask_optional(
        answers, "DJANGO_EMAIL_BRAND_NAME", "DJANGO_EMAIL_BRAND_NAME (optional)"
    )


def _ask_transfers_scanner(
    answers: dict, backend: SecretBackend, component: str
) -> None:
    """Optional file-scanner (antivirus) integration for transfers.

    When enabled, completed uploads are submitted to an external file-scanner
    service (deployable with ``st-cli bootstrap file-scanner``, or any compatible
    ClamAV REST endpoint) for an async virus scan; the verdict returns via a webhook
    and gates downloads. Declining leaves every key unset, so the app keeps its
    ``CLAMAV_SCAN_ENABLED=false`` default. The EdDSA signing key is a secret routed
    through the backend; the rest is plain config (numeric knobs keep upstream
    defaults, editable at the prompt).
    """
    if not _confirm(
        "Configure the file-scanner (antivirus) integration?", default=False
    ):
        return
    answers["CLAMAV_SCAN_ENABLED"] = "true"
    answers["CLAMAV_SERVICE_URL"] = _ask(
        "CLAMAV_SERVICE_URL (file-scanner REST base URL, no trailing slash)",
        placeholder="http://10.0.0.20:50800",
    )
    # Base URL of THIS backend as the scanner reaches it (webhook callback).
    # The backend port is not published on the host — the scanner reaches it
    # through the public frontend Caddy, which proxies /api to the backend.
    answers["SCAN_WEBHOOK_BASE_URL"] = _ask(
        "SCAN_WEBHOOK_BASE_URL (this backend, as reachable FROM the scanner "
        "— usually the public transfers URL)",
        placeholder="https://transfers.example.org",
    )
    # EdDSA (Ed25519) private key minting request-bound scan JWTs — a secret.
    value = _password("SCAN_JWT_PRIVATE_KEY") if backend.prompts_values() else None
    backend.env_secret(
        answers, "SCAN_JWT_PRIVATE_KEY", component=component, value=value
    )
    answers["SCAN_JWT_ISSUER"] = _ask("SCAN_JWT_ISSUER", "transferts")
    answers["SCAN_JWT_AUDIENCE"] = _ask("SCAN_JWT_AUDIENCE", "file-scanner")
    answers["SCAN_JWT_TTL"] = _ask("SCAN_JWT_TTL (seconds)", "300")
    answers["SCAN_MAX_FILE_SIZE"] = _ask("SCAN_MAX_FILE_SIZE (bytes)", "2147483648")
    answers["SCAN_PRESIGNED_URL_EXPIRY"] = _ask(
        "SCAN_PRESIGNED_URL_EXPIRY (seconds)", "3600"
    )
    answers["SCAN_PENDING_REAP_MINUTES"] = _ask("SCAN_PENDING_REAP_MINUTES", "15")


def _ask_cadvisor(label: str, default: bool = True) -> bool:
    """Prompt whether to enable the cadvisor monitoring sidecar for a component.

    Pass the recovered value as ``default`` so a rebootstrap offers the
    current choice, not the first-run default ``True``.
    """
    return _confirm(
        f"Enable cadvisor container monitoring for {label}?", default=default
    )


def _ask_db(answers: dict, backend: SecretBackend, component: str, app: str) -> None:
    """Prompt database connection: a DATABASE_URL or discrete DB_* vars.

    The mode select defaults to the shape actually recovered. Switching mode
    does not clean up the old shape: ``envblob.merge`` never deletes a line,
    so this warns the operator to remove the stale lines by hand.
    """
    had_discrete = "DB_HOST" in answers
    had_url = "DATABASE_URL" in answers
    default_mode = "discrete (DB_*)" if had_discrete else "DATABASE_URL"
    mode = _ask_select(
        "Database configuration:",
        ["DATABASE_URL", "discrete (DB_*)"],
        default=default_mode,
        # A total recovery gap (neither shape recovered) is a genuine new
        # question, not a mode switch. Silent mode must not auto-pick
        # "DATABASE_URL" for it (see the docstring above for why that default
        # exists at all).
        auto=had_url or had_discrete,
    )
    if mode.startswith("DATABASE_URL"):
        if had_discrete:
            ui.warn(
                "You switched from discrete DB_* vars to DATABASE_URL. "
                "The committed DB_HOST, DB_PORT, DB_NAME, DB_USER, and "
                "DB_PASSWORD lines stay in the env blob. Remove them by hand."
            )
        # DATABASE_URL is itself the secret (it may embed a password), so never
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
    if had_url:
        ui.warn(
            "You switched from DATABASE_URL to discrete DB_* vars. "
            "The committed DATABASE_URL line, and its vault entry, stay in "
            "place. Remove them by hand."
        )
    answers["DB_HOST"] = _ask("DB_HOST", _recall(answers, "DB_HOST"))
    answers["DB_NAME"] = _ask("DB_NAME", _recall(answers, "DB_NAME", app))
    answers["DB_USER"] = _ask("DB_USER", _recall(answers, "DB_USER", app))
    _ask_secret(answers, backend, "DB_PASSWORD", component)
    answers["DB_PORT"] = _ask("DB_PORT", _recall(answers, "DB_PORT", "5432"))


def _ask_keycloak(meta, backend: SecretBackend, answers: dict | None = None) -> dict:
    """Collect the keycloak core answers into the ``st_keycloak_env`` blob.

    Keycloak is not a Django app: no DOMAIN/Redis/S3/OIDC/email questionnaire.
    ``KC_DB_URL`` is recovered as one composed string and decomposed back into
    the 3 DB prompts by `_KC_DB_URL_RE`.
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
        Recovered(db_url_match.group("host")) if db_url_match else "",
        placeholder="db.example.org",
    )
    db_port = _ask(
        "Database port",
        Recovered(db_url_match.group("port")) if db_url_match else "5432",
    )
    db_name = _ask(
        "Database name",
        Recovered(db_url_match.group("name")) if db_url_match else "keycloak",
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


# projects' OIDC-enforced SSO defaults (mirrors the upstream docker-compose).
_PROJECTS_OIDC_DEFAULTS = {
    "ALLOW_ALL_TO_CREATE_PROJECTS": "true",
    "OIDC_ENFORCED": "true",
    "OIDC_SCOPES": "openid email profile",
    "OIDC_USE_DEFAULT_RESPONSE_MODE": "true",
    "OIDC_FULLNAME_ATTRIBUTES": "name",
    "OIDC_IGNORE_USERNAME": "true",
    "OIDC_IGNORE_ROLES": "true",
}


def _ask_projects_oidc(answers: dict, backend: SecretBackend, component: str) -> None:
    """Choose an identity provider and derive projects' single ``OIDC_ISSUER``.

    Unlike the Django apps' explicit ``OIDC_RP_*``/``OIDC_OP_*`` set, projects
    takes one issuer URL and discovers the endpoints itself; the bundled
    ProConnect issuers need no URL prompt.
    """
    recovered_issuer = str(answers.get("OIDC_ISSUER") or "")
    recovered_provider, recovered_base, recovered_realm = _projects_oidc_from_issuer(
        recovered_issuer
    )
    provider = _ask_select(
        "Identity provider:", _OIDC_PROVIDERS, default=recovered_provider
    )
    answers["OIDC_PROVIDER"] = provider
    same_provider = provider == recovered_provider
    base_default = Recovered(recovered_base) if same_provider and recovered_base else ""
    realm_default = (
        Recovered(recovered_realm) if same_provider and recovered_realm else "master"
    )
    base_url = realm = None
    if provider == "keycloak":
        base_url = _ask("Keycloak base URL", base_default, placeholder=_IDP_PLACEHOLDER)
        realm = _ask("Keycloak realm", realm_default)
    elif provider == "custom":
        # Required here (unlike the Django flow, which can fall back to explicit
        # per-endpoint answers): with no issuer, projects cannot discover anything.
        base_url = _ask(
            "OIDC issuer URL (discovery base)",
            base_default,
            placeholder=f"{_IDP_PLACEHOLDER}/realms/main",
        )
    answers["OIDC_ISSUER"] = envrender.oidc_issuer(provider, base_url, realm)
    if provider.startswith("proconnect"):
        # ProConnect specifics (override the generic keycloak-ish defaults set in
        # _ask_projects): it returns the userinfo as a signed JWT (RS256, not JSON),
        # and exposes given_name/usual_name/email/siret via per-claim scopes. There
        # is no `profile` scope nor a `name` claim. Without these, login loops back
        # to the landing page (userinfo parse error, then the fullname check fails).
        answers["OIDC_SCOPES"] = "openid given_name usual_name email siret"
        answers["OIDC_USERINFO_SIGNED_RESPONSE_ALG"] = "RS256"
        answers["OIDC_FULLNAME_ATTRIBUTES"] = "given_name,usual_name"
    elif recovered_provider.startswith("proconnect"):
        # Switched away from ProConnect: the recovered per-claim overrides
        # would break a keycloak/custom login, so restore the generic defaults.
        answers["OIDC_SCOPES"] = _PROJECTS_OIDC_DEFAULTS["OIDC_SCOPES"]
        answers["OIDC_FULLNAME_ATTRIBUTES"] = _PROJECTS_OIDC_DEFAULTS[
            "OIDC_FULLNAME_ATTRIBUTES"
        ]
        if answers.pop("OIDC_USERINFO_SIGNED_RESPONSE_ALG", None):
            ui.warn(
                "OIDC_USERINFO_SIGNED_RESPONSE_ALG cleared — the merge never "
                "deletes committed lines: remove the "
                "OIDC_USERINFO_SIGNED_RESPONSE_ALG= line from vars.yml by hand."
            )
    answers["OIDC_CLIENT_ID"] = _ask(
        "OIDC_CLIENT_ID", _recall(answers, "OIDC_CLIENT_ID")
    )
    _ask_secret(answers, backend, "OIDC_CLIENT_SECRET", component)


def _projects_oidc_from_issuer(issuer: str) -> tuple[str, str, str]:
    """Invert `envrender.oidc_issuer` for the projects questionnaire.

    Returns ``(provider, base_url, realm)``; an empty issuer (first run) gives
    ``("", "", "")`` so the select falls back to its first entry.
    """
    if not issuer:
        return "", "", ""
    for name in ("proconnect-prod", "proconnect-integ"):
        if issuer == envrender.oidc_issuer(name, None, None):
            return name, "", ""
    m = re.match(r"^(?P<base>.+)/realms/(?P<realm>[^/]+)/?$", issuer)
    if m:
        return "keycloak", m.group("base"), m.group("realm")
    return "custom", issuer, ""


def _ask_projects_storage(
    answers: dict,
    backend: SecretBackend,
    component: str,
    *,
    scaling: bool = False,
    default: bool = True,
) -> None:
    """Prompt S3 object storage for projects uploads (the recommended target).

    ``scaling=True`` skips the opt-out confirm: uploads on local disk are not
    shared between instances, so S3 is mandatory once more than one runs.
    """
    if scaling:
        ui.info(
            "Horizontal scaling requires S3 object storage (local uploads are "
            "not shared between instances)."
        )
    elif not _confirm(
        "Configure S3 object storage for uploads (recommended)?", default=default
    ):
        ui.warn(
            "Local storage keeps uploads on this host's disk: scaling to "
            "several instances later will first require moving them to S3."
        )
        return
    answers["S3_ENDPOINT"] = _ask(
        "S3_ENDPOINT",
        _recall(answers, "S3_ENDPOINT"),
        placeholder=_S3_ENDPOINT_PLACEHOLDER,
    )
    _ask_optional(answers, "S3_REGION", "S3_REGION (optional)")
    answers["S3_ACCESS_KEY_ID"] = _ask(
        "S3_ACCESS_KEY_ID", _recall(answers, "S3_ACCESS_KEY_ID")
    )
    _ask_secret(answers, backend, "S3_SECRET_ACCESS_KEY", component)
    answers["S3_BUCKET"] = _ask("S3_BUCKET", _recall(answers, "S3_BUCKET", "projects"))
    answers["S3_FORCE_PATH_STYLE"] = (
        "true"
        if _confirm(
            "S3_FORCE_PATH_STYLE?",
            default=_recall_bool(answers, "S3_FORCE_PATH_STYLE", True),
        )
        else "false"
    )


def _ask_projects_scaling(
    answers: dict, backend: SecretBackend, component: str
) -> bool:
    """Prompt the optional Redis wiring for horizontal scaling.

    Returns whether scaling was accepted, so the caller can make the S3
    prompt mandatory (local uploads are not shared across instances).
    """
    if not _confirm(
        "Configure Redis for horizontal scaling (multiple instances)?",
        default=bool(answers.get("REDIS_URL")),
    ):
        return False
    if "REDIS_URL" not in answers:
        value = (
            _ask(
                "REDIS_URL",
                placeholder="redis://user:password@redis.example.org:6379/0",
            )
            if backend.prompts_values()
            else None
        )
        backend.env_secret(answers, "REDIS_URL", component=component, value=value)
    return True


def _ask_projects_email(answers: dict, backend: SecretBackend, component: str) -> None:
    """Prompt optional transactional email (SMTP) settings for projects.

    projects uses nodemailer's ``SMTP_*`` keys (not Django's ``DJANGO_EMAIL_*``).
    The password is a secret routed through the backend; optional fields land in
    ``answers`` only when filled in so the template guards stay clean. Same
    gate as `_ask_email`: a recovered ``SMTP_HOST`` means the
    confirm defaults to "review", never to silently dropping the block.
    """
    smtp_configured = bool(_recall(answers, "SMTP_HOST"))
    prompt = (
        "SMTP is configured — review its settings?"
        if smtp_configured
        else "Configure transactional email (SMTP) settings?"
    )
    if not _confirm(prompt, default=smtp_configured):
        return
    answers["SMTP_HOST"] = _ask(
        "SMTP_HOST", _recall(answers, "SMTP_HOST"), placeholder=_SMTP_PLACEHOLDER
    )
    answers["SMTP_PORT"] = _ask("SMTP_PORT", _recall(answers, "SMTP_PORT", "587"))
    answers["SMTP_SECURE"] = (
        "true"
        if _confirm(
            "SMTP_SECURE (implicit TLS)?",
            default=_recall_bool(answers, "SMTP_SECURE", False),
        )
        else "false"
    )
    had_user = "SMTP_USER" in answers
    _ask_optional(answers, "SMTP_USER", "SMTP_USER (optional)")
    if "SMTP_USER" in answers:
        _ask_secret(answers, backend, "SMTP_PASSWORD", component)
    elif had_user and answers.pop("SMTP_PASSWORD", None):
        _warn_cleared_password(backend, "SMTP_PASSWORD")
    answers["SMTP_FROM"] = _ask(
        "SMTP_FROM",
        _recall(answers, "SMTP_FROM"),
        placeholder='"Projects" <noreply@example.org>',
    )


def _ask_projects(meta, backend: SecretBackend, answers: dict | None = None) -> dict:
    """Collect the projects core answers into the ``st_projects_env`` blob.

    projects (a Planka fork) is a Sails.js app, not Django: a single free-form
    ``st_projects_env`` blob, OIDC-enforced login (no local accounts).
    """
    core_key = meta.core().key
    seeded = bool(answers)
    answers = dict(answers) if answers else {}
    # DOMAIN is not an env key of its own for projects: BASE_URL carries it.
    recovered_domain = str(answers.get("BASE_URL") or "").removeprefix("https://")
    domain = _ask(
        "Public domain for projects",
        Recovered(recovered_domain) if recovered_domain else "",
        placeholder="projects.example.org",
    )
    # DOMAIN feeds _print_summary; BASE_URL is the actual env key.
    answers["DOMAIN"] = domain
    answers["BASE_URL"] = f"https://{domain}"
    # A recovered value (or a hand-edit) wins over the first-run default.
    for key, value in _PROJECTS_OIDC_DEFAULTS.items():
        answers.setdefault(key, value)

    # SECRET_KEY: generated like the Django apps' DJANGO_SECRET_KEY.
    _ask_secret(answers, backend, "SECRET_KEY", core_key, gen=secrets.gen_secret)

    # DATABASE_URL embeds the DB password, so it is routed through the secret backend.
    if "DATABASE_URL" not in answers:
        value = (
            _ask(
                "DATABASE_URL",
                placeholder="postgresql://user:password@db.example.org:5432/projects",
            )
            if backend.prompts_values()
            else None
        )
        backend.env_secret(answers, "DATABASE_URL", component=core_key, value=value)

    # OpenID Connect is required: login has no local fallback when OIDC_ENFORCED.
    _ask_projects_oidc(answers, backend, core_key)
    _ask_optional(
        answers,
        "ORGANIZATION_ID_CLAIM",
        "ORGANIZATION_ID_CLAIM (optional OIDC claim for org mode; blank for free mode)",
    )

    # Scaling before storage: accepting it makes the S3 questionnaire mandatory
    # (uploads must be shared between instances), skipping its opt-out confirm.
    scaling = _ask_projects_scaling(answers, backend, core_key)
    _ask_projects_storage(
        answers,
        backend,
        core_key,
        scaling=scaling,
        default="S3_ENDPOINT" in answers if seeded else True,
    )
    _ask_projects_email(answers, backend, core_key)
    return answers


def _ask_file_scanner(
    meta, backend: SecretBackend, answers: dict | None = None
) -> dict:
    """Collect the file-scanner core answers → the ``st_file_scanner_env`` blob.

    file-scanner is not a Django app: a FastAPI API + dramatiq worker pair whose
    compose stack bundles its own clamav daemon and Redis broker, so there is no
    DOMAIN/DB/S3/OIDC questionnaire — callers (e.g. the transfers backend) reach
    it at ``http://<host>:<st_file_scanner_port>``. Callers are trusted via their
    *public* Ed25519 keys (``JWT_ISSUER_KEYS``, plain config); the secrets are
    the webhook signing seed (generated — 32 random bytes base64url IS a valid
    Ed25519 seed) and the optional ``/metrics`` bearer token (generated too, on
    by default: the API port is published on the host, and the ``api_client``
    metric label leaks caller identities to anyone who can scrape it).
    """
    core_key = meta.core().key
    answers = dict(answers) if answers else {}
    answers["JWT_ISSUER_KEYS"] = _ask(
        "JWT_ISSUER_KEYS (comma-separated iss:base64url-ed25519-pubkey pairs)",
        placeholder="transferts:8sicDCDZLZY5SPNNjr4aBwwh0Dyrqr7Ca9neK_nA6Eg",
    )
    backend.env_secret(
        answers,
        "JWT_SIGNING_KEY",
        component=core_key,
        value=secrets.gen_token() if backend.prompts_values() else None,
    )
    answers["JWT_SIGNING_KID"] = _ask("JWT_SIGNING_KID (webhook key label)", "v1")
    if _confirm(
        "Protect /metrics with a bearer token (PROMETHEUS_API_KEY)?", default=True
    ):
        backend.env_secret(
            answers,
            "PROMETHEUS_API_KEY",
            component=core_key,
            value=secrets.gen_token() if backend.prompts_values() else None,
        )
    allowed = _ask(
        "ALLOWED_URL_HOSTS (optional allowlist of scannable URL hosts, e.g. your "
        "S3 host; blank = any host may be submitted)",
        required=False,
    )
    # SSRF bypass: only needed when a scannable host resolves to a private IP
    # (e.g. an S3 endpoint reached over an internal network) — the worker's
    # SSRF guard would otherwise refuse to download from it. With an allowlist
    # set, a yes/no reusing that same list beats re-typing it; without one,
    # fall back to a free-text prompt (a bypass still needs explicit hostnames).
    if allowed:
        answers["ALLOWED_URL_HOSTS"] = allowed
        if _confirm(
            "Do these hosts resolve to private IPs from the scanner hosts "
            "(e.g. an internal S3 endpoint)? Sets SSRF_ALLOWED_HOSTS to the same list.",
            default=False,
        ):
            answers["SSRF_ALLOWED_HOSTS"] = allowed
    else:
        ssrf = _ask(
            "SSRF_ALLOWED_HOSTS (optional: hosts allowed to resolve to private IPs, "
            "e.g. an internal S3 endpoint)",
            required=False,
        )
        if ssrf:
            answers["SSRF_ALLOWED_HOSTS"] = ssrf
    return answers


def _ask_core(meta, backend: SecretBackend, answers: dict | None = None) -> dict:
    """Collect the core component answers (domain, db, redis, s3, secrets, OIDC).

    ``answers`` is copied, never mutated in place: the caller's dict stays
    disposable.
    """
    app = meta.app
    core_key = meta.core().key
    answers = dict(answers) if answers else {}
    # DOMAIN recovers directly for meet/drive via the component-var inversion;
    # other apps fall back to DJANGO_ALLOWED_HOSTS. A comma there means an
    # operator hand-edited it into a multi-host list. Pre-filling DOMAIN with
    # that list would poison every derived value, so drop the fallback.
    allowed_hosts = _recall(answers, "DJANGO_ALLOWED_HOSTS")
    domain_fallback = "" if "," in allowed_hosts else allowed_hosts
    recovered_domain = _recall(answers, "DOMAIN") or domain_fallback
    domain = _ask(
        f"Public domain for {app}",
        recovered_domain,
        placeholder=f"{app}.example.org",
    )

    answers.update(
        {
            "DOMAIN": domain,
            "DJANGO_SETTINGS_MODULE": f"{app}.settings",
            "DJANGO_CONFIGURATION": "Production",
        }
    )
    if app == "transfers":
        # The app's Python package is `transferts` (French spelling), so the
        # generic f"{app}.settings" set above would import the nonexistent
        # transfers.settings. Its public URLs stay domain-derived (else below).
        answers["DJANGO_SETTINGS_MODULE"] = "transferts.settings"

    if app == "meet":
        # Single source of truth: every public-domain var references
        # st_meet_public_host so the operator changes the domain in one place.
        # The value is the literal "{{ st_meet_public_host }}" string,
        # resolved by Ansible at deploy; resolving it here would emit empty.
        host = "{{ st_meet_public_host }}"
        derived = {
            "DJANGO_ALLOWED_HOSTS": host,
            "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{host}",
            "DJANGO_CORS_ALLOWED_ORIGINS": f"https://{host}",
            "LOGIN_REDIRECT_URL": f"https://{host}/",
            "LOGIN_REDIRECT_URL_FAILURE": f"https://{host}/",
            "LOGOUT_REDIRECT_URL": f"https://{host}/",
        }
    elif app == "docs":
        # the upstream docs Django package is named "impress", so the generic
        # f"{app}.settings" default would import the nonexistent docs.settings.
        answers["DJANGO_SETTINGS_MODULE"] = "impress.settings"
        # same single-source-of-truth indirection as meet, via st_docs_public_host.
        host = "{{ st_docs_public_host }}"
        derived = {
            "DJANGO_ALLOWED_HOSTS": host,
            "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{host}",
            "DJANGO_CORS_ALLOWED_ORIGINS": f"https://{host}",
            "LOGIN_REDIRECT_URL": f"https://{host}/",
            "LOGIN_REDIRECT_URL_FAILURE": f"https://{host}/",
            "LOGOUT_REDIRECT_URL": f"https://{host}/",
        }
    elif app == "drive":
        # Public-facing URLs point at st_drive_public_host (resolved at
        # deploy), like meet above. DJANGO_ALLOWED_HOSTS/CSRF/CORS keep the
        # literal domain.
        host = "{{ st_drive_public_host }}"
        derived = {
            "DJANGO_ALLOWED_HOSTS": domain,
            "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{domain}",
            "DJANGO_CORS_ALLOWED_ORIGINS": f"https://{domain}",
            "LOGIN_REDIRECT_URL": f"https://{host}/",
            "LOGIN_REDIRECT_URL_FAILURE": f"https://{host}/",
            "LOGOUT_REDIRECT_URL": f"https://{host}/",
            "MEDIA_BASE_URL": f"https://{host}",
        }
    else:
        derived = {
            "DJANGO_ALLOWED_HOSTS": domain,
            "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{domain}",
            "DJANGO_CORS_ALLOWED_ORIGINS": f"https://{domain}",
            "LOGIN_REDIRECT_URL": f"https://{domain}/",
            "LOGIN_REDIRECT_URL_FAILURE": f"https://{domain}/",
            "LOGOUT_REDIRECT_URL": f"https://{domain}/",
        }
    # A changed DOMAIN rebuilds every derived key; otherwise a recovered
    # hand-edit wins over the recomputed default.
    if recovered_domain and domain != recovered_domain:
        answers.update(derived)
    else:
        for key, value in derived.items():
            answers.setdefault(key, value)
    _ask_secret(
        answers,
        backend,
        "DJANGO_SECRET_KEY",
        core_key,
        gen=secrets.gen_secret,
    )

    _ask_db(answers, backend, core_key, app)

    # REDIS_URL can embed a password, so it routes through the secret backend
    # like DATABASE_URL. CELERY_BROKER_URL references the same secret rather
    # than prompting again.
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

    # messages does NOT use the django-lasuite default S3 storage (AWS_S3_*). It
    # uses STORAGE_MESSAGE_* instead (see _ask_messages_storage), so skip the S3
    # questionnaire entirely for it.
    if app != "messages":
        # Every app keeps the literal endpoint and bucket in the backend blob.
        endpoint = _ask(
            "AWS_S3_ENDPOINT_URL",
            _recall(answers, "AWS_S3_ENDPOINT_URL"),
            placeholder=_S3_ENDPOINT_PLACEHOLDER,
            validate=valid_s3_endpoint,
        )
        answers["AWS_S3_ACCESS_KEY_ID"] = _ask(
            "AWS_S3_ACCESS_KEY_ID", _recall(answers, "AWS_S3_ACCESS_KEY_ID")
        )
        _ask_secret(answers, backend, "AWS_S3_SECRET_ACCESS_KEY", core_key)
        bucket = _ask(
            "AWS_STORAGE_BUCKET_NAME", _recall(answers, "AWS_STORAGE_BUCKET_NAME")
        )
        _ask_optional(answers, "AWS_S3_REGION_NAME", "AWS_S3_REGION_NAME (optional)")
        answers["AWS_S3_ENDPOINT_URL"] = endpoint
        answers["AWS_STORAGE_BUCKET_NAME"] = bucket

        if app in ("meet", "docs", "drive"):
            # The caddy container of the app reads CADDY_S3_* from the caddy_env
            # file: see the caddy env_render layer in apps/<app>.yml.
            protocol, host = caddy_s3_parts(endpoint)
            answers["CADDY_S3_PROTOCOL"] = protocol
            answers["CADDY_S3_HOST"] = host
            answers["CADDY_S3_BUCKET"] = bucket

        if app == "drive":
            answers["WOPI_CLIENTS"] = "collabora"
            answers["WOPI_SRC_BASE_URL"] = "https://{{ st_drive_public_host }}"
            # The collabora shared rule has no `var`, so recover_shared cannot
            # recover COLLABORA_DOMAIN. Reconstruct it from the already
            # recovered WOPI_COLLABORA_DISCOVERY_URL instead.
            m = _COLLABORA_URL_RE.match(
                _recall(answers, "WOPI_COLLABORA_DISCOVERY_URL")
            )
            if m:
                answers.setdefault("COLLABORA_DOMAIN", m.group("domain"))
        elif app == "docs":
            answers["MEDIA_BASE_URL"] = "https://{{ st_docs_public_host }}"
        elif app == "transfers":
            # Uploads and downloads use presigned URLs straight to S3, so the
            # frontend Caddy's CSP must allow the S3 origin — derived from the
            # endpoint above (single source of truth), Jinja-safe like the
            # CADDY_S3_* pair.
            protocol, host = caddy_s3_parts(endpoint)
            answers["AWS_S3_SIGNATURE_VERSION"] = "s3v4"
            answers["TRANSFERTS_FRONTEND_S3_ORIGIN"] = f"{protocol}://{host}"
            # transfers sits behind the frontend Caddy, which sets
            # X-Forwarded-For; enable request-IP logging from that proxy header.
            answers["USE_X_FORWARDED_FOR"] = "true"

    if app == "docs":
        # derived answers, never prompted; single source of truth via
        # st_docs_public_host (the {{ }} resolves at deploy from the core vars.yml).
        answers["OIDC_REDIRECT_ALLOWED_HOSTS"] = '["https://{{ st_docs_public_host }}"]'
        answers["COLLABORATION_WS_URL"] = (
            "wss://{{ st_docs_public_host }}/collaboration/ws/"
        )
        answers["COLLABORATION_API_URL"] = (
            "https://{{ st_docs_public_host }}/collaboration/api/"
        )
        # the docspec conversion service ships in the core compose by default, so
        # enable the upload/import feature and point the backend at it over the
        # compose network (backend-only, like Y_PROVIDER_API_BASE_URL).
        answers["CONVERSION_UPLOAD_ENABLED"] = "true"
        answers["DOCSPEC_API_URL"] = "http://docspec:4000/conversion"
        # COLLABORATION_SERVER_SECRET / Y_PROVIDER_API_KEY are owned by the docs
        # core and mirrored into yprovider's vault at deploy, the same pattern
        # as messages' MDA_API_SECRET (see _ask_docs_yprovider).
        _ask_secret(
            answers,
            backend,
            "COLLABORATION_SERVER_SECRET",
            core_key,
            gen=secrets.gen_secret,
        )
        _ask_secret(
            answers, backend, "Y_PROVIDER_API_KEY", core_key, gen=secrets.gen_token
        )

    if app == "messages":
        # MDA_API_SECRET is a messages-core secret (mta-in is only a consumer).
        # Generate it here so it exists whenever messages is bootstrapped,
        # independent of whether mta-in is deployed, skipped, or external.
        _ask_secret(
            answers, backend, "MDA_API_SECRET", core_key, gen=secrets.gen_secret
        )
        # SALT_KEY: django-fernet-encrypted-fields key (DKIM keys, channel secrets).
        # Required in practice: an empty value makes encrypted-field writes raise.
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
    if app == "docs" and answers.get("DJANGO_EMAIL_HOST"):
        answers["DJANGO_EMAIL_LOGO_IMG"] = (
            "https://{{ st_docs_public_host }}/assets/logo-suite-numerique.png"
        )
        answers["DJANGO_EMAIL_URL_APP"] = "https://{{ st_docs_public_host }}"
    if app == "transfers":
        _ask_transfers_scanner(answers, backend, core_key)
    if app == "messages":
        _ask_messages_outbound(answers, backend, core_key)
    if app == "meet":
        _set_meet_recording(answers)
    return answers


def _ask_messages_provider(
    provider_key, answers, backend, hosts, core_key, app, env, pvars
):
    """messages mta-in / socks-proxy / mpa: collect provider-local env values,
    route their secrets, and build each provider's computed consumer value
    (MTA_OUT_DIRECT_PROXIES, SPAM_CONFIG), never prompted.
    """
    # setdefault: a core-recovered or this-run answer wins over the provider's own
    # recovery. This covers both a full run and a standalone `-c <provider>` run.
    for k, v in recover.recover(app, env, provider_key).items():
        answers.setdefault(k, v)
    if provider_key == "mta-in":
        # DOMAIN feeds MDA_API_BASE_URL; present in full bootstrap, prompt if
        # standalone.
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
        # MDA_API_SECRET is owned by the messages core (generated in
        # _ask_core). Mirror it into mta-in's own vault: the live core
        # buffer, else the on-disk messages vault, else prompt the operator.
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
            # hashi standalone: no core-set ref in answers, so prompt a lookup term.
            backend.env_secret(
                answers, "MDA_API_SECRET", component=provider_key, value=None
            )
        # hashi full run: answers[MDA_API_SECRET] already holds the lookup ref,
        # so reuse it.
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
            # Never rotate a recovered credential; re-buffer the on-disk
            # provider value so a full run also mirrors it into the core vault.
            if backend.prompts_values():
                pvp = paths.vault_path(app, env, provider_key)
                if pvp.exists():
                    v = vault.decrypt_to_dict(pvp).get("vault_proxy_users")
                    if v is not None:
                        backend.env_secret(
                            answers, "PROXY_USERS", component=core_key, value=str(v)
                        )
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
        # A messages-core value computed from the proxy hosts + port + the
        # PROXY_USERS ref the backend produced. Never prompted.
        answers["MTA_OUT_DIRECT_PROXIES"] = ",".join(
            "socks5s://" + answers["PROXY_USERS"] + "@" + h + ":" + port for h in hosts
        )
    elif provider_key == "mpa":
        # rspamd_url: a single mpa host derives it from the host + caddy port; a
        # load-balanced (multi-host) mpa prompts for the LB URL. Shared by both
        # backends.
        if len(hosts) == 1:
            rspamd_url = "http://" + hosts[0] + ":{{ st_messages_mpa_caddy_port }}"
        else:
            rspamd_url = _ask(
                "rspamd URL for SPAM_CONFIG (mpa load balancer)",
                placeholder="https://mpa.example.org",
            )
        # SPAM_CONFIG is a messages-core env var (mpa is only its provider):
        # always constructed, never prompted.
        if backend.prompts_values():  # ansible-vault: a {{ vault_* }} ref
            # A replay's buffer misses the bearer (mpa's shared rule has no
            # consumer_env_key/answer_key), so fall back to mpa's own vault.
            # Without it an override drops the bearer from the core vault.
            token = backend.component_secrets(provider_key).get("vault_mpa_auth_bearer")
            if token is None:
                mvp = paths.vault_path(app, env, provider_key)
                if mvp.exists():
                    token = vault.decrypt_to_dict(mvp).get("vault_mpa_auth_bearer")
            if token is not None:
                # mirror the bearer into the messages vault under the same vault_mpa_*
                # name so the {{ vault_mpa_auth_bearer }} ref in SPAM_CONFIG resolves
                # there.
                backend.var_secret(
                    CommentedMap(), "vault_mpa_auth_bearer", token, component=core_key
                )
            else:
                ui.warn(
                    "vault_mpa_auth_bearer not found in the mpa vault — "
                    "SPAM_CONFIG keeps a reference the core vault cannot "
                    "resolve. Restore the key in the mpa vault, then replay "
                    f"`st-cli bootstrap {app} {env}`."
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

    messages does not use the generic AWS_S3_* storage; STORAGE_MESSAGE_* is
    its only object storage. The blobs-offload confirm defaults to the
    recovered ``MESSAGES_BLOBS_OFFLOAD_ENABLED`` flag.
    """
    answers["STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL"] = _ask(
        "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL",
        _recall(answers, "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL"),
        placeholder=_S3_ENDPOINT_PLACEHOLDER,
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
    _ask_optional(
        answers,
        "STORAGE_MESSAGE_IMPORTS_REGION_NAME",
        "STORAGE_MESSAGE_IMPORTS_REGION_NAME (optional)",
    )
    answers["STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY"] = _ask(
        "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY",
        _recall(answers, "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
    )

    blobs_enabled = _recall_bool(answers, "MESSAGES_BLOBS_OFFLOAD_ENABLED", False)
    blobs_prompt = (
        "Blobs offloading is enabled — review its settings?"
        if blobs_enabled
        else "Enable blobs offloading to S3 (pg→ S3)?"
    )
    if not _confirm(blobs_prompt, default=blobs_enabled):
        return
    answers["MESSAGES_BLOBS_OFFLOAD_ENABLED"] = "1"
    answers["STORAGE_MESSAGE_BLOBS_ENDPOINT_URL"] = _ask(
        "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL",
        _recall(answers, "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL"),
        placeholder=_S3_ENDPOINT_PLACEHOLDER,
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
    _ask_optional(
        answers,
        "STORAGE_MESSAGE_BLOBS_REGION_NAME",
        "STORAGE_MESSAGE_BLOBS_REGION_NAME (optional)",
    )
    # MESSAGES_BLOBS_ENCRYPT_KEYS is recovered verbatim from the blob (it may
    # carry operator-added rotation slots). Mint the secret and compose the
    # JSON only on first setup.
    if "MESSAGES_BLOBS_ENCRYPT_KEYS" not in answers:
        _ask_secret(
            answers,
            backend,
            "MESSAGES_BLOBS_ENCRYPT_KEY",
            core_key,
            gen=secrets.gen_token,
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
    RELAY (external SMTP smarthost). Direct leaves MTA_OUT_MODE unset and lets
    the socks-proxy dependency prompt handle egress.
    """
    choices = [
        "direct: send from the messages host / socks-proxy",
        "relay: send via an external SMTP server",
    ]
    was_relay = answers.get("MTA_OUT_MODE") == "relay"
    default_choice = choices[1] if was_relay else choices[0]
    choice = _ask_select(
        "Outbound mail mode (MTA_OUT_MODE):", choices, default=default_choice
    )
    if not choice.startswith("relay"):
        if was_relay:
            ui.warn(
                "You switched outbound mail from relay to direct. "
                "Remove the MTA_OUT_MODE and MTA_OUT_RELAY_* lines from the "
                "st_messages_env blob in the core vars.yml by hand. "
                "The rebootstrap never deletes committed lines, so the file "
                "still says relay until you remove them."
            )
        return
    answers["MTA_OUT_MODE"] = "relay"
    answers["MTA_OUT_RELAY_HOST"] = _ask(
        "MTA_OUT_RELAY_HOST",
        _recall(answers, "MTA_OUT_RELAY_HOST"),
        placeholder=f"{_SMTP_PLACEHOLDER}:587",
    )
    had_relay_user = "MTA_OUT_RELAY_USERNAME" in answers
    _ask_optional(
        answers,
        "MTA_OUT_RELAY_USERNAME",
        "MTA_OUT_RELAY_USERNAME (optional, blank = no auth)",
    )
    if "MTA_OUT_RELAY_USERNAME" in answers:
        _ask_secret(answers, backend, "MTA_OUT_RELAY_PASSWORD", core_key)
    elif had_relay_user and answers.pop("MTA_OUT_RELAY_PASSWORD", None):
        # A cleared username must not leave a half-active auth config behind.
        _warn_cleared_password(backend, "MTA_OUT_RELAY_PASSWORD")


# yprovider's published port is a role contract constant (st_docs_yprovider_port
# default), not an operator choice. No precedent in this file for prompting a
# fixed port, so it is hardcoded.
_DOCS_YPROVIDER_PORT = "50601"


def _docs_yprovider_endpoints(
    answers: dict, app: str, env: str, core_key: str, yp_hosts: list[str]
) -> str:
    """The CADDY_YPROVIDER_ENDPOINTS value for the core caddy_env blob.

    A single host shared by the core and yprovider uses
    ``host.containers.internal`` (the podman host alias); any other topology
    keeps the real ``host:port`` list.
    """
    if not yp_hosts:
        raise StCliError(
            "yprovider hosts list is empty — check the yprovider hosts file."
        )
    core_hosts = answers.get("_core_hosts") or tree.read_hosts(app, env, core_key)
    if len(yp_hosts) == 1 and sorted(core_hosts) == sorted(yp_hosts):
        return "host.containers.internal:" + _DOCS_YPROVIDER_PORT
    return " ".join(f"{h}:{_DOCS_YPROVIDER_PORT}" for h in yp_hosts)


def _ensure_domain(
    answers: dict, prompt: str, placeholder: str, recovered_domain: str = ""
) -> None:
    """Prompt DOMAIN when a standalone provider run has none yet.

    A full bootstrap already collects DOMAIN on the core; a standalone
    ``bootstrap -c <provider>`` run has an empty ``answers``, so this fills
    the gap, pre-filled with ``recovered_domain`` on a rebootstrap.
    """
    if not answers.get("DOMAIN"):
        answers["DOMAIN"] = _ask(prompt, recovered_domain, placeholder=placeholder)


def _mirror_docs_secret(
    answers: dict,
    backend: SecretBackend,
    core_key: str,
    target_component: str,
    app: str,
    env: str,
    env_key: str,
) -> None:
    """Mirror one docs-core-owned secret into ``target_component``'s own vault.

    A full run reads the live core buffer, a standalone/kept-core run reads
    the core vault on disk, else the operator is prompted.
    """
    vault_key = "vault_" + env_key.lower()
    if backend.prompts_values():
        v = backend.component_secrets(core_key).get(vault_key)
        if v is None:
            cvp = paths.vault_path(app, env, core_key)
            if cvp.exists():
                v = vault.decrypt_to_dict(cvp).get(vault_key)
        if v is None:
            v = _password(f"{env_key} (shared with the docs core — must match it)")
        backend.env_secret(answers, env_key, component=target_component, value=v)
    elif not answers.get(env_key):
        # hashi standalone: no core-set ref in answers, so prompt a lookup term.
        backend.env_secret(answers, env_key, component=target_component, value=None)
    # hashi full run: answers[env_key] already holds the lookup ref, so reuse it.


def _ask_docs_yprovider(
    answers: dict,
    backend: SecretBackend,
    hosts: list[str],
    core_key: str,
    app: str,
    env: str,
) -> None:
    """docs/yprovider: ensure DOMAIN, mirror the two core-owned collaboration
    secrets into yprovider's own vault, and derive ``CADDY_YPROVIDER_ENDPOINTS``.
    ``Y_PROVIDER_API_BASE_URL`` is backend-only and points at the first endpoint.
    """
    core_domain = recover.recover(app, env, core_key).get("DOMAIN", "")
    _ensure_domain(
        answers,
        "Public domain for docs (for the collaboration server origin)",
        "docs.example.org",
        Recovered(core_domain) if core_domain else "",
    )
    for env_key in _DOCS_CORE_SECRETS:
        _mirror_docs_secret(answers, backend, core_key, "yprovider", app, env, env_key)
    endpoints = _docs_yprovider_endpoints(answers, app, env, core_key, hosts)
    answers["CADDY_YPROVIDER_ENDPOINTS"] = endpoints
    answers["Y_PROVIDER_API_BASE_URL"] = f"http://{endpoints.split()[0]}/api/"


def _ensure_meet_domain(answers: dict, recovered_domain: str = "") -> None:
    """meet/livekit: prompt DOMAIN for a standalone `bootstrap -c livekit` run.

    ``recovered_domain`` is the livekit unit's OWN committed DOMAIN
    (``recover.recover(app, env, "livekit")``'s component-var inversion),
    not the core's.
    """
    _ensure_domain(
        answers,
        "Public domain for meet (for the LiveKit recording webhook)",
        "meet.example.org",
        recovered_domain,
    )


def _set_meet_recording(answers: dict) -> None:
    """meet-only: always enable LiveKit egress recording, never prompted.

    Not a confirm: the egress recorder is bundled into the livekit bootstrap
    step unconditionally, so declining here would only disable the backend's
    display of recordings while the recorder kept running, not a real choice.
    """
    answers["RECORDING_ENABLE"] = "True"
    answers["RECORDING_OUTPUT_FOLDER"] = "recordings"
    # SINGULAR /recording matches the meet frontend SPA route (upstream default
    # is RECORDING_DOWNLOAD_BASE_URL=http://localhost:3000/recording). Do not
    # pluralize: that would 404 the emailed recording-ready link. Unrelated to
    # RECORDING_OUTPUT_FOLDER above, which is legitimately plural (an S3 folder
    # prefix, not a URL path).
    answers["RECORDING_DOWNLOAD_BASE_URL"] = (
        "https://{{ st_meet_public_host }}/recording"
    )


def _redis_topology(
    livekit_hosts: list[str], egress_hosts: list[str]
) -> tuple[bool, str | None]:
    """(valkey_enabled, redis_address|None). A single co-located node uses local
    valkey (``127.0.0.1:6379``); otherwise the operator must supply a shared
    redis url. The caller prompts it (no format validation: any non-empty
    string is accepted).
    """
    single = sorted(livekit_hosts) == sorted(egress_hosts) and len(livekit_hosts) == 1
    return (True, "127.0.0.1:6379") if single else (False, None)


def _ask_egress_hosts(
    meta,
    env: str,
    livekit_hosts: list[str],
    prior_livekit_hosts: list[str] | None = None,
) -> tuple[list[str], bool]:
    """Ask the egress hosts (blank co-locates on livekit's) and decide the
    redis topology up front. Returns (egress_hosts, valkey_enabled).

    A recovered co-located egress keeps a blank default, so Enter follows
    livekit to its CURRENT hosts instead of pinning egress to an old one.
    """
    recovered = recover.recover_hosts(meta.app, env, "egress")
    was_colocated = prior_livekit_hosts is not None and sorted(recovered) == sorted(
        prior_livekit_hosts
    )
    egress_hosts = _ask_hosts(
        "egress (leave blank to co-locate on the livekit hosts)",
        allow_empty=True,
        default=None if was_colocated else recovered,
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
    """Mirror livekit's already-decided secrets in ``names`` into egress's own
    vault, under the same ``st_meet_livekit_*`` var names egress reuses.

    Fails fast (``StCliError``) on a missing secret/ref instead of silently
    surfacing it much later at deploy time as an undefined variable.
    """
    if (
        backend.prompts_values()
    ):  # ansible-vault: copy raw values into egress vault buffer
        src = backend.component_secrets("livekit")
        disk = None
        for name in names:
            val = src.get(name)
            if val is None:
                if disk is None:
                    disk = _livekit_vault_snapshot(meta, env)
                val = disk.get(name)
            if val is None:
                raise StCliError(
                    f"livekit secret {name} missing — cannot mirror it to egress; "
                    "re-bootstrap livekit."
                )
            backend.var_secret(CommentedMap(), name, val, component="egress")
    else:
        # hashi: reuse livekit's lookup refs directly in egress vars.yml (NO re-prompt).
        for name in names:
            ref = lk_vars.get(name)
            if ref is None:
                raise StCliError(
                    f"livekit lookup ref {name} missing — cannot mirror it to egress; "
                    "re-bootstrap livekit."
                )
            ev[name] = ref


def _livekit_vault_snapshot(meta, env) -> dict:
    """The on-disk livekit vault, decrypted, or ``{}`` when none exists yet.

    Lets a caller check for an already-decided secret with no live buffer to read.
    """
    lvp = paths.vault_path(meta.app, env, "livekit")
    return vault.decrypt_to_dict(lvp) if lvp.exists() else {}


def _resolve_egress_redis_password(meta, env, backend, reuse_disk: bool) -> str:
    """Never re-prompt a decided secret: the live buffer wins first, then the
    on-disk livekit vault, and only then a fresh prompt.

    ``reuse_disk`` is False on a new redis address, so an old password never
    silently follows it.
    """
    decided = backend.component_secrets("livekit").get("st_meet_livekit_redis_password")
    if decided:
        return decided
    if reuse_disk:
        disk = _livekit_vault_snapshot(meta, env)
        if disk.get("st_meet_livekit_redis_password"):
            return disk["st_meet_livekit_redis_password"]
    return _password(
        "Redis password shared by livekit and egress (leave blank if none)",
        required=False,
    )


def _bundle_egress(
    meta, lk_pvars, answers, backend, env, egress_hosts, valkey_enabled
) -> None:
    """Called from the livekit deploy tail, before livekit writes its vars.yml.

    Prompts the redis address/username/password when NOT co-located, mirrors
    livekit's creds into egress's own vault, and writes the egress unit.
    It records ``answers["_egress_bundled"]`` so the deps loop registers the unit.
    """
    if valkey_enabled:
        addr, username, pw = "127.0.0.1:6379", "", None
        if lk_pvars.get("st_meet_livekit_valkey_enabled") is False:
            # Topology flip external -> co-located: the local valkey has no
            # auth, so drop the redis auth keys this tool owns. write_vault
            # never deletes, so name the stale vault entries for hand removal.
            lk_pvars.pop("st_meet_livekit_redis_username", None)
            lk_pvars.pop("st_meet_livekit_redis_password", None)
            if backend.prompts_values() and _livekit_vault_snapshot(meta, env).get(
                "st_meet_livekit_redis_password"
            ):
                ui.warn(
                    "Redis is now the co-located valkey (no auth). Remove the "
                    "stale st_meet_livekit_redis_password entries from the "
                    "livekit and egress vault.yml by hand."
                )
    else:
        # pre-fill from lk_pvars (the on-disk load at the top of this dep run)
        # ONLY when the recovered topology was already external. A fresh or
        # previously co-located unit has nothing worth pre-filling.
        prev_external = lk_pvars.get("st_meet_livekit_valkey_enabled") is False
        addr_default = (
            lk_pvars.get("st_meet_livekit_redis_address", "") if prev_external else ""
        )
        username_default = (
            lk_pvars.get("st_meet_livekit_redis_username", "") if prev_external else ""
        )
        addr = _ask(
            "Redis address shared by livekit and egress (host:port)",
            Recovered(addr_default) if addr_default else "",
        )
        username = _ask(
            "Redis username shared by livekit and egress (leave blank if none)",
            Recovered(username_default) if username_default else "",
            required=False,
        )
        pw = (
            _resolve_egress_redis_password(
                meta,
                env,
                backend,
                reuse_disk=prev_external
                and addr == lk_pvars.get("st_meet_livekit_redis_address"),
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
        else:
            # "leave blank if none" is an explicit no-auth answer. Drop the
            # tool-owned key instead of keeping a stale username line.
            lk_pvars.pop("st_meet_livekit_redis_username", None)
        if backend.prompts_values():
            # a blank password stores + mirrors nothing (an unauthenticated
            # external redis); a decided one re-stores unchanged, and write_vault's
            # no-change check makes that a byte no-op.
            if pw:
                backend.var_secret(
                    lk_pvars, "st_meet_livekit_redis_password", pw, component="livekit"
                )
                mirror_names.append("st_meet_livekit_redis_password")
            elif _livekit_vault_snapshot(meta, env).get(
                "st_meet_livekit_redis_password"
            ):
                ui.warn(
                    "Redis password left blank but a stored one exists. Remove "
                    "the stale st_meet_livekit_redis_password entries from the "
                    "livekit and egress vault.yml by hand."
                )
        else:
            # hashi: only prompt a fresh lookup term when none is recovered yet;
            # the ref (recovered or fresh) always needs mirroring into egress.
            if "st_meet_livekit_redis_password" not in lk_pvars:
                backend.var_secret(
                    lk_pvars,
                    "st_meet_livekit_redis_password",
                    None,
                    component="livekit",
                )
            mirror_names.append("st_meet_livekit_redis_password")
    ev = tree.load_vars(meta.app, env, "egress")
    ev["st_meet_livekit_domain"] = lk_pvars["st_meet_livekit_domain"]
    ev["st_meet_livekit_redis_address"] = addr
    if not valkey_enabled and username:
        ev["st_meet_livekit_redis_username"] = username
    else:
        # keep the loaded egress map free of stale tool-owned redis auth keys
        # after a no-auth answer or a flip back to the co-located valkey.
        ev.pop("st_meet_livekit_redis_username", None)
    if valkey_enabled:
        ev.pop("st_meet_livekit_redis_password", None)
    _mirror_livekit_creds_to_egress(backend, meta, env, ev, lk_pvars, mirror_names)
    writer.apply_component_vars(ev, meta, meta.component("egress"), answers)
    writer.expand_var_markers(ev, backend)
    ev[writer.cadvisor_var(meta.app)] = _ask_cadvisor(
        "egress", _cadvisor_default(meta.app, env, "egress")
    )
    if not ev.ca.comment:
        # Only stamp the header when the file has no start comment already: a
        # rebootstrap over an existing header must not stack a duplicate one
        # (mirrors write_core's same guard).
        ev.yaml_set_start_comment(
            writer.vars_header(meta.app, meta, meta.component("egress"), backend)
        )
    tree.save_vars(meta.app, env, "egress", ev)
    writer.write_vault(meta.app, env, "egress", backend)
    tree.write_hosts(
        meta.app, env, "egress", meta.component("egress").app_name, egress_hosts
    )
    answers["_egress_bundled"] = MODE_MANAGED


def _standalone_egress(meta, ev_pvars, backend, env) -> None:
    """Adopt livekit's decided domain + redis topology for a standalone
    ``bootstrap -c egress`` run; the generic dep tail then writes the unit.

    Never re-prompts the topology: egress must share livekit's redis. The redis
    password is mirrored only when livekit's redis is external and has one.
    """
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
        # truthy: an old blank-auth store ("") counts as no password.
        has_password = bool(
            lk.get("st_meet_livekit_redis_password")
            if not backend.prompts_values()
            else _livekit_vault_snapshot(meta, env).get(
                "st_meet_livekit_redis_password"
            )
        )
        if has_password:
            mirror_names.append("st_meet_livekit_redis_password")
    _mirror_livekit_creds_to_egress(backend, meta, env, ev_pvars, lk, mirror_names)


def _reuse_egress(meta, answers, backend, env) -> None:
    """livekit REUSE: keep egress in the deployment.

    Re-registers an existing egress tree, or creates one co-located on the
    livekit hosts when livekit predates egress bundling.
    """
    if paths.vars_path(meta.app, env, "egress").exists():
        answers["_egress_bundled"] = MODE_MANAGED  # keep as-is, re-register
        return
    ev = CommentedMap()
    _standalone_egress(meta, ev, backend, env)
    writer.apply_component_vars(ev, meta, meta.component("egress"), answers)
    writer.expand_var_markers(ev, backend)
    ev[writer.cadvisor_var(meta.app)] = _ask_cadvisor("egress")
    ev.yaml_set_start_comment(
        writer.vars_header(meta.app, meta, meta.component("egress"), backend)
    )
    tree.save_vars(meta.app, env, "egress", ev)
    writer.write_vault(meta.app, env, "egress", backend)
    egress_hosts = tree.read_hosts(meta.app, env, "livekit")
    tree.write_hosts(
        meta.app, env, "egress", meta.component("egress").app_name, egress_hosts
    )
    answers["_egress_bundled"] = MODE_MANAGED


def _prompt_shared(rule: dict, default: str = "") -> str:
    """Prompt for a shared value described by ``rule``.

    ``default`` pre-fills a NON-secret prompt only; a secret field has no
    editable default.
    """
    if writer.rule_is_secret(rule):
        return _password(writer.rule_label(rule))
    return _ask(writer.rule_label(rule), default)


def _shared_default(answers: dict, rule: dict) -> str:
    """Return the best pre-fill for a shared-rule prompt with no ``var``.

    `_prompt_shared` wraps a recovered value in `Recovered` for the silent replay.
    """
    key = rule.get("answer_key")
    if not key:
        return ""
    value = answers.get(key)
    return Recovered(str(value)) if value is not None else ""


def _handle_dependency(
    meta,
    dep,
    answers,
    backend: SecretBackend,
    env,
    m: StCliManifest,
    flagged: dict[str, UpgradeNeed],
    wire_only: bool = False,
    assume_deploy: bool = False,
    offer: NewComponentOffer | None = None,
    override_core: bool = False,
) -> str:
    """Run the dependency prompt for one dependency; wire shared vars. Returns mode.

    A FRESH provider offers deploy / skip / external (deploy omitted under
    ``wire_only``; a long-ago-declined optional dep with no ``offer`` skips
    quietly in a silent replay instead). A unit recorded ``external`` wins
    over the tree state and, outside ``wire_only``, offers keep / re-enter /
    deploy-now.

    An EXISTING, non-external provider never offers skip/external again. It
    forces a replay (no select) when ``override_core`` or ``flagged`` (unless
    ``wire_only``, which can never deploy a provider and only warns);
    otherwise it offers reuse/modify, defaulting to reuse, or takes reuse
    directly under ``wire_only``. An override rebuilds the core from an empty
    tree, so a reuse would drop the constructed consumer values.

    ``assume_deploy=True`` skips every select above and assumes deploy, since the
    operator explicitly targeted this provider (e.g. ``bootstrap -c livekit``).
    """
    provider = meta.component(dep.on)
    core = meta.core()

    ui.console.print()
    optional_hint = "[bold]optional[/bold] " if dep.optional else ""
    ui.info(f"Bootstrapping {dep.on}/{env} ({optional_hint}dependency of {meta.app}).")

    has_existing = paths.vars_path(meta.app, env, provider.key).exists()
    need = flagged.get(dep.on)
    unit = next(
        (
            u
            for u in m.units
            if u.app == meta.app and u.env == env and u.component == dep.on
        ),
        None,
    )
    recorded_external = unit is not None and unit.mode == MODE_EXTERNAL
    # upgrades.needed skips an external unit, so flagged never holds one.
    # A fresh dependency reached during a silent replay has nothing recovered,
    # so it needs its own menu handling instead of the ordinary fresh-provider
    # select (which would ask a question the operator never opted into).
    fresh_silent = (
        not assume_deploy
        and not recorded_external
        and not has_existing
        and in_silent_replay()
    )

    if assume_deploy:
        if recorded_external:
            ui.info(
                f"{dep.on}: bootstrapping it directly (`-c {dep.on}`) makes it "
                "locally managed again."
            )
        choice = "deploy"
    elif recorded_external:
        if wire_only:
            choice = MODE_EXTERNAL
        else:
            external_menu = {
                "Keep external (recorded)": MODE_EXTERNAL,
                "Re-enter external values (URL + keys)": "external-redo",
                "Bootstrap now (manage locally)": "deploy",
            }
            choice = external_menu[
                _ask_select(
                    f"Bootstrap {dep.on} now?",
                    list(external_menu),
                    default="Keep external (recorded)",
                )
            ]
    elif has_existing and override_core:
        ui.info(
            f"{dep.on}: core override — replaying its questionnaire to "
            "rebuild the wiring (pre-filled)."
        )
        choice = "deploy"
    elif has_existing and wire_only:
        if need is not None:
            ui.warn(
                f"{dep.on}: a rebootstrap is pending ({need.version} — "
                f"{need.reason}). This core-only run cannot deploy providers, "
                "so the flag stays pending. Run "
                f"`st-cli bootstrap {meta.app} {env}` to clear it."
            )
        choice = "reuse"
    elif has_existing and need is not None:
        ui.info(
            f"{dep.on}: rebootstrap required ({need.version} — {need.reason}) "
            "— replaying its questionnaire."
        )
        choice = "deploy"
    elif has_existing:
        reuse_or_modify = {
            "Reuse existing in the repo": "reuse",
            "Modify (replay the questionnaire)": "deploy",
        }
        choice = reuse_or_modify[
            _ask_select(
                f"Bootstrap {dep.on} now?",
                list(reuse_or_modify),
                default="Reuse existing in the repo",
            )
        ]
    elif fresh_silent and offer is None:
        # A long-ago-declined optional dep stays quiet on every silent replay;
        # the offer mechanism exists to avoid nagging about it forever.
        ui.info(
            f"{dep.on}: not bootstrapped — skipped (add it with "
            f"`st-cli bootstrap {meta.app} {env} -c {dep.on}`)."
        )
        return "skip"
    else:
        if fresh_silent:
            msg = f"{dep.on}: newly available since {offer.version} — {offer.reason}"
            if offer.link:
                msg += f" See {offer.link}."
            ui.info(msg)
        options: dict[str, str] = {}
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
        options["Already deployed (enter URL + keys)"] = MODE_EXTERNAL
        # suspend_silent no-ops outside silent mode; inside it, a fresh menu
        # for a genuinely new component must ask, not auto-accept.
        with suspend_silent():
            choice = options[_ask_select(f"Bootstrap {dep.on} now?", list(options))]

    # The post-menu handling of a fresh dependency must ask for real, not
    # auto-accept a Recovered default meant for the outer replay's answers.
    with suspend_silent() if fresh_silent else nullcontext():
        if choice == "skip":
            ui.info(
                f"{dep.on}: bootstrap later — add it with "
                f"`st-cli bootstrap {meta.app} {env} -c {dep.on}`."
            )
            return "skip"

        if choice in (MODE_EXTERNAL, "external-redo"):
            # "external" (kept/fresh) skips a rule already recovered into `answers`:
            # re-prompting it would rotate a secret or clobber a committed value.
            # "external-redo" re-asks every rule regardless (the operator chose to
            # retype the external endpoint).
            only_missing = choice == MODE_EXTERNAL
            for rule in dep.shared:
                key = rule.get("consumer_env_key")
                if not key:
                    continue
                # Truthy check: a prior "bootstrap later" run commits the consumer
                # keys as empty lines, and recover() brings them back as "". An
                # empty value is an unanswered prompt, not a decided one.
                if only_missing and answers.get(key):
                    continue
                if writer.rule_is_secret(rule):
                    value = _prompt_shared(rule) if backend.prompts_values() else None
                else:
                    value = _prompt_shared(rule, _shared_default(answers, rule))
                if rule.get("answer_key") and value is not None:
                    answers[rule["answer_key"]] = value
                writer.inject_consumer(rule, value, answers, backend, core.key)
            if (
                meta.app == "messages"
                and dep.on == "socks-proxy"
                and not (only_missing and "MTA_OUT_DIRECT_PROXIES" in answers)
            ):
                value = (
                    _password(
                        "MTA_OUT_DIRECT_PROXIES (socks5s://user:pass@host:port,...)"
                    )
                    if backend.prompts_values()
                    else None
                )
                backend.env_secret(
                    answers, "MTA_OUT_DIRECT_PROXIES", component=core.key, value=value
                )
            if (
                meta.app == "messages"
                and dep.on == "mpa"
                and not (only_missing and "SPAM_CONFIG" in answers)
            ):
                value = (
                    _password("SPAM_CONFIG (JSON for the external mpa)")
                    if backend.prompts_values()
                    else None
                )
                backend.env_secret(
                    answers, "SPAM_CONFIG", component=core.key, value=value
                )
            # A kept, recorded external yprovider keeps its recovered wiring. A
            # freshly declared one, a redo, or a core override must prompt
            # everything: the core just generated its own
            # COLLABORATION_SERVER_SECRET/Y_PROVIDER_API_KEY, and the external
            # unit's values have to replace them.
            if (
                meta.app == "docs"
                and dep.on == "yprovider"
                and not (only_missing and recorded_external and not override_core)
            ):
                eps_raw = _ask(
                    "yprovider endpoints (host:port, comma-separated)",
                    _recall(answers, "CADDY_YPROVIDER_ENDPOINTS").replace(" ", ","),
                    placeholder="10.0.0.9:50601,10.0.0.10:50601",
                )
                answers["CADDY_YPROVIDER_ENDPOINTS"] = " ".join(
                    e.strip() for e in eps_raw.split(",") if e.strip()
                )
                answers["Y_PROVIDER_API_BASE_URL"] = _ask(
                    "Y_PROVIDER_API_BASE_URL (backend-only conversion API)",
                    _recall(answers, "Y_PROVIDER_API_BASE_URL"),
                    placeholder="http://yprovider.internal:50601/api/",
                )
                for env_key in _DOCS_CORE_SECRETS:
                    value = _password(env_key) if backend.prompts_values() else None
                    backend.env_secret(
                        answers, env_key, component=core.key, value=value
                    )
            ui.info(f"{dep.on}: external — values prompted, not deployed.")
            return MODE_EXTERNAL

        if choice == "reuse":
            # The unit is still managed (deploys with the app); only the
            # consumer ref is re-injected, the provider's own values stay.
            pvars = tree.load_vars(meta.app, env, provider.key)
            # Decrypt the vault only in ansible-vault mode: hashi_vault
            # prompts a fresh lookup term for each consumer ref instead.
            pvault = (
                vault.decrypt_to_dict(paths.vault_path(meta.app, env, provider.key))
                if backend.prompts_values()
                else {}
            )
            for rule in dep.shared:
                consumer_key = rule.get("consumer_env_key")
                if not consumer_key:
                    continue
                # Already recovered: under hashi_vault env_secret always
                # prompts a fresh lookup term, so skip it to keep a silent
                # replay from asking one (a no-op under ansible-vault anyway).
                if writer.rule_is_secret(rule) and answers.get(consumer_key):
                    continue
                var = rule.get("var")
                if writer.rule_is_secret(rule):
                    if backend.prompts_values():
                        value = pvault.get(var) if var else None
                        value = (
                            str(value) if value is not None else _prompt_shared(rule)
                        )
                    else:
                        # hashi_vault: value is not needed; env_secret prompts a
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
            if meta.app == "docs" and provider.key == "yprovider":
                # Rebuild the caddy upstream list and conversion base URL
                # from the reused unit's hosts file.
                yp_hosts = tree.read_hosts(meta.app, env, provider.key)
                endpoints = _docs_yprovider_endpoints(
                    answers, meta.app, env, core.key, yp_hosts
                )
                answers["CADDY_YPROVIDER_ENDPOINTS"] = endpoints
                answers["Y_PROVIDER_API_BASE_URL"] = (
                    f"http://{endpoints.split()[0]}/api/"
                )
                # The core must adopt the REUSED unit's secrets: _ask_core just
                # generated fresh values, which would break the backend/yprovider
                # auth. hashi mode keeps the lookup refs already collected.
                if backend.prompts_values():
                    for env_key in _DOCS_CORE_SECRETS:
                        v = pvault.get("vault_" + env_key.lower())
                        if v is None:
                            v = _password(
                                f"{env_key} (must match the reused yprovider unit)"
                            )
                        backend.env_secret(
                            answers, env_key, component=core.key, value=str(v)
                        )
            ui.info(f"{dep.on}: reuse — kept existing unit (still deployed).")
            return MODE_MANAGED

        # deploy: create + manage this unit. A rebootstrap pre-fills every
        # prompt below from what is already on disk.
        existing_hosts = recover.recover_hosts(meta.app, env, provider.key)
        hosts = _ask_hosts(dep.on, default=existing_hosts)
        egress_hosts = valkey_enabled = None
        if meta.app == "meet" and provider.key == "livekit":
            egress_hosts, valkey_enabled = _ask_egress_hosts(
                meta, env, hosts, existing_hosts
            )  # Q2
        # Merge, not replace: loading the existing vars.yml means a
        # hand-edited/custom key on this provider survives.
        pvars = tree.load_vars(meta.app, env, provider.key)
        existing_shared = recover.recover_shared(
            meta.app, env, provider.key, dep.shared
        )
        for rule in dep.shared:
            var = rule.get("var")
            consumer_key = rule.get("consumer_env_key")
            is_secret = writer.rule_is_secret(rule)
            recovered = existing_shared.get(var) if var else None

            if is_secret and recovered is not None:
                # Already decided: never regenerate/re-prompt. `pvars` already
                # holds the provider-side value, so only the consumer side
                # (this run's `answers`) needs re-injecting.
                if consumer_key:
                    if backend.prompts_values():
                        backend.env_secret(
                            answers, consumer_key, component=core.key, value=recovered
                        )
                    else:
                        # hashi_vault: reuse the committed lookup ref verbatim;
                        # env_secret ignores `value` and would repoint it.
                        answers[consumer_key] = recovered
                if rule.get("answer_key"):
                    answers[rule["answer_key"]] = recovered
                continue

            if is_secret:
                if rule.get("generate"):
                    if backend.prompts_values():  # ansible-vault mints it
                        value = writer.gen_value(rule)
                        ui.info(
                            f"{dep.on}: generated {consumer_key or var or 'value'}."
                        )
                    else:  # hashi_vault references an existing secret
                        value = None
                else:
                    # prompted secret: only prompt the value in ansible-vault mode
                    # (hashi_vault mode prompts a lookup term in var_secret/env_secret).
                    value = _prompt_shared(rule) if backend.prompts_values() else None
            else:
                # non-secret: re-asked every time, pre-filled from the
                # recovered value so accepting it is a no-op.
                default = (
                    Recovered(str(recovered))
                    if recovered is not None
                    else _shared_default(answers, rule)
                )
                value = _prompt_shared(rule, default)
            if is_secret and var and consumer_key:
                # Same secret on both sides: store once, ref it from both.
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
        if meta.app == "docs" and provider.key == "yprovider":
            _ask_docs_yprovider(answers, backend, hosts, core.key, meta.app, env)
        if meta.app == "meet" and provider.key == "livekit":
            _lk_domain = recover.recover(meta.app, env, provider.key).get("DOMAIN", "")
            _ensure_meet_domain(answers, Recovered(_lk_domain) if _lk_domain else "")
            # egress hosts already asked (Q2); redis+egress write happens AFTER the
            # livekit cadvisor confirm below.
        elif meta.app == "meet" and provider.key == "egress":
            _standalone_egress(meta, pvars, backend, env)
        writer.apply_component_vars(pvars, meta, provider, answers)
        writer.expand_var_markers(pvars, backend)
        pvars[writer.cadvisor_var(meta.app)] = _ask_cadvisor(
            dep.on, _cadvisor_default(meta.app, env, provider.key)
        )  # Q7 livekit cadvisor
        if meta.app == "meet" and provider.key == "livekit":
            # Q8 redis (address/username/password, only when NOT co-located) + Q9
            # egress cadvisor; runs before save_vars so the redis vars land in pvars.
            _bundle_egress(
                meta, pvars, answers, backend, env, egress_hosts, valkey_enabled
            )
        if not pvars.ca.comment:
            # Only stamp the header when the file has no start comment already: a
            # rebootstrap over an existing header must not stack a duplicate one
            # (mirrors write_core's same guard).
            pvars.yaml_set_start_comment(
                writer.vars_header(meta.app, meta, provider, backend)
            )
        tree.save_vars(meta.app, env, provider.key, pvars)
        writer.write_vault(meta.app, env, provider.key, backend)
        tree.write_hosts(meta.app, env, provider.key, provider.app_name, hosts)
        # hashi_vault mode buffers no secrets and writes no vault.yml, so don't
        # claim it.
        files = (
            "vars.yml + vault.yml + hosts"
            if backend.component_secrets(provider.key)
            else "vars.yml + hosts"
        )
        ui.success(f"{dep.on}: managed — wrote {files}.")
        return MODE_MANAGED


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
    # (re)written, so answers is empty. Skip the domain/provider lines and
    # narrow the listed units + the "Next" hint to that component.
    scoped = component is not None and component != core_key
    if not scoped:
        if answers.get("DOMAIN"):  # file-scanner has no public domain — skip the line
            ui.info(f"  domain: {answers['DOMAIN']}")
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
    # and `st-cli secrets` steps are ansible-vault only; skipped for hashi_vault
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
    is_vault = manifest.secret_config_for(m, app, env).backend == BACKEND_ANSIBLE_VAULT

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
    # App-tailored checklist from the manifest
    body = "Make sure you've prepared:\n" + "\n".join(
        f"  • {line}" for line in meta.requirements
    )
    ui.note(body, title="Requirements")
    _confirm_ready("Do you have all of the above ready to continue?")


def _ask_rebootstrap_action(
    app: str,
    env: str,
    flagged: dict[str, UpgradeNeed],
    allow_override: bool = True,
) -> ReplayAction:
    """Print every pending rebootstrap flag for ``(app, env)``, then offer the
    3-way Modify / Reuse / Override select (Modify is the default).

    Prints every flag, not only the run's own targeted component, so the
    operator sees a dependency's pending flag before picking Reuse.
    ``allow_override=False`` drops the Override choice for a wire-only run.
    """
    for comp in sorted(flagged):
        need = flagged[comp]
        msg = (
            f"{app}/{env}/{comp}: rebootstrap needed ({need.version} — {need.reason})."
        )
        if need.link:
            msg += f" See {need.link}."
        ui.warn(msg)

    labels: dict[str, ReplayAction] = {
        "Modify — replay the questionnaire (answers pre-filled)": ReplayAction.MODIFY,
        "Reuse — keep everything as-is (skip the questionnaire)": ReplayAction.REUSE,
    }
    if allow_override:
        labels["Override — rebuild from scratch (DESTRUCTIVE: regenerates secrets)"] = (
            ReplayAction.OVERRIDE
        )
    choice = _ask_select(
        f"{app}/{env} is already bootstrapped — what do you want to do?",
        list(labels),
        default=next(iter(labels)),
    )
    return labels[choice]


def _confirm_override(app: str, env: str) -> None:
    """Hard destructive gate for `ReplayAction.OVERRIDE`; raises on decline.

    Names every consequence up front. OVERRIDE rebuilds the core from an
    empty tree, so nothing here is a soft warning the operator can shrug off.
    """
    if not _confirm(
        f"Override {app}/{env}: this rebuilds the core from scratch. It "
        "REGENERATES the core's own generated secrets (for example "
        "DJANGO_SECRET_KEY), DISCARDS any hand-edits to vars.yml/vault.yml "
        "for the rebuilt unit, and BREAKS deployed services until you "
        "redeploy. A secret owned by a kept provider (for example the "
        "LiveKit API key/secret pair) is re-imported unchanged, never "
        "rotated. A managed dependency that mirrors this core's secrets "
        "(for example messages' mta-in copy of MDA_API_SECRET) is replayed "
        "automatically in the same run, so it picks up the regenerated "
        "value. Continue?",
        default=False,
        auto=False,
    ):
        raise StCliError("override cancelled — nothing touched.")


def bootstrap(
    app: str,
    env: str,
    component: str | None = None,
    *,
    replay: ReplayAction = ReplayAction.ASK,
) -> None:
    """Run the interactive bootstrap questionnaire for ``(app, env)``.

    ``component`` scaffolds only that unit: a provider, the core, or a worker.
    A wire-only core run shows no dependency select and registers no provider unit.
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
    # Shared by the 3-way select gating below and the deps-loop scoping.
    target_core = component in (None, core.key)

    m = _ensure_manifest()
    # Pending rebootstrap flags, newest per component only. Drives
    # _handle_dependency's forced-replay branch: a flagged existing provider
    # must not offer "Reuse", which would clear the flag without a replay.
    flagged: dict[str, UpgradeNeed] = {}
    for need in upgrades.needed(m, app, env):
        current = flagged.get(need.component)
        if current is None or upgrades.parse_version(
            need.version
        ) > upgrades.parse_version(current.version):
            flagged[need.component] = need

    # Newly declared components a flag makes available; only matters on the
    # SILENT path, but cheap to compute unconditionally.
    offers_by_component: dict[str, NewComponentOffer] = {
        o.component: o for o in upgrades.new_component_offers(m, app, env)
    }

    # A rebootstrap is detected purely from what's already committed: the
    # core's vars.yml existing means this run replays the questionnaire with
    # every answer pre-filled instead of starting fresh.
    core_exists = paths.vars_path(app, env, core.key).exists()
    is_rebootstrap = core_exists and (component is None or component in core_or_worker)

    # SILENT recovers its answers from a committed unit: there must be one.
    if replay is ReplayAction.SILENT:
        if component is None or component in core_or_worker:
            if not core_exists:
                raise StCliError(
                    "a silent replay needs a committed unit to recover from — "
                    f"{app}/{env}/{core.key} does not exist yet."
                )
        elif not paths.vars_path(app, env, component).exists():
            raise StCliError(
                "a silent replay needs a committed unit to recover from — "
                f"{app}/{env}/{component} does not exist yet."
            )

    # Fail fast: an unreadable vault.yml must abort before the questionnaire
    # runs, not partway through it. Checked against every registered unit.
    writer.ensure_vault_readable(
        app, env, [u.component for u in manifest.units_for(m, app, env)]
    )

    # Pre-questionnaire guidance for a full/core/workers bootstrap: the intro
    # on a fresh unit, or the 3-way Modify/Reuse/Override select (or a
    # silent-replay notice) when the core already exists. Provider-only runs
    # skip all of it except a programmatic SILENT replay.
    action = ReplayAction.MODIFY
    if component is None or component in core_or_worker:
        if not is_rebootstrap:
            _print_bootstrap_intro(meta)
        elif not target_core:
            # workers-only (`-c <workers>`): no 3-way select. Workers own no
            # files of their own, so REUSE/OVERRIDE stay core-only; keep the
            # plain MODIFY replay (SILENT still applies).
            if replay in (ReplayAction.REUSE, ReplayAction.OVERRIDE):
                raise StCliError(
                    f"replay={replay.value} applies to the core path only — "
                    f"`bootstrap {app} {env} -c {component}` targets the "
                    "workers component. Run it without -c, or -c the core."
                )
            if replay is ReplayAction.SILENT:
                action = ReplayAction.SILENT
                ui.note(
                    f"Upgrading {app}/{env}/{component} — replaying bootstrap "
                    "with your recovered answers; only new settings will be "
                    "asked.",
                    title="Upgrade",
                )
            else:
                ui.note(
                    f"Rebootstrapping {app}/{env}/{component} — every answer "
                    "is pre-filled from your current config; press Enter to "
                    "keep it.",
                    title="Rebootstrap",
                )
        else:
            # A wire-only run never touches a provider, so an Override there
            # would silently drop the core-side wiring it rebuilds.
            wire_only_run = component == core.key
            if wire_only_run and replay is ReplayAction.OVERRIDE:
                raise StCliError(
                    f"replay=override needs the full run — `bootstrap {app} "
                    f"{env} -c {core.key}` is wire-only and cannot rebuild "
                    f"the provider wiring. Run `st-cli bootstrap {app} {env}`."
                )
            action = (
                _ask_rebootstrap_action(
                    app, env, flagged, allow_override=not wire_only_run
                )
                if replay is ReplayAction.ASK
                else replay
            )

            if action is ReplayAction.REUSE:
                # No manifest write, so a pending flag stays pending. Warns
                # for every flagged component: Reuse leaves core and
                # dependency providers untouched alike.
                for comp in sorted(flagged):
                    need = flagged[comp]
                    msg = (
                        f"{app}/{env}/{comp}: rebootstrap still pending "
                        f"({need.version} — {need.reason}) — deploy will "
                        "refuse until a real replay runs."
                    )
                    if need.link:
                        msg += f" See {need.link}."
                    ui.warn(msg)
                ui.info(f"{app}/{env} kept as-is — nothing written.")
                return

            if action is ReplayAction.OVERRIDE:
                _confirm_override(app, env)

            if action is ReplayAction.SILENT:
                ui.note(
                    f"Upgrading {app}/{env} — replaying bootstrap with your "
                    "recovered answers; only new settings will be asked.",
                    title="Upgrade",
                )
            elif action is ReplayAction.MODIFY:
                ui.note(
                    f"Rebootstrapping {app}/{env} — every answer is pre-filled from "
                    "your current config; press Enter to keep it.",
                    title="Rebootstrap",
                )
    elif replay is ReplayAction.SILENT:
        action = ReplayAction.SILENT
        ui.note(
            f"Upgrading {app}/{env}/{component} — replaying bootstrap with your "
            "recovered answers; only new settings will be asked.",
            title="Upgrade",
        )
    elif replay in (ReplayAction.REUSE, ReplayAction.OVERRIDE):
        raise StCliError(
            f"replay={replay.value} applies to the core path only — "
            f"`bootstrap {app} {env} -c {component}` targets a dependency "
            "provider. Run it without -c, or -c the core/workers component."
        )

    # A replay pre-fills every answer, so the operator needs no such tip.
    if not is_rebootstrap and replay is ReplayAction.ASK:
        ui.note(
            "This questionnaire only scaffolds your config files.\n"
            "If you mistype an answer, don't start over: finish "
            "the questionnaire, then edit the generated files directly under "
            "<app>/<env>/<component>/."
        )
    # Choose the secret backend per (app, env); persisted into .st-cli.yml.
    # SILENT wraps setup through the manifest save so prompts.py's primitives
    # auto-accept a recovered default inside this context.
    override_core = action is ReplayAction.OVERRIDE
    ctx = silent_replay() if action is ReplayAction.SILENT else nullcontext()
    with ctx as silent_stats:
        backend = setup_backend(m, app, env)
        if backend.kind == BACKEND_ANSIBLE_VAULT:
            vault.ensure_vault_password(create=True)
        tree.ensure_common(app, env)
        tree.ensure_ssh_scaffold()

        ui.info(f"Bootstrapping {app}/{env}.")

        # Scope flags gate the sections below so the no-flag path is unchanged.
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

        # Core: always runs the questionnaire when targeted: fresh, a
        # rebootstrap (pre-filled), or an OVERRIDE (no seed: rebuilt empty).
        if target_core:
            recoverable = core_exists and not override_core
            seed = recover.recover(app, env, core.key) if recoverable else {}
            if app == "drive" and seed:
                _seed_drive_legacy_s3(seed, tree.load_vars(app, env, core.key))
            core_hosts_default = (
                recover.recover_hosts(app, env, core.key) if recoverable else []
            )
            core_hosts = _ask_hosts(core.key, default=core_hosts_default)  # hosts first
            # Optional worker IPs: blank ⇒ workers co-locate on the core hosts (the
            # default). Meet has no workers implementation, so it is never prompted.
            if worker and worker.implemented:
                worker_hosts_default = (
                    tree.read_hosts(app, env, core.key, group=worker.app_name)
                    if recoverable
                    else []
                )
                worker_hosts = _ask_hosts(
                    f"workers (leave blank to run on the {core.key} hosts)",
                    allow_empty=True,
                    default=worker_hosts_default,
                )
            # keycloak / projects / file-scanner are not Django apps — each takes
            # its own (raw-env) questionnaire instead of the shared core one.
            if app == "keycloak":
                answers = _ask_keycloak(meta, backend, seed)
            elif app == "projects":
                answers = _ask_projects(meta, backend, seed)
            elif app == "file-scanner":
                answers = _ask_file_scanner(meta, backend, seed)
            else:
                answers = _ask_core(meta, backend, seed)
            # a fresh run has no core hosts file on disk yet, so stash the hosts
            # so dependency hooks can detect co-location (_docs_yprovider_endpoints).
            answers["_core_hosts"] = core_hosts
            core_cadvisor = _ask_cadvisor(
                core.key,
                True if override_core else _cadvisor_default(app, env, core.key),
            )  # last core question

        # Worker-only bootstrap: the core must already exist (workers reuse its
        # vars/vault/hosts). In the full path the core was just (re)written above.
        if (
            target_worker
            and worker is not None
            and component == worker.key
            and not paths.vars_path(app, env, core.key).exists()
        ):
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
                # egress is bundled into the livekit step, not a separate iteration.
                continue
            mode = _handle_dependency(
                meta,
                dep,
                answers,
                backend,
                env,
                m,
                flagged,
                wire_only=wire_only,
                assume_deploy=assume_deploy,
                offer=offers_by_component.get(dep.on),
                override_core=override_core,
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
                if (
                    app == "meet"
                    and dep.on == "livekit"
                    and answers.get("_egress_bundled")
                ):
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
                meta,
                answers,
                backend,
                core_hosts,
                worker_hosts,
                env,
                core_cadvisor,
                fresh=override_core,
            )
            manifest.upsert_unit(
                m,
                UnitState(
                    app=app,
                    env=env,
                    component=core.key,
                    mode=MODE_MANAGED,
                    bootstrapped_with=__version__,
                ),
            )
        # Workers own no files; they reuse the core unit's vars/vault and
        # only flip st_<app>_workers_enabled.
        if target_worker:
            manifest.upsert_unit(
                m,
                UnitState(
                    app=app,
                    env=env,
                    component=worker.key,
                    mode=MODE_MANAGED,
                    bootstrapped_with=__version__,
                ),
            )

        units = manifest.units_for(m, app, env)
        manifest.save_manifest(m)

    if silent_stats is not None:
        if silent_stats.asked:
            ui.info(
                f"Kept {silent_stats.auto} recovered answer(s); asked "
                f"{silent_stats.asked} new question(s)."
            )
        else:
            ui.info(f"Kept {silent_stats.auto} recovered answer(s); no new questions.")

    _print_summary(app, env, answers, units, component)
