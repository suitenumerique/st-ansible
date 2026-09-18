"""Tests for the `upgrade` command (st_cli.cmd.upgrade)."""

from __future__ import annotations

import ruamel.yaml
import st_cli
from helpers import seed_creds, seed_scaffolding_artifacts

from st_cli.core import manifest, paths, ui, upgrades
from st_cli.core.models import StCliManifest, UnitState


def _set_flags(monkeypatch, tmp_path, flags: list[dict]):
    """Point upgrades._RESOURCE at a temp flags file (see test_upgrades.py)."""
    p = tmp_path / "upgrades.yml"
    y = ruamel.yaml.YAML(typ="safe")
    with p.open("w", encoding="utf-8") as fh:
        y.dump(flags, fh)
    monkeypatch.setattr(upgrades, "_RESOURCE", p)
    return p


def _touch_core_vars(app: str, env: str) -> None:
    """Create an empty vars.yml so upgrade sees a committed core tree for (app, env)."""
    p = paths.vars_path(app, env, app)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}\n")


def test_upgrade_behind_with_pipx_raises_and_stops(repo, mocker):
    """Check that a pipx-owned install behind upstream raises before any replay."""
    import pytest

    from st_cli.cmd import upgrade as upgrade_mod
    from st_cli.core.errors import StCliError

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    mocker.patch.object(upgrade_mod, "_upstream_latest", return_value="99.0.0")
    mocker.patch.object(
        upgrade_mod.upstream, "owning_pipx", return_value="/usr/bin/pipx"
    )
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")

    with pytest.raises(StCliError) as exc_info:
        upgrade_mod.upgrade()

    assert "pipx upgrade st-cli" in str(exc_info.value)
    boot_spy.assert_not_called()
    m = manifest.load_manifest()
    assert (m.collection_version, m.cli_version) == ("0.0.19", "0.0.19")


def test_upgrade_behind_no_pipx_raises_docker_pull_and_stops(repo, mocker):
    """Check that a non-pipx install behind upstream raises the docker pull command."""
    import pytest

    from st_cli.cmd import upgrade as upgrade_mod
    from st_cli.core.errors import StCliError

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    mocker.patch.object(upgrade_mod, "_upstream_latest", return_value="99.0.0")
    mocker.patch.object(upgrade_mod.upstream, "owning_pipx", return_value=None)
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")

    with pytest.raises(StCliError) as exc_info:
        upgrade_mod.upgrade()

    assert "docker pull ghcr.io/suitenumerique/st-cli:latest" in str(exc_info.value)
    boot_spy.assert_not_called()
    m = manifest.load_manifest()
    assert (m.collection_version, m.cli_version) == ("0.0.19", "0.0.19")


def test_upgrade_stops_when_cli_older_than_pin(repo, mocker, monkeypatch):
    """Check that an installed CLI older than the pin raises the install hint."""
    import pytest

    from st_cli.cmd import upgrade as upgrade_mod
    from st_cli.core.errors import StCliError

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest("0.4.0", "0.4.0", [UnitState("meet", "prod", "meet", "managed")])
    )
    monkeypatch.setattr(st_cli, "__version__", "0.3.0")
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")

    with pytest.raises(StCliError) as exc_info:
        upgrade_mod.upgrade()

    assert "st-cli 0.3.0 is older than the .st-cli.yml pin 0.4.0" in str(exc_info.value)
    boot_spy.assert_not_called()
    m = manifest.load_manifest()
    assert (m.collection_version, m.cli_version) == ("0.4.0", "0.4.0")


def test_upgrade_unknown_upstream_continues(repo, mocker, monkeypatch):
    """Check that an unparseable upstream check informs and the run continues without early return."""
    from st_cli.cmd import upgrade as upgrade_mod

    monkeypatch.delenv("ST_CLI_NO_UPSTREAM_CHECK", raising=False)
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    mocker.patch.object(upgrade_mod, "_upstream_latest", return_value="not-a-version")
    mocker.patch.object(st_cli, "__version__", "0.0.99")
    info_spy = mocker.patch.object(ui, "info")

    upgrade_mod.upgrade()

    assert any(
        "continuing with the installed version" in str(c.args[0])
        for c in info_spy.call_args_list
    )
    m = manifest.load_manifest()
    assert m.collection_version == "0.0.99"


