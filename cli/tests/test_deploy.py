"""Tests for the `deploy` command (st_cli.cmd.deploy)."""

from __future__ import annotations

from helpers import seed_creds, seed_meet_unit
from typer.testing import CliRunner

from st_cli import main as main_mod
from st_cli.core import drift, generate, manifest, runner, sshuser, tree, ui
from st_cli.core.models import StCliManifest, UnitState


def test_deploy_call_order_check_first_then_generate_galaxy_play(repo, mocker):
    """deploy: the rebootstrap gate (check_app) runs FIRST, before install —
    then preflight (generate→galaxy) and play."""
    seed_meet_unit(repo)
    call_order: list[str] = []

    def _spy(name, real):
        def _impl(*args, **kwargs):
            call_order.append(name)
            return real(*args, **kwargs)

        return _impl

    mocker.patch.object(drift, "check_app", _spy("check_app", lambda *a, **k: []))
    mocker.patch.object(
        generate, "generate_all", _spy("generate_all", lambda *a, **k: None)
    )
    mocker.patch.object(
        runner, "galaxy_install", _spy("galaxy_install", lambda *a, **k: None)
    )
    mocker.patch.object(runner, "play", _spy("play", lambda *a, **k: 0))

    from st_cli.cmd import deploy as deploy_mod

    deploy_mod.run("meet", "prod", None, dry_run=False, deploy_only=False)

    assert call_order == ["check_app", "generate_all", "galaxy_install", "play"]


def test_deploy_resolves_host_alias_to_play_limit(repo, mocker):
    """`deploy -H <alias>` resolves the alias and passes it as runner.play(limit=...)."""
    seed_meet_unit(repo)  # writes host meet1 (ansible_host=10.0.0.5)
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    mocker.patch.object(drift, "check_app", lambda *a, **k: [])
    play = mocker.patch.object(runner, "play", return_value=0)

    from st_cli.cmd import deploy as deploy_mod

    deploy_mod.run("meet", "prod", None, dry_run=False, deploy_only=False, host="meet1")
    assert play.call_args.kwargs["limit"] == "meet1"  # the inventory alias

    # no host → limit=None (all hosts)
    deploy_mod.run("meet", "prod", None, dry_run=False, deploy_only=False)
    assert play.call_args.kwargs["limit"] is None


def test_deploy_unknown_host_alias_raises(repo, mocker):
    """`deploy -c <comp> -H <bad-alias>` raises (host not in the component)."""
    import pytest

    from st_cli.core.errors import StCliError

    seed_meet_unit(repo)
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    mocker.patch.object(drift, "check_app", lambda *a, **k: [])
    mocker.patch.object(runner, "play", return_value=0)

    from st_cli.cmd import deploy as deploy_mod

    with pytest.raises(StCliError):
        deploy_mod.run(
            "meet", "prod", ["meet"], dry_run=False, deploy_only=False, host="nope1"
        )


def test_deploy_aborts_when_rebootstrap_pending(repo, mocker):
    """deploy hard-gates on a pending rebootstrap: no override flag, exit 1,
    the error names the exact `st-cli bootstrap` command to run, and the gate
    fires before any ssh/network side effect (ensure_ssh_user, galaxy_install)."""
    seed_meet_unit(repo)
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    galaxy_install = mocker.patch.object(runner, "galaxy_install")
    ensure_ssh_user = mocker.patch.object(sshuser, "ensure_ssh_user")
    mocker.patch.object(
        drift,
        "check_app",
        lambda *a, **k: [
            "meet/prod/meet: rebootstrap needed (0.3.0 — reason). "
            "Run `st-cli bootstrap meet prod`."
        ],
    )
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 1
    # rich wraps long lines, so check content rather than one exact substring
    assert "Rebootstrap required before deploying" in result.output
    assert "st-cli bootstrap" in result.output
    assert "meet" in result.output and "prod" in result.output
    play.assert_not_called()
    ensure_ssh_user.assert_not_called()
    galaxy_install.assert_not_called()


def test_deploy_runs_normally_when_no_rebootstrap_pending(repo, mocker):
    """No pending rebootstrap (check_app returns no warnings) → deploy proceeds
    as normal."""
    seed_meet_unit(repo)
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    mocker.patch.object(drift, "check_app", lambda *a, **k: [])
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 0
    play.assert_called_once()


def test_deploy_two_components_runs_play_for_each(repo, mocker):
    """`st-cli deploy APP ENV -c drive -c collabora` (repeatable -c) is collected
    into a list by Typer and runs runner.play once per requested component, in
    deploy_order (collabora=10 before drive=20)."""
    seed_creds(repo)
    tree.write_hosts("drive", "prod", "drive", "drive", ["10.0.0.1"])
    tree.write_hosts("drive", "prod", "collabora", "collabora", ["10.0.0.3"])
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("drive", "prod", "drive", "managed"),
                UnitState("drive", "prod", "collabora", "managed"),
            ],
        )
    )
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    mocker.patch.object(drift, "check_app", lambda *a, **k: [])
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(
        main_mod.app, ["deploy", "drive", "prod", "-c", "collabora", "-c", "drive"]
    )

    assert result.exit_code == 0
    assert play.call_count == 2
    played = [c.args[2] for c in play.call_args_list]  # play(app, env, component, ...)
    assert played == ["collabora", "drive"]  # deploy_order sort applied


# --------------------------------------------------------------------------- env_key_report wiring (warn-only)


def test_deploy_env_key_advisory_does_not_block(repo, mocker):
    """An env-key advisory alone must not raise — it is warn-only, unlike a
    rebootstrap flag warning from check_app."""
    seed_meet_unit(repo)
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    mocker.patch.object(drift, "check_app", lambda *a, **k: [])
    mocker.patch.object(
        drift,
        "env_key_report",
        lambda *a, **k: (
            [
                "meet/prod/meet: new env keys available: FOO — run "
                "`st-cli bootstrap meet prod` to set them."
            ],
            [],
        ),
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


def test_deploy_rebootstrap_flag_still_blocks_even_with_clean_env_keys(repo, mocker):
    """A rebootstrap flag from check_app still hard-gates the deploy, regardless
    of what env_key_report reports."""
    seed_meet_unit(repo)
    mocker.patch.object(generate, "generate_all", lambda *a, **k: None)
    mocker.patch.object(runner, "galaxy_install", lambda *a, **k: None)
    mocker.patch.object(
        drift,
        "check_app",
        lambda *a, **k: [
            "meet/prod/meet: rebootstrap needed (0.3.0 — reason). "
            "Run `st-cli bootstrap meet prod`."
        ],
    )
    env_key_report_spy = mocker.patch.object(
        drift, "env_key_report", return_value=([], [])
    )
    play = mocker.patch.object(runner, "play", return_value=0)

    result = CliRunner().invoke(main_mod.app, ["deploy", "meet", "prod"])

    assert result.exit_code == 1
    play.assert_not_called()
    env_key_report_spy.assert_not_called()  # hard gate raises before it is reached
