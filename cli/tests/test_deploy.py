"""Tests for the `deploy` command (st_cli.cmd.deploy)."""

from __future__ import annotations

from helpers import call_order_spy, seed_creds, seed_drive_unit, seed_meet_unit
from typer.testing import CliRunner

import st_cli
from st_cli import main as main_mod
from st_cli.core import (
    drift,
    generate,
    manifest,
    runner,
    sshuser,
    tree,
    ui,
    upgrades,
    upstream,
)
from st_cli.core.models import StCliManifest, UnitState, UpgradeNeed


def test_deploy_call_order_check_first_then_generate_galaxy_play(repo, mocker):
    """Check that the pending-needs gate runs before generate, galaxy_install, and
    play."""
    seed_meet_unit(repo)
    call_order, spy = call_order_spy()

    mocker.patch.object(
        drift, "pending_needs", spy("pending_needs", drift.pending_needs)
    )
    mocker.patch.object(
        generate, "generate_all", spy("generate_all", lambda *a, **k: None)
    )
    mocker.patch.object(
        runner, "galaxy_install", spy("galaxy_install", lambda *a, **k: None)
    )
    mocker.patch.object(runner, "play", spy("play", lambda *a, **k: 0))

    from st_cli.cmd import deploy as deploy_mod

    deploy_mod.run("meet", "prod", None, dry_run=False, deploy_only=False)

    assert call_order == ["pending_needs", "generate_all", "galaxy_install", "play"]


def test_deploy_resolves_host_alias_to_play_limit(repo, mocker):
    """`deploy -H <alias>` resolves the alias and passes it as
    runner.play(limit=...)."""
    seed_meet_unit(repo)  # writes host meet1 (ansible_host=10.0.0.5)
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    play = mocker.patch.object(runner, "play", return_value=0)

    from st_cli.cmd import deploy as deploy_mod

    deploy_mod.run("meet", "prod", None, dry_run=False, deploy_only=False, host="meet1")
    assert play.call_args.kwargs["limit"] == "meet1"  # the inventory alias

    # no host means limit=None (all hosts)
    deploy_mod.run("meet", "prod", None, dry_run=False, deploy_only=False)
    assert play.call_args.kwargs["limit"] is None


def test_deploy_unknown_host_alias_raises(repo, mocker):
    """`deploy -c <comp> -H <bad-alias>` raises because the host is not in the
    component."""
    import pytest

    from st_cli.core.errors import StCliError

    seed_meet_unit(repo)
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    mocker.patch.object(runner, "play", return_value=0)

    from st_cli.cmd import deploy as deploy_mod

    with pytest.raises(StCliError):
        deploy_mod.run(
            "meet", "prod", ["meet"], dry_run=False, deploy_only=False, host="nope1"
        )


def test_deploy_blocks_when_cli_older_than_pin(repo, mocker, monkeypatch):
    """An installed CLI older than the pin blocks before any ssh/network side effect."""
    seed_meet_unit(repo)  # pin = 0.0.19
    monkeypatch.setattr(st_cli, "__version__", "0.0.10")
    generate_spy = mocker.patch.object(generate, "generate_all")
    galaxy_spy = mocker.patch.object(runner, "galaxy_install")
    ensure_ssh_user = mocker.patch.object(sshuser, "ensure_ssh_user")
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])
    output = " ".join(result.output.split())  # rich wraps long lines

    assert result.exit_code == 1
    assert "older than the .st-cli.yml pin" in output
    assert upstream.install_hint() in output
    play.assert_not_called()
    ensure_ssh_user.assert_not_called()
    galaxy_spy.assert_not_called()
    generate_spy.assert_not_called()


def test_deploy_aborts_when_rebootstrap_pending(repo, mocker, monkeypatch):
    """A real pending flag at or below the pin blocks deploy before any ssh/network
    call."""
    seed_meet_unit(repo)  # pin = 0.0.19, unit stamp = "" (reads as 0.0.0)
    generate_spy = mocker.patch.object(generate, "generate_all")
    galaxy_install = mocker.patch.object(runner, "galaxy_install")
    ensure_ssh_user = mocker.patch.object(sshuser, "ensure_ssh_user")
    play = mocker.patch.object(runner, "play", return_value=0)
    monkeypatch.setattr(
        upgrades,
        "load_flags",
        lambda: [
            {"version": "0.0.10", "apps": ["meet"], "reason": "reason", "link": ""}
        ],
    )

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 1
    # rich wraps long lines, so check content rather than one exact substring
    assert "Rebootstrap required before deploying" in result.output
    assert "Run `st-cli upgrade` to resume the replay" in result.output
    assert "meet" in result.output and "prod" in result.output
    play.assert_not_called()
    ensure_ssh_user.assert_not_called()
    galaxy_install.assert_not_called()
    generate_spy.assert_not_called()


