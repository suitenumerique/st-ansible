"""Match per-release upgrade requirements against each unit's bootstrap stamp.

Declared in the bundled `resources/upgrades.yml`. Read-only: it detects
which units need a rebootstrap; it never runs the questionnaire itself.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

from . import appmeta, tree
from .models import (
    MODE_EXTERNAL,
    NewComponentOffer,
    StCliManifest,
    UnitState,
    UpgradeNeed,
)

# Points at the bundled declaration file. Module-level (not a function-local
# constant) so tests can monkeypatch it to a tmp_path file without touching
# the real bundled resource.
_RESOURCE: Path = Path(__file__).resolve().parent / "resources" / "upgrades.yml"

_INT_PREFIX = re.compile(r"\d+")

NEXT = "next"
NEXT_RANK = (sys.maxsize, 0, 0)
_CHANGELOG_URL = (
    "https://github.com/suitenumerique/st-ansible/blob/main/CHANGELOG.md#v{anchor}"
)


def _normalise_warnings(raw: object) -> tuple[str, ...]:
    """Normalise `warnings` to a tuple of non-empty strings; malformed input yields
    `()`."""
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(s.strip() for s in raw if isinstance(s, str) and s.strip())


def parse_version(v: str | None) -> tuple[int, int, int]:
    """Parse a tolerant `X.Y.Z` version stamp into a comparable tuple.

    Never raises: a missing or non-numeric segment degrades to 0. `next`
    outranks every real version.
    """
    if v == NEXT:
        return NEXT_RANK
    if not v:
        return (0, 0, 0)
    parts = str(v).split(".")
    out = []
    for i in range(3):
        segment = parts[i] if i < len(parts) else ""
        m = _INT_PREFIX.match(segment)
        out.append(int(m.group()) if m else 0)
    return (out[0], out[1], out[2])


def changelog_link(version: str | None) -> str:
    """Return the CHANGELOG anchor of a release; `""` for `next` or no version."""
    if not version or version == NEXT:
        return ""
    return _CHANGELOG_URL.format(anchor=str(version).replace(".", "-"))


def _load_document() -> dict:
    """Load the bundled resource as a mapping; tolerates every benign shape.

    A bare list (the pre-baseline shape) reads as a flags-only document.
    """
    if not _RESOURCE.is_file():
        return {}
    data = tree.yaml_safe().load(_RESOURCE)
    if data is None:
        return {}
    if isinstance(data, list):
        return {"flags": data}
    return data if isinstance(data, dict) else {}


def load_flags() -> list[dict]:
    """Return the raw upgrade flag list from the bundled resource."""
    return list(_load_document().get("flags") or [])


def load_baseline() -> str:
    """Return the oldest supported bootstrap stamp; `""` when none is set."""
    return str(_load_document().get("baseline") or "")


def _need(
    u: UnitState,
    *,
    version: str,
    reason: str,
    link: str,
    full_replay: bool,
    warnings: tuple[str, ...] = (),
) -> UpgradeNeed:
    return UpgradeNeed(
        app=u.app,
        env=u.env,
        component=u.component,
        version=version,
        reason=reason,
        link=link,
        full_replay=full_replay,
        warnings=warnings,
    )


def needed(
    m: StCliManifest, app: str | None = None, env: str | None = None
) -> list[UpgradeNeed]:
    """Return every outstanding rebootstrap need for units in `m`.

    Skips external units. A unit below the declared baseline gets one
    synthetic full-replay need. Sorted by (app, env, component, version).
    """
    flags = load_flags()
    baseline = load_baseline()
    baseline_v = parse_version(baseline)
    out: list[UpgradeNeed] = []
    for u in m.units:
        if u.mode == MODE_EXTERNAL:
            continue
        if app is not None and u.app != app:
            continue
        if env is not None and u.env != env:
            continue
        current = parse_version(u.bootstrapped_with or "0.0.0")
        if baseline_v > current:
            out.append(
                _need(
                    u,
                    version=baseline,
                    reason=(
                        "the committed unit was bootstrapped by an st-cli "
                        "version that is no longer supported"
                    ),
                    link="",
                    full_replay=True,
                )
            )
        for flag in flags:
            apps = flag.get("apps")
            if apps != "all" and u.app not in (apps or []):
                continue
            components = flag.get("components")
            if components and u.component not in components:
                continue
            if parse_version(flag.get("version")) > current:
                flag_version = str(flag.get("version", ""))
                out.append(
                    _need(
                        u,
                        version=flag_version,
                        reason=flag.get("reason", ""),
                        link=flag.get("link") or changelog_link(flag_version),
                        full_replay=bool(flag.get("full_replay")),
                        warnings=_normalise_warnings(flag.get("warnings")),
                    )
                )
    out.sort(key=lambda n: (n.app, n.env, n.component, n.version))
    return out


def newest_per_unit(needs: list[UpgradeNeed]) -> list[UpgradeNeed]:
    """Collapse `needs` to the newest entry per (app, env, component).

    `full_replay` is OR-ed and `warnings` unioned across the collapsed
    entries, so an older full-replay or warning still surfaces.
    """
    newest: dict[tuple[str, str, str], UpgradeNeed] = {}
    full_replay: dict[tuple[str, str, str], bool] = {}
    warnings: dict[tuple[str, str, str], list[str]] = {}
    for n in needs:
        key = (n.app, n.env, n.component)
        cur = newest.get(key)
        if cur is None or parse_version(n.version) > parse_version(cur.version):
            newest[key] = n
        full_replay[key] = full_replay.get(key, False) or n.full_replay
        bucket = warnings.setdefault(key, [])
        for text in n.warnings:
            if text not in bucket:
                bucket.append(text)
    return [
        dataclasses.replace(
            newest[k], full_replay=full_replay[k], warnings=tuple(warnings[k])
        )
        for k in sorted(newest)
    ]


def pending_warnings(needs: list[UpgradeNeed]) -> list[tuple[str, str]]:
    """Return every distinct `(version, text)` warning pair carried by `needs`.

    Reads every need, not only the newest per unit, so an operator who
    skips releases sees every intervening manual step.
    """
    first_index: dict[tuple[str, str], int] = {}
    for i, n in enumerate(needs):
        for text in n.warnings:
            key = (n.version, text)
            if key not in first_index:
                first_index[key] = i
    return sorted(first_index, key=lambda k: (parse_version(k[0]), first_index[k]))


def offerable_components(app: str) -> set[str]:
    """Return `app` component keys that `new_components` may legally name.

    Only `dependencies[].on` targets qualify; meet's egress is excluded
    since it is bundled into the livekit step. Best-effort: yields `set()`.
    """
    try:
        meta = appmeta.load_app(app)
    except Exception:  # noqa: BLE001
        # A malformed manifest degrades to "nothing offerable".
        return set()
    targets = {d.on for d in meta.dependencies}
    if app == "meet":
        targets.discard("egress")
    return targets


def new_component_offers(
    m: StCliManifest, app: str | None = None, env: str | None = None
) -> list[NewComponentOffer]:
    """Return components a flag newly offers that are not yet bootstrapped.

    A flag with `apps: all` is skipped: `new_components` needs a concrete
    app. A pair with no non-external unit is skipped too. Sorted by
    (app, env, component, version).
    """
    out: list[NewComponentOffer] = []
    for flag in load_flags():
        new_components = flag.get("new_components")
        if not new_components:
            continue
        apps = flag.get("apps")
        if apps == "all":
            continue
        flag_apps = set(apps or [])
        flag_version = str(flag.get("version", ""))
        flag_v = parse_version(flag_version)

        pairs = sorted(
            {
                (u.app, u.env)
                for u in m.units
                if u.app in flag_apps
                and (app is None or u.app == app)
                and (env is None or u.env == env)
            }
        )
        for a, e in pairs:
            offerable = offerable_components(a)
            if not offerable:
                continue

            existing = {u.component for u in m.units if u.app == a and u.env == e}
            non_external = [
                u
                for u in m.units
                if u.app == a and u.env == e and u.mode != MODE_EXTERNAL
            ]
            if not non_external:
                continue
            oldest = min(
                parse_version(u.bootstrapped_with or "0.0.0") for u in non_external
            )
            if oldest >= flag_v:
                continue

            for comp in new_components:
                if comp not in offerable or comp in existing:
                    continue
                out.append(
                    NewComponentOffer(
                        app=a,
                        env=e,
                        component=comp,
                        version=flag_version,
                        reason=flag.get("reason", ""),
                        link=flag.get("link") or changelog_link(flag_version),
                    )
                )
    out.sort(key=lambda o: (o.app, o.env, o.component, o.version))
    return out
