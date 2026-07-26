"""Tests for st_cli.core.drift + the `doctor` command — rebootstrap-status check."""

from __future__ import annotations

import ruamel.yaml
import pytest
from typer.testing import CliRunner

from st_cli import main as main_mod
from st_cli.core import drift, generate, manifest, rebootstrap, runner, ui
from st_cli.core.errors import StCliError
from st_cli.core.models import StCliManifest, UnitState

from helpers import seed_creds, seed_meet_unit


def _set_flags(monkeypatch, tmp_path, flags: list[dict]):
    """Point rebootstrap._RESOURCE at a temp flags file (see test_rebootstrap.py)."""
    p = tmp_path / "rebootstrap.yml"
    y = ruamel.yaml.YAML(typ="safe")
    with p.open("w", encoding="utf-8") as fh:
        y.dump(flags, fh)
    monkeypatch.setattr(rebootstrap, "_RESOURCE", p)
    return p


# --------------------------------------------------------------------------- check_app (rebootstrap status)


def test_check_app_no_flags_is_clean(repo, tmp_path, monkeypatch):
    seed_meet_unit(repo)
    _set_flags(monkeypatch, tmp_path, [])
    assert drift.check_app("meet", "prod") == []


def test_check_app_reports_flag_newer_than_stamp(repo, tmp_path, monkeypatch):
    """A flag outranking the unit's stamp is reported with version, reason,
    link, and the exact rebootstrap command."""
    seed_meet_unit(repo)
    m = manifest.load_manifest()
    m.units[0].bootstrapped_with = "0.2.0"
    manifest.save_manifest(m)
    _set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": "0.3.0",
                "apps": "all",
                "reason": "meet 1.5 adds mandatory recording env vars",
                "link": "https://example.org/changelog#v030",
            }
        ],
    )
    warnings = drift.check_app("meet", "prod")
    assert len(warnings) == 1
    w = warnings[0]
    assert "meet/prod/meet" in w
    assert "0.3.0" in w
    assert "meet 1.5 adds mandatory recording env vars" in w
    assert "st-cli bootstrap meet prod" in w
    assert "https://example.org/changelog#v030" in w


def test_check_app_flag_older_than_stamp_is_silent(repo, tmp_path, monkeypatch):
    seed_meet_unit(repo)
    m = manifest.load_manifest()
    m.units[0].bootstrapped_with = "0.5.0"
    manifest.save_manifest(m)
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": ""}],
    )
    assert drift.check_app("meet", "prod") == []


def test_check_app_missing_stamp_is_reported(repo, tmp_path, monkeypatch):
    """A unit with no `bootstrapped_with` predates the feature and is treated
    as 0.0.0, so every applicable flag matches (see core/rebootstrap.needed)."""
    seed_meet_unit(repo)  # bootstrapped_with defaults to ""
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.0.1", "apps": "all", "reason": "r", "link": ""}],
    )
    warnings = drift.check_app("meet", "prod")
    assert len(warnings) == 1
    assert "0.0.1" in warnings[0]


def test_check_app_multiple_flags_reports_only_newest(repo, tmp_path, monkeypatch):
    """Several outstanding flags on the same unit collapse to just the newest:
    an operator only needs to run the rebootstrap once, and the freshly-replayed
    questionnaire naturally re-asks everything the older flags would have too —
    listing every historical flag would just be noise."""
    seed_meet_unit(repo)
    _set_flags(
        monkeypatch,
        tmp_path,
        [
            {"version": "0.2.0", "apps": "all", "reason": "older reason", "link": ""},
            {"version": "0.4.0", "apps": "all", "reason": "newest reason", "link": ""},
        ],
    )
    warnings = drift.check_app("meet", "prod")
    assert len(warnings) == 1
    assert "0.4.0" in warnings[0]
    assert "newest reason" in warnings[0]
    assert "0.2.0" not in warnings[0]
    assert "older reason" not in warnings[0]


