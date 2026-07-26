"""Pure writers for the committed config tree (vars.yml / vault.yml / hosts).

Extracted from :mod:`st_cli.cmd.bootstrap` so the interactive questionnaire
stays separate from the I/O that materialises a unit's files. These helpers
take already-collected answers + a secret-backend strategy and write the
plaintext ``vars.yml`` (with ``{{ vault_* }}`` Jinja refs) and the
ansible-vault-encrypted ``vault.yml`` (no-op when empty). No prompting, no
manifest mutation — callers drive the flow.
"""

from __future__ import annotations

import os

from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import LiteralScalarString

from .. import __version__
from . import envblob, envrender, paths, secrets, tree, ui, vault
from .errors import StCliError
from .secretbackend import SecretBackend


# --------------------------------------------------------------------------- #
# shared-rule helpers
# --------------------------------------------------------------------------- #
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

    Provider-only rules (no ``consumer_env_key``) inject nothing. Secrets are
    routed through the backend (``env_secret``); ``consumer_format`` reshapes
    the value (e.g. ``wss://{value}``) for non-secret refs like the LiveKit URL.
    """
    key = rule.get("consumer_env_key")
    if not key:
        return
    if rule_is_secret(rule):
        backend.env_secret(answers, key, component=component, value=value)
    else:
        answers[key] = (rule.get("consumer_format") or "{value}").format(value=value)


# --------------------------------------------------------------------------- #
# writers
# --------------------------------------------------------------------------- #
_REFERENCE_URL = (
    "https://github.com/suitenumerique/st-ansible/blob/main/roles/{role}/REFERENCE.md"
)


def vars_header(app: str, meta, comp) -> str:
    """A documentation comment for the top of a component's vars.yml."""
    role = comp.role.split(".")[-1]
    lines = [
        f" st-cli config for {app}/{comp.key} — safe to edit by hand.",
        "",
        " Ansible variables (st_*) for this component:",
        f"   {_REFERENCE_URL.format(role=role)}",
        " App environment variables (the KEY=value lines inside the *_env blocks):",
        f"   {meta.env_docs_url}",
        # literal (non-f) line so the Jinja braces survive verbatim:
        " Secrets are referenced as {{ vault_* }} and stored encrypted in vault.yml.",
    ]
    return "\n".join(lines)


def apply_component_vars(data, meta, comp, answers: dict) -> None:
    """Add metadata-declared component vars (e.g. st_drive_public_host,
    st_drive_collabora_env) to vars.yml, rendering ``{DOMAIN}``-style placeholders
    from the questionnaire answers. Multi-line values become readable `|` blocks.

    When a placeholder cannot be rendered (the answer is missing or the template
    is malformed) the committed value is KEPT if there already is one, and only
    an absent key falls back to writing the literal template for the operator to
    fix by hand. That asymmetry matters since ``write_core`` became a merge: on a
    rebootstrap, ``answers`` is recovered from the tree and a recovery gap would
    otherwise overwrite a perfectly good committed value with the literal string
    ``"{DOMAIN}"`` — turning a partial recovery into silent config corruption.
    Writing the literal is only ever an improvement on writing nothing at all.
    """
    for name, tmpl in meta.component_vars(comp.key).items():
        try:
            rendered = str(tmpl).format(**answers)
        except (KeyError, IndexError, ValueError):
            if name in data:
                continue  # keep what is committed — never clobber it with "{PLACEHOLDER}"
            rendered = str(
                tmpl
            )  # nothing to preserve → leave literal for the user to fix
        data[name] = LiteralScalarString(rendered) if "\n" in rendered else rendered


def expand_var_markers(data, backend: SecretBackend) -> None:
    """Expand inline @openbao()/@vault() markers in every string leaf of a
    component's vars map (env blobs + st_* scalars) via the backend.

    No-op for ansible-vault (its expand_markers returns the value unchanged);
    idempotent for hashi_vault (already-rendered lookup refs carry no marker).
    Multi-line values are re-wrapped as LiteralScalarString to preserve the
    readable `|` block style.
    """
    for name, val in list(data.items()):
        if isinstance(val, str):
            rendered = backend.expand_markers(val)
            if rendered != val:
                data[name] = (
                    LiteralScalarString(rendered) if "\n" in rendered else rendered
                )


def write_vault(app: str, env: str, component: str, backend: SecretBackend) -> None:
    """Write + ansible-vault encrypt a unit's ``vault.yml``, merging on rebootstrap.

    The secret mapping comes from ``backend.component_secrets(component)``:
    empty (e.g. hashi_vault mode, or a rebootstrap that prompted no NEW secret)
    ⇒ **no-op**, file untouched — this matters because on a rebootstrap that
    buffer holds only newly-prompted secrets (an already-answered secret is
    never re-prompted), so writing it wholesale would silently destroy every
    secret already committed. When the buffer is non-empty and ``vault.yml``
    already exists, the existing mapping is decrypted first and the new values
    are merged over it (new wins, everything else survives) before the union
    is (re)encrypted. The decrypt happens before any write is attempted, so a
    missing/wrong ``.vault-pass`` or a corrupt file raises ``StCliError`` with
    nothing on disk touched.

    A merge that changes nothing is also a no-op. ansible-vault salts every
    encryption, so re-encrypting an identical mapping produces a completely
    different ciphertext — the file would show up in ``git diff`` on every
    rebootstrap even though not one secret changed. That is not merely noise:
    a rerun that reports "nothing changed" while rewriting an encrypted file
    is exactly the kind of diff an operator learns to ignore, and it hides the
    reruns that DID rotate something. Several paths legitimately re-mirror an
    unchanged secret (e.g. reusing a livekit provider re-mirrors its api
    key/secret into the meet core's vault), so this is the common case, not
    the rare one.
    """
    vault_vars = backend.component_secrets(component)
    if not vault_vars:
        return
    path = paths.vault_path(app, env, component)
    merged = dict(vault_vars)
    if path.exists():
        existing = vault.decrypt_to_dict(path)
        merged = {**existing, **vault_vars}
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
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            tree.yaml().dump(cm, fh)
        vault.encrypt_file(tmp)
        os.replace(tmp, path)
    except BaseException:
        # never leave plaintext secrets on disk if encryption/replace fails
        tmp.unlink(missing_ok=True)
        raise


def ensure_vault_readable(app: str, env: str, components: list[str]) -> None:
    """Raise ``StCliError`` up front if any named component's vault can't be read.

    A no-op for a component with no ``vault.yml`` (fresh unit, or hashi_vault
    mode which never writes one). Meant to be called by the rebootstrap flow
    BEFORE the (potentially long) questionnaire runs: a missing/wrong
    ``.vault-pass`` or a corrupt vault file should fail immediately, not after
    the operator has re-answered every prompt only to lose the write at the end.
    """
    for component in components:
        path = paths.vault_path(app, env, component)
        if path.exists():
            vault.decrypt_to_dict(path)


def cadvisor_var(app: str) -> str:
    """The per-app cadvisor toggle var name (`st_<app>_cadvisor_enabled`).

    Every component of an app runs the same role, so the var name is uniform
    across the core and every provider unit — only the vars.yml it lands in
    (and the hosts it deploys to) differs per component.
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
) -> None:
    """Render + write the core component's vars.yml (plaintext) + vault.yml + hosts.

    This is a **merge**, not a replace: ``tree.load_vars`` loads whatever is
    already committed (an empty ``CommentedMap`` when the unit is fresh) and we
    mutate it in place, updating only the keys st-cli itself owns (the
    manifest-declared component vars, the cadvisor toggle, and the env-render
    blobs). Everything else an operator hand-edited — extra ``st_*`` vars,
    comments, ``*_env_template``/``*_compose_template`` overrides — lives on
    keys this function never touches, so it round-trips untouched. This is the
    safety property the rebootstrap flow rests on: an Enter-through rerun must
    leave ``vars.yml`` byte-identical.

    The core's ``hosts`` ini may carry two inventory groups: the core group
    (``core.app_name``) and, when worker IPs were entered, a ``[workers]`` group
    (``worker.app_name``). Workers own no directory of their own — both groups
    live in the core's ``hosts`` file. An empty ``worker_hosts`` writes no
    ``[workers]`` section, so the workers fall back to the core group.
    """
    app, core = meta.app, meta.core()
    rendered = envrender.render_env(app, core.key, answers)

    data = tree.load_vars(app, env, core.key)
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
        # Only stamp the header when the file has no start comment already — a
        # rebootstrap over an existing header must not stack a duplicate one.
        data.yaml_set_start_comment(vars_header(app, meta, core))
    tree.save_vars(app, env, core.key, data)
    write_vault(app, env, core.key, backend)
    groups = {core.app_name: core_hosts}
    worker = meta.worker()
    if worker and worker.implemented:
        groups[worker.app_name] = worker_hosts
    tree.write_groups(app, env, core.key, groups)
    verb = "updated" if existed else "wrote"
    ui.success(f"{core.key}: {verb} vars.yml + vault.yml + hosts.")
