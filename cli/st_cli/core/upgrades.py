"""Match per-release upgrade requirements against each unit's bootstrap stamp.

Some releases change what bootstrap must ask about or write for a unit — a
newly mandatory env var, a new secret, a changed default that needs an
explicit choice, or a whole new optional component. Such releases are
declared in the bundled ``resources/upgrades.yml`` (schema and the prune rule
documented in that file's header). This module loads that declaration and
matches it against each unit's ``bootstrapped_with`` stamp
(``core/models.UnitState``) to answer "which units still need a rebootstrap,
and why" (:func:`needed`), and "which newly declared components could a unit
now bootstrap" (:func:`new_component_offers`).

This module only detects; it does not run the questionnaire (that is
``cmd/bootstrap.py``'s lane). It is read-only: it never touches the committed
tree or ``.st-cli.yml``.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from ruamel.yaml import YAML

from . import appmeta
from .models import NewComponentOffer, StCliManifest, UpgradeNeed

# Points at the bundled declaration file. Module-level (not a function-local
# constant) so tests can monkeypatch it to a tmp_path file without touching
# the real bundled resource.
_RESOURCE: Path = Path(__file__).resolve().parent / "resources" / "upgrades.yml"

_INT_PREFIX = re.compile(r"\d+")


def parse_version(v: str | None) -> tuple[int, int, int]:
    """Parse a tolerant ``X.Y.Z`` version stamp into a comparable tuple.

    The repo's ``make version`` enforces the ``X.Y.Z`` shape for real
    releases, but this function must never raise on whatever ends up in a
    ``bootstrapped_with``/flag ``version`` field — a hand-edited manifest, an
    empty string, ``None``, or a stray suffix (e.g. ``"1.2.3-rc1"``) are all
    plausible. Each of the (up to) three dot-separated segments is parsed
    independently by taking its leading digit run; a missing or
    non-numeric-leading segment degrades to ``0`` rather than raising, so a
    malformed stamp never breaks ``doctor`` or ``needed()`` — it just sorts as
    low as possible (equivalent to "no version").
    """
    if not v:
        return (0, 0, 0)
    parts = str(v).split(".")
    out = []
    for i in range(3):
        segment = parts[i] if i < len(parts) else ""
        m = _INT_PREFIX.match(segment)
        out.append(int(m.group()) if m else 0)
    return (out[0], out[1], out[2])


def _load_document() -> dict:
    """Load the bundled resource as a mapping; tolerate every benign shape.

    A missing file or an empty/``null`` document yields ``{}`` — the expected
    shape right after this feature ships, so it must never raise. A bare list
    (the pre-baseline shape, still used by some test fixtures) reads as a
    flags-only document with no baseline.
    """
    if not _RESOURCE.is_file():
        return {}
    y = YAML(typ="safe")
    data = y.load(_RESOURCE)
    if data is None:
        return {}
    if isinstance(data, list):
        return {"flags": data}
    return data if isinstance(data, dict) else {}


def load_flags() -> list[dict]:
    """Load the raw upgrade flag list from the bundled resource."""
    return list(_load_document().get("flags") or [])


def load_baseline() -> str:
    """Load the oldest supported bootstrap stamp; ``""`` when none is set."""
    return str(_load_document().get("baseline") or "")


def needed(
    m: StCliManifest, app: str | None = None, env: str | None = None
) -> list[UpgradeNeed]:
    """Return every outstanding rebootstrap for units in ``m``.

    Walks ``m.units`` (skipping ``mode == "external"`` — externally-run units
    have no local tree for bootstrap to rewrite) against every flag in
    ``load_flags()`` whose ``apps`` is the literal string ``"all"`` or a list
    containing the unit's app. A flag applies when its ``version`` outranks
    the unit's ``bootstrapped_with`` (compared via :func:`parse_version`).

    A missing or empty ``bootstrapped_with`` means the unit predates this
    feature entirely (bootstrapped before st-cli recorded the stamp at all).
    It is treated as ``"0.0.0"`` — conservative by design for this first
    rollout: every applicable flag matches, so upgraders see the full backlog
    rather than a silently-skipped one. There is no backfill logic anywhere;
    the stamp only ever advances via a real bootstrap/rebootstrap run.

    A unit whose stamp is below the declared ``baseline`` (the oldest
    supported bootstrap stamp) gets one synthetic flag at the baseline
    version, ``interactive=True`` — a unit that far behind deserves a full
    review, not a silent replay. A replay is cumulative, so this one flag
    stands in for every entry that was pruned at or below the baseline —
    deleting those entries loses nothing but their reason text.

    ``app``/``env`` narrow the result to units matching those, when given.
    Results are sorted deterministically by ``(app, env, component, version)``.
    """
    flags = load_flags()
    baseline = load_baseline()
    baseline_v = parse_version(baseline)
    out: list[UpgradeNeed] = []
    for u in m.units:
        if u.mode == "external":
            continue
        if app is not None and u.app != app:
            continue
        if env is not None and u.env != env:
            continue
        current = parse_version(u.bootstrapped_with or "0.0.0")
        if baseline_v > current:
            out.append(
                UpgradeNeed(
                    app=u.app,
                    env=u.env,
                    component=u.component,
                    version=baseline,
                    reason=(
                        "the committed unit was bootstrapped by an st-cli "
                        "version that is no longer supported"
                    ),
                    link="",
                    interactive=True,
                )
            )
        for flag in flags:
            apps = flag.get("apps")
            if apps != "all" and u.app not in (apps or []):
                continue
            if parse_version(flag.get("version")) > current:
                out.append(
                    UpgradeNeed(
                        app=u.app,
                        env=u.env,
                        component=u.component,
                        version=str(flag.get("version", "")),
                        reason=flag.get("reason", ""),
                        link=flag.get("link", ""),
                        interactive=bool(flag.get("interactive")),
                    )
                )
    out.sort(key=lambda n: (n.app, n.env, n.component, n.version))
    return out


def newest_per_unit(needs: list[UpgradeNeed]) -> list[UpgradeNeed]:
    """Collapse ``needs`` to one, the newest, entry per ``(app, env, component)``.

    A replay is cumulative — one rebootstrap covers every older flag too — so
    callers (``check_app``, ``upgrade``) only need to report the newest need
    per unit; listing every historical flag would just be noise. The kept
    entry's ``version``/``reason``/``link`` come from the newest flag, but
    ``interactive`` is OR-ed across every collapsed entry: an older
    ``interactive`` flag must still force a full replay even when a newer
    flag for the same unit is silent. Result order is deterministic: sorted
    by ``(app, env, component)``.
    """
    newest: dict[tuple[str, str, str], UpgradeNeed] = {}
    interactive: dict[tuple[str, str, str], bool] = {}
    for n in needs:
        key = (n.app, n.env, n.component)
        cur = newest.get(key)
        if cur is None or parse_version(n.version) > parse_version(cur.version):
            newest[key] = n
        interactive[key] = interactive.get(key, False) or n.interactive
    return [
        dataclasses.replace(newest[k], interactive=interactive[k])
        for k in sorted(newest)
    ]


def offerable_components(app: str) -> set[str]:
    """Component keys of ``app`` that ``new_components`` may legally name.

    Only a ``dependencies[].on`` target can ever be reached through the
    fresh-dependency menu (``cmd/bootstrap.py``'s ``_handle_dependency``),
    which is what actually asks the operator to bootstrap a newly offered
    component — so a non-dependency component (e.g. an ``is_worker`` one)
    offers nothing and is never valid. ``meet``'s ``egress`` is a
    ``dependencies[].on`` entry too, but the deps loop always skips its own
    iteration and bundles it into the ``livekit`` step instead (see
    ``cmd/bootstrap.py``'s deps loop) — it is excluded here for the same
    reason. Best-effort: an ``appmeta`` failure yields an empty set.
    """
    try:
        meta = appmeta.load_app(app)
    except Exception:  # noqa: BLE001 — best-effort by design (see docstring);
        # a malformed bundled manifest must degrade to "nothing offerable",
        # not crash `needed()`/`doctor`/`upgrade`, so every failure mode
        # (StCliError, a YAML parse error, a KeyError on a missing field)
        # is deliberately swallowed the same way.
        return set()
    targets = {d.on for d in meta.dependencies}
    if app == "meet":
        targets.discard("egress")
    return targets


def new_component_offers(
    m: StCliManifest, app: str | None = None, env: str | None = None
) -> list[NewComponentOffer]:
    """Return components a flag declares as newly available, still unbootstrapped.

    For every flag carrying a ``new_components`` list, walks each distinct
    ``(app, env)`` pair among ``m.units`` whose app is in the flag's ``apps``
    (narrowed by the optional ``app``/``env`` filters). A flag with
    ``apps: all`` is skipped defensively: ``new_components`` needs a concrete
    app to look the component up against.

    A component is offered when all of these hold: it is one of that app's
    :func:`offerable_components` (best-effort — an appmeta failure just skips
    that pair, matching this module's tolerant style); no unit for ``(app,
    env, component)`` already exists, managed or external (either means the
    operator already decided); and the OLDEST
    ``bootstrapped_with`` stamp among that pair's non-external units is older
    than the flag version. A pair with no non-external unit has nothing to
    measure the stamp against, so it is skipped.

    The same replay that clears the flag for the pair's existing units also
    carries the offer, so once every unit reaches the flag version the offer
    goes quiet on its own — no separate bookkeeping needed.

    Results are sorted deterministically by ``(app, env, component, version)``.
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
        flag_v = parse_version(flag.get("version"))

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
                u for u in m.units if u.app == a and u.env == e and u.mode != "external"
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
                        version=str(flag.get("version", "")),
                        reason=flag.get("reason", ""),
                        link=flag.get("link", ""),
                    )
                )
    out.sort(key=lambda o: (o.app, o.env, o.component, o.version))
    return out
