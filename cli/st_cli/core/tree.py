"""Read/write the committed config tree: ``<app>/<env>/<component>/{vars.yml,hosts}``.

Uses a single round-trip ruamel YAML instance that preserves comments and
round-trips ansible-vault ``!vault`` tagged scalars untouched (via
:class:`VaultString`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ruamel.yaml import YAML

from . import paths


class VaultString(str):
    """A string carrying the ``!vault`` tag, rendered as a literal block scalar.

    Subclasses ``str`` so the rest of the code can treat it as text while ruamel
    re-emits it with its tag and ``|`` style.
    """

    yaml_tag = "!vault"


def _construct_vault(constructor, node):
    return VaultString(node.value)


def _represent_vault(representer, data):
    return representer.represent_scalar("!vault", str(data), style="|")


_YAML: Optional[YAML] = None


def yaml() -> YAML:
    """Return the shared round-trip YAML instance (with !vault registered)."""
    global _YAML
    if _YAML is None:
        y = YAML(typ="rt")
        y.preserve_quotes = True
        y.width = 4096  # don't wrap long env lines
        y.indent(mapping=2, sequence=4, offset=2)
        y.constructor.add_constructor("!vault", _construct_vault)
        y.representer.add_representer(VaultString, _represent_vault)
        _YAML = y
    return _YAML


def _load_yaml(path: Path):
    """Load a YAML file via the shared round-trip instance.

    Returns an empty ``CommentedMap`` when the file is absent or empty, so
    callers always get a mutable mapping whose comments/order round-trip on save.
    """
    from ruamel.yaml.comments import CommentedMap

    if not path.exists():
        return CommentedMap()
    with path.open("r", encoding="utf-8") as fh:
        data = yaml().load(fh)
    return data if data is not None else CommentedMap()


def _save_yaml(path: Path, data) -> None:
    """Dump ``data`` to ``path`` via the shared YAML instance, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml().dump(data, fh)


def load_vars(app: str, env: str, component: str):
    """Load ``vars.yml`` for a unit (returns a ruamel CommentedMap; {} if absent)."""
    return _load_yaml(paths.vars_path(app, env, component))


def save_vars(app: str, env: str, component: str, data) -> None:
    """Write ``vars.yml`` for a unit, creating parent dirs."""
    _save_yaml(paths.vars_path(app, env, component), data)


def _host_vars_suffix(host_vars: Optional[dict]) -> str:
    """Render ``host_vars`` as a leading-space ``k=v`` suffix (``""`` when none)."""
    if not host_vars:
        return ""
    return " " + " ".join(f"{k}={v}" for k, v in host_vars.items())


def _group_lines(group: str, hosts: list[str], suffix: str) -> list[str]:
    """INI lines for one ``[group]``: a header + a ``<group><n> ansible_host=<ip>``
    alias per host, each carrying ``suffix``."""
    lines = [f"[{group}]"]
    for i, ip in enumerate(hosts, start=1):
        lines.append(f"{group}{i} ansible_host={ip}{suffix}")
    return lines


def _write_ini(app: str, env: str, component: str, lines: list[str]) -> None:
    """Write INI ``lines`` to the unit's ``hosts`` file (trailing newline, mkdir)."""
    p = paths.hosts_path(app, env, component)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_hosts(
    app: str,
    env: str,
    component: str,
    group: str,
    hosts: list[str],
    host_vars: Optional[dict] = None,
) -> None:
    """Write an INI inventory with one ``[group]`` listing the given host IPs.

    Each host gets a generated alias ``<group><n>`` with ``ansible_host=<ip>``.
    ``host_vars`` (if given) are appended to every host line as ``k=v`` pairs.
    """
    _write_ini(
        app, env, component, _group_lines(group, hosts, _host_vars_suffix(host_vars))
    )


def write_groups(
    app: str,
    env: str,
    component: str,
    groups: dict[str, list[str]],
    host_vars: Optional[dict] = None,
) -> None:
    """Write an INI inventory with one ``[group]`` section per NON-EMPTY group.

    Reuses the ``<group><n> ansible_host=<ip>`` alias format of :func:`write_hosts`.
    Empty groups are omitted entirely (so a worker list of ``[]`` writes no
    ``[workers]`` section → workers fall back to the core group). Preserves the
    trailing newline. Used by bootstrap to write a core's core+workers groups in
    one shot (workers own no directory of their own — both live in the core's
    ``hosts`` file).
    """
    suffix = _host_vars_suffix(host_vars)
    lines: list[str] = []
    for group, hosts in groups.items():
        if not hosts:
            continue
        lines += _group_lines(group, hosts, suffix)
    _write_ini(app, env, component, lines)


