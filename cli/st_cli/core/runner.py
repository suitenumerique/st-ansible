"""Subprocess wrappers around ansible-galaxy / ansible-playbook."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import appmeta, paths
from .errors import StCliError


class RunnerError(StCliError):
    """Raised when an ansible subprocess exits non-zero."""


def ansible_bin(name: str) -> str:
    """Resolve an ansible executable (ansible-playbook/-galaxy/-vault).

    Prefers the copy next to st-cli's own interpreter (it also has hvac) over
    PATH. Raises `StCliError` if neither exists.
    """
    candidate = Path(sys.executable).parent / name
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise RunnerError(
        f"'{name}' not found — install ansible-core (e.g. "
        f'pipx install "st-cli[full]") or put it on your PATH.'
    )


def _ansible_env() -> dict:
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(paths.ansible_cfg_path())
    return env


def galaxy_install() -> None:
    """Install the pinned collection into ``.st-cli/collections``.

    The version is baked into ``galaxy-requirements.yml`` at generate time, so this
    just installs ``-r`` that file; no version argument is needed.
    """
    req = paths.galaxy_requirements_path()
    if not req.exists():
        raise RunnerError("galaxy-requirements.yml missing — run generate first.")
    rc = _run(
        [
            ansible_bin("ansible-galaxy"),
            "collection",
            "install",
            "-r",
            str(req),
            "-p",
            str(paths.collections_dir()),
            "--force",
        ]
    )
    if rc != 0:
        raise RunnerError("ansible-galaxy collection install failed.")


def _playbook_cmd(app: str, env: str, component: str, extra: list[str]) -> list[str]:
    from .generate import playbook_path

    # workers reuse the core unit's inventory (they own no hosts file).
    files = appmeta.load_app(app).files_component(component)
    hosts = paths.hosts_path(app, env, files.key)
    pb = playbook_path(app, env, component)
    if not pb.exists():
        raise RunnerError(f"Playbook {pb} not found — run generate first.")
    return [
        ansible_bin("ansible-playbook"),
        "--diff",
        "-i",
        str(hosts),
        str(pb),
        *extra,
    ]


def play(
    app: str,
    env: str,
    component: str,
    check: bool = False,
    tags: list[str] | None = None,
    limit: str | None = None,
) -> int:
    """Run the generated playbook for one unit; return the ansible exit code.

    `check=True` passes `--check`; `tags` and `limit` map to ansible
    `--tags`/`--limit`.
    """
    extra: list[str] = []
    if check:
        extra += ["--check"]
    if tags:
        extra += ["--tags", ",".join(tags)]
    if limit:
        extra += ["--limit", limit]
    return _run(_playbook_cmd(app, env, component, extra))


def _run(cmd: list[str]) -> int:
    proc = subprocess.run(cmd, env=_ansible_env(), check=False)
    return proc.returncode
