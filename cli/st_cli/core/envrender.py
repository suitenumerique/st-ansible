"""Render per-component env blobs from Jinja2 templates + bootstrap answers.

Public surface (CONTRACT section 3):

* :func:`render_env` — ``render_env(app, component, answers) -> {blob_var: text}``
* :func:`oidc_endpoints` — derive the ``OIDC_OP_*`` endpoint dict for a provider.

Templates live under ``st_cli/core/resources/templates/env`` (bundled with the wheel).
Backend overlays ``{% include "base.django.env.j2" %}``; frontend overlays do
not. ``answers`` is the dict produced by bootstrap; missing keys render as the
empty string so partial answers never blow up :func:`render_env`.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Undefined, select_autoescape

from .appmeta import load_app

_TEMPLATES_DIR = Path(__file__).resolve().parent / "resources" / "templates" / "env"

_OIDC_ENDPOINT_KEYS = (
    "OIDC_OP_JWKS_ENDPOINT",
    "OIDC_OP_AUTHORIZATION_ENDPOINT",
    "OIDC_OP_TOKEN_ENDPOINT",
    "OIDC_OP_USER_ENDPOINT",
    "OIDC_OP_LOGOUT_ENDPOINT",
    "OIDC_OP_INTROSPECTION_ENDPOINT",
    "OIDC_OP_URL",
)


class _EmptyUndefined(Undefined):
    """Undefined that renders as ``""`` and is falsy — keeps templates tolerant."""

    def __str__(self) -> str:
        return ""

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return False


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(disabled_extensions=("j2", "env.j2")),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=_EmptyUndefined,
    )


def render_env(app: str, component: str, answers: dict) -> dict[str, str]:
    """Render the env blobs for ``(app, component)`` using ``answers``.

    Returns ``{blob_var_name: rendered_text}``. For components without an
    ``env_render`` spec (e.g. livekit, collabora, mta-in, mpa, socks-proxy)
    returns ``{}``.
    """
    spec = load_app(app).env_render_spec(component)
    if not spec:
        return {}

    env = _environment()
    ctx = {"answers": dict(answers or {}), "oidc_endpoints": oidc_endpoints}

    out: dict[str, str] = {}
    for _layer, info in spec.items():
        blob_var = info["blob_var"]
        templates = info.get("templates") or []
        parts: list[str] = []
        for name in templates:
            text = env.get_template(name).render(**ctx)
            parts.append(text)
            if not text.endswith("\n"):
                parts.append("\n")
        out[blob_var] = "".join(parts).rstrip("\n") + "\n"
    return out


def _endpoints(issuer: str, oidc_base: str) -> dict[str, str]:
    return {
        "OIDC_OP_URL": issuer,
        "OIDC_OP_JWKS_ENDPOINT": f"{oidc_base}/certs",
        "OIDC_OP_AUTHORIZATION_ENDPOINT": f"{oidc_base}/auth",
        "OIDC_OP_TOKEN_ENDPOINT": f"{oidc_base}/token",
        "OIDC_OP_USER_ENDPOINT": f"{oidc_base}/userinfo",
        "OIDC_OP_LOGOUT_ENDPOINT": f"{oidc_base}/logout",
        "OIDC_OP_INTROSPECTION_ENDPOINT": f"{oidc_base}/token/introspect",
    }


# ProConnect (DINUM) issuer bases, from each environment's
# /.well-known/openid-configuration (fetched 2026-07).
_PROCONNECT_BASES = {
    "proconnect-prod": "https://auth.agentconnect.gouv.fr/api/v2",
    "proconnect-integ": "https://fca.integ01.dev-agentconnect.fr/api/v2",
}


def _proconnect_endpoints(provider: str) -> dict[str, str]:
    """Bundled ProConnect endpoints (paths differ from Keycloak)."""
    base = _PROCONNECT_BASES[provider]
    return {
        "OIDC_OP_URL": base,
        "OIDC_OP_JWKS_ENDPOINT": f"{base}/jwks",
        "OIDC_OP_AUTHORIZATION_ENDPOINT": f"{base}/authorize",
        "OIDC_OP_TOKEN_ENDPOINT": f"{base}/token",
        "OIDC_OP_USER_ENDPOINT": f"{base}/userinfo",
        "OIDC_OP_LOGOUT_ENDPOINT": f"{base}/session/end",
        "OIDC_OP_INTROSPECTION_ENDPOINT": f"{base}/token/introspection",
    }


def oidc_endpoints(
    provider: str, base_url: str | None, realm: str | None
) -> dict[str, str]:
    """Return the ``OIDC_OP_*`` endpoint dict for ``provider``.

    ``provider`` ∈ {"keycloak","proconnect-prod","proconnect-integ","custom"}.

    * keycloak: derive ``/realms/<realm>/protocol/openid-connect/...`` from
      ``base_url`` + ``realm``.
    * proconnect-prod / proconnect-integ: bundled ProConnect endpoints from each
      environment's ``/.well-known/openid-configuration``.
    * custom: pass through — endpoints are expected to already be supplied by
      the user; only ``OIDC_OP_URL`` is echoed when ``base_url`` is given.
    """
    if provider == "keycloak":
        if not base_url or not realm:
            return {key: "" for key in _OIDC_ENDPOINT_KEYS}
        issuer = f"{base_url.rstrip('/')}/realms/{realm}"
        return _endpoints(issuer, f"{issuer}/protocol/openid-connect")

    if provider in ("proconnect-prod", "proconnect-integ"):
        return _proconnect_endpoints(provider)

    if provider == "custom":
        out = {key: "" for key in _OIDC_ENDPOINT_KEYS}
        if base_url:
            out["OIDC_OP_URL"] = base_url.rstrip("/")
        return out

    return {key: "" for key in _OIDC_ENDPOINT_KEYS}
