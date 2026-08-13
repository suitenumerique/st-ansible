"""Shared dataclasses for st-cli (CONTRACT section 2).

These are pure data holders with no I/O. ``appmeta.py`` owns ``AppMeta`` and
``Dependency``; everything else that needs the component/unit/manifest shapes
imports from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Component:
    """A deployable piece of an app (maps to a collection role + systemd unit)."""

    key: str  # "livekit"
    role: str  # "suitenumerique.st.meet"
    user: str  # "meet"
    app_name: str  # "livekit" (systemd unit + inventory group)
    dir_var: str  # "st_meet_livekit_dir"
    enabled_var: str  # "st_meet_livekit_enabled"
    deploy_order: int  # lower deploys first
    is_core: bool  # True for the app's main Django component
    is_worker: (
        bool  # True for the app's Celery workers component (same role/user as core)
    )
    # False when the role has no workers implementation yet (e.g. meet) — such a
    # component is metadata-only: bootstrap neither prompts for worker IPs nor
    # registers a workers unit, so it never deploys.
    implemented: bool = True


@dataclass
class UnitState:
    """One bootstrapped component for an (app, env), as recorded in .st-cli.yml."""

    app: str
    env: str
    component: str  # component key
    mode: str  # "managed" (deployed by us) | "external" (runs elsewhere)
    # NB: hosts are NOT stored here — the <app>/<env>/<component>/hosts ini file
    # is the single source of truth (read via tree.read_hosts()).
    # The st-cli version whose bootstrap questionnaire last ran for this unit.
    # Stamped by `bootstrap` itself on every run (including a rebootstrap) —
    # it records an action st-cli performed, never a claim that an operator
    # did some matching manual work. Empty ("") means the unit predates this
    # field (bootstrapped before rebootstrap tracking existed); see
    # `core/upgrades.py` for how that default is treated.
    bootstrapped_with: str = ""


@dataclass
class SecretConfig:
    """Per-(app, env) secret backend choice recorded in .st-cli.yml.

    Only the backend name lives here — no paths, mount, or connection details
    (those go in ``<app>/<env>/common.yml`` for hashi_vault). A manifest with no
    ``secrets:`` block yields the ``ansible-vault`` default.
    """

    app: str
    env: str
    backend: str = "ansible-vault"  # "ansible-vault" | "hashi_vault"


@dataclass
class StCliManifest:
    """In-memory view of .st-cli.yml."""

    collection_version: str
    cli_version: str
    units: list[UnitState] = field(default_factory=list)
    secrets: list[SecretConfig] = field(default_factory=list)


@dataclass(frozen=True)
class UpgradeNeed:
    """One outstanding rebootstrap flag matched against a bootstrapped unit.

    Produced by ``core/upgrades.needed()`` (one flag x one unit, when the
    flag's version outranks the unit's ``bootstrapped_with`` stamp). Pure data
    — no I/O — hence its home here alongside the other manifest-shaped holders
    rather than in ``core/upgrades.py``, which owns the matching logic.
    """

    app: str
    env: str
    component: str
    version: str  # the flagged release version, e.g. "0.3.0"
    reason: str
    link: str
    # True forces a full pre-filled replay (ReplayAction.MODIFY) instead of a
    # silent one — a unit that old, or a change that big, needs a full review.
    interactive: bool = False


@dataclass(frozen=True)
class NewComponentOffer:
    """A component newly declared by a flag that a unit could now bootstrap.

    Produced by ``core/upgrades.new_component_offers()``: the flag names
    ``component`` as newly available for ``app``, and ``(app, env)`` has no
    unit for it yet. Pure data, same rationale as :class:`UpgradeNeed`.
    """

    app: str
    env: str
    component: str
    version: str  # the flag version that introduced the component
    reason: str
    link: str