def test_deploy_runs_normally_when_no_rebootstrap_pending(repo, mocker):
    """Check that deploy proceeds as normal when pending_needs returns no needs."""
    seed_meet_unit(repo)
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 0
    play.assert_called_once()


def test_deploy_warns_but_runs_on_pending_flag_above_pin(repo, mocker):
    """A pending flag above the pin warns without blocking deploy."""
    seed_meet_unit(repo)  # pin = 0.0.19
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    need = UpgradeNeed(
        app="meet",
        env="prod",
        component="meet",
        version="0.5.0",
        reason="reason",
        link="",
        full_replay=False,
    )
    mocker.patch.object(drift, "pending_needs", return_value=[need])
    warn_spy = mocker.patch.object(ui, "warn")
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 0
    play.assert_called_once()
    assert any(
        "meet/prod/meet: rebootstrap needed (0.5.0 — reason). Run `st-cli upgrade`."
        in str(c.args[0])
        for c in warn_spy.call_args_list
    )


def test_deploy_next_flag_warns_and_runs(repo, mocker, monkeypatch):
    """A `next` flag warns and lets deploy run."""
    seed_meet_unit(repo)  # pin = 0.0.19, unit stamp = "" (reads as 0.0.0)
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    warn_spy = mocker.patch.object(ui, "warn")
    play = mocker.patch.object(runner, "play", return_value=0)
    monkeypatch.setattr(
        upgrades,
        "load_flags",
        lambda: [{"version": "next", "apps": ["meet"], "reason": "reason"}],
    )

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 0
    play.assert_called_once()
    assert any(
        "meet/prod/meet: rebootstrap needed (next — reason). Run `st-cli upgrade`."
        in str(c.args[0])
        for c in warn_spy.call_args_list
    )


def test_deploy_warn_line_omits_warning_text(repo, mocker):
    """A pending need's warnings stay out of the deploy warn line."""
    seed_meet_unit(repo)  # pin = 0.0.19
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    need = UpgradeNeed(
        app="meet",
        env="prod",
        component="meet",
        version="0.5.0",
        reason="reason",
        link="",
        full_replay=False,
        warnings=("S3_REPLICATION_BUCKET must now start with https://",),
    )
    mocker.patch.object(drift, "pending_needs", return_value=[need])
    warn_spy = mocker.patch.object(ui, "warn")
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 0
    play.assert_called_once()
    assert not any(
        "S3_REPLICATION_BUCKET" in str(c.args[0]) for c in warn_spy.call_args_list
    )


def test_deploy_blocking_message_omits_warning_text(repo, mocker):
    """A blocking need's warnings stay out of the deploy-blocking message."""
    seed_meet_unit(repo)  # pin = 0.0.19
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    galaxy_install = mocker.patch.object(runner, "galaxy_install")
    need = UpgradeNeed(
        app="meet",
        env="prod",
        component="meet",
        version="0.0.5",
        reason="reason",
        link="",
        full_replay=False,
        warnings=("rotate the OIDC secret by hand",),
    )
    mocker.patch.object(drift, "pending_needs", return_value=[need])
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])
    output = " ".join(result.output.split())  # rich wraps long lines

    assert result.exit_code == 1
    assert "rotate the OIDC secret by hand" not in output
    assert "Run `st-cli upgrade` to resume the replay." in output
    play.assert_not_called()
    galaxy_install.assert_not_called()


def test_deploy_blocking_message_includes_link(repo, mocker):
    """A blocking need's link does not appear in the deploy-blocking message."""
    seed_meet_unit(repo)  # pin = 0.0.19
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    galaxy_install = mocker.patch.object(runner, "galaxy_install")
    need = UpgradeNeed(
        app="meet",
        env="prod",
        component="meet",
        version="0.0.5",
        reason="reason",
        link="https://example.org/changelog#v005",
        full_replay=False,
    )
    mocker.patch.object(drift, "pending_needs", return_value=[need])
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])
    output = " ".join(result.output.split())  # rich wraps long lines

    assert result.exit_code == 1
    assert "https://example.org/changelog#v005" not in output
    play.assert_not_called()
    galaxy_install.assert_not_called()


def test_deploy_warns_with_format_need_text_on_real_flag_data(
    repo, mocker, monkeypatch
):
    """A real flag above the pin warns with `drift.format_need`'s exact text, and
    the deploy still runs."""
    seed_meet_unit(repo)  # pin = 0.0.19, unit stamp reads as 0.0.0
    monkeypatch.setattr(
        upgrades,
        "load_flags",
        lambda: [
            {
                "version": "0.5.0",
                "apps": ["meet"],
                "reason": "reason",
                "link": "",
                "warnings": ["rotate the OIDC secret by hand"],
            }
        ],
    )
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    warn_spy = mocker.patch.object(ui, "warn")
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 0
    play.assert_called_once()
    need = UpgradeNeed(
        app="meet",
        env="prod",
        component="meet",
        version="0.5.0",
        reason="reason",
        link="",
        full_replay=False,
        warnings=("rotate the OIDC secret by hand",),
    )
    warn_spy.assert_any_call(drift.format_need("meet", "prod", need))
    assert not any(
        "rotate the OIDC secret by hand" in str(c.args[0])
        for c in warn_spy.call_args_list
    )


