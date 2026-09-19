"""Secret-backend strategy for st-cli.

Two backends share a small strategy surface so `st_cli.cmd.bootstrap` and
`st_cli.core.generate` branch in one place:

* `AnsibleVaultBackend`, the default. Real values are buffered per-component
  and written to an ansible-vault-encrypted `vault.yml`. The plaintext env
  blob carries `{{ vault_<key> }}` Jinja refs.
* `HashiVaultBackend`, OpenBao/Vault KV-v2, reference-only. It writes no
  `vault.yml`, generates no secret, and writes nothing to OpenBao: the env
  blob carries lookup refs to existing OpenBao entries. Each
  `@openbao(<path>)` or `@vault(<path>)` marker in a prompted value becomes
  a lookup ref; a value with no marker stays literal text.

Only the backend choice lives in `.st-cli.yml`. Connection details live in
`<app>/<env>/common.yml`, and the token passes through the inherited env.
"""

from __future__ import annotations

import re

from . import manifest, tree
from .models import BACKEND_ANSIBLE_VAULT, BACKEND_HASHI_VAULT


def hashi_lookup_ref(term: str) -> str:
    """Build the env-blob Jinja ref that resolves a secret through OpenBao.

    Escapes `\\` and `'` in the lookup term so it cannot break out of the
    surrounding single-quoted Jinja string literal.
    """
    escaped = term.replace("\\", "\\\\").replace("'", "\\'")
    return "{{ lookup('community.hashi_vault.hashi_vault', '" + escaped + "') }}"


# Inline markers: @openbao(<path>) or @vault(<path>). Everything between the
# parens is the OpenBao lookup path; text outside the marker stays literal. A
# path may contain ':' and '/' but not ')'.
_OPENBAO_MARKER = re.compile(r"@(?:openbao|vault)\(([^)]*)\)")


def hashi_render(raw: str) -> str:
    """Turn a user-entered hashi secret value into its env-blob string.

    Replaces each `@openbao(<path>)` or `@vault(<path>)` marker with a lookup
    ref, keeping surrounding text literal. A value with no marker stays literal.
    """
    if _OPENBAO_MARKER.search(raw):
        return _OPENBAO_MARKER.sub(lambda m: hashi_lookup_ref(m.group(1).strip()), raw)
    return raw


def _extract_start_comment(raw: str) -> str:
    """Extract the leading `# ...` comment block text, without the `#` prefixes.

    ruamel does not round-trip a comment block before the `---` document
    marker, so `write_common_connection` captures it from the raw text and
    re-applies it. Stops at the first `---` or non-comment line.
    """
    lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("#"):
            lines.append(line.lstrip("# ").rstrip())
        elif line.strip():
            break
    return "\n".join(lines).strip()


