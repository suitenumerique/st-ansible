"""Pure writers for the committed config tree (vars.yml / vault.yml / hosts).

Takes already-collected answers and a secret backend. Prompts nothing and
mutates no manifest.
"""

from __future__ import annotations

import os

from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import LiteralScalarString

from .. import __version__
from . import envblob, envrender, paths, secrets, tree, ui, vault
from .errors import StCliError
from .models import BACKEND_HASHI_VAULT
from .secretbackend import SecretBackend


def _scalar(value: str):
    """Wrap a multi-line string as a readable `|` block; leave a one-liner plain."""
    return LiteralScalarString(value) if "\n" in value else value


def gen_value(rule: dict) -> str:
    """Materialise a generated shared value (token/secret) from a rule."""
    kind = rule.get("generate")
    if kind == "token":
        return secrets.gen_token()
    if kind == "secret":
        return secrets.gen_secret()
    raise StCliError(f"unknown generate kind {kind!r} for rule {rule!r}")


def rule_is_secret(rule: dict) -> bool:
    """True if a shared rule carries a secret (generated, or flagged)."""
    return bool(rule.get("generate")) or bool(rule.get("secret"))


def rule_label(rule: dict) -> str:
    """Human-friendly prompt label for a shared rule."""
    return (
        rule.get("label") or rule.get("consumer_env_key") or rule.get("var") or "value"
    )


def inject_consumer(
    rule: dict,
    value,
    answers: dict,
    backend: SecretBackend,
    component: str,
) -> None:
    """Inject a shared value into the consumer's env answers.

    No `consumer_env_key` means no injection. Secrets route through the
    backend; `consumer_format` reshapes a non-secret value.
    """
    key = rule.get("consumer_env_key")
    if not key:
        return
    if rule_is_secret(rule):
        backend.env_secret(answers, key, component=component, value=value)
    else:
        answers[key] = (rule.get("consumer_format") or "{value}").format(value=value)


_REFERENCE_URL = (
    "https://github.com/suitenumerique/st-ansible/blob/main/roles/{role}/REFERENCE.md"
)


def vars_header(app: str, meta, comp, backend: SecretBackend | None = None) -> str:
    """A documentation comment for the top of a component's vars.yml.

    Describes the secret backend in use; `backend=None` defaults to the
    ansible-vault wording.
    """
    role = comp.role.split(".")[-1]
    if backend is not None and backend.kind == BACKEND_HASHI_VAULT:
        # literal (non-f) line so the Jinja braces survive verbatim:
        secrets_line = (
            " Secrets are referenced as {{ lookup('community.hashi_vault.hashi_vault',"
            " ...) }} and stored in OpenBao (no vault.yml)."
        )
    else:
        secrets_line = " Secrets are referenced as {{ vault_* }} and stored encrypted in vault.yml."
    lines = [
        f" st-cli config for {app}/{comp.key} — safe to edit by hand.",
        "",
        " Ansible variables (st_*) for this component:",
        f"   {_REFERENCE_URL.format(role=role)}",
        " App environment variables (the KEY=value lines inside the *_env blocks):",
        f"   {meta.env_docs_url}",
        secrets_line,
    ]
    return "\n".join(lines)


def apply_component_vars(data, meta, comp, answers: dict) -> None:
    """Add metadata-declared component vars to vars.yml, rendering
    `{DOMAIN}`-style placeholders from the answers.

    A failed render keeps the committed value if one exists; only an absent
    key gets the literal template.
    """
    for name, tmpl in meta.component_vars(comp.key).items():
        try:
            rendered = str(tmpl).format(**answers)
        except (KeyError, IndexError, ValueError):
            if name in data:
                # keep what is committed; do not clobber it with "{PLACEHOLDER}"
                continue
            # nothing to preserve; leave the literal template for the user to fix
            rendered = str(tmpl)
        data[name] = _scalar(rendered)


def expand_var_markers(data, backend: SecretBackend) -> None:
    """Expand inline `@openbao()`/`@vault()` markers in every string leaf of a
    component's vars map, via the backend.

    No-op for ansible-vault; idempotent for hashi_vault.
    """
    for name, val in list(data.items()):
        if isinstance(val, str):
            rendered = backend.expand_markers(val)
            if rendered != val:
                data[name] = _scalar(rendered)


