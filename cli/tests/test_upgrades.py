"""Tests for st_cli.core.upgrades — flag declaration + detection."""

from __future__ import annotations

import re

import ruamel.yaml

import st_cli
from st_cli.core import appmeta, upgrades
from st_cli.core.models import NewComponentOffer, StCliManifest, UnitState, UpgradeNeed

# --------------------------------------------------------------------------- parse_version


def test_parse_version_well_formed():
    assert upgrades.parse_version("1.2.3") == (1, 2, 3)
    assert upgrades.parse_version("0.0.0") == (0, 0, 0)


def test_parse_version_garbage_inputs_degrade_to_zero():
    assert upgrades.parse_version("") == (0, 0, 0)
    assert upgrades.parse_version(None) == (0, 0, 0)
    assert upgrades.parse_version("garbage") == (0, 0, 0)
    assert upgrades.parse_version("not.a.version") == (0, 0, 0)


def test_parse_version_stray_suffix_degrades_that_segment_only():
    # leading digit run is taken per-segment; a non-numeric-leading segment
    # (or a missing one) falls back to 0, rather than the whole tuple.
    assert upgrades.parse_version("1.2.3-rc1") == (1, 2, 3)
    assert upgrades.parse_version("1.2") == (1, 2, 0)
    assert upgrades.parse_version("1") == (1, 0, 0)
    assert upgrades.parse_version("1.x.3") == (1, 0, 3)


# --------------------------------------------------------------------------- load_flags


def test_load_flags_missing_file_yields_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrades, "_RESOURCE", tmp_path / "does-not-exist.yml")
    assert upgrades.load_flags() == []


def test_load_flags_empty_file_yields_empty_list(tmp_path, monkeypatch):
    p = tmp_path / "upgrades.yml"
    p.write_text("# nothing here yet\n[]\n")
    monkeypatch.setattr(upgrades, "_RESOURCE", p)
    assert upgrades.load_flags() == []


def test_load_flags_null_document_yields_empty_list(tmp_path, monkeypatch):
    p = tmp_path / "upgrades.yml"
    p.write_text("")
    monkeypatch.setattr(upgrades, "_RESOURCE", p)
    assert upgrades.load_flags() == []


def test_load_flags_real_content(tmp_path, monkeypatch):
    p = tmp_path / "upgrades.yml"
    p.write_text(
        "- version: '0.3.0'\n"
        "  apps: [meet, drive]\n"
        "  reason: 'mandatory recording env vars'\n"
        "  link: 'https://example.org/changelog#v030'\n"
    )
    monkeypatch.setattr(upgrades, "_RESOURCE", p)
    flags = upgrades.load_flags()
    assert len(flags) == 1
    assert flags[0]["version"] == "0.3.0"
    assert flags[0]["apps"] == ["meet", "drive"]
    assert flags[0]["reason"] == "mandatory recording env vars"


def test_load_mapping_shape_yields_baseline_and_flags(tmp_path, monkeypatch):
    p = tmp_path / "upgrades.yml"
    p.write_text(
        "baseline: '0.2.0'\n"
        "flags:\n"
        "  - version: '0.3.0'\n"
        "    apps: all\n"
        "    reason: 'r'\n"
        "    link: 'l'\n"
    )
    monkeypatch.setattr(upgrades, "_RESOURCE", p)
    assert upgrades.load_baseline() == "0.2.0"
    assert [f["version"] for f in upgrades.load_flags()] == ["0.3.0"]


def test_load_baseline_absent_yields_empty_string(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrades, "_RESOURCE", tmp_path / "does-not-exist.yml")
    assert upgrades.load_baseline() == ""
    p = tmp_path / "upgrades.yml"
    p.write_text("- version: '0.3.0'\n  apps: all\n  reason: 'r'\n  link: 'l'\n")
    monkeypatch.setattr(upgrades, "_RESOURCE", p)
    assert upgrades.load_baseline() == ""


# --------------------------------------------------------------------------- needed()


def _set_flags(monkeypatch, tmp_path, flags: list[dict]):
    p = tmp_path / "upgrades.yml"
    y = ruamel.yaml.YAML(typ="safe")
    with p.open("w", encoding="utf-8") as fh:
        y.dump(flags, fh)
    monkeypatch.setattr(upgrades, "_RESOURCE", p)
    return p