def ensure_common(app: str, env: str) -> None:
    """Seed an empty ``common.yml`` next to the env's component trees if absent.

    Idempotent: writes a header comment + ``---`` document marker only when the
    file does NOT already exist, so a hand-edited ``common.yml`` is never
    overwritten. The file is loaded FIRST into every component playbook's
    ``vars_files`` (see :func:`generate.generate_all`), letting users set
    app-wide vars (e.g. ``st_<app>_uid``) once instead of per component.
    """
    p = paths.common_path(app, env)
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"# st-cli app/env-wide vars for {app}/{env} — loaded before EVERY component's\n"
        "# vars.yml. Put values shared across all components here, e.g. "
        f"st_{app}_uid /\n"
        f"# st_{app}_gid / st_{app}_registries. Safe to edit by hand.\n"
        "---\n"
    )
    p.write_text(text, encoding="utf-8")


_SSH_CONFIG_SEED = """\
# st-cli shared SSH client config — COMMITTED to your deployment repo.
#
# A shared place for host / bastion (ProxyJump) definitions used to reach your
# target servers. It is mounted automatically in the st-cli container.
#
# Do NOT put private keys here, this file is committed. Keep keys in your ssh-agent
# (forward it into the container with
#   -v "$SSH_AUTH_SOCK:/ssh-agent" -e SSH_AUTH_SOCK=/ssh-agent
# ).
#
# See ssh/config.local for per-operator configurations.
#
# Example — reach every 10.0.0.* host through a bastion:
#
#   Host bastion
#       HostName bastion.example.org
#
#   Host 10.0.0.*
#       ProxyJump bastion
"""

_SSH_CONFIG_LOCAL_SEED = """\
# st-cli per-operator SSH config — GITIGNORED (never committed).
#
# This is the per-operator companion to the committed ssh/config: put your own
# ssh identity here. This file is gitignored, _never_ commit it.
#
#   Host 10.0.0.*
#       User alice
"""

_SSH_KNOWN_HOSTS_SEED = """\
# st-cli known_hosts — COMMITTED pinned host keys for your target servers.
#
# Inside the st-cli container ssh runs with StrictHostKeyChecking=accept-new against
# this file: a NEW host key is trusted on first connect and appended here (review
# `git diff ssh/known_hosts` and commit it), while a CHANGED key makes ssh refuse to
# connect (MITM protection). You can also seed keys deliberately and verify the
# fingerprints out of band:
#
#   ssh-keyscan -H 10.0.0.11 10.0.0.12 >> ssh/known_hosts
"""


def ensure_ssh_scaffold() -> None:
    """Seed the committed ``ssh/`` dir (config + known_hosts + config.local) if absent.

    Idempotent and never overwrites a hand-edited file (mirrors :func:`ensure_common`).
    ``ssh/config`` and ``ssh/known_hosts`` are COMMITTED (not gitignored): host-key /
    bastion config is not secret and the inventory IPs are already tracked. The
    st-cli container auto-Includes ``ssh/config`` (and the gitignored
    ``ssh/config.local`` first) and pins keys against ``ssh/known_hosts``; native
    installs may Include the config too. ``ssh/config.local`` is the per-operator
    place for ssh identity (User/IdentityFile/ProxyJump); it is gitignored and seeded
    fully commented so an untouched file is a no-op. No active ``Host *`` block is
    seeded in ``ssh/config`` (it could override a user's global ssh defaults when
    Included).
    """
    paths.ssh_dir().mkdir(parents=True, exist_ok=True)
    cfg = paths.ssh_config_path()
    if not cfg.exists():
        cfg.write_text(_SSH_CONFIG_SEED, encoding="utf-8")
    # ssh refuses an Included client config that is group/other-writable ("Bad owner
    # or permissions" at connect time). Git does not track file modes beyond the
    # exec bit, so a checkout under umask 002 leaves this COMMITTED file
    # group-writable (0664) — which would block every deploy. Normalise to 0644
    # (strip group/other WRITE bits) on every pass — not just at creation — so a
    # loose-mode checkout or hand-edit is repaired before the next deploy connects.
    cfg.chmod(0o644)
    local_cfg = paths.ssh_config_local_path()
    if not local_cfg.exists():
        local_cfg.write_text(_SSH_CONFIG_LOCAL_SEED, encoding="utf-8")
    # ssh refuses a client config that is group/other-writable ("Bad owner or
    # permissions" at connect time). Normalise to 0600 on every pass — not just at
    # creation — so a file seeded under a loose umask or hand-edited by the operator
    # is repaired before the next deploy connects. Safe: config.local is gitignored
    # and per-operator (git only tracks the exec bit anyway).
    local_cfg.chmod(0o600)
    kh = paths.ssh_known_hosts_path()
    if not kh.exists():
        kh.write_text(_SSH_KNOWN_HOSTS_SEED, encoding="utf-8")


_GITIGNORE_ENTRIES = [".st-cli/", ".vault-pass", "ssh/config.local"]


