"""Application metadata loader for st-cli.

Reads the bundled ``resources/apps/<app>.yml`` files (the single source of truth for the
app/component map of section 0 of CONTRACT.md) and exposes typed views consumed
by :mod:`st_cli.cmd.bootstrap` and :mod:`st_cli.core.envrender`.

This module owns the YAML files and the :class:`Dependency` / :class:`AppMeta`
dataclasses. The :class:`Component` dataclass is owned by :mod:`st_cli.core.models`
and imported from there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml import YAML

from .errors import StCliError
from .models import (
    Component,
)  # CONTRACT section 2: Component lives in st_cli.core.models


_APPS_DIR = Path(__file__).resolve().parent / "resources" / "apps"


def list_apps() -> list[str]:
    """Return the sorted list of supported app names (e.g. ``["drive","meet","messages"]``)."""
    return sorted(p.stem for p in _APPS_DIR.glob("*.yml"))


def _yaml() -> YAML:
    y = YAML(typ="safe")
    y.default_flow_style = False
    return y


def _component_from(c: dict) -> Component:
    return Component(
        key=c["key"],
        role=c["role"],
        user=c["user"],
        app_name=c["app_name"],
        dir_var=c["dir_var"],
        enabled_var=c["enabled_var"],
        deploy_order=c["deploy_order"],
        is_core=c["is_core"],
        is_worker=c.get("is_worker", False),
        implemented=c.get("implemented", True),
    )


def load_app(app: str) -> "AppMeta":
    """Load metadata for ``app`` from the bundled ``apps/<app>.yml``.

    Raises :class:`StCliError` if the app is unknown.
    """
    path = _APPS_DIR / f"{app}.yml"
    if not path.is_file():
        raise StCliError(f"unknown app {app!r}; available: {', '.join(list_apps())}")
    data = _yaml().load(path) or {}

    components = [_component_from(c) for c in data.get("components", [])]
    component_raw: dict[str, dict] = {c["key"]: c for c in data.get("components", [])}

    deps = [
        Dependency(
            of=d["of"],
            on=d["on"],
            optional=d.get("optional", False),
            shared=list(d.get("shared", []) or []),
        )
        for d in data.get("dependencies", [])
    ]

    return AppMeta(
        app=data["app"],
        env_docs_url=data.get("env_docs_url", ""),
        arch_docs_url=data.get("arch_docs_url", ""),
        components=components,
        dependencies=deps,
        _component_raw=component_raw,
    )


@dataclass
class Dependency:
    of: str  # consumer component key (the one holding the env blob)
    on: str  # provider component key (the dependency)
    optional: bool = False  # messages' mpa/socks_proxy: skippable at bootstrap
    shared: list[dict] = field(default_factory=list)
    # each rule dict: {consumer_env_key, var, generate|prompt}. Either
    # generate ("secret"|"token") or prompt (kind) is set; never both.


@dataclass
class AppMeta:
    app: str
    env_docs_url: str
    arch_docs_url: str
    components: list
    dependencies: list
    # private: the raw per-component dicts (kept for env_render lookups)
    _component_raw: dict = field(default_factory=dict, repr=False, compare=False)

    def core(self) -> Component:
        for c in self.components:
            if c.is_core:
                return c
        raise KeyError(f"app {self.app!r} has no core component")

    def worker(self) -> Component | None:
        """Return the app's workers component, or ``None`` if it has none.

        Non-raising counterpart of :meth:`core` — workers are optional metadata,
        so callers guard with ``if meta.worker():``.
        """
        for c in self.components:
            if c.is_worker:
                return c
        return None

    def component(self, key: str) -> Component:
        for c in self.components:
            if c.key == key:
                return c
        raise StCliError(
            f"unknown component {key!r} for app {self.app!r} (stale .st-cli.yml?)"
        )

    def files_component(self, key: str) -> Component:
        """Return the component whose on-disk unit files back ``key``.

        Workers own no ``vars.yml``/``vault.yml``/``hosts`` of their own — they
        reuse the core unit's files verbatim (``st_<app>_workers_env`` defaults to
        ``st_<app>_backend_env``, and they run on the same hosts). So a worker
        resolves to :meth:`core`; every other component resolves to itself.
        """
        comp = self.component(key)
        return self.core() if comp.is_worker else comp

    def env_render_spec(self, component_key: str) -> dict:
        """Return the ``env_render`` mapping for the given component.

        Shape: ``{layer: {"blob_var": str, "templates": [str, ...]}}`` where
        ``layer`` is e.g. ``"backend"`` or ``"frontend"``. Returns ``{}`` when
        the component has no templated env blob (e.g. livekit / collabora /
        mta-in / mpa / socks-proxy).
        """
        raw = self._component_raw.get(component_key, {})
        return dict(raw.get("env_render") or {})

    def component_vars(self, component_key: str) -> dict:
        """Return component-level ansible vars (``st_*``) with answer templates.

        Values are strings that may reference questionnaire answers via
        ``str.format`` placeholders, e.g. ``{"st_drive_public_host": "{DOMAIN}"}``.
        Returns ``{}`` when the component declares none.
        """
        raw = self._component_raw.get(component_key, {})
        return dict(raw.get("vars") or {})
