"""Tests for st_cli.core.rebootstrap — flag declaration + detection."""

from __future__ import annotations

import ruamel.yaml

from st_cli.core import rebootstrap
from st_cli.core.models import RebootstrapNeed, StCliManifest, UnitState

# --------------------------------------------------------------------------- parse_version


def test_parse_version_well_formed():
    assert rebootstrap.parse_version("1.2.3") == (1, 2, 3)
    assert rebootstrap.parse_version("0.0.0") == (0, 0, 0)


def test_parse_version_garbage_inputs_degrade_to_zero():
    assert rebootstrap.parse_version("") == (0, 0, 0)
    assert rebootstrap.parse_version(None) == (0, 0, 0)
    assert rebootstrap.parse_version("garbage") == (0, 0, 0)
    assert rebootstrap.parse_version("not.a.version") == (0, 0, 0)


def test_parse_version_stray_suffix_degrades_that_segment_only():
    # leading digit run is taken per-segment; a non-numeric-leading segment
    # (or a missing one) falls back to 0, rather than the whole tuple.
    assert rebootstrap.parse_version("1.2.3-rc1") == (1, 2, 3)
    assert rebootstrap.parse_version("1.2") == (1, 2, 0)
    assert rebootstrap.parse_version("1") == (1, 0, 0)
    assert rebootstrap.parse_version("1.x.3") == (1, 0, 3)


# --------------------------------------------------------------------------- load_flags


def test_load_flags_missing_file_yields_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(rebootstrap, "_RESOURCE", tmp_path / "does-not-exist.yml")
    assert rebootstrap.load_flags() == []


def test_load_flags_empty_file_yields_empty_list(tmp_path, monkeypatch):
    p = tmp_path / "rebootstrap.yml"
    p.write_text("# nothing here yet\n[]\n")
    monkeypatch.setattr(rebootstrap, "_RESOURCE", p)
    assert rebootstrap.load_flags() == []


def test_load_flags_null_document_yields_empty_list(tmp_path, monkeypatch):
    p = tmp_path / "rebootstrap.yml"
    p.write_text("")
    monkeypatch.setattr(rebootstrap, "_RESOURCE", p)
    assert rebootstrap.load_flags() == []


def test_load_flags_real_content(tmp_path, monkeypatch):
    p = tmp_path / "rebootstrap.yml"
    p.write_text(
        "- version: '0.3.0'\n"
        "  apps: [meet, drive]\n"
        "  reason: 'mandatory recording env vars'\n"
        "  link: 'https://example.org/changelog#v030'\n"
    )
    monkeypatch.setattr(rebootstrap, "_RESOURCE", p)
    flags = rebootstrap.load_flags()
    assert len(flags) == 1
    assert flags[0]["version"] == "0.3.0"
    assert flags[0]["apps"] == ["meet", "drive"]
    assert flags[0]["reason"] == "mandatory recording env vars"


# --------------------------------------------------------------------------- needed()


def _set_flags(monkeypatch, tmp_path, flags: list[dict]):
    p = tmp_path / "rebootstrap.yml"
    y = ruamel.yaml.YAML(typ="safe")
    with p.open("w", encoding="utf-8") as fh:
        y.dump(flags, fh)
    monkeypatch.setattr(rebootstrap, "_RESOURCE", p)
    return p


def _manifest(units):
    return StCliManifest("0.0.20", "0.0.20", units)


def test_needed_no_flags_yields_empty(tmp_path, monkeypatch):
    _set_flags(monkeypatch, tmp_path, [])
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.1.0")])
    assert rebootstrap.needed(m) == []


def test_needed_flag_older_than_stamp_is_not_needed(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.1.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.2.0")])
    assert rebootstrap.needed(m) == []


def test_needed_flag_newer_than_stamp_is_needed(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest([UnitState("meet", "prod", "meet", "managed", "0.2.0")])
    result = rebootstrap.needed(m)
    assert result == [
        RebootstrapNeed(
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
    result = rebootstrap.needed(m)
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
    result = rebootstrap.needed(m)
    assert [n.app for n in result] == ["meet"]


def test_needed_missing_stamp_treated_as_0_0_0(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.0.1", "apps": "all", "reason": "r", "link": "l"}],
    )
    # bootstrapped_with defaults to "" — a unit that predates the feature.
    m = _manifest([UnitState("meet", "prod", "meet", "managed")])
    result = rebootstrap.needed(m)
    assert len(result) == 1
    assert result[0].version == "0.0.1"


def test_needed_external_units_are_skipped(tmp_path, monkeypatch):
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": "l"}],
    )
    m = _manifest([UnitState("meet", "prod", "livekit", "external", "0.1.0")])
    assert rebootstrap.needed(m) == []


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
    result = rebootstrap.needed(m, app="meet")
    assert {(n.app, n.env) for n in result} == {("meet", "prod"), ("meet", "staging")}

    result = rebootstrap.needed(m, app="meet", env="prod")
    assert [(n.app, n.env) for n in result] == [("meet", "prod")]


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
    result = rebootstrap.needed(m)
    keys = [(n.app, n.env, n.component, n.version) for n in result]
    assert keys == sorted(keys)
