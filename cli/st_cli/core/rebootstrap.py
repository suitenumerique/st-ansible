"""Detect outstanding **rebootstraps**: units whose committed tree predates a
release that requires operators to replay the bootstrap questionnaire.

Some releases change what bootstrap must ask about or write for a unit — a
newly mandatory env var, a new secret, a changed default that needs an
explicit choice. Such releases are declared in the bundled, append-only
``resources/rebootstrap.yml`` (schema documented in that file's header). This
module loads that declaration and matches it against each unit's
``bootstrapped_with`` stamp (``core/models.UnitState``) to answer "which units
still need a rebootstrap, and why".

This module only detects; it does not run the questionnaire (that is another
agent's lane — see the CONTRACT). It is read-only: it never touches the
committed tree or ``.st-cli.yml``.
"""

from __future__ import annotations

import re
from pathlib import Path

from ruamel.yaml import YAML

from .models import RebootstrapNeed, StCliManifest

# Points at the bundled declaration file. Module-level (not a function-local
# constant) so tests can monkeypatch it to a tmp_path file without touching
# the real bundled resource.
_RESOURCE: Path = Path(__file__).resolve().parent / "resources" / "rebootstrap.yml"

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


def load_flags() -> list[dict]:
    """Load the raw rebootstrap flag list from the bundled resource.

    A missing file or an empty/``null`` document both yield ``[]`` — this is
    the expected shape right after this feature ships (the file starts
    empty), so it must never raise.
    """
    if not _RESOURCE.is_file():
        return []
    y = YAML(typ="safe")
    data = y.load(_RESOURCE)
    return list(data or [])


def needed(
    m: StCliManifest, app: str | None = None, env: str | None = None
) -> list[RebootstrapNeed]:
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

    ``app``/``env`` narrow the result to units matching those, when given.
    Results are sorted deterministically by ``(app, env, component, version)``.
    """
    flags = load_flags()
    out: list[RebootstrapNeed] = []
    for u in m.units:
        if u.mode == "external":
            continue
        if app is not None and u.app != app:
            continue
        if env is not None and u.env != env:
            continue
        current = parse_version(u.bootstrapped_with or "0.0.0")
        for flag in flags:
            apps = flag.get("apps")
            if apps != "all" and u.app not in (apps or []):
                continue
            if parse_version(flag.get("version")) > current:
                out.append(
                    RebootstrapNeed(
                        app=u.app,
                        env=u.env,
                        component=u.component,
                        version=str(flag.get("version", "")),
                        reason=flag.get("reason", ""),
                        link=flag.get("link", ""),
                    )
                )
    out.sort(key=lambda n: (n.app, n.env, n.component, n.version))
    return out
