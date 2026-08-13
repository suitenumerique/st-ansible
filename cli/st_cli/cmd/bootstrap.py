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
from ..core.models import NewComponentOffer, StCliManifest, UnitState, UpgradeNeed
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
from ..core.secretbackend import SecretBackend, setup_backend

__all__ = ["ReplayAction", "bootstrap"]


class ReplayAction(str, enum.Enum):
    """What :func:`bootstrap` does when the targeted unit already exists."""

    ASK = "ask"  # CLI default: 3-way select when the unit exists
    MODIFY = "modify"  # pre-filled interactive replay (current behaviour)
    OVERRIDE = "override"  # rebuild from scratch, regenerate secrets
    REUSE = "reuse"  # keep as-is, write nothing, never stamp
    SILENT = "silent"  # upgrade: auto-accept recovered answers


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

    Returns a :class:`~st_cli.core.prompts.Recovered` marker when ``key`` is
    in ``answers`` (a silent replay auto-accepts it), the plain ``fallback``
    otherwise (a silent replay still asks it — a genuinely new question).
    """
    if key in answers:
        return Recovered(str(answers[key]))
    return fallback


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


def _ask_optional(answers: dict, key: str, label: str) -> None:
    """Ask an optional text field; a blank answer over a recovered value pops
    the key instead of silently keeping the stale committed line.

    ``envblob.merge`` never deletes a line (see the module docstring), so
    clearing ``key`` from ``answers`` alone leaves the old committed
    ``KEY=value`` line in place — warn the operator to remove it by hand,
    matching the DB-mode / outbound-mode switch convention used elsewhere in
    this module.
    """
    value = _ask(label, _recall(answers, key), required=False)
    if value:
        answers[key] = value
    elif answers.pop(key, None):
        ui.warn(
            f"{key} cleared — the merge never deletes committed lines: "
            f"remove the {key}= line from vars.yml by hand."
        )


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
    # Recovered() only around the recovered value itself — "master" below is a
    # first-run fallback, not a recovered realm, so it must still be asked.
    base_default = Recovered(recovered_base) if same_provider and recovered_base else ""
    realm_default = (
        Recovered(recovered_realm) if same_provider and recovered_realm else "master"
    )
    if provider == "keycloak":
        base_url = _ask(
            "Keycloak base URL",
            base_default,
            placeholder="https://idp.example.org",
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
    """Prompt Django transactional email (SMTP) settings for drive / meet.

    Skipped entirely for ``messages`` (no ``DJANGO_EMAIL_*`` upstream). The SMTP
    password is a secret routed through the backend like the other env secrets;
    optional fields are only written into ``answers`` when filled in so template
    guards stay clean.

    The confirm gate's default is derived from whether SMTP was already
    configured (``DJANGO_EMAIL_HOST`` recovered) — hardcoding ``default=False``
    here would mean an Enter-through rebootstrap silently DROPS a working SMTP
    configuration (the confirm declines, none of the fields below are asked,
    and the whole block is omitted from the next render). When SMTP is already
    configured, the prompt text says so ("review its settings?") instead of
    asking "Configure … settings?" as if nothing were set up yet — an honest
    gate, not just an honest default.
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
        placeholder="smtp.example.org",
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
    recovered (``DB_HOST`` present ⇒ discrete; otherwise "DATABASE_URL") — a
    rebootstrap must not silently flip an operator from discrete DB_* vars to
    DATABASE_URL (or back). "DATABASE_URL" is given explicitly (not left as
    "no default") so a silent replay can auto-accept it too; interactively it
    was already the first, pre-highlighted choice, so this changes nothing.

    Switching mode here does not clean up the shape left behind: ``envblob.merge``
    never deletes a line, so the old mode's committed lines (and, for a switch
    away from DATABASE_URL, its vault entry) stay in the tree. Warn about that
    in both directions instead of leaving two conflicting DB configs in place.
    """
    had_discrete = "DB_HOST" in answers
    had_url = "DATABASE_URL" in answers
    default_mode = "discrete (DB_*)" if had_discrete else "DATABASE_URL"
    mode = _ask_select(
        "Database configuration:",
        ["DATABASE_URL", "discrete (DB_*)"],
        default=default_mode,
        # A total recovery gap (neither shape recovered) is a genuine new
        # question, not a mode switch — silent mode must not auto-pick
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
    # domain string — exactly what was typed here originally. A comma in the
    # recalled value means an operator hand-edited DJANGO_ALLOWED_HOSTS into a
    # multi-host list — pre-filling DOMAIN with that list would poison every
    # derived value (DJANGO_CSRF_TRUSTED_ORIGINS, etc.), so drop the fallback.
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
        derived = {
            "DJANGO_ALLOWED_HOSTS": host,
            "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{host}",
            "DJANGO_CORS_ALLOWED_ORIGINS": f"https://{host}",
            "LOGIN_REDIRECT_URL": f"https://{host}/",
            "LOGIN_REDIRECT_URL_FAILURE": f"https://{host}/",
            "LOGOUT_REDIRECT_URL": f"https://{host}/",
        }
    elif app == "drive":
        # public-facing URLs (incl. MEDIA_BASE_URL) point at st_drive_public_host
        # (resolved at deploy), matching the role's healthcheck Host var rather
        # than the raw domain — mirrors meet's st_meet_public_host override above.
        # DJANGO_ALLOWED_HOSTS/CSRF/CORS keep the literal domain (no indirection).
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
    # An operator who changed DOMAIN this run wants every derived key rebuilt;
    # otherwise a recovered hand-edit (e.g. a custom DJANGO_CORS_ALLOWED_ORIGINS)
    # wins over the recomputed default. `recovered_domain` also guards the
    # messages multi-host case (a comma in DJANGO_ALLOWED_HOSTS empties the
    # DOMAIN pre-fill, see above) — an unrecoverable DOMAIN never forces a
    # recompute, so the recovered multi-host DJANGO_ALLOWED_HOSTS survives.
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
            # f-string composition drops the Recovered marker — re-wrap explicitly.
            endpoint_default = (
                Recovered(f"{s3_protocol}://{s3_host}") if s3_host else ""
            )
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
    # setdefault: a core-recovered or this-run answer wins over the provider's own
    # recovery — covers both a full run and a standalone `-c <provider>` run.
    for k, v in recover.recover(app, env, provider_key).items():
        answers.setdefault(k, v)
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
            # Never rotate a recovered credential. The {{ vault_proxy_users }} ref
            # is also embedded in the core's MTA_OUT_DIRECT_PROXIES, and a mint on
            # a standalone run never flushed the core mirror. Re-buffer the on-disk
            # provider value so a full run writes it into the core vault;
            # write_vault skips the write when the value is already there.
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
            # This run's buffer holds the bearer only on a FRESH mint (the
            # shared-rule loop's "already decided" fast path re-injects a
            # `consumer_env_key`/`answer_key` only — mpa's rule has neither).
            # A REPLAY must fall back to mpa's own on-disk vault, or an
            # OVERRIDE (`write_vault(replace=True)`, no merge with the old
            # file) would drop `vault_mpa_auth_bearer` from the core vault
            # entirely, leaving SPAM_CONFIG's ref dangling.
            token = backend.component_secrets(provider_key).get("vault_mpa_auth_bearer")
            if token is None:
                mvp = paths.vault_path(app, env, provider_key)
                if mvp.exists():
                    token = vault.decrypt_to_dict(mvp).get("vault_mpa_auth_bearer")
            if token is not None:
                # mirror the bearer into the messages vault under the same vault_mpa_*
                # name so the {{ vault_mpa_auth_bearer }} ref in SPAM_CONFIG resolves there.
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

    messages does not use the django-lasuite generic AWS_S3_* default storage at all
    (those are not prompted for it) — STORAGE_MESSAGE_* is its only object storage.
    Secret keys route through the backend; the blobs encrypt key is generated
    (ansible-vault) or looked up (hashi) and embedded into the
    MESSAGES_BLOBS_ENCRYPT_KEYS JSON.

    The blobs-offload confirm's default is derived from the recovered
    ``MESSAGES_BLOBS_OFFLOAD_ENABLED`` flag — hardcoding ``default=False`` would
    silently drop a configured offload bucket on an Enter-through rebootstrap.
    When it is already enabled, the prompt text says so ("review its
    settings?") instead of asking as if offload were still off.
    ``MESSAGES_BLOBS_ENCRYPT_KEYS`` is recovered verbatim from the committed env
    blob (it can carry more than one encryption slot, e.g. after an operator
    rotated the key by hand) — mint the secret and compose the JSON only when
    it is not already in ``answers``, so a rebootstrap never overwrites a
    hand-added rotation slot with a freshly minted single-slot JSON.
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
    _ask_optional(
        answers,
        "STORAGE_MESSAGE_IMPORTS_REGION_NAME",
        "STORAGE_MESSAGE_IMPORTS_REGION_NAME (optional)",
    )
    answers["STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY"] = _ask(
        "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY",
        _recall(answers, "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
    )

    # --- blobs offload bucket (optional) ---
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
    _ask_optional(
        answers,
        "STORAGE_MESSAGE_BLOBS_REGION_NAME",
        "STORAGE_MESSAGE_BLOBS_REGION_NAME (optional)",
    )
    # MESSAGES_BLOBS_ENCRYPT_KEYS is recovered verbatim from the blob (it may
    # carry operator-added rotation slots) — mint the secret and compose the
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
    RELAY (external SMTP smarthost). Direct leaves MTA_OUT_MODE unset (the app
    default) and lets the socks-proxy dependency prompt handle egress; relay
    collects the smarthost host + optional credentials (password routed through
    the secret backend) and suppresses the socks-proxy prompt (see the deps loop).

    ``MTA_OUT_MODE`` is only ever recovered as ``"relay"`` (direct mode never
    sets it — see the ``return`` below): so the default is "direct" unless
    relay was recovered. "direct" is given explicitly (not left as "no
    default") so a silent replay can auto-accept it too; interactively it was
    already the first, pre-highlighted choice, so this changes nothing.

    Switching FROM relay TO direct here does not by itself clean up the
    committed tree: ``envblob.merge`` never deletes a line, so the relay
    settings stay in the ``st_messages_env`` blob until an operator removes
    them by hand. Warn about that instead of leaving a half-switched config
    that silently still says relay.
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
        placeholder="smtp.example.org:587",
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
        vault_hint = (
            " and the vault_mta_out_relay_password entry from vault.yml"
            if backend.prompts_values()
            else ""
        )
        ui.warn(
            "MTA_OUT_RELAY_PASSWORD cleared with the username: remove the "
            f"MTA_OUT_RELAY_PASSWORD= line from vars.yml{vault_hint} by hand."
        )


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
    meta,
    env: str,
    livekit_hosts: list[str],
    prior_livekit_hosts: list[str] | None = None,
) -> tuple[list[str], bool]:
    """meet/livekit: ask the egress hosts (blank ⇒ co-locate on the livekit hosts)
    right after the livekit hosts prompt (Q2 — BEFORE the LiveKit domain/TURN
    prompts) and decide the livekit↔egress redis topology up front so the redis
    prompt (if any) can be asked later, after the livekit cadvisor confirm.
    Returns (egress_hosts, valkey_enabled).

    The pre-fill is egress's own recovered hosts. When those equal
    ``prior_livekit_hosts``, the unit was co-located and the default stays
    blank: Enter then follows livekit to its CURRENT hosts through the ``or
    list(livekit_hosts)`` fallback, instead of pinning egress to a host
    livekit just moved away from. A genuinely standalone egress keeps its
    recovered pre-fill."""
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
    vault, raw under the same ``st_meet_livekit_*`` var names egress reuses.

    ansible-vault: read the live buffer first, then livekit's on-disk vault
    (a standalone ``-c egress`` run has no buffer). hashi: reuse livekit's
    decided lookup refs from ``lk_vars`` directly in ``ev`` — never prompt a
    fresh term. Both branches fail fast (``StCliError``) on a missing
    secret/ref; a silent skip would surface much later at deploy time as an
    undefined variable."""
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