def test_check_app_external_unit_excluded_even_with_managed_sibling(
    repo, tmp_path, monkeypatch
):
    """An external unit never gets a rebootstrap warning, even when a managed
    sibling in the same (app, env) does — only the managed one is reported."""
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "meet", "managed"),
                UnitState("meet", "prod", "livekit", "external"),
            ],
        )
    )
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.0.1", "apps": "all", "reason": "r", "link": ""}],
    )
    warnings = drift.check_app("meet", "prod")
    assert len(warnings) == 1
    assert "meet/prod/meet" in warnings[0]
    assert "livekit" not in warnings[0]


def test_check_app_all_external_warns_not_silent(repo, tmp_path, monkeypatch):
    """An all-external (app, env) yields a warning, not an empty (clean) list —
    nothing was actually evaluated, so it must not look like a clean pass."""
    _set_flags(monkeypatch, tmp_path, [])
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "external")]
        )
    )
    warnings = drift.check_app("meet", "prod")
    assert warnings, "all-external check must not look like a clean pass"
    assert any("external" in w for w in warnings)


def test_check_app_no_units_still_raises(repo):
    """No units at all for the (app, env) is a hard error."""
    manifest.save_manifest(StCliManifest("0.0.19", "0.0.19", []))
    with pytest.raises(StCliError):
        drift.check_app("meet", "prod")


# --------------------------------------------------------------------------- preflight (single pair)


def test_preflight_calls_generate_galaxy_check_in_order(repo, mocker):
    """preflight: generate_all → galaxy_install → check_app (materialize then
    rebootstrap-status check) — deploy needs the collection regardless."""
    seed_meet_unit(repo)
    call_order: list[str] = []

    def _spy(name, real):
        def _impl(*args, **kwargs):
            call_order.append(name)
            return real(*args, **kwargs)

        return _impl

    mocker.patch.object(
        generate, "generate_all", _spy("generate_all", lambda *a, **k: None)
    )
    mocker.patch.object(
        runner, "galaxy_install", _spy("galaxy_install", lambda *a, **k: None)
    )
    mocker.patch.object(drift, "check_app", _spy("check_app", lambda *a, **k: []))

    warnings = drift.preflight("meet", "prod")

    assert call_order == ["generate_all", "galaxy_install", "check_app"]
    assert warnings == []


# --------------------------------------------------------------------------- preflight_all (sweep)


def test_preflight_all_never_touches_collection_or_network(
    repo, mocker, tmp_path, monkeypatch
):
    """With the argspec check gone, preflight_all (the doctor sweep) needs
    neither the collection nor the network — pin that guarantee down."""
    _set_flags(monkeypatch, tmp_path, [])
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "meet", "managed"),
                UnitState("meet", "staging", "meet", "managed"),
                UnitState(
                    "drive", "prod", "drive", "external"
                ),  # external-only → skipped
            ],
        )
    )
    gen_spy = mocker.patch.object(generate, "generate_all")
    gal_spy = mocker.patch.object(runner, "galaxy_install")
    info_spy = mocker.patch.object(ui, "info")

    warnings = drift.preflight_all()

    gen_spy.assert_not_called()
    gal_spy.assert_not_called()
    info_msgs = [str(c.args[0]) for c in info_spy.call_args_list]
    assert any("meet/prod" in m for m in info_msgs)
    assert any("meet/staging" in m for m in info_msgs)
    assert not any("drive/prod" in m for m in info_msgs)
    assert warnings == []


