"""Tests for st_cli.core.upgrades — flag declaration + detection."""

from __future__ import annotations

import re

import ruamel.yaml
from helpers import set_flags

import st_cli
from st_cli.core import appmeta, upgrades
from st_cli.core.models import NewComponentOffer, StCliManifest, UnitState, UpgradeNeed


def test_parse_version_well_formed():
    assert upgrades.parse_version("1.2.3") == (1, 2, 3)
    assert upgrades.parse_version("0.0.0") == (0, 0, 0)


def test_parse_version_garbage_inputs_degrade_to_zero():
    assert upgrades.parse_version("") == (0, 0, 0)
    assert upgrades.parse_version(None) == (0, 0, 0)
    assert upgrades.parse_version("garbage") == (0, 0, 0)
    assert upgrades.parse_version("not.a.version") == (0, 0, 0)


def test_parse_version_stray_suffix_degrades_that_segment_only():
    # a non-numeric-leading segment (or a missing one) falls back to 0, per-segment
    assert upgrades.parse_version("1.2.3-rc1") == (1, 2, 3)
    assert upgrades.parse_version("1.2") == (1, 2, 0)
    assert upgrades.parse_version("1") == (1, 0, 0)
    assert upgrades.parse_version("1.x.3") == (1, 0, 3)


def test_parse_version_next_outranks_every_release():
    assert upgrades.parse_version(upgrades.NEXT) == upgrades.NEXT_RANK
    assert upgrades.parse_version(upgrades.NEXT) > upgrades.parse_version("999.999.999")


def test_changelog_link_derives_anchor_from_version():
    assert (
        upgrades.changelog_link("0.4.0")
        == "https://github.com/suitenumerique/st-ansible/blob/main/CHANGELOG.md#v0-4-0"
    )


def test_changelog_link_empty_for_next_or_missing():
    assert upgrades.changelog_link(upgrades.NEXT) == ""
    assert upgrades.changelog_link(None) == ""
    assert upgrades.changelog_link("") == ""


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


def test_normalise_warnings_degrades_every_malformed_shape_to_empty_tuple():
    assert upgrades._normalise_warnings(5) == ()
    assert upgrades._normalise_warnings({"a": "b"}) == ()
    assert upgrades._normalise_warnings(True) == ()
    assert upgrades._normalise_warnings([]) == ()
    assert upgrades._normalise_warnings(["a", 2, None]) == ("a",)
    assert upgrades._normalise_warnings(["", "  "]) == ()


def _manifest(units):
    return StCliManifest("0.0.20", "0.0.20", units)


def test_needed_no_flags_yields_empty(tmp_path, monkeypatch):
    set_flags(monkeypatch, tmp_path, [])
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.1.0")])
    assert upgrades.needed(m) == []


def test_needed_flag_older_than_stamp_is_not_needed(tmp_path, monkeypatch):
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.1.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.2.0")])
    assert upgrades.needed(m) == []


def test_needed_flag_newer_than_stamp_is_needed(tmp_path, monkeypatch):
    set_flags(
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


def test_needed_next_flag_applies_to_a_current_stamp(tmp_path, monkeypatch):
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "next", "apps": "all", "reason": "reason"}],
    )
    # a "next" flag applies even when the unit is stamped with the current CLI.
    m = _manifest([UnitState("meet", "prod", "meet", "managed", st_cli.__version__)])
    result = upgrades.needed(m)
    assert len(result) == 1
    assert result[0].version == "next"
    assert result[0].link == ""


def test_needed_derives_link_when_flag_has_none(tmp_path, monkeypatch):
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r"}],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.1.0")])
    result = upgrades.needed(m)
    assert (
        result[0].link
        == "https://github.com/suitenumerique/st-ansible/blob/main/CHANGELOG.md#v0-3-0"
    )


def test_needed_keeps_explicit_link(tmp_path, monkeypatch):
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.1.0")])
    result = upgrades.needed(m)
    assert result[0].link == "l"


def test_needed_apps_all_matches_every_app(tmp_path, monkeypatch):
    set_flags(
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
    set_flags(
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


def test_needed_components_narrow_the_flag_to_listed_units(tmp_path, monkeypatch):
    set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": "0.4.0",
                "apps": ["drive"],
                "components": ["drive"],
                "reason": "r",
                "link": "l",
            }
        ],
    )
    m = _manifest(
        [
            UnitState("drive", "prod", "drive", "managed", "0.1.0"),
            UnitState("drive", "prod", "collabora", "managed", "0.1.0"),
            UnitState("drive", "prod", "workers", "managed", "0.1.0"),
        ]
    )
    result = upgrades.needed(m)
    assert [n.component for n in result] == ["drive"]


def test_needed_missing_stamp_treated_as_0_0_0(tmp_path, monkeypatch):
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.0.1", "apps": "all", "reason": "r", "link": "l"}],
    )
    # bootstrapped_with defaults to "": a unit that predates the feature.
    m = _manifest([UnitState("meet", "prod", "meet", "managed")])
    result = upgrades.needed(m)
    assert len(result) == 1
    assert result[0].version == "0.0.1"


