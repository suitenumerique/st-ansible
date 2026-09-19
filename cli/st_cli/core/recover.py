"""Reconstruct a bootstrap ``answers`` dict from an already-committed unit,
the inverse of ``envrender.render_env`` and ``writer.apply_component_vars``.

This module never branches on the app name; app-specific logic stays in the
questionnaire. It never strips, resolves, or normalises a recovered value,
and every function is best-effort.
"""

from __future__ import annotations

import contextlib
import re

from . import appmeta, envblob, envrender, paths, tree, vault, writer
from .errors import StCliError

# Matches a component-var template that is exactly one {PLACEHOLDER}, with nothing
# before or after it. Only such a template has a reliable single-answer inverse.
_PLACEHOLDER_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# The inverse of the keycloak issuer built by envrender.oidc_endpoints:
# f"{base_url.rstrip('/')}/realms/{realm}/protocol/openid-connect/certs".
_KEYCLOAK_JWKS_RE = re.compile(
    r"^(?P<base>.+)/realms/(?P<realm>[^/]+)/protocol/openid-connect/certs$"
)


def recover(app: str, env: str, component: str) -> dict:
    r"""Rebuild the bootstrap ``answers`` dict for an already-committed unit.

    Reads the env blob, then fills gaps by inverting ``component_vars`` templates
    that are exactly one placeholder (or, line by line, a dotenv-shaped block).
    A blob-parsed value always wins over an inverted one for the same key.
    """
    try:
        meta = appmeta.load_app(app)
    except StCliError:
        return {}

    data = tree.load_vars(app, env, component)
    if not data:
        return {}

    answers: dict = {}

    for info in meta.env_render_spec(component).values():
        blob_var = info.get("blob_var")
        if not blob_var:
            continue
        text = data.get(blob_var)
        if isinstance(text, str):
            answers.update(envblob.parse(text))

    for name, tmpl in meta.component_vars(component).items():
        tmpl = str(tmpl)
        m = _PLACEHOLDER_RE.match(tmpl)
        if m:
            placeholder = m.group(1)
            if placeholder in answers:
                continue  # the blob already supplied it, so the blob wins
            if name not in data:
                continue
            answers[placeholder] = str(data[name])
            continue
        if name not in data or not isinstance(data[name], str):
            continue
        # A dotenv-shaped component var is a literal block, not one placeholder.
        # Invert it line by line instead.
        tmpl_lines = envblob.parse(tmpl)
        committed_lines = envblob.parse(data[name])
        for key, value in tmpl_lines.items():
            pm = _PLACEHOLDER_RE.match(value)
            if not pm:
                continue  # embedded text has no reliable single-answer inverse
            placeholder = pm.group(1)
            if placeholder in answers:
                continue  # the blob already supplied it, so the blob wins
            if key not in committed_lines:
                continue
            answers[placeholder] = committed_lines[key]

    return answers


def parse_bool(value: object) -> bool | None:
    """Parse a YAML bool or a "true"/"yes"/"on"/"1"-shaped string, any case.

    Returns None for anything else.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
    return None


def recover_cadvisor(app: str, env: str, component: str) -> bool | None:
    """Recover the per-app cadvisor toggle (``st_<app>_cadvisor_enabled``).

    Returns ``None`` when the unit, the var, or its value is unrecognised, so the
    caller can fall back to bootstrap's first-run default instead of assuming ``False``.
    """
    data = tree.load_vars(app, env, component)
    if not data:
        return None
    return parse_bool(data.get(writer.cadvisor_var(app)))


def recover_hosts(app: str, env: str, component: str) -> list[str]:
    """Recover a unit's committed host IPs (``[]`` if it has none, or doesn't exist)."""
    try:
        return tree.read_hosts(app, env, component)
    except Exception:
        return []


def recover_oidc(answers: dict) -> tuple[str | None, str | None, str | None]:
    """Infer ``(provider, base_url, realm)`` from already-recovered OIDC answers.

    Inverts ``envrender.oidc_endpoints``, since the provider choice itself is
    never stored: the committed ``OIDC_OP_*`` endpoints are the provider choice.
    """
    proconnect_by_base = {v: k for k, v in envrender._PROCONNECT_BASES.items()}
    for key, value in answers.items():
        if key.startswith("OIDC_OP_") and value in proconnect_by_base:
            return proconnect_by_base[value], None, None

    jwks = answers.get("OIDC_OP_JWKS_ENDPOINT")
    if isinstance(jwks, str):
        m = _KEYCLOAK_JWKS_RE.match(jwks)
        if m:
            return "keycloak", m.group("base"), m.group("realm")

    if any(k.startswith("OIDC_OP_") for k in answers):
        return "custom", (answers.get("OIDC_OP_URL") or None), None

    return None, None, None


def recover_shared(
    app: str, env: str, provider_component: str, shared: list[dict]
) -> dict[str, str]:
    """Recover ``{rule["var"]: current_value}`` for a dependency's ``shared`` rules.

    Reads both the provider's ``vars.yml`` and, if present, its decrypted
    ``vault.yml``, since a ``generate:``-backed rule with no ``vault_key`` stores
    its raw value only in the vault. Recovering it avoids rotating a live secret.
    """
    data: dict = {}
    with contextlib.suppress(Exception):
        data.update(tree.load_vars(app, env, provider_component))

    vpath = paths.vault_path(app, env, provider_component)
    if vpath.exists():
        with contextlib.suppress(StCliError):
            data.update(vault.decrypt_to_dict(vpath))

    out: dict[str, str] = {}
    for rule in shared:
        var = rule.get("var")
        if var and var in data:
            out[var] = str(data[var])
    return out
