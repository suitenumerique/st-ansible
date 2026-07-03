"""Secret-backend strategy for st-cli (CONTRACT section 3).

Two backends share a small strategy surface so :mod:`st_cli.cmd.bootstrap` and
:mod:`st_cli.core.generate` branch in one place:

* :class:`AnsibleVaultBackend` — the default. Real values are buffered
  per-component and written to an ansible-vault-encrypted ``vault.yml``; the
  plaintext env blob carries ``{{ vault_<key> }}`` Jinja refs.
* :class:`HashiVaultBackend` — OpenBao/Vault KV-v2, **reference-only**. No
  ``vault.yml`` is written, no secret is generated, and nothing is written to
  OpenBao: the env blob carries
  ``{{ lookup('community.hashi_vault.hashi_vault', '<term>') }}`` refs to
  existing OpenBao entries. Each ``@openbao(<path>)`` / ``@vault(<path>)`` marker
  in a prompted value becomes a lookup ref; a value with NO marker is kept
  literal (plain text) — the operator pre-creates every secret in OpenBao and
  uses a marker (the prompt pre-fills an editable ``@openbao(kv/data/<app>:<VAR>)``
  default) to opt a value into a lookup.

Only the backend *choice* (``ansible-vault`` | ``hashi_vault``) lives in
``.st-cli.yml``; connection details (``ansible_hashi_vault_*``) are written into
``<app>/<env>/common.yml`` and the token passes through the inherited env.
"""

from __future__ import annotations

import re

from . import manifest, tree


# --------------------------------------------------------------------------- #
# pure helpers (unit-testable without a TTY)
# --------------------------------------------------------------------------- #
def hashi_lookup_ref(term: str) -> str:
    """Build the env-blob Jinja ref that resolves a secret via OpenBao.

    The user's lookup term is embedded into a single-quoted Jinja string literal
    with ``\\`` and ``'`` escaped, so a term containing a quote or backslash
    cannot break out of the literal and inject arbitrary Jinja (the ``{{``/``}}``
    delimiters are inert inside a quoted literal, so escaping the quote is
    sufficient). No other munging is done — no path rewriting, no ``:field``
    split (reference-only: nothing is minted or written).
    """
    escaped = term.replace("\\", "\\\\").replace("'", "\\'")
    return "{{ lookup('community.hashi_vault.hashi_vault', '" + escaped + "') }}"


# Inline markers: @openbao(<path>) or @vault(<path>). Everything between the
# parens is the OpenBao lookup path; text outside the marker stays literal. A
# path may contain ':' and '/' but not ')'.
_OPENBAO_MARKER = re.compile(r"@(?:openbao|vault)\(([^)]*)\)")


def hashi_render(raw: str) -> str:
    """Turn a user-entered hashi secret value into its env-blob string.

    Each ``@openbao(<path>)`` / ``@vault(<path>)`` marker is replaced by a
    ``community.hashi_vault`` lookup ref for ``<path>`` (via ``hashi_lookup_ref``),
    with the surrounding text kept literal. A value with NO marker is kept literal
    (plain text, even for a secret var) — the operator must use ``@openbao()`` /
    ``@vault()`` to opt a value into a lookup.
    """
    if _OPENBAO_MARKER.search(raw):
        return _OPENBAO_MARKER.sub(lambda m: hashi_lookup_ref(m.group(1).strip()), raw)
    return raw


def _extract_start_comment(raw: str) -> str:
    """Extract the leading ``# ...`` comment block text (without the ``#`` prefixes).

    ruamel does not round-trip a comment block sitting before the ``---`` document
    marker, so :func:`write_common_connection` captures it from the raw text and
    re-applies it via ``yaml_set_start_comment`` after the merge. Stops at the
    first ``---`` or non-comment line.
    """
    lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("#"):
            lines.append(line.lstrip("# ").rstrip())
        elif line.strip() == "---":
            break
        elif line.strip():
            break  # first content line — stop
    return "\n".join(lines).strip()


def write_common_connection(
    app: str, env: str, url: str, validate_certs: bool, auth_method: str
) -> None:
    """Merge the ``ansible_hashi_vault_*`` connection vars into ``common.yml``.

    Non-interactive: callers (setup_backend + tests) pass the resolved values.
    Preserves any pre-existing keys AND the header comment block (ruamel does
    not round-trip a comment before ``---``, so it is captured from the raw text
    and re-applied via ``yaml_set_start_comment``).
    """
    tree.ensure_common(app, env)
    preamble = _extract_start_comment(tree.read_common_text(app, env))
    data = tree.load_common(app, env)
    data["ansible_hashi_vault_url"] = url
    data["ansible_hashi_vault_validate_certs"] = bool(validate_certs)
    data["ansible_hashi_vault_auth_method"] = auth_method
    if preamble:
        data.yaml_set_start_comment(preamble)
    tree.save_common(app, env, data)