def _livekit_vault_snapshot(meta, env) -> dict:
    """The on-disk livekit vault, decrypted, or ``{}`` when none exists yet — lets
    a caller check for an already-decided secret with no live buffer to read."""
    lvp = paths.vault_path(meta.app, env, "livekit")
    return vault.decrypt_to_dict(lvp) if lvp.exists() else {}


def _resolve_egress_redis_password(meta, env, backend, reuse_disk: bool) -> str:
    """Never re-prompt a decided secret: the live buffer wins first, then the
    on-disk livekit vault, and only then a fresh prompt.

    ``reuse_disk`` is False when the operator typed a new redis address — the
    old server's password must not silently follow it. An empty stored value
    (an old blank-auth store) counts as absent, so the prompt stays reachable.
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
        # ONLY when the recovered topology was already external — a fresh or
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
            # "leave blank if none" is an explicit no-auth answer — drop the
            # tool-owned key instead of keeping a stale username line.
            lk_pvars.pop("st_meet_livekit_redis_username", None)
        if backend.prompts_values():
            # a blank password stores + mirrors nothing (an unauthenticated
            # external redis); a decided one re-stores unchanged — write_vault's
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
        # Only stamp the header when the file has no start comment already — a
        # rebootstrap over an existing header must not stack a duplicate one
        # (mirrors write_core's same guard).
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
    """Adopt livekit's decided domain + redis topology for a standalone
    ``bootstrap -c egress`` run; the generic dep tail then writes the unit.

    This never re-prompts the topology — egress must share the redis the
    livekit unit was bootstrapped with. The redis password is mirrored only
    when the livekit unit uses an external redis AND a password was decided;
    an unauthenticated external redis stays adoptable with no password.
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

    A FRESH provider (no committed ``vars.yml``) keeps today's menu: "Yes —
    bootstrap now" (omitted under ``wire_only``), "No — bootstrap later"
    (returns "skip" — registers no unit), and "Already deployed (enter URL +
    keys)" (external). A skipped dependency registers no unit and an external
    one is intercepted below, so no recorded mode can reach this fresh menu.

    In a :func:`~st_cli.core.prompts.silent_replay`, a fresh dependency with no
    matching ``offer`` (see ``core/upgrades.new_component_offers``) is a
    long-ago-declined optional dep — it skips quietly, with no select at all
    (``fresh_silent`` below). A fresh dependency WITH an offer prints it, then
    runs the normal fresh menu inside :func:`~st_cli.core.prompts.suspend_silent`
    (a fresh provider has nothing recovered, so its questionnaire must really
    ask, and the answers it produces are a new component, not a "new setting"
    of the replayed unit — they must not count in the silent-replay stats).

    A unit recorded ``external`` in ``.st-cli.yml`` (``recorded_external``)
    wins over whatever the tree looks like — checked BEFORE ``has_existing``,
    so a leftover local tree from before the unit was marked external can
    never smuggle it into the reuse/modify or fresh-provider branches below.
    Under ``wire_only`` it takes ``"external"`` with no select at all
    (core-only runs never deploy a provider). Otherwise it offers a 3-option
    select: "Keep external (recorded)" (default; skips every already-answered
    prompt, see the external branch below), "Re-enter external values (URL +
    keys)" (re-asks all of them), or "Bootstrap now (manage locally)"
    (deploys — same as ``choice = "deploy"``).

    An EXISTING, non-external provider (``vars.yml`` already committed) never
    offers "skip"/"external" any more — those would abandon or disown a unit
    that is already live. It offers at most a plain reuse/modify choice, and
    sometimes no choice at all:

    * ``override_core`` (the CORE run is an ``OVERRIDE``) forces a replay —
      no select, straight to the deploy branch, regardless of ``wire_only`` —
      before either check below. An Override rebuilds the core from an empty
      tree, so a "Reuse" here would silently drop the wiring a fresh core
      buffer needs (the consumer-side secrets/refs a plain reuse only
      re-injects for a ``shared`` rule with a ``consumer_env_key``, missing
      e.g. messages' ``SPAM_CONFIG``/``MTA_OUT_DIRECT_PROXIES``, which
      ``_ask_messages_provider`` only constructs on this deploy branch). The
      provider's OWN committed tree is untouched by a core override, so its
      recovered secrets are re-injected here, never rotated.
    * ``flagged`` (this component has an outstanding rebootstrap need, see
      ``core/upgrades.needed``) forces a replay — no select, straight to
      the deploy branch — UNLESS ``wire_only``, which can never deploy a
      provider; there it only warns that the flag stays pending. This closes
      the bug the whole rework exists for: the old "Reuse" choice replayed no
      provider questionnaire yet still restamped the unit, silently clearing
      the flag.
    * Otherwise (unflagged, or ``wire_only``): "Reuse existing in the repo" /
      "Modify (replay the questionnaire)", defaulting to Reuse. Under
      ``wire_only`` even that pair is moot — "Modify" would deploy a provider
      in a core-only run — so the select is skipped and "reuse" is taken
      directly.

    With ``assume_deploy=True`` (direct provider-target bootstrap, e.g.
    ``bootstrap -c livekit``) every select above is skipped and
    ``choice = "deploy"`` is assumed — the user explicitly asked to bootstrap
    that provider. This is the one path where the deploy branch's own
    rebootstrap machinery actually matters (there is no "Reuse" fallback to
    fall back on): hosts, cadvisor, and every ``shared`` rule with a ``var``
    are pre-filled/recovered via :func:`core.recover.recover_hosts`/
    :func:`_cadvisor_default`/:func:`core.recover.recover_shared`.

    Restamping a reused unit (the deps loop, unchanged) is harmless now:
    "reuse" is only reachable when the unit is unflagged, so there is no
    pending need left to clear.
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
    # upgrades.needed skips units with mode == "external", so `flagged`
    # never contains one. No flag/external interaction exists to handle here.
    recorded_external = unit is not None and unit.mode == "external"
    # A fresh dependency reached during a silent replay: nothing recovered for
    # it, so it needs its own menu handling below rather than falling into the
    # ordinary fresh-provider select (which would ask a question the operator
    # never opted into during an unattended upgrade).
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
            choice = "external"
        else:
            external_menu = {
                "Keep external (recorded)": "external",
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
        # A long-ago-declined optional dep (or one never offered at all) stays
        # quiet on every silent replay — nagging about it forever is exactly
        # what the offer mechanism (core/upgrades.new_component_offers) exists
        # to avoid.
        ui.info(
            f"{dep.on}: not bootstrapped — skipped (add it with "
            f"`st-cli bootstrap {meta.app} {env} -c {dep.on}`)."
        )
        return "skip"
    else:
        if fresh_silent:
            # offer is not None here (the branch above catches offer is None).
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
        options["Already deployed (enter URL + keys)"] = "external"
        # suspend_silent no-ops outside silent mode; inside it, a fresh menu
        # for a genuinely new component must ask, not auto-accept.
        with suspend_silent():
            choice = options[_ask_select(f"Bootstrap {dep.on} now?", list(options))]

    # A fresh dependency reached during a silent replay has nothing recovered
    # for it, so its whole post-menu handling (skip/external/deploy) must ask
    # for real, not auto-accept a Recovered default meant for the outer
    # replay's own recovered answers.
    with suspend_silent() if fresh_silent else nullcontext():
        if choice == "skip":
            ui.info(
                f"{dep.on}: bootstrap later — add it with "
                f"`st-cli bootstrap {meta.app} {env} -c {dep.on}`."
            )
            return "skip"

        if choice in ("external", "external-redo"):
            # "external" (kept/fresh) skips a rule already recovered into `answers`
            # — re-prompting it would rotate a secret or clobber a committed value.
            # "external-redo" re-asks every rule regardless (the operator chose to
            # retype the external endpoint).
            only_missing = choice == "external"
            for rule in dep.shared:
                key = rule.get("consumer_env_key")
                if not key:
                    continue
                # Truthy check: a prior "bootstrap later" run commits the consumer
                # keys as empty lines, and recover() brings them back as "" — an
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
                consumer_key = rule.get("consumer_env_key")
                if not consumer_key:
                    continue
                # Already recovered (mirrors the external branch's only-missing
                # check): re-injecting a secret rule would be a byte no-op under
                # ansible-vault (write_vault's merge already no-ops on it), but
                # under hashi_vault env_secret ALWAYS prompts a fresh lookup term —
                # skipping here is what keeps a silent replay from asking one.
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
            egress_hosts, valkey_enabled = _ask_egress_hosts(
                meta, env, hosts, existing_hosts
            )  # Q2
        # Merge, not replace (mirrors write_core's rationale): loading the existing
        # vars.yml means a hand-edited/custom key on this provider survives, and a
        # shared-rule var recovered below (never re-set) is simply left as-is.
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
                    if backend.prompts_values():
                        backend.env_secret(
                            answers, consumer_key, component=core.key, value=recovered
                        )
                    else:
                        # hashi_vault: `recovered` is the committed rendered lookup
                        # ref from the provider's vars.yml. Reuse it verbatim — it
                        # reproduces what shared_provider_secret wrote on the first
                        # run. env_secret ignores `value` and would re-prompt a
                        # fresh term, repointing the committed ref.
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
                    # prompted secret — only prompt the value in ansible-vault mode
                    # (hashi_vault mode prompts a lookup term in var_secret/env_secret).
                    value = _prompt_shared(rule) if backend.prompts_values() else None
            else:
                # non-secret: unlike a secret, this DOES get re-asked every time —
                # just pre-filled from the recovered value (or the answer_key
                # fallback for a rule with no `var`, e.g. drive's collabora
                # domain) so accepting it is a no-op and editing it still works.
                default = (
                    Recovered(str(recovered))
                    if recovered is not None
                    else _shared_default(answers, rule)
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
            _lk_domain = recover.recover(meta.app, env, provider.key).get("DOMAIN", "")
            _ensure_meet_domain(answers, Recovered(_lk_domain) if _lk_domain else "")
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
            _bundle_egress(
                meta, pvars, answers, backend, env, egress_hosts, valkey_enabled
            )
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


def _ask_rebootstrap_action(
    app: str,
    env: str,
    flagged: dict[str, UpgradeNeed],
    allow_override: bool = True,
) -> ReplayAction:
    """Print every pending rebootstrap flag for ``(app, env)``, then offer the
    3-way Modify / Reuse / Override select (Modify is the default).

    Prints ALL of ``flagged`` (already scoped to this ``(app, env)`` by the
    caller), not only the run's own targeted component(s) — a dependency
    provider's pending flag must be visible here too, so the operator sees it
    BEFORE picking Reuse (which leaves every unit, core and dependencies
    alike, exactly as-is).

    ``allow_override=False`` (a wire-only ``-c <core>`` run) drops the
    Override choice: an Override must force-replay every kept provider to
    rebuild the core-side wiring, and a wire-only run by contract never
    touches a provider.
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
    """Hard destructive gate for :attr:`ReplayAction.OVERRIDE`; raises on decline.

    Names every consequence up front — OVERRIDE rebuilds the core from an
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

    ``replay`` picks what happens when the targeted unit already exists.
    ``ASK`` (the CLI default) offers a 3-way Modify / Reuse / Override select;
    the other members are the programmatic entry point used by ``st-cli
    upgrade`` and by tests to skip that select. See :class:`ReplayAction`.

    **Rebootstrap.** Whether the core (or the single targeted component) ALREADY
    has a committed ``vars.yml`` is detected up front (``core_exists`` /
    ``has_existing`` inside ``_handle_dependency``) and drives three things,
    every one of them BEFORE any prompt is shown:

    1. ``writer.ensure_vault_readable`` is called against every unit already
       registered for ``(app, env)`` — an unreadable ``vault.yml`` aborts here,
       not 40 questions into the questionnaire.
    2. The pre-questionnaire intro + readiness gate (``_print_bootstrap_intro``)
       is replaced by the 3-way select (``_ask_rebootstrap_action``) or, for a
       non-``ASK`` replay, straight to the matching notice.
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
    # Computed early (no backend/manifest dependency) so both the 3-way
    # select gating below and the deps-loop scoping further down share one
    # definition.
    target_core = component in (None, core.key)

    m = _ensure_manifest()
    # Pending rebootstrap flags, newest per component only — mirrors
    # drift.check_app's pick-newest loop (kept local here rather than shared,
    # so this module doesn't reach into drift.py). Drives _handle_dependency's
    # forced-replay branch below: a flagged existing provider must not offer
    # "Reuse" (that would restamp it without ever replaying its questionnaire,
    # silently clearing the pending flag).
    flagged: dict[str, UpgradeNeed] = {}
    for need in upgrades.needed(m, app, env):
        current = flagged.get(need.component)
        if current is None or upgrades.parse_version(
            need.version
        ) > upgrades.parse_version(current.version):
            flagged[need.component] = need

    # Newly declared components a flag makes available for this (app, env) —
    # only matters on the SILENT path (_handle_dependency's fresh_silent
    # branch), but harmless (and cheap) to compute unconditionally.
    offers_by_component: dict[str, NewComponentOffer] = {
        o.component: o for o in upgrades.new_component_offers(m, app, env)
    }

    # A rebootstrap is detected purely from what's already committed: the
    # core's vars.yml existing means this run replays the questionnaire with
    # every answer pre-filled instead of starting fresh.
    core_exists = paths.vars_path(app, env, core.key).exists()
    is_rebootstrap = core_exists and (component is None or component in core_or_worker)

    # SILENT recovers its answers from a committed unit — there must be one.
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

    # Fail fast: an unreadable vault.yml must abort BEFORE the (potentially
    # 40+ question) questionnaire runs, not partway through it. Checked
    # against every unit already registered for this (app, env) regardless of
    # which ones this particular invocation will touch — ensure_vault_readable
    # is a no-op for a component with no vault.yml (fresh unit, hashi_vault).
    writer.ensure_vault_readable(
        app, env, [u.component for u in manifest.units_for(m, app, env)]
    )

    # Pre-questionnaire guidance for a full/core/workers bootstrap: an
    # architecture-docs pointer + a requirements checklist on a fresh unit;
    # the 3-way Modify/Reuse/Override select (or a silent-replay notice) when
    # the CORE already exists — a workers-only run gets a plain MODIFY/SILENT
    # note instead (see `target_core` below). Provider-only runs
    # (`-c <provider>`) skip all of it, except a programmatic SILENT replay
    # (`st-cli upgrade`'s per-component call on a provider-only repo).
    action = ReplayAction.MODIFY
    if component is None or component in core_or_worker:
        if not is_rebootstrap:
            _print_bootstrap_intro(meta)
        elif not target_core:
            # workers-only (`-c <workers>`): no 3-way select. Workers own no
            # files of their own (CLAUDE.md "Workers own no files" — they
            # only flip a flag in the CORE's committed vars.yml), so an
            # Override here would confirm destructively but destroy nothing,
            # and a Reuse would return before ever registering the unit.
            # REUSE/OVERRIDE stay core-only; keep today's plain MODIFY
            # replay (SILENT still applies — `st-cli upgrade` targets a
            # flagged workers unit the same way as a provider).
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
            # A wire-only run (`-c <core>`) never touches a provider, so it
            # cannot force-replay the kept providers that rebuild the
            # core-side wiring (constructed values, mirrored vault keys) —
            # an Override there would silently drop them.
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
                # No manifest write, no upsert_unit — the stamp cannot move, so
                # a pending flag stays pending (structurally, not by
                # convention). Warns for EVERY flagged component of (app,
                # env), not only the targeted ones — Reuse leaves core AND
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

    ui.note(
        "This questionnaire only scaffolds your config files.\n"
        "If you mistype an answer, don't start over: finish "
        "the questionnaire, then edit the generated files directly under "
        "<app>/<env>/<component>/."
    )
    # Choose the secret backend (ansible-vault | hashi_vault) per (app, env).
    # The choice is persisted into .st-cli.yml; connection details for
    # hashi_vault go into <app>/<env>/common.yml.
    #
    # SILENT wraps setup through the manifest save: core/prompts.py's
    # primitives auto-accept a recovered default inside this context (a
    # rerun's setup_backend/vault-password calls are themselves no-ops, since
    # both are already persisted by the time a silent replay is reachable).
    override_core = action is ReplayAction.OVERRIDE
    ctx = silent_replay() if action is ReplayAction.SILENT else nullcontext()
    with ctx as silent_stats:
        backend = setup_backend(m, app, env)
        if backend.kind == "ansible-vault":
            vault.ensure_vault_password(create=True)
        tree.ensure_common(app, env)
        tree.ensure_ssh_scaffold()

        ui.info(f"Bootstrapping {app}/{env}.")

        # Scope flags — gate the sections below so the no-flag path is unchanged.
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
        # recover), a rebootstrap (every prompt pre-filled from
        # `recover.recover`), or an OVERRIDE (no seed at all: `write_core`
        # rebuilds vars.yml/vault.yml from an empty tree).
        if target_core:
            recoverable = core_exists and not override_core
            seed = recover.recover(app, env, core.key) if recoverable else {}
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
            # keycloak is not a Django app — it takes its own (raw-env) questionnaire.
            answers = (
                _ask_keycloak(meta, backend, seed)
                if app == "keycloak"
                else _ask_core(meta, backend, seed)
            )
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
                continue  # egress is bundled into the livekit step, not a separate iteration
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

    if silent_stats is not None:
        if silent_stats.asked:
            ui.info(
                f"Kept {silent_stats.auto} recovered answer(s); asked "
                f"{silent_stats.asked} new question(s)."
            )
        else:
            ui.info(f"Kept {silent_stats.auto} recovered answer(s); no new questions.")

    _print_summary(app, env, answers, units, component)