def test_upgrade_bumps_pin_and_cleans_scaffolding_on_change(repo, mocker):
    """Check that a real version change realigns the pin and cleans scaffolding."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    seed_scaffolding_artifacts()
    mocker.patch.object(st_cli, "__version__", "0.0.99")

    upgrade_mod.upgrade()

    m = manifest.load_manifest()
    assert m.collection_version == "0.0.99"
    assert m.cli_version == "0.0.99"
    assert not (paths.st_cli_dir() / "ansible.cfg").exists()
    assert not (paths.st_cli_dir() / "galaxy-requirements.yml").exists()
    assert not paths.playbooks_dir().exists()
    assert not paths.collections_dir().exists()
    assert paths.st_cli_dir().exists()
    assert (repo / ".vault-pass").exists()


def test_upgrade_no_change_leaves_scaffolding_intact(repo, mocker):
    """No version change (installed == pin) → scaffolding stays untouched."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    seed_scaffolding_artifacts()
    mocker.patch.object(st_cli, "__version__", "0.0.20")

    upgrade_mod.upgrade()

    assert (paths.st_cli_dir() / "ansible.cfg").exists()
    assert (paths.st_cli_dir() / "galaxy-requirements.yml").exists()
    assert paths.playbooks_dir().exists()
    assert paths.collections_dir().exists()
    m = manifest.load_manifest()
    assert m.collection_version == "0.0.20"
    assert m.cli_version == "0.0.20"


def test_upgrade_final_message_points_at_deploy(repo, mocker):
    """The final success message points at `st-cli deploy <app> <env>`."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    mocker.patch.object(st_cli, "__version__", "0.0.99")
    success_spy = mocker.patch.object(ui, "success")

    upgrade_mod.upgrade()

    final = " ".join(str(c.args[0]) for c in success_spy.call_args_list if c.args)
    assert "st-cli deploy <app> <env>" in final


def test_upgrade_groups_by_app_env_and_picks_replay_mode(
    repo, mocker, tmp_path, monkeypatch
):
    """Check that each (app, env) group gets one grouped bootstrap call with replay=SILENT."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "meet", "managed", "0.1.0"),
                UnitState("drive", "prod", "drive", "managed", "0.1.0"),
            ],
        )
    )
    _touch_core_vars("meet", "prod")
    _touch_core_vars("drive", "prod")
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.5.0", "apps": ["meet", "drive"], "reason": "r", "link": ""}],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.19")
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")

    upgrade_mod.upgrade()

    calls = {
        (c.args[0], c.args[1]): c.kwargs["replay"] for c in boot_spy.call_args_list
    }
    assert calls == {
        ("drive", "prod"): upgrade_mod.ReplayAction.SILENT,
        ("meet", "prod"): upgrade_mod.ReplayAction.SILENT,
    }