# --------------------------------------------------------------------------- #
# strategy
# --------------------------------------------------------------------------- #
class SecretBackend:
    """Base strategy. The two concrete backends override every method."""

    kind: str = ""

    def prompts_values(self) -> bool:
        raise NotImplementedError

    def env_secret(
        self, answers: dict, env_key: str, component: str, *, value=None
    ) -> None:
        raise NotImplementedError

    def var_secret(
        self, pvars, var: str, value, *, component: str, vault_key: str | None = None
    ) -> None:
        raise NotImplementedError

    def component_secrets(self, component: str) -> dict:
        raise NotImplementedError

    def shared_provider_secret(
        self,
        pvars,
        answers: dict,
        var: str,
        consumer_key: str,
        value,
        *,
        provider: str,
        consumer: str,
    ) -> None:
        """A secret that is BOTH a provider standalone var and a consumer env ref.

        Default (ansible-vault): store independently — the raw value in the
        provider's vault buffer AND a ``vault_<consumer_key>`` copy in the
        consumer's (two separate stores). The hashi_vault backend overrides this
        to prompt once and point both refs at a single OpenBao location (single
        source of truth).
        """
        self.var_secret(pvars, var, value, component=provider)
        self.env_secret(answers, consumer_key, component=consumer, value=value)


class AnsibleVaultBackend(SecretBackend):
    """The default ansible-vault backend: real values in an encrypted vault.yml."""

    kind = "ansible-vault"

    def __init__(self) -> None:
        # per-component buffer of vault_<key> → raw value (written to vault.yml)
        self._buf: dict[str, dict] = {}

    def prompts_values(self) -> bool:
        return True

    def env_secret(
        self, answers: dict, env_key: str, component: str, *, value=None
    ) -> None:
        # exactly today's _secret(): vault_<key> in the component's vault buffer,
        # {{ vault_<key> }} ref in the env answers.
        name = "vault_" + env_key.lower()
        self._buf.setdefault(component, {})[name] = value
        answers[env_key] = "{{ " + name + " }}"

    def var_secret(
        self, pvars, var: str, value, *, component: str, vault_key: str | None = None
    ) -> None:
        # provider standalone secret scalar. With ``vault_key`` set, follow the
        # vault-ref split: a ``{{ vault_<key> }}`` ref in the plaintext pvars and
        # the real value under that ``vault_<key>`` name in vault.yml. Without it,
        # the raw value is stored under the var name in the vault buffer and pvars
        # is left untouched.
        if vault_key:
            pvars[var] = "{{ " + vault_key + " }}"
            self._buf.setdefault(component, {})[vault_key] = value
        else:
            self._buf.setdefault(component, {})[var] = value

    def component_secrets(self, component: str) -> dict:
        return self._buf.get(component, {})


class HashiVaultBackend(SecretBackend):
    """OpenBao/Vault KV-v2 backend — reference-only: env blobs carry lookup
    refs to existing OpenBao entries; no ``vault.yml``, no generation, no writes."""

    kind = "hashi_vault"

    def __init__(self, app: str) -> None:
        # app name seeds the pre-filled @openbao(kv/data/<app>:<VAR>) default hint.
        self._app = app

    # -- prompts (deferred to bootstrap so the strategy stays TTY-free) ------- #
    def _prompt_term(self, label: str) -> str:
        from .prompts import _ask

        # Standard var prompt: label is just the var name, like every other prompt.
        # Pre-fill an editable @openbao(kv/data/<app>:<VAR>) default the operator can
        # accept with Enter or edit; hashi_render() turns the marker into a lookup ref.
        hint = f"@openbao(kv/data/{self._app}:{label})"
        return _ask(label, default=hint)

    # -- strategy methods ----------------------------------------------------- #
    def prompts_values(self) -> bool:
        return False

    def env_secret(
        self,
        answers: dict,
        env_key: str,
        component: str,
        *,
        value=None,
    ) -> None:
        # reference-only: prompt the lookup term and drop the ref into answers.
        # `value` is accepted for API symmetry with ansible-vault but ignored —
        # st-cli mints nothing and writes nothing to OpenBao. The term may carry
        # inline @openbao()/@vault() markers; hashi_render interpolates them.
        answers[env_key] = hashi_render(self._prompt_term(env_key))

    def var_secret(
        self,
        pvars,
        var: str,
        value,
        *,
        component: str,
        vault_key: str | None = None,
    ) -> None:
        # reference-only: prompt the lookup term and drop the ref into pvars.
        # `value`/`vault_key` are ignored (kept for signature symmetry with
        # ansible-vault — hashi always writes a lookup ref into pvars.yml). The
        # term may carry inline @openbao()/@vault() markers; hashi_render
        # interpolates them.
        pvars[var] = hashi_render(self._prompt_term(var))

    def shared_provider_secret(
        self,
        pvars,
        answers: dict,
        var: str,
        consumer_key: str,
        value,
        *,
        provider: str,
        consumer: str,
    ) -> None:
        # single source of truth: prompt one lookup term and point BOTH the
        # provider var and the consumer env ref at it — no double prompt, no
        # writes (reference-only). The term may carry inline @openbao()/@vault()
        # markers; hashi_render interpolates them into a single shared ref.
        ref = hashi_render(self._prompt_term(var))
        pvars[var] = ref
        answers[consumer_key] = ref

    def component_secrets(self, component: str) -> dict:
        # always empty → _write_vault no-ops → no vault.yml is written
        return {}