def test_needed_external_units_are_skipped(tmp_path, monkeypatch):
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest([UnitState("meet", "prod", "livekit", "external", "0.1.0")])
    assert upgrades.needed(m) == []


def test_needed_filters_by_app_and_env(tmp_path, monkeypatch):
    set_flags(
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
    set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": "0.3.0",
                "apps": "all",
                "reason": "r",
                "link": "l",
                "full_replay": True,
            }
        ],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.2.0")])
    result = upgrades.needed(m)
    assert result[0].full_replay is True


def test_needed_interactive_defaults_to_false(tmp_path, monkeypatch):
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.2.0")])
    result = upgrades.needed(m)
    assert result[0].full_replay is False


def test_needed_ignores_a_string_warnings_field(tmp_path, monkeypatch):
    set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": "0.3.0",
                "apps": "all",
                "reason": "r",
                "link": "l",
                "warnings": "S3_REPLICATION_BUCKET must now start with https://",
            }
        ],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.2.0")])
    result = upgrades.needed(m)
    assert result[0].warnings == ()


def test_needed_carries_a_list_warning_stripped_and_non_empty(tmp_path, monkeypatch):
    set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": "0.3.0",
                "apps": "all",
                "reason": "r",
                "link": "l",
                "warnings": ["  first step  ", "", "second step"],
            }
        ],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.2.0")])
    result = upgrades.needed(m)
    assert result[0].warnings == ("first step", "second step")


def test_needed_no_warning_yields_empty_tuple(tmp_path, monkeypatch):
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.2.0")])
    result = upgrades.needed(m)
    assert result[0].warnings == ()


def test_needed_baseline_flag_carries_no_warning(tmp_path, monkeypatch):
    _set_document(monkeypatch, tmp_path, {"baseline": "0.2.0", "flags": []})
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.1.0")])
    result = upgrades.needed(m)
    assert result[0].warnings == ()


