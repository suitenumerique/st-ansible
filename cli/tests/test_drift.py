"""Tests for st_cli.core.drift + the `doctor` command — rebootstrap-status check."""

from __future__ import annotations

import pytest
from helpers import call_order_spy, seed_creds, seed_meet_unit, set_flags
from typer.testing import CliRunner

from st_cli import main as main_mod
from st_cli.core import (
    drift,
    envrender,
    generate,
    manifest,
    runner,
    tree,
    ui,
)
from st_cli.core.errors import StCliError
from st_cli.core.models import StCliManifest, UnitState, UpgradeNeed


def test_check_app_no_flags_is_clean(repo, tmp_path, monkeypatch):
    seed_meet_unit(repo)
    set_flags(monkeypatch, tmp_path, [])
    assert drift.check_app("meet", "prod") == []


def test_check_app_reports_flag_newer_than_stamp(repo, tmp_path, monkeypatch):
    """Check that an outranking flag is reported with version, reason, link, and
    command."""
    seed_meet_unit(repo)
    m = manifest.load_manifest()
    m.units[0].bootstrapped_with = "0.2.0"
    manifest.save_manifest(m)
    set_flags(
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
    assert "st-cli upgrade" in w
    assert "https://example.org/changelog#v030" not in w


def test_check_app_omits_warning_text(repo, tmp_path, monkeypatch):
    """Check that a flag's warnings stay out of the check_app line."""
    seed_meet_unit(repo)
    set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": "0.3.0",
                "apps": "all",
                "reason": "r",
                "link": "",
                "warnings": ["S3_REPLICATION_BUCKET must now start with https://"],
            }
        ],
    )
    warnings = drift.check_app("meet", "prod")
    assert len(warnings) == 1
    assert "S3_REPLICATION_BUCKET" not in warnings[0]
    assert warnings[0].endswith("Run `st-cli upgrade`.")


def test_format_need_omits_link_and_warnings():
    need = UpgradeNeed(
        app="meet",
        env="prod",
        component="meet",
        version="0.3.0",
        reason="r",
        link="https://example.org/changelog",
        warnings=("first step", "second step"),
    )
    msg = drift.format_need("meet", "prod", need)
    assert (
        msg == "meet/prod/meet: rebootstrap needed (0.3.0 — r). Run `st-cli upgrade`."
    )


def test_check_app_flag_older_than_stamp_is_silent(repo, tmp_path, monkeypatch):
    seed_meet_unit(repo)
    m = manifest.load_manifest()
    m.units[0].bootstrapped_with = "0.5.0"
    manifest.save_manifest(m)
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.3.0", "apps": "all", "reason": "r", "link": ""}],
    )
    assert drift.check_app("meet", "prod") == []


def test_check_app_missing_stamp_is_reported(repo, tmp_path, monkeypatch):
    """Check that a missing `bootstrapped_with` stamp is treated as 0.0.0 and matches
    every flag."""
    seed_meet_unit(repo)  # bootstrapped_with defaults to ""
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.0.1", "apps": "all", "reason": "r", "link": ""}],
    )
    warnings = drift.check_app("meet", "prod")
    assert len(warnings) == 1
    assert "0.0.1" in warnings[0]


def test_check_app_multiple_flags_reports_only_newest(repo, tmp_path, monkeypatch):
    """Check that several outstanding flags on one unit collapse to only the newest."""
    seed_meet_unit(repo)
    set_flags(
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
    """Check that an external unit gets no rebootstrap warning even with a managed
    sibling."""
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
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.0.1", "apps": "all", "reason": "r", "link": ""}],
    )
    warnings = drift.check_app("meet", "prod")
    assert len(warnings) == 1
    assert "meet/prod/meet" in warnings[0]
    assert "livekit" not in warnings[0]


def test_check_app_all_external_warns_not_silent(repo, tmp_path, monkeypatch):
    """An all-external (app, env) yields a warning, not an empty (clean) list,
    since nothing was actually evaluated."""
    set_flags(monkeypatch, tmp_path, [])
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


def test_pending_needs_newest_per_unit_narrowed_to_components(
    repo, tmp_path, monkeypatch
):
    """pending_needs narrows to --component and collapses to the newest flag per
    unit."""
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "meet", "managed"),
                UnitState("meet", "prod", "livekit", "managed"),
            ],
        )
    )
    set_flags(
        monkeypatch,
        tmp_path,
        [
            {"version": "0.2.0", "apps": "all", "reason": "older reason", "link": ""},
            {"version": "0.4.0", "apps": "all", "reason": "newest reason", "link": ""},
        ],
    )
    needs = drift.pending_needs("meet", "prod", ["meet"])
    assert len(needs) == 1
    assert needs[0].component == "meet"
    assert needs[0].version == "0.4.0"
    assert needs[0].reason == "newest reason"