def test_upgrade_full_replay_flag_escalates_its_group_to_modify(
    repo, mocker, tmp_path, monkeypatch
):
    """Check that a full_replay need escalates its group to MODIFY, not an unrelated group."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "meet", "managed", "0.1.0"),
                UnitState("drive", "prod", "drive", "managed", "0.1.0"),
            ],
        )
    )
    _touch_core_vars("meet", "prod")
    _touch_core_vars("drive", "prod")
    _set_flags(
        monkeypatch,
        tmp_path,
        [
            {"version": "0.5.0", "apps": ["meet", "drive"], "reason": "r", "link": ""},
            {
                "version": "0.6.0",
                "apps": ["meet"],
                "reason": "review needed",
                "link": "",
                "full_replay": True,
            },
        ],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.19")
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")

    upgrade_mod.upgrade()

    calls = {
        (c.args[0], c.args[1]): c.kwargs["replay"] for c in boot_spy.call_args_list
    }
    assert calls == {
        ("drive", "prod"): upgrade_mod.ReplayAction.SILENT,
        ("meet", "prod"): upgrade_mod.ReplayAction.MODIFY,
    }


def test_upgrade_pin_realigned_before_first_replay(repo, mocker, tmp_path, monkeypatch):
    """Check that the pin is saved to the new version before the first bootstrap call."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed", "0.1.0")]
        )
    )
    _touch_core_vars("meet", "prod")
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.5.0", "apps": ["meet"], "reason": "r", "link": ""}],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.99")

    seen: dict = {}

    def _fake_bootstrap(app, env, component=None, *, replay):
        seen["pin"] = manifest.load_manifest().collection_version

    mocker.patch.object(
        upgrade_mod.bootstrap_mod, "bootstrap", side_effect=_fake_bootstrap
    )

    upgrade_mod.upgrade()

    assert seen["pin"] == "0.0.99"


def test_upgrade_replays_when_pin_already_aligned(repo, mocker, tmp_path, monkeypatch):
    """Check that replays still run when the pin was already aligned."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.99", "0.0.99", [UnitState("meet", "prod", "meet", "managed", "0.1.0")]
        )
    )
    _touch_core_vars("meet", "prod")
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.5.0", "apps": ["meet"], "reason": "r", "link": ""}],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.99")
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")
    success_spy = mocker.patch.object(ui, "success")

    upgrade_mod.upgrade()

    boot_spy.assert_called_once_with(
        "meet", "prod", replay=upgrade_mod.ReplayAction.SILENT
    )
    # M12: a real replay must still print the closing "upgrade complete" success.
    assert any("upgrade complete" in str(c.args[0]) for c in success_spy.call_args_list)


def test_upgrade_second_run_no_needs_is_noop(repo, mocker, tmp_path, monkeypatch):
    """Check that a second run with no needs makes zero bootstrap calls and prints no success line."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.99", "0.0.99", [UnitState("meet", "prod", "meet", "managed", "0.5.0")]
        )
    )
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.5.0", "apps": ["meet"], "reason": "r", "link": ""}],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.99")
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")
    info_spy = mocker.patch.object(ui, "info")
    success_spy = mocker.patch.object(ui, "success")

    upgrade_mod.upgrade()

    boot_spy.assert_not_called()
    assert any(
        "No pending rebootstraps" in str(c.args[0]) for c in info_spy.call_args_list
    )
    assert not any(
        "upgrade complete" in str(c.args[0]) for c in success_spy.call_args_list
    )


def test_upgrade_vault_check_runs_for_every_group_before_any_replay(
    repo, mocker, tmp_path, monkeypatch
):
    """Check that ensure_vault_readable runs for every group before any group replays."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "meet", "managed", "0.1.0"),
                UnitState("drive", "prod", "drive", "managed", "0.1.0"),
            ],
        )
    )
    _touch_core_vars("meet", "prod")
    _touch_core_vars("drive", "prod")
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.5.0", "apps": ["meet", "drive"], "reason": "r", "link": ""}],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.19")

    calls: list[tuple] = []
    mocker.patch.object(
        upgrade_mod.writer,
        "ensure_vault_readable",
        side_effect=lambda app, env, comps: calls.append(("vault", app, env)),
    )

    def _fake_bootstrap(app, env, component=None, *, replay):
        calls.append(("boot", app, env))

    mocker.patch.object(
        upgrade_mod.bootstrap_mod, "bootstrap", side_effect=_fake_bootstrap
    )

    upgrade_mod.upgrade()

    vault_calls = [c for c in calls if c[0] == "vault"]
    boot_calls = [c for c in calls if c[0] == "boot"]
    assert len(vault_calls) == 2
    assert len(boot_calls) == 2
    first_boot_index = min(calls.index(c) for c in boot_calls)
    assert all(calls.index(c) < first_boot_index for c in vault_calls)


def test_upgrade_provider_only_repo_calls_per_component_and_warns(
    repo, mocker, tmp_path, monkeypatch
):
    """Check that a provider-only repo replays each flagged component and warns of skipped offers."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [UnitState("meet", "prod", "livekit", "managed", "0.1.0")],
        )
    )
    # deliberately no meet/prod/meet/vars.yml
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.5.0", "apps": ["meet"], "reason": "r", "link": ""}],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.19")
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")
    warn_spy = mocker.patch.object(ui, "warn")

    upgrade_mod.upgrade()

    boot_spy.assert_called_once_with(
        "meet", "prod", component="livekit", replay=upgrade_mod.ReplayAction.SILENT
    )
    assert any(
        "provider-only" in str(c.args[0]) or "no core tree" in str(c.args[0])
        for c in warn_spy.call_args_list
    )