def test_deploy_baseline_synthetic_flag_warns_and_runs(repo, mocker, monkeypatch):
    """A unit stamped below the baseline warns through the synthetic flag when
    the installed CLI is newer than the pin, and the deploy still runs."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.1.0",
            "0.1.0",
            [UnitState("meet", "prod", "meet", "managed", "0.1.0")],
        )
    )
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_backend_env"] = "DJANGO_CONFIGURATION=Production\n"
    tree.save_vars("meet", "prod", "meet", data)
    tree.write_hosts("meet", "prod", "meet", "meet", ["10.0.0.5"])

    monkeypatch.setattr(st_cli, "__version__", "0.3.0")
    monkeypatch.setattr(upgrades, "load_baseline", lambda: "0.2.0")
    monkeypatch.setattr(upgrades, "load_flags", list)
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    warn_spy = mocker.patch.object(ui, "warn")
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 0
    play.assert_called_once()
    assert any("no longer supported" in str(c.args[0]) for c in warn_spy.call_args_list)


def test_deploy_baseline_synthetic_flag_blocks_after_crashed_upgrade(
    repo, mocker, monkeypatch
):
    """A unit stamped below the baseline blocks deploy once the pin matches the
    installed CLI, because an earlier upgrade crashed before the replay finished."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.2.0",
            "0.2.0",
            [UnitState("meet", "prod", "meet", "managed", "0.1.0")],
        )
    )
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_backend_env"] = "DJANGO_CONFIGURATION=Production\n"
    tree.save_vars("meet", "prod", "meet", data)
    tree.write_hosts("meet", "prod", "meet", "meet", ["10.0.0.5"])

    monkeypatch.setattr(st_cli, "__version__", "0.2.0")
    monkeypatch.setattr(upgrades, "load_baseline", lambda: "0.2.0")
    monkeypatch.setattr(upgrades, "load_flags", list)
    generate_spy = mocker.patch.object(generate, "generate_all")
    galaxy_install = mocker.patch.object(runner, "galaxy_install")
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 1
    assert "resume the replay" in result.output
    play.assert_not_called()
    galaxy_install.assert_not_called()
    generate_spy.assert_not_called()


def test_deploy_two_components_runs_play_for_each(repo, mocker):
    """`st-cli deploy APP ENV -c drive -c collabora` runs runner.play once per
    requested component, in deploy_order."""
    seed_drive_unit(
        repo,
        components=("drive", "collabora"),
        component_hosts={"drive": ["10.0.0.1"], "collabora": ["10.0.0.3"]},
    )
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(
        main_mod.app, ["deploy", "drive", "prod", "-c", "collabora", "-c", "drive"]
    )

    assert result.exit_code == 0
    assert play.call_count == 2
    played = [c.args[2] for c in play.call_args_list]  # play(app, env, component, ...)
    assert played == ["collabora", "drive"]  # deploy_order sort applied


def test_deploy_env_key_advisory_does_not_block(repo, mocker):
    """Check that an env-key advisory alone does not block the deploy."""
    seed_meet_unit(repo)
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    mocker.patch.object(
        drift,
        "env_key_report",
        lambda *a, **k: [
            "meet/prod/meet: new env keys available: FOO — run `st-cli bootstrap meet prod` to set them."
        ],
    )
    warn_spy = mocker.patch.object(ui, "warn")
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 0
    play.assert_called_once()
    warn_spy.assert_any_call(
        "meet/prod/meet: new env keys available: FOO — run "
        "`st-cli bootstrap meet prod` to set them."
    )


def test_deploy_rebootstrap_flag_still_blocks_even_with_clean_env_keys(
    repo, mocker, monkeypatch
):
    """Check that a blocking flag still blocks the deploy regardless of
    env_key_report."""
    seed_meet_unit(repo)  # pin = 0.0.19
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    monkeypatch.setattr(
        upgrades,
        "load_flags",
        lambda: [
            {"version": "0.0.5", "apps": ["meet"], "reason": "reason", "link": ""}
        ],
    )
    env_key_report_spy = mocker.patch.object(drift, "env_key_report", return_value=[])
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 1
    play.assert_not_called()
    env_key_report_spy.assert_not_called()  # the gate raises before it is reached