def write_vault(
    app: str,
    env: str,
    component: str,
    backend: SecretBackend,
    *,
    replace: bool = False,
) -> None:
    """Write and ansible-vault encrypt a unit's `vault.yml`, merging on rebootstrap.

    No-op when `backend.component_secrets` is empty: on a rebootstrap it holds
    only the newly prompted secrets, so a wholesale write would drop the rest.
    `replace=True` skips the merge and writes `vault_vars` wholesale.
    """
    vault_vars = backend.component_secrets(component)
    if not vault_vars:
        # Also the `replace=True` early exit: relies on a core override always
        # buffering at least DJANGO_SECRET_KEY under ansible-vault, or this
        # would leave the OLD vault.yml in place instead of dropping it.
        # hashi_vault never reaches here with a non-empty vault.yml at all.
        return
    path = paths.vault_path(app, env, component)
    merged = dict(vault_vars)
    if not replace and path.exists():
        existing = vault.decrypt_to_dict(path)
        merged = {**existing, **vault_vars}
        # ansible-vault salts every run; re-encrypting an unchanged mapping only
        # adds git churn.
        if merged == existing:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    cm = CommentedMap()
    for k, v in merged.items():
        cm[k] = v
    tmp = path.with_name(path.name + ".tmp")
    try:
        # Create the tmp at 0600 (not the default 0644 under umask 022): the
        # plaintext secrets are world-readable while ansible-vault encrypt runs
        # in-place below. O_TRUNC covers any pre-existing tmp from a prior
        # aborted run; 0o600 has no group/other bits so umask cannot relax it.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, paths.SECRET_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            tree.yaml().dump(cm, fh)
        vault.encrypt_file(tmp)
        os.replace(tmp, path)
    except BaseException:
        # never leave plaintext secrets on disk if encryption/replace fails
        tmp.unlink(missing_ok=True)
        raise


def ensure_vault_readable(app: str, env: str, components: list[str]) -> None:
    """Raise `StCliError` up front if any named component's vault cannot be read.

    Call before the rebootstrap questionnaire, so a bad `.vault-pass` fails
    before the operator re-answers every prompt.
    """
    for component in components:
        path = paths.vault_path(app, env, component)
        if path.exists():
            vault.decrypt_to_dict(path)


def cadvisor_var(app: str) -> str:
    """The per-app cadvisor toggle var name (`st_<app>_cadvisor_enabled`).

    Uniform across every component of the app; only the vars.yml it lands in
    differs.
    """
    return f"st_{app}_cadvisor_enabled"


def write_core(
    meta,
    answers,
    backend: SecretBackend,
    core_hosts,
    worker_hosts,
    env,
    cadvisor_enabled: bool = True,
    *,
    fresh: bool = False,
) -> None:
    """Render and write the core component's vars.yml, vault.yml, and hosts.

    Merges into what is committed; only the keys st-cli owns change, so a
    hand-edited key round-trips untouched. `fresh=True` starts from an empty
    map and rebuilds the vault wholesale.
    """
    app, core = meta.app, meta.core()
    rendered = envrender.render_env(app, core.key, answers)

    data = CommentedMap() if fresh else tree.load_vars(app, env, core.key)
    existed = bool(data)
    marker = f"# added by st-cli {__version__}"

    # NB: the enabled flag is injected on the deploy task in the generated
    # playbook (never here), so the root base phase stays base-only.
    apply_component_vars(data, meta, core, answers)
    data[cadvisor_var(app)] = cadvisor_enabled  # real YAML bool (role spec: bool)
    for blob_var, text in rendered.items():
        existing_blob = str(data[blob_var]) if blob_var in data else ""
        merged = envblob.merge(existing_blob, text, marker)
        data[blob_var] = LiteralScalarString(
            merged
        )  # readable `|` block, with {{ vault_* }} refs
    expand_var_markers(data, backend)
    if not data.ca.comment:
        # Only stamp the header when the file has no start comment already: a
        # rebootstrap over an existing header must not stack a duplicate one.
        data.yaml_set_start_comment(vars_header(app, meta, core, backend))
    tree.save_vars(app, env, core.key, data)
    write_vault(app, env, core.key, backend, replace=fresh)
    groups = {core.app_name: core_hosts}
    worker = meta.worker()
    if worker and worker.implemented:
        groups[worker.app_name] = worker_hosts
    tree.write_groups(app, env, core.key, groups)
    # hashi_vault mode buffers no secrets and writes no vault.yml, so don't claim it.
    files = (
        "vars.yml + vault.yml + hosts"
        if backend.component_secrets(core.key)
        else "vars.yml + hosts"
    )
    verb = "updated" if existed else "wrote"
    ui.success(f"{core.key}: {verb} {files}.")