def test_pending_needs_skips_external_units(repo, tmp_path, monkeypatch):
    """An external unit never contributes a pending need."""
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [UnitState("meet", "prod", "livekit", "external")],
        )
    )
    set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.0.1", "apps": "all", "reason": "r", "link": ""}],
    )
    assert drift.pending_needs("meet", "prod") == []


def test_pending_needs_no_units_raises(repo):
    """No units at all for the (app, env) is a hard error, like check_app."""
    manifest.save_manifest(StCliManifest("0.0.19", "0.0.19", []))
    with pytest.raises(StCliError):
        drift.pending_needs("meet", "prod")


def test_preflight_calls_generate_then_galaxy_in_order(repo, mocker):
    """Check that preflight only calls generate_all then galaxy_install, no rebootstrap
    check."""
    seed_meet_unit(repo)
    call_order, spy = call_order_spy()

    mocker.patch.object(
        generate, "generate_all", spy("generate_all", lambda *a, **k: None)
    )
    mocker.patch.object(
        runner, "galaxy_install", spy("galaxy_install", lambda *a, **k: None)
    )

    assert drift.preflight("meet", "prod") is None
    assert call_order == ["generate_all", "galaxy_install"]


def test_preflight_all_never_touches_collection_or_network(
    repo, mocker, tmp_path, monkeypatch
):
    """Check that preflight_all touches neither the collection nor the network."""
    set_flags(monkeypatch, tmp_path, [])
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "meet", "managed"),
                UnitState("meet", "staging", "meet", "managed"),
                UnitState(
                    "drive", "prod", "drive", "external"
                ),  # external-only, skipped
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
    set_flags(monkeypatch, tmp_path, [])
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "meet", "managed"),
                UnitState("meet", "staging", "meet", "managed"),
                UnitState(
                    "drive", "prod", "drive", "managed"
                ),  # different app, excluded
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
    """Check that check_app is called once for the pair and receives --component."""
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
    when there is an outstanding rebootstrap."""
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


def _seed_full_meet_unit(repo):
    """Seed a meet/prod/meet unit whose committed env blobs match a fresh render.

    Empty answers are valid here: the real templates still emit every
    unconditional key, so the blob is already a fixed point of
    ``env_key_report`` before a test changes it.
    """
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    rendered = envrender.render_env("meet", "meet", {})
    data = tree.load_vars("meet", "prod", "meet")
    for blob_var, text in rendered.items():
        data[blob_var] = text
    tree.save_vars("meet", "prod", "meet", data)
    tree.write_hosts("meet", "prod", "meet", "meet", ["10.0.0.5"])
    return rendered


def test_env_key_report_missing_key_is_advisory(repo):
    """A template-rendered key absent from the committed blob is an advisory."""
    _seed_full_meet_unit(repo)
    data = tree.load_vars("meet", "prod", "meet")
    lines = [
        line
        for line in data["st_meet_backend_env"].splitlines()
        if not line.startswith("FILE_UPLOAD_ENABLED=")
    ]
    data["st_meet_backend_env"] = "\n".join(lines) + "\n"
    tree.save_vars("meet", "prod", "meet", data)

    advisories = drift.env_key_report("meet", "prod")

    assert len(advisories) == 1
    assert "meet/prod/meet" in advisories[0]
    assert "FILE_UPLOAD_ENABLED" in advisories[0]
    assert "st-cli bootstrap meet prod" in advisories[0]


def test_env_key_report_custom_key_reports_nothing(repo):
    """An operator-added custom key in the blob is not reported."""
    _seed_full_meet_unit(repo)
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_backend_env"] = data["st_meet_backend_env"] + "MY_CUSTOM_VAR=1\n"
    tree.save_vars("meet", "prod", "meet", data)

    assert drift.env_key_report("meet", "prod") == []


def test_env_key_report_matching_blob_reports_nothing(repo):
    """A unit whose committed blob matches its fresh render is silent."""
    _seed_full_meet_unit(repo)

    assert drift.env_key_report("meet", "prod") == []


def test_env_key_report_skips_external_and_no_env_render_spec(repo):
    """External units, and units with no env_render spec (e.g. livekit), are skipped."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "livekit", "managed"),  # no env_render spec
                UnitState("meet", "prod", "egress", "external"),
            ],
        )
    )

    assert drift.env_key_report("meet", "prod") == []