def test_needed_is_deterministically_ordered(tmp_path, monkeypatch):
    set_flags(
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


def test_newest_per_unit_unions_warnings_of_two_flags_for_one_unit():
    old = UpgradeNeed(
        "meet", "prod", "meet", "0.2.0", "older", "", warnings=("first step",)
    )
    new = UpgradeNeed(
        "meet", "prod", "meet", "0.4.0", "newer", "", warnings=("second step",)
    )
    result = upgrades.newest_per_unit([old, new])
    assert len(result) == 1
    assert result[0].warnings == ("first step", "second step")  # first-appearance order


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


def test_newest_per_unit_next_outranks_release():
    release = UpgradeNeed("meet", "prod", "meet", "0.3.0", "older", "")
    next_need = UpgradeNeed("meet", "prod", "meet", "next", "newer", "")
    result = upgrades.newest_per_unit([release, next_need])
    assert len(result) == 1
    assert result[0].version == "next"
    assert upgrades.newest_per_unit([next_need, release])[0].version == "next"


def test_newest_per_unit_ors_interactive_across_the_collapsed_set():
    # the result carries the NEWEST version/reason, but full_replay stays True
    old_interactive = UpgradeNeed(
        "meet", "prod", "meet", "0.2.0", "older", "", full_replay=True
    )
    new_silent = UpgradeNeed(
        "meet", "prod", "meet", "0.4.0", "newer", "", full_replay=False
    )
    result = upgrades.newest_per_unit([old_interactive, new_silent])
    assert len(result) == 1
    assert result[0].version == "0.4.0"
    assert result[0].reason == "newer"
    assert result[0].full_replay is True
    # order-independent
    assert upgrades.newest_per_unit([new_silent, old_interactive]) == result


def test_pending_warnings_collects_every_need_not_only_the_newest():
    # an operator who jumps two releases must see both warnings, even though
    # newest_per_unit would keep only the 0.5.0 need.
    older = UpgradeNeed(
        "meet", "prod", "meet", "0.4.0", "r", "", warnings=("bump the bucket url",)
    )
    newer = UpgradeNeed(
        "meet", "prod", "meet", "0.5.0", "r", "", warnings=("rotate the token",)
    )
    assert upgrades.pending_warnings([older, newer]) == [
        ("0.4.0", "bump the bucket url"),
        ("0.5.0", "rotate the token"),
    ]


def test_pending_warnings_dedupes_keeping_first_occurrence():
    a = UpgradeNeed("meet", "prod", "meet", "0.4.0", "r", "", warnings=("same text",))
    b = UpgradeNeed(
        "meet", "prod", "workers", "0.4.0", "r", "", warnings=("same text",)
    )
    assert upgrades.pending_warnings([a, b]) == [("0.4.0", "same text")]


def test_pending_warnings_sorted_by_version_then_first_appearance():
    later_version = UpgradeNeed(
        "meet", "prod", "meet", "0.5.0", "r", "", warnings=("second",)
    )
    earlier_version = UpgradeNeed(
        "meet", "prod", "workers", "0.4.0", "r", "", warnings=("first",)
    )
    # fed out of version order; result must still sort by version
    result = upgrades.pending_warnings([later_version, earlier_version])
    assert result == [("0.4.0", "first"), ("0.5.0", "second")]


def test_pending_warnings_empty_when_no_need_carries_one():
    needs = [UpgradeNeed("meet", "prod", "meet", "0.4.0", "r", "")]
    assert upgrades.pending_warnings(needs) == []


def test_pending_warnings_sorts_next_last():
    next_need = UpgradeNeed(
        "meet", "prod", "meet", "next", "r", "", warnings=("next step",)
    )
    release_need = UpgradeNeed(
        "meet", "prod", "workers", "0.5.0", "r", "", warnings=("release step",)
    )
    result = upgrades.pending_warnings([next_need, release_need])
    assert result == [("0.5.0", "release step"), ("next", "next step")]


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
    assert result[0].full_replay is True


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
    # needed() reports both; the real flag outranks the baseline by the lint rule
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


def test_offerable_components_meet_excludes_egress_and_workers():
    # "egress" is bundled into the livekit step; "workers" is never a dependency target
    assert upgrades.offerable_components("meet") == {"livekit"}


def test_offerable_components_messages_lists_every_dependency_target():
    assert upgrades.offerable_components("messages") == {"mta-in", "mpa", "socks-proxy"}


def test_offerable_components_unknown_app_yields_empty_set():
    assert upgrades.offerable_components("not-a-real-app") == set()


def _mta_in_flag(**overrides) -> dict:
    # "mta-in" is a real dependency target; unlike "egress" it is its own
    # dep-loop iteration
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


def test_new_component_offers_next_flag_offers_on_current_stamp(tmp_path, monkeypatch):
    flag = _mta_in_flag(version=upgrades.NEXT)
    del flag["link"]
    _set_document(monkeypatch, tmp_path, {"flags": [flag]})
    # a "next" flag offers even when the unit is stamped with the current CLI.
    m = _manifest(
        [UnitState("messages", "prod", "messages", "managed", st_cli.__version__)]
    )
    result = upgrades.new_component_offers(m)
    assert len(result) == 1
    assert result[0].version == "next"
    assert result[0].link == ""


def test_new_component_offers_derives_link_when_flag_has_none(tmp_path, monkeypatch):
    flag = _mta_in_flag()
    del flag["link"]
    _set_document(monkeypatch, tmp_path, {"flags": [flag]})
    m = _manifest([UnitState("messages", "prod", "messages", "managed", "0.1.0")])
    result = upgrades.new_component_offers(m)
    assert (
        result[0].link
        == "https://github.com/suitenumerique/st-ansible/blob/main/CHANGELOG.md#v0-3-0"
    )


def test_new_component_offers_egress_is_never_offered(tmp_path, monkeypatch):
    # "egress" is bundled into meet's livekit step, so it is never a valid offer
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
    # the external mpa's ancient stamp must not drag the minimum down
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


_FLAG_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class TestUpgradeFlagFileLint:
    """CI guardrail over the real bundled resources/upgrades.yml (not a tmp copy)."""

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
        # prune rule: an entry at or below the baseline is dead weight the
        # baseline covers
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
            assert isinstance(version, str) and (
                _FLAG_VERSION_RE.match(version) or version == upgrades.NEXT
            ), (
                f"{label}: 'version' must be an X.Y.Z string or "
                f"{upgrades.NEXT!r}, got {version!r}"
            )
            if version != upgrades.NEXT:
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

            if "link" in entry:
                assert isinstance(entry["link"], str) and entry["link"], (
                    f"{label}: 'link' must be a non-empty string when "
                    f"present, got {entry['link']!r}"
                )

            if "full_replay" in entry:
                assert isinstance(entry["full_replay"], bool), (
                    f"{label}: 'full_replay' must be a bool, got "
                    f"{entry['full_replay']!r}"
                )

            components = entry.get("components")
            if components is not None:
                assert apps != "all" and isinstance(apps, list) and apps, (
                    f"{label}: 'components' requires an explicit 'apps' "
                    f'list (not "all"), got {apps!r}'
                )
                assert isinstance(components, list) and components, (
                    f"{label}: 'components' must be a non-empty list of "
                    f"strings, got {components!r}"
                )
                for name in apps:
                    known = {c.key for c in appmeta.load_app(name).components}
                    for key in components:
                        assert isinstance(key, str) and key in known, (
                            f"{label}: components key {key!r} is not a "
                            f"component of app {name!r} ({sorted(known)})"
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

    def test_every_warnings_field_is_a_list_of_strings(self):
        for entry in upgrades.load_flags():
            label = f"upgrades.yml entry {entry!r}"
            warnings = entry.get("warnings")
            if warnings is None:
                continue
            assert isinstance(warnings, list) and all(
                isinstance(s, str) for s in warnings
            ), f"{label}: 'warnings' must be a list of strings, got {warnings!r}"