# --------------------------------------------------------------------------- #
# interactive + non-interactive construction
# --------------------------------------------------------------------------- #
def setup_backend(m, app: str, env: str) -> SecretBackend:
    """Interactive: ask the user to choose a secret backend, persist + return it.

    Upserts a :class:`~st_cli.core.models.SecretConfig` on ``m`` (caller saves
    the manifest). For hashi_vault, also prompts connection vars and merges
    them into ``<app>/<env>/common.yml`` via :func:`write_common_connection`.

    Non-interactive on re-runs: once an (app, env) has been bootstrapped (either
    a ``secrets:`` entry exists OR prior units were registered — the ansible-vault
    backend writes no secrets entry), the persisted backend choice is reused
    silently. Re-asking would be redundant and dangerous: picking a different
    backend breaks the existing tree.
    """
    # REUSE short-circuit: a backend was already chosen for this (app, env) at
    # the first bootstrap. hashi_vault persists a secrets entry; ansible-vault
    # writes none (per the "omit empty secrets block" convention), so prior
    # units are the signal it was bootstrapped.
    existing = next((s for s in m.secrets if s.app == app and s.env == env), None)
    if existing is not None or manifest.units_for(m, app, env):
        from . import ui

        backend_name = manifest.secret_config_for(m, app, env).backend
        ui.info(f"Reusing the '{backend_name}' secret backend for {app}/{env}")
        return (
            HashiVaultBackend(app)
            if backend_name == "hashi_vault"
            else AnsibleVaultBackend()
        )

    from .prompts import _ask, _ask_select, _confirm

    choice = _ask_select(
        "Secret backend:",
        [
            "ansible-vault — secrets encrypted locally with a generated password",
            "hashi_vault (OpenBao) — HashiCorp Vault or OpenBao external instance",
        ],
    )
    backend_name = "hashi_vault" if "hashi_vault" in choice else "ansible-vault"
    manifest.upsert_secret(
        m, manifest.SecretConfig(app=app, env=env, backend=backend_name)
    )

    if backend_name == "hashi_vault":
        from . import ui

        ui.note(
            "st-cli never writes secrets, you need to pre-create them in OpenBao first.\n"
            "Each secret prompt is pre-filled with an editable default like:\n"
            "  [bold]@openbao(kv/data/<app>:<VAR>)[/bold]\n"
            "press Enter to accept it, or edit the path/field.\n\n"
            "Only [bold]@openbao(...)[/bold] / [bold]@vault(...)[/bold] markers become a "
            "lookup ref.\n\n"
            "To mix literal text with a lookup, embed an inline marker anywhere:\n"
            "  redis://user1:[bold]@openbao(kv/data/messages:redis_pw)[/bold]@redis:6379",
            title="hashi_vault",
        )
        url = _ask("OpenBao / Vault URL", placeholder="https://vault.internal:8200")
        skip_tls = _confirm("Skip TLS verification?", default=False)
        # auth_method is always token here (the user supplies VAULT_TOKEN at
        # runtime) — written for documentation, not prompted. Edit common.yml to
        # switch to approle/etc.
        write_common_connection(app, env, url, not skip_tls, "token")
        return HashiVaultBackend(app)
    return AnsibleVaultBackend()
