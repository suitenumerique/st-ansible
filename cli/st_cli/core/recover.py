"""Reconstruct a bootstrap ``answers`` dict from an already-committed unit.

The "rebootstrap" flow re-runs the interactive questionnaire over an existing
unit with every answer pre-filled from the tree, so pressing Enter through
the whole thing reproduces the current config byte-for-byte. This module is
the inverse of :func:`core.envrender.render_env` (answers -> env blob) and
:func:`core.writer.apply_component_vars` (answers -> ``{DOMAIN}``-style
component vars): where those render ``answers`` INTO the tree, this module
reads it back OUT.

The key insight making the inversion possible: ``answers`` and the env blob
are 1:1 by construction — ``answers["DB_HOST"]`` is emitted verbatim as
``DB_HOST={{ answers.DB_HOST }}`` by the Jinja templates in
``resources/templates/env``. Parsing ``KEY=value`` back out of the blob
(:mod:`core.envblob`) therefore reconstructs the exact value the
questionnaire held for that key — including a raw ``{{ vault_x }}`` /
``{{ lookup(...) }}`` Jinja ref, which must never be resolved, stripped or
otherwise normalised: recovering it unchanged is what lets a rebootstrap skip
re-prompting secrets and leave ``vault.yml`` untouched. **Never strip,
unquote, resolve or normalise a recovered value anywhere in this module.**

This module is deliberately **generic** — no ``if app == "..."`` branches.
App-specific questionnaire logic (e.g. recomposing drive's S3 endpoint prompt
from the recovered ``S3_PROTOCOL``/``S3_HOST`` pair) belongs to the
questionnaire itself, not here; this module only surfaces the raw recovered
material for it to compose.

Every function here is **best-effort**: a missing unit, an unknown app/
component, an absent key, or a vault that cannot be decrypted all degrade to
an empty/``None``/partial result rather than raising. A partial recovery
just means fewer pre-filled answers — the questionnaire falls back to
prompting (or its first-run default) for whatever could not be recovered.
Nothing in this module raises :class:`~.errors.StCliError` of its own; where
a lower-level helper can (``appmeta.load_app`` on an unknown app,
``vault.decrypt_to_dict`` on a missing password/binary), it is caught here.
"""

from __future__ import annotations

import re

from . import appmeta, envblob, envrender, paths, tree, vault, writer
from .errors import StCliError

# Matches a component-var template that is EXACTLY one ``{PLACEHOLDER}`` —
# nothing before or after it. Anything else (embedded text, several
# placeholders, none at all — e.g. meet/drive's quadrupled-brace
# ``..._run_migrations`` Jinja expression) has no reliable single-answer
# inverse and is skipped by :func:`recover`.
_PLACEHOLDER_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# The exact inverse of how core.envrender.oidc_endpoints builds a keycloak
# issuer: issuer = f"{base_url.rstrip('/')}/realms/{realm}", then
# f"{issuer}/protocol/openid-connect/certs" for the JWKS endpoint.
_KEYCLOAK_JWKS_RE = re.compile(
    r"^(?P<base>.+)/realms/(?P<realm>[^/]+)/protocol/openid-connect/certs$"
)


