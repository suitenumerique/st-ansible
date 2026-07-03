"""Tests for st_cli.core.drift + the `doctor` command — materialize + warn-only drift check."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from st_cli import main as main_mod
from st_cli.core import drift, generate, manifest, runner, tree, ui
from st_cli.core.errors import StCliError
from st_cli.core.models import StCliManifest, UnitState

from helpers import seed_creds, seed_meet_unit


# --------------------------------------------------------------------------- check_app


def test_doctor_flags_unknown_var(repo):
    seed_meet_unit(repo)
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_nonexistent_var"] = "x"
    tree.save_vars("meet", "prod", "meet", data)
    warnings = drift.check_app("meet", "prod")
    assert any("st_meet_nonexistent_var" in w or "skipping" in w for w in warnings)


def test_check_app_all_external_warns_not_silent(repo):
    """An all-external (app, env) yields a warning, not an empty (clean) list —
    nothing was actually evaluated, so it must not look like a clean pass."""
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
    """preflight: generate_all → galaxy_install → check_app (materialize then drift)."""
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


def test_preflight_all_no_args_iterates_all_managed_pairs(repo, mocker):
    """preflight_all() with no args checks every managed (app, env) pair:
    generate_all + check_app per pair, but galaxy_install EXACTLY ONCE for the
    whole sweep (the collection pin is shared, so one install covers all pairs)."""
    seed_creds(repo)
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
    check_spy = mocker.patch.object(drift, "check_app", return_value=[])
    info_spy = mocker.patch.object(ui, "info")

    warnings = drift.preflight_all()

    # generate_all + check_app called once per managed pair; drive/prod skipped
    gen_pairs = [(c.args[0], c.args[1]) for c in gen_spy.call_args_list]
    check_pairs = [(c.args[0], c.args[1]) for c in check_spy.call_args_list]
    # sorted: ("meet","prod") < ("meet","staging"); the external drive/prod pair is skipped
    assert gen_pairs == [("meet", "prod"), ("meet", "staging")]
    assert check_pairs == [("meet", "prod"), ("meet", "staging")]
    assert ("drive", "prod") not in gen_pairs
    # galaxy_install called EXACTLY ONCE for the whole sweep (install-once)
    assert gal_spy.call_count == 1
    # one "Checking a/env …" info line per checked pair
    info_msgs = [str(c.args[0]) for c in info_spy.call_args_list]
    assert any("meet/prod" in m for m in info_msgs)
    assert any("meet/staging" in m for m in info_msgs)
    assert not any("drive/prod" in m for m in info_msgs)
    assert warnings == []


def test_preflight_all_app_only_narrows_to_app_envs(repo, mocker):
    """preflight_all(app='meet') checks all envs of meet and no other app."""
    seed_creds(repo)
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
    mocker.patch.object(runner, "galaxy_install")
    mocker.patch.object(drift, "check_app", return_value=[])

    drift.preflight_all(app="meet")

    called_pairs = [(c.args[0], c.args[1]) for c in gen_spy.call_args_list]
    assert called_pairs == [("meet", "prod"), ("meet", "staging")]
    assert ("drive", "prod") not in called_pairs


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
    """With both APP and ENV, the pair is checked once and --component is forwarded to check_app."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    mocker.patch.object(generate, "generate_all")
    mocker.patch.object(runner, "galaxy_install")
    check_spy = mocker.patch.object(drift, "check_app", return_value=[])

    drift.preflight_all("meet", "prod", ["core"])

    check_spy.assert_called_once_with("meet", "prod", ["core"])


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
    clean success message when no drift is found."""
    seed_meet_unit(repo)
    preflight_all_spy = mocker.patch.object(drift, "preflight_all", return_value=[])
    success_spy = mocker.patch.object(ui, "success")

    result = CliRunner().invoke(main_mod.app, argv)

    assert result.exit_code == 0
    preflight_all_spy.assert_called_once_with(*expected_call)
    success_spy.assert_called_once_with("No variable drift detected.")


def test_doctor_command_aggregates_warnings_on_drift(repo, mocker):
    """doctor warns per preflight_all warning and skips the clean success message
    when there IS drift."""
    seed_meet_unit(repo)
    warnings = [
        "meet/prod/meet: unknown var 'st_meet_typo'",
        "meet/staging/meet: unknown var 'st_other'",
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
