"""Shared dataclasses for st-cli.

Pure data holders with no I/O. ``appmeta.py`` owns ``AppMeta`` and
``Dependency``. Every other module imports the component, unit, and manifest
shapes from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MODE_MANAGED = "managed"
MODE_EXTERNAL = "external"
BACKEND_ANSIBLE_VAULT = "ansible-vault"
BACKEND_HASHI_VAULT = "hashi_vault"


@dataclass(frozen=True)
class Component:
    """A deployable piece of an app. Maps to a collection role and a systemd unit."""

    key: str  # "livekit"
    role: str  # "suitenumerique.st.meet"
    user: str  # "meet"
    app_name: str  # "livekit", the systemd unit and inventory group
    dir_var: str  # "st_meet_livekit_dir"
    enabled_var: str  # "st_meet_livekit_enabled"
    deploy_order: int  # lower deploys first
    is_core: bool  # True for the app's main Django component
    is_worker: bool  # True for the Celery workers component, same role and user as core
    # False when the role has no workers implementation yet, for example meet.
    # Such a component is metadata-only: bootstrap does not prompt for worker
    # IPs and does not register a workers unit, so it never deploys.
    implemented: bool = True


@dataclass
class UnitState:
    """One bootstrapped component for an app and env, as recorded in .st-cli.yml."""

    app: str
    env: str
    component: str  # component key
    mode: str  # MODE_MANAGED (deployed by us) or MODE_EXTERNAL (runs elsewhere)
    # Hosts are not stored here. The <app>/<env>/<component>/hosts file is
    # the source of truth, read through tree.read_hosts().
    # bootstrap stamps this value on every run, including a rebootstrap. It
    # records an action st-cli performed, not an operator's manual work.
    # An empty value means the unit predates this field.
    bootstrapped_with: str = ""


@dataclass
class SecretConfig:
    """Per app, per env secret backend choice, recorded in .st-cli.yml.

    Only the backend name lives here. Connection details for hashi_vault live
    in <app>/<env>/common.yml.
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

    Produced by core/upgrades.needed() when a flag's version outranks the
    unit's bootstrapped_with stamp.
    """

    app: str
    env: str
    component: str
    version: str  # the flagged release version, for example "0.3.0"
    reason: str
    link: str
    # True forces a full pre-filled replay through ReplayAction.MODIFY,
    # instead of a silent one. A unit that old, or a change that big, needs
    # a full review.
    full_replay: bool = False
    # Manual steps a rebootstrap cannot do for the operator, from the flag's
    # warnings list. Empty when the flag carries none.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class NewComponentOffer:
    """A component newly declared by a flag that a unit could now bootstrap.

    Produced by core/upgrades.new_component_offers() when the flag names
    component as newly available for app, and (app, env) has no unit for it.
    """

    app: str
    env: str
    component: str
    version: str  # the flag version that introduced the component
    reason: str
    link: str