def recover(app: str, env: str, component: str) -> dict:
    r"""Rebuild the bootstrap ``answers`` dict for an already-committed unit.

    Returns ``{}`` when the unit does not exist (no committed ``vars.yml``,
    or ``app`` is unknown). Otherwise combines two sources:

    1. **Env blobs** (authoritative — see the precedence rule below). For
       each ``env_render`` layer declared for ``component``
       (:meth:`appmeta.AppMeta.env_render_spec`), the corresponding
       ``blob_var`` is read from the unit's ``vars.yml``
       (:func:`core.tree.load_vars`) and, when present and string-valued,
       parsed with :func:`core.envblob.parse`. A component with no
       ``env_render`` spec at all (a provider unit like livekit/collabora/
       mta-in/mpa/socks-proxy) contributes nothing here — that is normal,
       not an error. Multiple layers (e.g. meet's ``backend`` + ``caddy``)
       are merged into the same dict.

    2. **The component-var inversion** (fallback — fills gaps the blob
       cannot). :meth:`appmeta.AppMeta.component_vars` gives
       ``{name: tmpl}`` pairs whose ``tmpl`` may reference a questionnaire
       answer via ``str.format`` (``writer.apply_component_vars`` is the
       forward direction). When ``tmpl`` is *exactly* one placeholder
       (``^\{[A-Za-z_]\w*\}$``, e.g. ``"{DOMAIN}"``) and ``name`` has a
       committed value, the placeholder is recovered as ``str(value)``. This
       is how ``DOMAIN`` and drive's ``S3_PROTOCOL``/``S3_HOST``/``S3_BUCKET``
       come back — none of those are literal ``KEY=value`` lines in any env
       blob, so the blob alone can never recover them. A template with
       embedded text, more than one placeholder, or none at all (e.g. meet's
       ``st_meet_backend_run_migrations``, whose quadrupled braces are a
       literal Jinja expression baked in by the manifest, not a
       questionnaire answer) is **skipped outright** — there is no reliable
       inverse for those, and guessing would silently corrupt the recovered
       answers.

    **Precedence rule**: a blob-parsed value always wins; the inversion step
    never overwrites a placeholder the blob already supplied. The blob is
    the literal, verbatim record of what was fed to the Jinja renderer
    (ground truth — :func:`envrender.render_env` is the actual consumer of
    ``answers``), whereas the inversion is a structural heuristic over
    ``str.format`` templates. In practice the two never compete for the same
    key (``DOMAIN``/``S3_*`` are never emitted as env blob ``KEY=`` lines),
    but if a unit was ever hand-edited into disagreement, trusting the blob
    is the safer default since it is what the next render will actually see.
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
        m = _PLACEHOLDER_RE.match(str(tmpl))
        if not m:
            continue
        placeholder = m.group(1)
        if placeholder in answers:
            continue  # blob already supplied it — blob wins, see docstring
        if name not in data:
            continue
        answers[placeholder] = str(data[name])

    return answers


def recover_cadvisor(app: str, env: str, component: str) -> bool | None:
    """Recover the per-app cadvisor toggle (``st_<app>_cadvisor_enabled``).

    Returns ``None`` when the unit or the var itself is absent, so the
    caller can fall back to bootstrap's first-run default instead of
    silently assuming ``False``. Handles both a real YAML bool and a value
    that round-tripped as a string (``"true"``/``"false"``, any case,
    ``yes``/``no``/``on``/``off``/``1``/``0``) — ruamel preserves whatever
    scalar style is on disk, and a hand-edited ``vars.yml`` may have quoted
    it. An unrecognised string value also degrades to ``None``.
    """
    data = tree.load_vars(app, env, component)
    if not data:
        return None
    val = data.get(writer.cadvisor_var(app))
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
    return None


def recover_hosts(app: str, env: str, component: str) -> list[str]:
    """Recover a unit's committed host IPs (``[]`` if it has none, or doesn't exist).

    Thin, defensive wrapper over :func:`core.tree.read_hosts` — that function
    already returns ``[]`` for a missing ``hosts`` file, but recovery must
    never raise regardless of how a future hosts-format change might affect
    parsing, so any exception here is swallowed the same way.
    """
    try:
        return tree.read_hosts(app, env, component)
    except Exception:
        return []


def recover_oidc(answers: dict) -> tuple[str | None, str | None, str | None]:
    """Infer ``(provider, base_url, realm)`` from already-recovered OIDC answers.

    Lets the OIDC provider select (and its base-url/realm follow-ups) be
    pre-filled on a rebootstrap without the provider choice itself ever
    being stored anywhere in the tree — the committed ``OIDC_OP_*`` endpoints
    ARE the provider choice; this just inverts
    :func:`core.envrender.oidc_endpoints`. Inference order (first match wins):

    1. Any ``OIDC_OP_*`` value equal to one of the bundled ProConnect bases
       (:data:`core.envrender._PROCONNECT_BASES`) -> that provider key
       (``"proconnect-prod"``/``"proconnect-integ"``),
       ``(provider, None, None)``.
    2. ``OIDC_OP_JWKS_ENDPOINT`` matching
       ``^(?P<base>.+)/realms/(?P<realm>[^/]+)/protocol/openid-connect/certs$``
       -> ``("keycloak", base, realm)``. This is the exact inverse of how
       ``oidc_endpoints`` builds a keycloak issuer (see :data:`_KEYCLOAK_JWKS_RE`).
    3. Any ``OIDC_OP_*`` key present at all -> ``("custom", answers.get(
       "OIDC_OP_URL") or None, None)`` — pass-through, matching how
       ``oidc_endpoints`` treats ``"custom"``.
    4. Otherwise -> ``(None, None, None)`` — no pre-selection; the
       questionnaire prompts fresh.
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
    """Recover current values for a dependency's ``shared`` rules.

    ``shared`` is one dependency's rule list from the app manifest (see
    :class:`core.appmeta.Dependency`). Returns ``{rule["var"]: current_value}``
    for every rule that declares a ``var`` AND has a committed value for it;
    a rule without a ``var`` (a pure consumer-side prompt, e.g. drive's
    Collabora-domain rule) contributes nothing.

    **This is the module's sharpest edge.** A ``generate: token``/``generate:
    secret`` rule (e.g. the LiveKit API key/secret) mints a brand-new value
    on every fresh bootstrap. If a rebootstrap regenerated it instead of
    recovering the existing one, every consumer already wired to the OLD
    value would break — and worse, it would silently rotate a live
    credential out from under the operator. Recovering ``rule["var"]`` here
    is what lets the rebootstrap flow skip ``writer.gen_value``/
    ``backend.var_secret`` entirely for a value that was already decided.

    Where the current value actually lives depends on how
    ``SecretBackend.var_secret`` originally stored it, which in turn depends
    on whether the rule declares a ``vault_key``:

    * rule has ``vault_key`` (e.g. messages' mpa rules) -> the provider's
      ``vars.yml`` carries ``{var}: "{{ vault_key }}"`` — a Jinja ref,
      recovered verbatim straight from ``vars.yml``, no decryption needed.
    * rule has no ``vault_key`` (e.g. meet's LiveKit key/secret rules) ->
      ``var_secret`` stores the RAW value directly under ``var`` in the
      provider's ``vault.yml`` and never touches ``vars.yml`` for it at
      all — the value is only recoverable by decrypting the vault.
    * a non-secret rule (e.g. the LiveKit domain/TURN hostnames) -> a plain
      scalar directly in ``vars.yml``.

    So — despite what the name might suggest — this function reads BOTH the
    provider's ``vars.yml`` (:func:`core.tree.load_vars`) and, if present,
    its decrypted ``vault.yml`` (:func:`core.vault.decrypt_to_dict`),
    layering vault values on top of vars.yml values before matching rules
    against the merged result: a vars.yml-only read would silently fail to
    recover exactly the generated secrets this function exists to protect.
    A hashi_vault-backed unit never has a ``vault.yml`` (its ``var_secret``
    always writes a lookup ref straight into ``vars.yml`` instead), so for
    those units this naturally degrades to a vars.yml-only read — no special
    casing needed.

    Best-effort: a missing unit, a missing ``vault.yml``, or a vault that
    cannot be decrypted (no ``ansible-vault`` binary on PATH, no
    ``.vault-pass`` yet) all degrade to omitting just the affected keys —
    never raise. In that last case the caller's fallback is no worse than a
    fresh bootstrap: prompt or regenerate for those specific values.
    """
    data: dict = {}
    try:
        data.update(tree.load_vars(app, env, provider_component))
    except Exception:
        pass

    vpath = paths.vault_path(app, env, provider_component)
    if vpath.exists():
        try:
            data.update(vault.decrypt_to_dict(vpath))
        except StCliError:
            pass

    out: dict[str, str] = {}
    for rule in shared:
        var = rule.get("var")
        if var and var in data:
            out[var] = str(data[var])
    return out