def _manifest(units):
    return StCliManifest("0.0.20", "0.0.20", units)


def test_needed_no_flags_yields_empty(tmp_path, monkeypatch):
    _set_flags(monkeypatch, tmp_path, [])
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.1.0")])
    assert upgrades.needed(m) == []


def test_needed_flag_older_than_stamp_is_not_needed(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.1.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.2.0")])
    assert upgrades.needed(m) == []


def test_needed_flag_newer_than_stamp_is_needed(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.2.0")])
    result = upgrades.needed(m)
    assert result == [
        UpgradeNeed(
            app="meet",
            env="prod",
            component="meet",
            version="0.3.0",
            reason="r",
            link="l",
        )
    ]


def test_needed_apps_all_matches_every_app(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest(
        [
            UnitState("meet", "prod", "meet", "managed", "0.1.0"),
            UnitState("drive", "prod", "drive", "managed", "0.1.0"),
        ]
    )
    result = upgrades.needed(m)
    assert {(n.app, n.component) for n in result} == {
        ("drive", "drive"),
        ("meet", "meet"),
    }


def test_needed_app_not_in_flag_list_is_skipped(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": ["meet"], "reason": "r", "link": "l"}],
    )
    m = _manifest(
        [
            UnitState("meet", "prod", "meet", "managed", "0.1.0"),
            UnitState("drive", "prod", "drive", "managed", "0.1.0"),
        ]
    )
    result = upgrades.needed(m)
    assert [n.app for n in result] == ["meet"]


def test_needed_missing_stamp_treated_as_0_0_0(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.0.1", "apps": "all", "reason": "r", "link": "l"}],
    )
    # bootstrapped_with defaults to "" — a unit that predates the feature.
    m = _manifest([UnitState("meet", "prod", "meet", "managed")])
    result = upgrades.needed(m)
    assert len(result) == 1
    assert result[0].version == "0.0.1"


def test_needed_external_units_are_skipped(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest([UnitState("meet", "prod", "livekit", "external", "0.1.0")])
    assert upgrades.needed(m) == []


def test_needed_filters_by_app_and_env(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest(
        [
            UnitState("meet", "prod", "meet", "managed", "0.1.0"),
            UnitState("meet", "staging", "meet", "managed", "0.1.0"),
            UnitState("drive", "prod", "drive", "managed", "0.1.0"),
        ]
    )
    result = upgrades.needed(m, app="meet")
    assert {(n.app, n.env) for n in result} == {("meet", "prod"), ("meet", "staging")}

    result = upgrades.needed(m, app="meet", env="prod")
    assert [(n.app, n.env) for n in result] == [("meet", "prod")]


def test_needed_carries_interactive_from_flag(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": "0.3.0",
                "apps": "all",
                "reason": "r",
                "link": "l",
                "interactive": True,
            }
        ],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.2.0")])
    result = upgrades.needed(m)
    assert result[0].interactive is True


def test_needed_interactive_defaults_to_false(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.2.0")])
    result = upgrades.needed(m)
    assert result[0].interactive is False


def test_needed_is_deterministically_ordered(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [
            {"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"},
            {"version": "0.2.0", "apps": "all", "reason": "r2", "link": "l2"},
        ],
    )
    m = _manifest(
        [
            UnitState("meet", "prod", "workers", "managed", "0.0.1"),
            UnitState("meet", "prod", "meet", "managed", "0.0.1"),
            UnitState("drive", "prod", "drive", "managed", "0.0.1"),
        ]
    )
    result = upgrades.needed(m)
    keys = [(n.app, n.env, n.component, n.version) for n in result]
    assert keys == sorted(keys)


# --------------------------------------------------------------------------- newest_per_unit


def test_newest_per_unit_collapses_same_unit_to_higher_version():
    lo = UpgradeNeed("meet", "prod", "meet", "0.2.0", "older", "")
    hi = UpgradeNeed("meet", "prod", "meet", "0.4.0", "newer", "")
    assert upgrades.newest_per_unit([lo, hi]) == [hi]
    assert upgrades.newest_per_unit([hi, lo]) == [hi]  # order-independent


def test_newest_per_unit_keeps_distinct_units():
    a = UpgradeNeed("meet", "prod", "meet", "0.2.0", "r", "")
    b = UpgradeNeed("meet", "prod", "workers", "0.2.0", "r", "")
    c = UpgradeNeed("drive", "prod", "drive", "0.2.0", "r", "")
    assert upgrades.newest_per_unit([a, b, c]) == [c, a, b]  # sorted by unit key


def test_newest_per_unit_ors_interactive_across_the_collapsed_set():
    # An older interactive flag must survive a newer silent one: the result
    # carries the NEWEST version/reason, but interactive stays True.
    old_interactive = UpgradeNeed(
        "meet", "prod", "meet", "0.2.0", "older", "", interactive=True
    )
    new_silent = UpgradeNeed(
        "meet", "prod", "meet", "0.4.0", "newer", "", interactive=False
    )
    result = upgrades.newest_per_unit([old_interactive, new_silent])
    assert len(result) == 1
    assert result[0].version == "0.4.0"
    assert result[0].reason == "newer"
    assert result[0].interactive is True
    # order-independent
    assert upgrades.newest_per_unit([new_silent, old_interactive]) == result


def _set_document(monkeypatch, tmp_path, doc: dict):
    p = tmp_path / "upgrades.yml"
    y = ruamel.yaml.YAML(typ="safe")
    with p.open("w", encoding="utf-8") as fh:
        y.dump(doc, fh)
    monkeypatch.setattr(upgrades, "_RESOURCE", p)


def test_needed_stamp_below_baseline_gets_synthetic_flag(tmp_path, monkeypatch):
    _set_document(monkeypatch, tmp_path, {"baseline": "0.2.0", "flags": []})
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.1.0")])
    result = upgrades.needed(m)
    assert len(result) == 1
    assert result[0].version == "0.2.0"
    assert "no longer supported" in result[0].reason
    assert result[0].link == ""
    # a unit this far behind needs a full review, not a silent replay.
    assert result[0].interactive is True


def test_needed_stamp_at_baseline_gets_no_synthetic_flag(tmp_path, monkeypatch):
    _set_document(monkeypatch, tmp_path, {"baseline": "0.2.0", "flags": []})
    m = _manifest(
        [
            UnitState("meet", "prod", "meet", "managed", "0.2.0"),
            UnitState("drive", "prod", "drive", "managed", "0.3.0"),
        ]
    )
    assert upgrades.needed(m) == []


def test_needed_baseline_and_real_flag_both_reported(tmp_path, monkeypatch):
    # drift/bootstrap collapse to the newest flag per unit; needed() itself
    # reports both, and the real flag outranks the baseline by the lint rule.
    _set_document(
        monkeypatch,
        tmp_path,
        {
            "baseline": "0.2.0",
            "flags": [{"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"}],
        },
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.1.0")])
    assert [n.version for n in upgrades.needed(m)] == ["0.2.0", "0.3.0"]


def test_needed_baseline_skips_external_units(tmp_path, monkeypatch):
    _set_document(monkeypatch, tmp_path, {"baseline": "0.2.0", "flags": []})
    m = _manifest([UnitState("meet", "prod", "livekit", "external", "0.1.0")])
    assert upgrades.needed(m) == []


# --------------------------------------------------------------------------- offerable_components()


def test_offerable_components_meet_excludes_egress_and_workers():
    # "livekit" is a real dep-loop target; "egress" is bundled into the
    # livekit step (never its own iteration); "workers" is an `is_worker`
    # component and never a `dependencies[].on` target at all.
    assert upgrades.offerable_components("meet") == {"livekit"}


def test_offerable_components_messages_lists_every_dependency_target():
    assert upgrades.offerable_components("messages") == {"mta-in", "mpa", "socks-proxy"}


def test_offerable_components_unknown_app_yields_empty_set():
    assert upgrades.offerable_components("not-a-real-app") == set()


# --------------------------------------------------------------------------- new_component_offers()


def _mta_in_flag(**overrides) -> dict:
    # "mta-in" is a real `dependencies[].on` target of "messages" (unlike
    # "egress", which is a `dependencies[].on` entry too but is bundled into
    # the livekit step and never its own dep-loop iteration — see
    # `upgrades.offerable_components`).
    flag = {
        "version": "0.3.0",
        "apps": ["messages"],
        "reason": "messages 1.5 adds an mta-in relay component",
        "link": "l",
        "new_components": ["mta-in"],
    }
    flag.update(overrides)
    return flag


def test_new_component_offers_untracked_and_stale_stamp_is_offered(
    tmp_path, monkeypatch
):
    _set_document(monkeypatch, tmp_path, {"flags": [_mta_in_flag()]})
    m = _manifest([UnitState("messages", "prod", "messages", "managed", "0.1.0")])
    assert upgrades.new_component_offers(m) == [
        NewComponentOffer(
            app="messages",
            env="prod",
            component="mta-in",
            version="0.3.0",
            reason="messages 1.5 adds an mta-in relay component",
            link="l",
        )
    ]


def test_new_component_offers_egress_is_never_offered(tmp_path, monkeypatch):
    # "egress" is bundled into meet's livekit step, never its own dep-loop
    # iteration — it must never be a valid `new_components` offer, even
    # though it is nominally a `dependencies[].on` entry of "meet".
    _set_document(
        monkeypatch,
        tmp_path,
        {"flags": [_mta_in_flag(apps=["meet"], new_components=["egress"])]},
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.1.0")])
    assert upgrades.new_component_offers(m) == []


def test_new_component_offers_already_tracked_managed_is_not_offered(
    tmp_path, monkeypatch
):
    _set_document(monkeypatch, tmp_path, {"flags": [_mta_in_flag()]})
    m = _manifest(
        [
            UnitState("messages", "prod", "messages", "managed", "0.1.0"),
            UnitState("messages", "prod", "mta-in", "managed", "0.1.0"),
        ]
    )
    assert upgrades.new_component_offers(m) == []


def test_new_component_offers_already_tracked_external_is_not_offered(
    tmp_path, monkeypatch
):
    _set_document(monkeypatch, tmp_path, {"flags": [_mta_in_flag()]})
    m = _manifest(
        [
            UnitState("messages", "prod", "messages", "managed", "0.1.0"),
            UnitState("messages", "prod", "mta-in", "external", "0.1.0"),
        ]
    )
    assert upgrades.new_component_offers(m) == []


def test_new_component_offers_stamp_at_or_above_flag_version_is_not_offered(
    tmp_path, monkeypatch
):
    _set_document(monkeypatch, tmp_path, {"flags": [_mta_in_flag()]})
    m = _manifest([UnitState("messages", "prod", "messages", "managed", "0.3.0")])
    assert upgrades.new_component_offers(m) == []


def test_new_component_offers_app_not_listed_is_not_offered(tmp_path, monkeypatch):
    _set_document(monkeypatch, tmp_path, {"flags": [_mta_in_flag(apps=["drive"])]})
    m = _manifest([UnitState("messages", "prod", "messages", "managed", "0.1.0")])
    assert upgrades.new_component_offers(m) == []


def test_new_component_offers_excludes_external_units_from_stamp_minimum(
    tmp_path, monkeypatch
):
    # The external mpa's ancient stamp must not drag the minimum down — only
    # messages' own (current) stamp counts, so no offer.
    _set_document(monkeypatch, tmp_path, {"flags": [_mta_in_flag()]})
    m = _manifest(
        [
            UnitState("messages", "prod", "messages", "managed", "0.3.0"),
            UnitState("messages", "prod", "mpa", "external", "0.0.1"),
        ]
    )
    assert upgrades.new_component_offers(m) == []


def test_new_component_offers_apps_all_is_defensively_skipped(tmp_path, monkeypatch):
    _set_document(monkeypatch, tmp_path, {"flags": [_mta_in_flag(apps="all")]})
    m = _manifest([UnitState("messages", "prod", "messages", "managed", "0.1.0")])
    assert upgrades.new_component_offers(m) == []


def test_new_component_offers_filters_by_app_and_env(tmp_path, monkeypatch):
    _set_document(monkeypatch, tmp_path, {"flags": [_mta_in_flag()]})
    m = _manifest(
        [
            UnitState("messages", "prod", "messages", "managed", "0.1.0"),
            UnitState("messages", "staging", "messages", "managed", "0.1.0"),
        ]
    )
    result = upgrades.new_component_offers(m, app="messages", env="staging")
    assert [(o.app, o.env) for o in result] == [("messages", "staging")]


# --------------------------------------------------------------------------- flag-file lint (real bundled resource)

_FLAG_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class TestUpgradeFlagFileLint:
    """CI guardrail over the REAL bundled ``resources/upgrades.yml``.

    Does not monkeypatch ``upgrades._RESOURCE`` — every other test in this
    file points it at a tmp copy, but this one must catch a malformed or
    unsafe entry in the file that actually ships.
    """

    def test_document_shape(self):
        assert isinstance(upgrades.load_flags(), list)
        assert isinstance(upgrades.load_baseline(), str)

    def test_baseline_is_well_formed(self):
        baseline = upgrades.load_baseline()
        assert _FLAG_VERSION_RE.match(baseline), (
            f"upgrades.yml: 'baseline' must be an X.Y.Z string, got {baseline!r}"
        )
        assert upgrades.parse_version(baseline) <= upgrades.parse_version(
            st_cli.__version__
        ), (
            f"upgrades.yml: baseline {baseline} is newer than the shipped "
            f"CLI ({st_cli.__version__}) — every unit it bootstraps would be "
            "flagged as unsupported immediately."
        )

    def test_every_flag_outranks_the_baseline(self):
        # The prune rule: an entry at or below the baseline is dead weight the
        # baseline already covers — delete it when you raise the baseline.
        baseline = upgrades.parse_version(upgrades.load_baseline())
        for entry in upgrades.load_flags():
            version = entry.get("version")
            assert upgrades.parse_version(version) > baseline, (
                f"upgrades.yml entry {entry!r}: version {version} does not "
                "outrank the baseline — prune it, the baseline covers it."
            )

    def test_every_entry_is_well_formed(self):
        flags = upgrades.load_flags()
        cli_version = upgrades.parse_version(st_cli.__version__)

        for entry in flags:
            label = f"upgrades.yml entry {entry!r}"

            version = entry.get("version")
            assert isinstance(version, str) and _FLAG_VERSION_RE.match(version), (
                f"{label}: 'version' must be an X.Y.Z string, got {version!r}"
            )
            assert upgrades.parse_version(version) <= cli_version, (
                f"{label}: version {version} is newer than the shipped CLI "
                f"({st_cli.__version__}) — such a flag can never be cleared "
                "by a rebootstrap and would block deploy forever."
            )

            apps = entry.get("apps")
            if apps != "all":
                assert isinstance(apps, list) and apps, (
                    f"{label}: 'apps' must be the string \"all\" or a "
                    f"non-empty list of app names, got {apps!r}"
                )
                for name in apps:
                    app_file = appmeta._APPS_DIR / f"{name}.yml"
                    assert app_file.is_file(), (
                        f"{label}: app {name!r} has no matching "
                        f"st_cli/core/resources/apps/{name}.yml"
                    )

            reason = entry.get("reason")
            assert isinstance(reason, str) and reason.strip(), (
                f"{label}: 'reason' must be a non-empty string, got {reason!r}"
            )

            assert "link" in entry, f"{label}: missing 'link' key"

            if "interactive" in entry:
                assert isinstance(entry["interactive"], bool), (
                    f"{label}: 'interactive' must be a bool, got "
                    f"{entry['interactive']!r}"
                )

            new_components = entry.get("new_components")
            if new_components is not None:
                assert apps != "all" and isinstance(apps, list) and apps, (
                    f"{label}: 'new_components' requires an explicit 'apps' "
                    f'list (not "all"), got {apps!r}'
                )
                assert isinstance(new_components, list) and new_components, (
                    f"{label}: 'new_components' must be a non-empty list of "
                    f"strings, got {new_components!r}"
                )
                offerable = set()
                for name in apps:
                    offerable |= upgrades.offerable_components(name)
                for key in new_components:
                    assert isinstance(key, str), (
                        f"{label}: 'new_components' entry {key!r} is not a string"
                    )
                    assert key in offerable, (
                        f"{label}: new_components key {key!r} does not appear as "
                        f"a dependencies[].on target of any of {apps!r}"
                    )