def test_upgrade_unknown_app_group_warns_and_skips_without_aborting(
    repo, mocker, tmp_path, monkeypatch
):
    """Check that a stale unit for a dropped app is warned and skipped, not aborting the run."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "meet", "managed", "0.1.0"),
                UnitState("ghost-app", "prod", "ghost-app", "managed", "0.1.0"),
            ],
        )
    )
    _touch_core_vars("meet", "prod")
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.5.0", "apps": "all", "reason": "r", "link": ""}],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.19")
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")
    warn_spy = mocker.patch.object(ui, "warn")

    upgrade_mod.upgrade()

    boot_spy.assert_called_once_with(
        "meet", "prod", replay=upgrade_mod.ReplayAction.SILENT
    )
    assert any(
        "ghost-app" in str(c.args[0]) and "skipped" in str(c.args[0])
        for c in warn_spy.call_args_list
    )


def test_upgrade_no_upstream_check_env_skips_could_not_check_info(
    repo, mocker, monkeypatch
):
    """Check that the ST_CLI_NO_UPSTREAM_CHECK opt-out skips the "could not check" info."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    monkeypatch.setenv("ST_CLI_NO_UPSTREAM_CHECK", "1")
    mocker.patch.object(st_cli, "__version__", "0.0.19")
    info_spy = mocker.patch.object(ui, "info")

    upgrade_mod.upgrade()

    assert not any("Could not check" in str(c.args[0]) for c in info_spy.call_args_list)


def test_upgrade_unknown_pin_state_still_realigns_and_replays(
    repo, mocker, tmp_path, monkeypatch
):
    """Check that an empty pin (PinState.UNKNOWN) still realigns the pin and replays."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "", [UnitState("meet", "prod", "meet", "managed", "0.1.0")]
        )
    )
    _touch_core_vars("meet", "prod")
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.5.0", "apps": ["meet"], "reason": "r", "link": ""}],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.99")
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")

    upgrade_mod.upgrade()

    m = manifest.load_manifest()
    assert (m.collection_version, m.cli_version) == ("0.0.99", "0.0.99")
    boot_spy.assert_called_once_with(
        "meet", "prod", replay=upgrade_mod.ReplayAction.SILENT
    )


def test_upgrade_prints_both_warnings_of_a_two_release_jump(
    repo, mocker, tmp_path, monkeypatch
):
    """Two flags for the same unit, each with a warning, must both print in
    the final "Manual steps" block, even though the unit replays only once."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [UnitState("meet", "prod", "meet", "managed", "0.3.1")],
        )
    )
    _touch_core_vars("meet", "prod")
    _set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": "0.4.0",
                "apps": ["meet"],
                "reason": "r1",
                "link": "",
                "warnings": ["first manual step"],
            },
            {
                "version": "0.5.0",
                "apps": ["meet"],
                "reason": "r2",
                "link": "",
                "warnings": ["second manual step"],
            },
        ],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.19")
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")
    warn_spy = mocker.patch.object(ui, "warn")

    upgrade_mod.upgrade()

    boot_spy.assert_called_once_with(
        "meet", "prod", replay=upgrade_mod.ReplayAction.SILENT
    )
    warn_lines = [str(c.args[0]) for c in warn_spy.call_args_list]
    assert any("0.4.0: first manual step" in line for line in warn_lines)
    assert any("0.5.0: second manual step" in line for line in warn_lines)
    assert any("Manual steps for this upgrade:" in line for line in warn_lines)
    assert any("meet/prod 0.4.0: first manual step" in line for line in warn_lines)
    assert any("meet/prod 0.5.0: second manual step" in line for line in warn_lines)