def write_common_connection(
    app: str, env: str, url: str, validate_certs: bool, auth_method: str
) -> None:
    """Merge the `ansible_hashi_vault_*` connection vars into `common.yml`.

    Non-interactive: callers pass the resolved values. Preserves any
    pre-existing keys and the header comment block.
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


class SecretBackend:
    """Base strategy. The two concrete backends override every method."""

    kind: str = ""

    def prompts_values(self) -> bool:
        raise NotImplementedError

    def expand_markers(self, value: str) -> str:
        """Expand inline @openbao()/@vault() markers in a non-secret value.

        No-op on the base: only the hashi_vault backend recognizes markers.
        """
        return value

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
        """A secret that is both a provider standalone var and a consumer env ref.

        Default: stored twice, in the provider's vault buffer and as a
        `vault_<consumer_key>` consumer copy. `HashiVaultBackend` uses one ref.
        """
        self.var_secret(pvars, var, value, component=provider)
        self.env_secret(answers, consumer_key, component=consumer, value=value)


class AnsibleVaultBackend(SecretBackend):
    """The default ansible-vault backend: real values in an encrypted vault.yml."""

    kind = BACKEND_ANSIBLE_VAULT

    def __init__(self) -> None:
        # per-component buffer of vault_<key> -> raw value, written to vault.yml
        self._buf: dict[str, dict] = {}

    def prompts_values(self) -> bool:
        return True

    def env_secret(
        self, answers: dict, env_key: str, component: str, *, value=None
    ) -> None:
        name = "vault_" + env_key.lower()
        self._buf.setdefault(component, {})[name] = value
        answers[env_key] = "{{ " + name + " }}"

    def var_secret(
        self, pvars, var: str, value, *, component: str, vault_key: str | None = None
    ) -> None:
        # With vault_key set: a {{ vault_<key> }} ref in the plaintext pvars,
        # the real value under vault_key in vault.yml. Without it: the raw
        # value goes straight into the vault buffer under the var name.
        if vault_key:
            pvars[var] = "{{ " + vault_key + " }}"
            self._buf.setdefault(component, {})[vault_key] = value
        else:
            self._buf.setdefault(component, {})[var] = value

    def component_secrets(self, component: str) -> dict:
        return self._buf.get(component, {})


class HashiVaultBackend(SecretBackend):
    """OpenBao/Vault KV-v2 backend, reference-only.

    Env blobs carry lookup refs to existing OpenBao entries. Writes no
    `vault.yml`, generates no secret, writes nothing to OpenBao.
    """

    kind = BACKEND_HASHI_VAULT

    def __init__(self, app: str) -> None:
        self._app = app

    def expand_markers(self, value: str) -> str:
        return hashi_render(value)

    def _prompt_term(self, label: str) -> str:
        from .prompts import _ask

        # Pre-fill an editable default the operator can accept with Enter or
        # edit; hashi_render() turns the marker into a lookup ref.
        hint = f"@openbao(kv/data/{self._app}:{label})"
        return _ask(label, default=hint)

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
        # `value` is accepted for API symmetry with ansible-vault but ignored:
        # st-cli mints nothing and writes nothing to OpenBao.
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
        # `value`/`vault_key` are ignored; kept for signature symmetry with
        # ansible-vault, which uses them.
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
        # Single source of truth: one prompt, both the provider var and the
        # consumer env ref point at the same lookup.
        ref = hashi_render(self._prompt_term(var))
        pvars[var] = ref
        answers[consumer_key] = ref

    def component_secrets(self, component: str) -> dict:
        return {}


def setup_backend(m, app: str, env: str) -> SecretBackend:
    """Ask the user to choose a secret backend, persist it, and return it.

    Reuses the persisted choice of an already-bootstrapped (app, env): a
    different backend breaks the tree. Registered units with no `secrets:`
    entry, from an older manifest, count as ansible-vault.
    """
    existing = next((s for s in m.secrets if s.app == app and s.env == env), None)
    if existing is not None or manifest.units_for(m, app, env):
        from . import ui

        backend_name = manifest.secret_config_for(m, app, env).backend
        ui.info(f"Reusing the '{backend_name}' secret backend for {app}/{env}")
        return (
            HashiVaultBackend(app)
            if backend_name == BACKEND_HASHI_VAULT
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
    backend_name = (
        BACKEND_HASHI_VAULT if BACKEND_HASHI_VAULT in choice else BACKEND_ANSIBLE_VAULT
    )
    manifest.upsert_secret(
        m, manifest.SecretConfig(app=app, env=env, backend=backend_name)
    )

    if backend_name == BACKEND_HASHI_VAULT:
        from . import ui

        ui.note(
            "st-cli never writes secrets, you need to pre-create them in OpenBao first.\n"
            "Each secret prompt is pre-filled with an editable default like:\n"
            "  [bold]@openbao(kv/data/<app>:<VAR>)[/bold]\n"
            "press Enter to accept it, or edit the path/field.\n\n"
            "Only [bold]@openbao(...)[/bold] / [bold]@vault(...)[/bold] markers become a "
            "lookup ref — and this isn't limited to secret prompts: a marker works in "
            "ANY field (including non-secret env values and provider vars), turning it "
            "or an embedded segment into a lookup.\n\n"
            "To mix literal text with a lookup, embed an inline marker anywhere:\n"
            "  redis://user1:[bold]@openbao(kv/data/messages:redis_pw)[/bold]@redis:6379",
            title="hashi_vault",
        )
        url = _ask("OpenBao / Vault URL", placeholder="https://vault.internal:8200")
        skip_tls = _confirm("Skip TLS verification?", default=False)
        # auth_method is always token: the user supplies VAULT_TOKEN at
        # runtime. Written for documentation, not prompted.
        write_common_connection(app, env, url, not skip_tls, "token")
        return HashiVaultBackend(app)
    return AnsibleVaultBackend()