def test_preflight_all_app_only_narrows_to_app_envs(
    repo, mocker, tmp_path, monkeypatch
):
    """preflight_all(app='meet') checks all envs of meet and no other app."""
    _set_flags(monkeypatch, tmp_path, [])
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "meet", "managed"),
                UnitState("meet", "staging", "meet", "managed"),
                UnitState(
                    "drive", "prod", "drive", "managed"
                ),  # different app → excluded
            ],
        )
    )
    gen_spy = mocker.patch.object(generate, "generate_all")
    gal_spy = mocker.patch.object(runner, "galaxy_install")
    check_spy = mocker.patch.object(drift, "check_app", return_value=[])

    drift.preflight_all(app="meet")

    called_pairs = [(c.args[0], c.args[1]) for c in check_spy.call_args_list]
    assert called_pairs == [("meet", "prod"), ("meet", "staging")]
    assert ("drive", "prod") not in called_pairs
    gen_spy.assert_not_called()
    gal_spy.assert_not_called()


def test_preflight_all_component_without_app_env_raises(repo):
    """--component with neither APP nor ENV is meaningless → StCliError."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    with pytest.raises(StCliError, match="--component requires both APP and ENV"):
        drift.preflight_all(components=["core"])


def test_preflight_all_no_managed_units_raises(repo):
    """A manifest with only external units → StCliError on a full sweep."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("drive", "prod", "drive", "external")]
        )
    )
    with pytest.raises(StCliError, match="No managed units in .st-cli.yml"):
        drift.preflight_all()


def test_preflight_all_app_with_no_managed_envs_raises(repo):
    """APP given but it has no managed units → StCliError naming the app."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    with pytest.raises(StCliError, match="No managed units for app drive"):
        drift.preflight_all(app="drive")


def test_preflight_all_single_pair_passes_component(repo, mocker):
    """With both APP and ENV, the pair is checked once and --component is
    forwarded to check_app; still no collection/network touched."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    gen_spy = mocker.patch.object(generate, "generate_all")
    gal_spy = mocker.patch.object(runner, "galaxy_install")
    check_spy = mocker.patch.object(drift, "check_app", return_value=[])

    drift.preflight_all("meet", "prod", ["core"])

    check_spy.assert_called_once_with("meet", "prod", ["core"])
    gen_spy.assert_not_called()
    gal_spy.assert_not_called()


# --------------------------------------------------------------------------- doctor command


@pytest.mark.parametrize(
    ("argv", "expected_call"),
    [
        (["doctor", "meet", "prod"], ("meet", "prod", None)),
        (["doctor", "meet"], ("meet", None, None)),  # env omitted
        (["doctor"], (None, None, None)),  # bare, sweeps everything
    ],
)
def test_doctor_command_clean_routes_through_preflight_all(
    repo, mocker, argv, expected_call
):
    """doctor (with/without APP/ENV) routes through preflight_all and prints the
    clean success message when no rebootstrap is needed."""
    seed_meet_unit(repo)
    preflight_all_spy = mocker.patch.object(drift, "preflight_all", return_value=[])
    success_spy = mocker.patch.object(ui, "success")

    result = CliRunner().invoke(main_mod.app, argv)

    assert result.exit_code == 0
    preflight_all_spy.assert_called_once_with(*expected_call)
    success_spy.assert_called_once_with("No rebootstrap needed.")


def test_doctor_command_aggregates_warnings_on_drift(repo, mocker):
    """doctor warns per preflight_all warning and skips the clean success message
    when there IS an outstanding rebootstrap."""
    seed_meet_unit(repo)
    warnings = [
        "meet/prod/meet: rebootstrap needed (0.3.0 — reason). Run `st-cli bootstrap meet prod`.",
        "meet/staging/meet: rebootstrap needed (0.4.0 — reason). Run `st-cli bootstrap meet staging`.",
    ]
    mocker.patch.object(drift, "preflight_all", return_value=warnings)
    warn_spy = mocker.patch.object(ui, "warn")
    success_spy = mocker.patch.object(ui, "success")

    result = CliRunner().invoke(main_mod.app, ["doctor"])

    assert result.exit_code == 0
    success_spy.assert_not_called()
    assert warn_spy.call_count == 2
    warn_spy.assert_any_call(warnings[0])
    warn_spy.assert_any_call(warnings[1])
