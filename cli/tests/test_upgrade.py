"""Tests for the `upgrade` command (st_cli.cmd.upgrade)."""

from __future__ import annotations

import ruamel.yaml
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


def test_upgrade_behind_with_pipx_warns_and_stops(repo, mocker):
    """Behind upstream + pipx-owned → warns the pipx command and returns before
    touching the pin or replaying anything."""
    from st_cli.cmd import upgrade as upgrade_mod

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
    warn_spy = mocker.patch.object(ui, "warn")

    upgrade_mod.upgrade()

    assert any("pipx upgrade st-cli" in str(c.args[0]) for c in warn_spy.call_args_list)
    boot_spy.assert_not_called()
    m = manifest.load_manifest()
    assert (m.collection_version, m.cli_version) == ("0.0.19", "0.0.19")


def test_upgrade_behind_no_pipx_warns_docker_pull_and_stops(repo, mocker):
    """Behind upstream + no pipx (container/pip install) → warns the docker pull
    command and returns before touching the pin."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    mocker.patch.object(upgrade_mod, "_upstream_latest", return_value="99.0.0")
    mocker.patch.object(upgrade_mod.upstream, "owning_pipx", return_value=None)
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")
    warn_spy = mocker.patch.object(ui, "warn")

    upgrade_mod.upgrade()

    assert any(
        "docker pull ghcr.io/suitenumerique/st-cli:latest" in str(c.args[0])
        for c in warn_spy.call_args_list
    )
    boot_spy.assert_not_called()
    m = manifest.load_manifest()
    assert (m.collection_version, m.cli_version) == ("0.0.19", "0.0.19")


def test_upgrade_unknown_upstream_continues(repo, mocker, monkeypatch):
    """A genuinely UNKNOWN upstream (the check ran but the result did not
    parse — offline, unreachable, or a malformed cached value) informs and
    the run continues with the installed version — no early return. Unlike
    the deliberate `ST_CLI_NO_UPSTREAM_CHECK` opt-out (M11, see the test
    below), this must still print the "could not check" info, so the
    autouse upstream-check disabler is turned off here."""
    from st_cli.cmd import upgrade as upgrade_mod

    monkeypatch.delenv("ST_CLI_NO_UPSTREAM_CHECK", raising=False)
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    mocker.patch.object(upgrade_mod, "_upstream_latest", return_value="not-a-version")
    mocker.patch("importlib.metadata.version", return_value="0.0.99")
    info_spy = mocker.patch.object(ui, "info")

    upgrade_mod.upgrade()

    assert any(
        "continuing with the installed version" in str(c.args[0])
        for c in info_spy.call_args_list
    )
    m = manifest.load_manifest()
    assert m.collection_version == "0.0.99"


def test_upgrade_bumps_pin_and_cleans_scaffolding_on_change(repo, mocker):
    """Real version change: pin realigns from on-disk metadata, scaffolding
    cleaned, .vault-pass preserved."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    seed_scaffolding_artifacts()
    mocker.patch("importlib.metadata.version", return_value="0.0.99")

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
    mocker.patch("importlib.metadata.version", return_value="0.0.20")

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
    mocker.patch("importlib.metadata.version", return_value="0.0.99")
    success_spy = mocker.patch.object(ui, "success")

    upgrade_mod.upgrade()

    final = " ".join(str(c.args[0]) for c in success_spy.call_args_list if c.args)
    assert "st-cli deploy <app> <env>" in final


def test_upgrade_groups_by_app_env_and_picks_replay_mode(
    repo, mocker, tmp_path, monkeypatch
):
    """Flagged units in two (app, env) groups get one grouped bootstrap call each,
    with replay=SILENT when no need in the group is interactive."""
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
    mocker.patch("importlib.metadata.version", return_value="0.0.19")
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")

    upgrade_mod.upgrade()

    calls = {
        (c.args[0], c.args[1]): c.kwargs["replay"] for c in boot_spy.call_args_list
    }
    assert calls == {
        ("drive", "prod"): upgrade_mod.ReplayAction.SILENT,
        ("meet", "prod"): upgrade_mod.ReplayAction.SILENT,
    }


def test_upgrade_interactive_flag_escalates_its_group_to_modify(
    repo, mocker, tmp_path, monkeypatch
):
    """A group with one interactive need replays under MODIFY; an unrelated
    group with only silent needs stays SILENT."""
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
                "interactive": True,
            },
        ],
    )
    mocker.patch("importlib.metadata.version", return_value="0.0.19")
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
    """The pin is saved to the new version before the first bootstrap call runs
    — a crash mid-replay must still leave the pin correct."""
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
    mocker.patch("importlib.metadata.version", return_value="0.0.99")

    seen: dict = {}

    def _fake_bootstrap(app, env, component=None, *, replay):
        seen["pin"] = manifest.load_manifest().collection_version

    mocker.patch.object(
        upgrade_mod.bootstrap_mod, "bootstrap", side_effect=_fake_bootstrap
    )

    upgrade_mod.upgrade()

    assert seen["pin"] == "0.0.99"


def test_upgrade_replays_when_pin_already_aligned(repo, mocker, tmp_path, monkeypatch):
    """Replays run even when the pin was already aligned — no early return on
    a no-version-change run."""
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
    mocker.patch("importlib.metadata.version", return_value="0.0.99")
    boot_spy = mocker.patch.object(upgrade_mod.bootstrap_mod, "bootstrap")
    success_spy = mocker.patch.object(ui, "success")

    upgrade_mod.upgrade()

    boot_spy.assert_called_once_with(
        "meet", "prod", replay=upgrade_mod.ReplayAction.SILENT
    )
    # M12: a real replay ran (even though the pin itself was already
    # aligned) — the closing "upgrade complete" success must still print.
    assert any("upgrade complete" in str(c.args[0]) for c in success_spy.call_args_list)


def test_upgrade_second_run_no_needs_is_noop(repo, mocker, tmp_path, monkeypatch):
    """A second run with fresh stamps (no needs) makes zero bootstrap calls,
    reports 'No pending rebootstraps', and prints NO closing "upgrade
    complete" success (M12: that line only prints when something actually
    happened — the pin changed or a replay ran)."""
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
    mocker.patch("importlib.metadata.version", return_value="0.0.99")
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
    """`ensure_vault_readable` runs for every group before any group replays —
    one bad vault must abort the whole upgrade before any questionnaire."""
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
    mocker.patch("importlib.metadata.version", return_value="0.0.19")

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
    """No committed core tree for (app, env) (provider-only repo): upgrade
    replays each flagged component individually and warns that new-component
    offers are skipped there."""
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
    mocker.patch("importlib.metadata.version", return_value="0.0.19")
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
    """M9: a manifest unit for an app this CLI version no longer ships (a
    stale `.st-cli.yml` after an app was dropped) must not abort the whole
    upgrade — `appmeta.load_app` raising is caught, that ONE group is warned
    and skipped, and every other (valid) group still replays."""
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
    mocker.patch("importlib.metadata.version", return_value="0.0.19")
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
    """M11: `ST_CLI_NO_UPSTREAM_CHECK` is a deliberate opt-out — it must not
    print the "Could not check for a newer st-cli version" info, which is
    reserved for a genuinely failed/unknown check."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    monkeypatch.setenv("ST_CLI_NO_UPSTREAM_CHECK", "1")
    mocker.patch("importlib.metadata.version", return_value="0.0.19")
    info_spy = mocker.patch.object(ui, "info")

    upgrade_mod.upgrade()

    assert not any("Could not check" in str(c.args[0]) for c in info_spy.call_args_list)