def test_upgrade_skipped_group_still_prints_its_warnings(
    repo, mocker, tmp_path, monkeypatch
):
    """A group skipped because its app manifest fails to load still prints its
    warnings in the "Manual steps" block."""
    from st_cli.cmd import upgrade as upgrade_mod
    from st_cli.core.errors import StCliError

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [UnitState("ghost-app", "prod", "ghost-app", "managed", "0.1.0")],
        )
    )
    _set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": "0.5.0",
                "apps": "all",
                "reason": "r",
                "link": "",
                "warnings": ["manual step for the dropped app"],
            }
        ],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.19")
    mocker.patch.object(
        upgrade_mod.appmeta,
        "load_app",
        side_effect=StCliError("no manifest for ghost-app"),
    )
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")
    warn_spy = mocker.patch.object(ui, "warn")

    upgrade_mod.upgrade()

    boot_spy.assert_not_called()
    warn_lines = [str(c.args[0]) for c in warn_spy.call_args_list]
    assert any("0.5.0: manual step for the dropped app" in line for line in warn_lines)
    assert any(
        "ghost-app/prod 0.5.0: manual step for the dropped app" in line
        for line in warn_lines
    )


def test_upgrade_warnings_print_only_in_manual_steps_block_before_success(
    repo, mocker, tmp_path, monkeypatch
):
    """The warnings print once, under the "Manual steps" header, and the
    "upgrade complete" success line prints after that block."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [UnitState("meet", "prod", "meet", "managed", "0.3.1")],
        )
    )
    _touch_core_vars("meet", "prod")
    _set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": "0.5.0",
                "apps": ["meet"],
                "reason": "r",
                "link": "",
                "warnings": ["manual step"],
            }
        ],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.19")
    mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")
    calls: list[tuple[str, str]] = []
    mocker.patch.object(
        ui, "warn", side_effect=lambda msg: calls.append(("warn", str(msg)))
    )
    mocker.patch.object(
        ui, "success", side_effect=lambda msg: calls.append(("success", str(msg)))
    )

    upgrade_mod.upgrade()

    warning_indexes = [
        i for i, (_, text) in enumerate(calls) if "0.5.0: manual step" in text
    ]
    manual_steps_index = next(
        i
        for i, (_, text) in enumerate(calls)
        if "Manual steps for this upgrade:" in text
    )
    success_index = next(
        i for i, (_, text) in enumerate(calls) if "upgrade complete" in text
    )
    assert len(warning_indexes) == 1
    assert manual_steps_index < warning_indexes[0] < success_index


def test_upgrade_no_warning_prints_no_manual_steps_block(
    repo, mocker, tmp_path, monkeypatch
):
    """A flag with no warning must not trigger the "Manual steps" block."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [UnitState("meet", "prod", "meet", "managed", "0.1.0")],
        )
    )
    _touch_core_vars("meet", "prod")
    _set_flags(
        monkeypatch,
        tmp_path,
        [{"version": "0.5.0", "apps": ["meet"], "reason": "r", "link": ""}],
    )
    mocker.patch.object(st_cli, "__version__", "0.0.19")
    mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")
    warn_spy = mocker.patch.object(ui, "warn")

    upgrade_mod.upgrade()

    warn_lines = [str(c.args[0]) for c in warn_spy.call_args_list]
    assert not any("Manual steps for this upgrade:" in line for line in warn_lines)
