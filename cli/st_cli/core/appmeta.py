"""Application metadata loader for st-cli.

Reads the bundled `resources/apps/<app>.yml` files and exposes typed views
consumed by `st_cli.cmd.bootstrap` and `st_cli.core.envrender`.

This module owns the `Dependency` and `AppMeta` dataclasses. `Component` is
owned by `st_cli.core.models` and imported from there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import tree
from .errors import StCliError
from .models import Component

_APPS_DIR = Path(__file__).resolve().parent / "resources" / "apps"


def list_apps() -> list[str]:
    """Return the sorted list of supported app names (e.g.
    `["drive","meet","messages"]`)."""
    return sorted(p.stem for p in _APPS_DIR.glob("*.yml"))


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


def load_app(app: str) -> AppMeta:
    """Load metadata for `app` from the bundled `apps/<app>.yml`.

    Raises `StCliError` if the app is unknown.
    """
    path = _APPS_DIR / f"{app}.yml"
    if not path.is_file():
        raise StCliError(f"unknown app {app!r}; available: {', '.join(list_apps())}")
    data = tree.yaml_safe().load(path) or {}

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
        requires=list(data.get("requires", []) or []),
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
    # External infrastructure the operator must provision before bootstrapping,
    # as capability keys ("postgresql", "redis", "s3", "oidc"). Drives the
    # pre-questionnaire Requirements checklist; empty ⇒ the generic full list.
    requires: list = field(default_factory=list)
    # private: the raw per-component dicts (kept for env_render lookups)
    _component_raw: dict = field(default_factory=dict, repr=False, compare=False)

    def core(self) -> Component:
        for c in self.components:
            if c.is_core:
                return c
        raise KeyError(f"app {self.app!r} has no core component")

    def worker(self) -> Component | None:
        """Return the app's workers component, or `None` if it has none.

        Non-raising counterpart of `core`.
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
        """Return the component whose on-disk unit files back `key`.

        A worker resolves to `core`; every other component resolves to itself.
        """
        comp = self.component(key)
        return self.core() if comp.is_worker else comp

    def env_render_spec(self, component_key: str) -> dict:
        """Return the `env_render` mapping for the given component.

        Shape: `{layer: {"blob_var": str, "templates": [str, ...]}}`. Returns
        `{}` when the component has no templated env blob.
        """
        raw = self._component_raw.get(component_key, {})
        return dict(raw.get("env_render") or {})

    def component_vars(self, component_key: str) -> dict:
        """Return component-level ansible vars (`st_*`) with answer templates.

        Values may reference answers via `str.format` placeholders. Returns
        `{}` when the component declares none.
        """
        raw = self._component_raw.get(component_key, {})
        return dict(raw.get("vars") or {})
