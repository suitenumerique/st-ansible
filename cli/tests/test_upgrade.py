"""Tests for the `upgrade` command (st_cli.cmd.upgrade)."""

from __future__ import annotations

from st_cli.core import drift, generate, manifest, paths, runner, ui
from st_cli.core.models import StCliManifest, UnitState

from helpers import seed_creds, seed_scaffolding_artifacts


def test_upgrade_bumps_pin_and_cleans_scaffolding(repo, mocker):
    """upgrade: pin bumps from on-disk metadata, scaffolding cleaned,
    .vault-pass preserved, and generate/galaxy/preflight NOT called."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)  # writes repo/.vault-pass
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    seed_scaffolding_artifacts()

    # avoid shelling out to pipx; read a NEWER version from on-disk metadata
    mocker.patch.object(upgrade_mod.upstream, "owning_pipx", return_value=None)
    mocker.patch("importlib.metadata.version", return_value="0.0.99")

    gen_spy = mocker.patch.object(generate, "generate_all")
    gal_spy = mocker.patch.object(runner, "galaxy_install")
    pre_spy = mocker.patch.object(drift, "preflight")

    upgrade_mod.upgrade()

    # pin bumped to the freshly-installed on-disk version
    m = manifest.load_manifest()
    assert m.collection_version == "0.0.99"
    assert m.cli_version == "0.0.99"
    # regeneratable scaffolding removed
    assert not (paths.st_cli_dir() / "ansible.cfg").exists()
    assert not (paths.st_cli_dir() / "galaxy-requirements.yml").exists()
    assert not paths.playbooks_dir().exists()
    assert not paths.collections_dir().exists()
    # .st-cli/ dir itself preserved (a vault-pass could live there in an edge case)
    assert paths.st_cli_dir().exists()
    # .vault-pass at repo root preserved (never touched by upgrade)
    assert (repo / ".vault-pass").exists()
    # upgrade does not generate / install / doctor
    gen_spy.assert_not_called()
    gal_spy.assert_not_called()
    pre_spy.assert_not_called()


def test_upgrade_no_change_leaves_scaffolding_intact(repo, mocker):
    """No version change (installed == pin) + pipx present → no clean, just
    'nothing to do'; the 4 pre-created artifacts survive untouched."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    seed_scaffolding_artifacts()

    # pipx present — mock the subprocess so no real pipx runs.
    mocker.patch.object(
        upgrade_mod.upstream, "owning_pipx", return_value="/usr/bin/pipx"
    )
    mocker.patch.object(
        upgrade_mod.subprocess, "run", return_value=mocker.MagicMock(returncode=0)
    )
    # installed version == pin → no change
    mocker.patch("importlib.metadata.version", return_value="0.0.20")
    info_spy = mocker.patch.object(ui, "info")

    upgrade_mod.upgrade()

    # scaffolding NOT cleaned — all 4 artifacts still exist
    assert (paths.st_cli_dir() / "ansible.cfg").exists()
    assert (paths.st_cli_dir() / "galaxy-requirements.yml").exists()
    assert paths.playbooks_dir().exists()
    assert paths.collections_dir().exists()
    # .st-cli/ dir itself still there
    assert paths.st_cli_dir().exists()
    # pin unchanged
    m = manifest.load_manifest()
    assert m.collection_version == "0.0.20"
    assert m.cli_version == "0.0.20"
    # 'nothing to do' info emitted
    assert any("nothing to do" in str(c) for c in info_spy.call_args_list)


def test_upgrade_no_change_no_pipx_warns_pip_hint(repo, mocker):
    """pipx missing + no change → emits the pip-upgrade warning and does NOT clean."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    seed_scaffolding_artifacts()

    mocker.patch.object(upgrade_mod.upstream, "owning_pipx", return_value=None)
    mocker.patch("importlib.metadata.version", return_value="0.0.20")
    warn_spy = mocker.patch.object(ui, "warn")

    upgrade_mod.upgrade()

    # pip-upgrade warning emitted (mentions `pip install -U st-cli`)
    assert any("pip install -U st-cli" in str(c) for c in warn_spy.call_args_list)
    # scaffolding NOT cleaned
    assert (paths.st_cli_dir() / "ansible.cfg").exists()
    assert (paths.st_cli_dir() / "galaxy-requirements.yml").exists()
    assert paths.playbooks_dir().exists()
    assert paths.collections_dir().exists()


def test_upgrade_behind_no_pipx_warns_docker_pull(repo, mocker):
    """Upstream behind, no pipx → docker-pull warn; no pipx subprocess call;
    the 'nothing to do' info line is not printed (the warn already told the
    user what to do)."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    seed_scaffolding_artifacts()

    mocker.patch.object(upgrade_mod, "_upstream_latest", return_value="99.0.0")
    mocker.patch.object(upgrade_mod.upstream, "owning_pipx", return_value=None)
    mocker.patch("importlib.metadata.version", return_value="0.0.20")
    run_spy = mocker.patch.object(upgrade_mod.subprocess, "run")
    warn_spy = mocker.patch.object(ui, "warn")
    info_spy = mocker.patch.object(ui, "info")

    upgrade_mod.upgrade()

    run_spy.assert_not_called()
    assert any(
        "docker pull ghcr.io/suitenumerique/st-cli:latest" in str(c)
        for c in warn_spy.call_args_list
    )
    assert not any("nothing to do" in str(c) for c in info_spy.call_args_list)


def test_upgrade_behind_with_pipx_runs_pipx_upgrade(repo, mocker):
    """Upstream behind, pipx present → the pipx upgrade subprocess runs."""
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
    mocker.patch("importlib.metadata.version", return_value="0.0.99")
    run_spy = mocker.patch.object(
        upgrade_mod.subprocess, "run", return_value=mocker.MagicMock(returncode=0)
    )

    upgrade_mod.upgrade()

    run_spy.assert_called_once_with(["/usr/bin/pipx", "upgrade", "st-cli"])


def test_upgrade_uptodate_with_pipx_skips_pipx_run(repo, mocker):
    """Installed matches upstream + pipx present → no pipx subprocess call;
    'nothing to do' still printed."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    seed_scaffolding_artifacts()

    mocker.patch.object(upgrade_mod, "_upstream_latest", return_value="0.0.20")
    mocker.patch.object(
        upgrade_mod.upstream, "owning_pipx", return_value="/usr/bin/pipx"
    )
    mocker.patch("importlib.metadata.version", return_value="0.0.20")
    run_spy = mocker.patch.object(upgrade_mod.subprocess, "run")
    info_spy = mocker.patch.object(ui, "info")

    upgrade_mod.upgrade()

    run_spy.assert_not_called()
    assert any("nothing to do" in str(c) for c in info_spy.call_args_list)


def test_upgrade_final_message_hints_bare_doctor(repo, mocker):
    """After a real upgrade, upgrade's final success message hints the
    parameterless `st-cli doctor` (no <app> placeholder)."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    # no pipx; read a NEWER version from on-disk metadata → real upgrade path
    mocker.patch.object(upgrade_mod.upstream, "owning_pipx", return_value=None)
    mocker.patch("importlib.metadata.version", return_value="0.0.99")
    success_spy = mocker.patch.object(ui, "success")

    upgrade_mod.upgrade()

    final = " ".join(str(c.args[0]) for c in success_spy.call_args_list if c.args)
    assert "st-cli doctor" in final  # parameterless doctor hint
    assert "st-cli doctor <app>" not in final  # no <app> placeholder on the doctor hint
