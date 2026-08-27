"""Read/write the committed config tree: per-unit vars.yml/hosts, common.yml, the ssh/
scaffold, and .gitignore.

Uses one round-trip ruamel YAML instance that preserves comments and vault tags.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from ruamel.yaml import YAML

from . import paths, ui
from .errors import StCliError


class VaultString(str):
    """A string carrying the ``!vault`` tag, rendered as a literal block scalar."""

    yaml_tag = "!vault"


def _construct_vault(_constructor, node):
    return VaultString(node.value)


def _represent_vault(representer, data):
    return representer.represent_scalar(VaultString.yaml_tag, str(data), style="|")


_YAML: YAML | None = None
_SAFE_YAML: YAML | None = None


def yaml() -> YAML:
    """Return the shared round-trip YAML instance (with !vault registered)."""
    global _YAML
    if _YAML is None:
        y = YAML(typ="rt")
        y.preserve_quotes = True
        y.width = 4096  # don't wrap long env lines
        y.indent(mapping=2, sequence=4, offset=2)
        y.constructor.add_constructor(VaultString.yaml_tag, _construct_vault)
        y.representer.add_representer(VaultString, _represent_vault)
        _YAML = y
    return _YAML


def yaml_safe() -> YAML:
    """Return the shared safe-load YAML instance, for read-only bundled resources."""
    global _SAFE_YAML
    if _SAFE_YAML is None:
        y = YAML(typ="safe")
        y.default_flow_style = False
        _SAFE_YAML = y
    return _SAFE_YAML


def _load_yaml(path: Path):
    """Load a YAML file via the shared round-trip instance.

    Returns an empty ``CommentedMap`` when the file is absent or empty, so a
    caller always gets a mutable mapping whose comments and order round-trip on save.
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


def _group_lines(group: str, hosts: list[str]) -> list[str]:
    lines = [f"[{group}]"]
    for i, ip in enumerate(hosts, start=1):
        lines.append(f"{group}{i} ansible_host={ip}")
    return lines


def _write_ini(app: str, env: str, component: str, lines: list[str]) -> None:
    p = paths.hosts_path(app, env, component)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_hosts(
    app: str, env: str, component: str, group: str, hosts: list[str]
) -> None:
    """Write an INI inventory with one ``[group]`` listing the given host IPs.

    Each host gets a generated alias ``<group><n>`` with ``ansible_host=<ip>``.
    """
    _write_ini(app, env, component, _group_lines(group, hosts))


def write_groups(
    app: str, env: str, component: str, groups: dict[str, list[str]]
) -> None:
    """Write an INI inventory with one ``[group]`` section per non-empty group.

    Empty groups are omitted entirely, so a worker list of ``[]`` writes no
    ``[workers]`` section and workers fall back to the core group.
    """
    lines: list[str] = []
    for group, hosts in groups.items():
        if not hosts:
            continue
        lines += _group_lines(group, hosts)
    _write_ini(app, env, component, lines)