def ensure_gitignore() -> None:
    """Append the st-cli scaffolding-ignore entries to the repo-root ``.gitignore``.

    Idempotent: only missing entries are appended (under a header comment on first
    write). Owns the sole write to the committed ``.gitignore``.
    """
    gi = paths.repo_root() / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    missing = [e for e in _GITIGNORE_ENTRIES if e not in existing]
    if missing:
        with gi.open("a", encoding="utf-8") as fh:
            if existing and existing[-1].strip():
                fh.write("\n")
            fh.write("# st-cli generated artifacts\n")
            fh.write("\n".join(missing) + "\n")


def read_common_text(app: str, env: str) -> str:
    """Return the raw text of ``<app>/<env>/common.yml`` (``""`` if absent).

    For callers needing the pre-``---`` header comment block that the round-trip
    loader (:func:`load_common`) does not preserve.
    """
    p = paths.common_path(app, env)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def load_common(app: str, env: str):
    """Load ``<app>/<env>/common.yml`` (returns a ruamel CommentedMap; {} if absent).

    Round-trips through the shared YAML instance so comments and existing keys
    are preserved when the caller updates and saves via :func:`save_common`.
    """
    return _load_yaml(paths.common_path(app, env))


def save_common(app: str, env: str, data) -> None:
    """Write ``<app>/<env>/common.yml``, creating parent dirs.

    Merges are the caller's responsibility — load via :func:`load_common`,
    mutate the returned CommentedMap, then save. Preserves comments + order.
    """
    _save_yaml(paths.common_path(app, env), data)


def read_hosts(
    app: str, env: str, component: str, group: Optional[str] = None
) -> list[str]:
    """Parse the unit's ``hosts`` ini and return its host IPs/names.

    The hosts file is the single source of truth for a unit's hosts (they are
    NOT duplicated in .st-cli.yml). Reads the ``ansible_host=<x>`` value of each
    inventory line, falling back to the leading token.

    When ``group`` is given, only the hosts under that ``[group]`` section are
    returned; when ``None`` (default), all hosts across all groups are returned.
    Section headers (``[...]``) and ``#`` / ``;`` comments are always ignored.

    Thin wrapper over :func:`read_inventory` — returns the ip half of each pair.
    """
    return [ip for _alias, ip in read_inventory(app, env, component, group)]


def read_inventory(
    app: str, env: str, component: str, group: Optional[str] = None
) -> list[tuple[str, str]]:
    """Parse the unit's ``hosts`` ini into ``(alias, ip)`` pairs.

    ``alias`` is the inventory hostname (the leading token of each line, e.g.
    ``meet1``) — the identifier an ansible pattern / ``--limit`` matches and what
    ``-H/--host`` accepts. ``ip`` is the ``ansible_host=<x>`` value (what ssh
    connects to), falling back to the alias when no ``ansible_host`` is set. Group
    filtering + comment/header skipping mirror :func:`read_hosts`.
    """
    p = paths.hosts_path(app, env, component)
    if not p.exists():
        return []
    entries: list[tuple[str, str]] = []
    current: Optional[str] = None
    want_group = group is not None
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            current = line.strip("[]").strip()
            continue
        if want_group and current != group:
            continue
        alias = line.split()[0]
        m = re.search(r"ansible_host=(\S+)", line)
        entries.append((alias, m.group(1) if m else alias))
    return entries


def find_host(entries: list[tuple[str, str]], alias: str) -> Optional[tuple[str, str]]:
    """Return the ``(alias, ip)`` entry whose alias equals ``alias`` (else None).

    Matching is on the inventory alias only (never the ip), so ``-H`` is scoped to
    a host that provably belongs to this app/env/component's inventory.
    """
    return next((e for e in entries if e[0] == alias), None)


def component_inventory(app: str, env: str, meta, comp) -> list[tuple[str, str]]:
    """The ``(alias, ip)`` inventory a component targets (worker→core aware).

    Reads the hosts file of the component's :func:`~appmeta.files_component` under
    its :func:`effective_group`. Shared by the ssh path (``cmd/remote.py``) and the
    playbook path (``cmd/deploy.py``) so the read + group rule lives in one place.
    """
    files = meta.files_component(comp.key)
    return read_inventory(
        app, env, files.key, group=effective_group(app, env, meta, comp)
    )


def effective_group(app: str, env: str, meta, comp) -> str:
    """Return the inventory group a component should target (the DRY rule).

    Workers with their own ``[workers]`` group in the core's ``hosts`` file target
    it; a worker without one falls back to the core (files) group. Non-workers
    always target their own (files) group. ``vars_files`` stay pointed at the
    core regardless — only the targeted ``hosts:`` group changes.
    """
    files = meta.files_component(comp.key)
    if comp.is_worker and read_hosts(app, env, files.key, group=comp.app_name):
        return comp.app_name
    return files.app_name