def ensure_common(app: str, env: str) -> None:
    """Seed an empty ``common.yml`` next to the env's component trees if absent.

    Never overwrites an existing file, so a hand-edited ``common.yml`` stays intact.
    """
    p = paths.common_path(app, env)
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    var_app = app.replace("-", "_")  # ansible var names can't carry dashes
    text = (
        f"# st-cli app/env-wide vars for {app}/{env} — loaded before EVERY component's\n"
        "# vars.yml. Put values shared across all components here, e.g. "
        f"st_{var_app}_uid /\n"
        f"# st_{var_app}_gid / st_{var_app}_registries. Safe to edit by hand.\n"
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
    """Seed the committed ``ssh/`` dir (config, known_hosts, config.local) if absent.

    Never overwrites a hand-edited file. ``ssh/config`` and ``ssh/known_hosts`` are
    committed; ``ssh/config.local`` is gitignored and seeded fully commented.
    """
    paths.ssh_dir().mkdir(parents=True, exist_ok=True)
    cfg = paths.ssh_config_path()
    if not cfg.exists():
        cfg.write_text(_SSH_CONFIG_SEED, encoding="utf-8")
    # Git tracks no mode beyond the exec bit, so a checkout under umask 002 can leave
    # this committed file group-writable; ssh then refuses it. Repair on every pass.
    cfg.chmod(0o644)
    local_cfg = paths.ssh_config_local_path()
    if not local_cfg.exists():
        local_cfg.write_text(_SSH_CONFIG_LOCAL_SEED, encoding="utf-8")
    # ssh refuses a group/other-writable client config. Repair on every pass.
    local_cfg.chmod(paths.SECRET_FILE_MODE)
    kh = paths.ssh_known_hosts_path()
    if not kh.exists():
        kh.write_text(_SSH_KNOWN_HOSTS_SEED, encoding="utf-8")


_GITIGNORE_ENTRIES = [".st-cli/", ".vault-pass", "ssh/config.local"]


def ensure_gitignore() -> None:
    """Append the missing st-cli scaffolding-ignore entries to the repo-root
    ``.gitignore``."""
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

    Keeps the pre-``---`` header comment block that `load_common` does not preserve.
    """
    p = paths.common_path(app, env)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def load_common(app: str, env: str):
    """Load ``<app>/<env>/common.yml`` (returns a ruamel CommentedMap; {} if absent)."""
    return _load_yaml(paths.common_path(app, env))


def save_common(app: str, env: str, data) -> None:
    """Write ``<app>/<env>/common.yml``, creating parent dirs.

    A merge is the caller's responsibility: load, mutate, then save.
    """
    _save_yaml(paths.common_path(app, env), data)


def read_hosts(
    app: str, env: str, component: str, group: str | None = None
) -> list[str]:
    """Parse the unit's ``hosts`` ini and return its host IPs/names.

    ``group`` narrows to one ``[group]`` section; ``None`` returns every group.
    """
    return [ip for _alias, ip in read_inventory(app, env, component, group)]


def read_inventory(
    app: str, env: str, component: str, group: str | None = None
) -> list[tuple[str, str]]:
    """Parse the unit's ``hosts`` ini into ``(alias, ip)`` pairs.

    ``alias`` is the inventory hostname an ansible pattern or ``-H/--host`` matches.
    ``ip`` is the ``ansible_host=<x>`` value, falling back to the alias.
    """
    p = paths.hosts_path(app, env, component)
    if not p.exists():
        return []
    entries: list[tuple[str, str]] = []
    current: str | None = None
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


def find_host(entries: list[tuple[str, str]], alias: str) -> tuple[str, str] | None:
    """Return the ``(alias, ip)`` entry whose alias equals ``alias`` (else ``None``).

    Matches on the inventory alias only, never the ip.
    """
    return next((e for e in entries if e[0] == alias), None)


def component_inventory(app: str, env: str, meta, comp) -> list[tuple[str, str]]:
    """Return the ``(alias, ip)`` inventory a component targets (worker-to-core
    aware)."""
    files = meta.files_component(comp.key)
    return read_inventory(
        app, env, files.key, group=effective_group(app, env, meta, comp)
    )


def effective_group(app: str, env: str, meta, comp) -> str:
    """Return the inventory group a component should target.

    A worker with its own ``[workers]`` group targets it; otherwise it falls back
    to the core group. A non-worker always targets its own group.
    """
    files = meta.files_component(comp.key)
    if comp.is_worker and read_hosts(app, env, files.key, group=comp.app_name):
        return comp.app_name
    return files.app_name


def alias_list(entries: list[tuple[str, str]]) -> str:
    """Format ``(alias, ip)`` entries as a comma-separated list of aliases."""
    return ", ".join(a for a, _ in entries)


def iter_targeted_hosts(
    app: str,
    env: str,
    meta,
    units,
    components: list[str] | None,
    host: str | None,
    skip_workers: bool = False,
) -> Iterator[tuple[object, str, str]]:
    """Yield ``(comp, alias, ip)`` for every targeted host across ``units``.

    Default is all hosts of each component; ``host`` (an alias) narrows to one.
    With ``components`` set, a missing ``host`` raises; without it, a component
    that lacks the host is skipped. ``skip_workers`` drops ``is_worker`` components.
    """
    matched = False
    for u in units:
        comp = meta.component(u.component)
        if skip_workers and comp.is_worker:
            continue
        entries = component_inventory(app, env, meta, comp)
        if not entries:
            raise StCliError(
                f"Unit {app}/{env}/{u.component} has no hosts (check its hosts file)."
            )
        if host is not None:
            e = find_host(entries, host)
            if e is None:
                if components:
                    raise StCliError(
                        f"Host '{host}' is not an alias of {app}/{env}/{u.component}: "
                        f"{alias_list(entries)}."
                    )
                ui.info(f"Skipping {u.component}: alias '{host}' not in its inventory.")
                continue
            entries = [e]
        for alias, ip in entries:
            matched = True
            yield comp, alias, ip
    if host is not None and not matched:
        raise StCliError(
            f"Host alias '{host}' matched no managed component's inventory."
        )
