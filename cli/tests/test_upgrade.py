"""Tests for the `upgrade` command (st_cli.cmd.upgrade)."""

from __future__ import annotations

import ruamel.yaml

from st_cli.core import drift, generate, manifest, paths, rebootstrap, runner, ui
from st_cli.core.models import StCliManifest, UnitState

from helpers import seed_creds, seed_scaffolding_artifacts


def _set_flags(monkeypatch, tmp_path, flags: list[dict]):
    """Point rebootstrap._RESOURCE at a temp flags file (see test_rebootstrap.py)."""
    p = tmp_path / "rebootstrap.yml"
    y = ruamel.yaml.YAML(typ="safe")
    with p.open("w", encoding="utf-8") as fh:
        y.dump(flags, fh)
    monkeypatch.setattr(rebootstrap, "_RESOURCE", p)
    return p


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
    mocker.patch.object(upgrade_mod.shutil, "which", return_value=None)
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
    mocker.patch.object(upgrade_mod.shutil, "which", return_value="/usr/bin/pipx")
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

    mocker.patch.object(upgrade_mod.shutil, "which", return_value=None)
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
    mocker.patch.object(upgrade_mod.shutil, "which", return_value=None)
    mocker.patch("importlib.metadata.version", return_value="0.0.99")
    success_spy = mocker.patch.object(ui, "success")

    upgrade_mod.upgrade()

    final = " ".join(str(c.args[0]) for c in success_spy.call_args_list if c.args)
    assert "st-cli doctor" in final  # parameterless doctor hint
    assert "st-cli doctor <app>" not in final  # no <app> placeholder on the doctor hint


def test_upgrade_reports_pending_rebootstraps_on_real_change(
    repo, mocker, tmp_path, monkeypatch
):
    """After realigning the pin on a real version change, upgrade reports which
    apps now need a rebootstrap so the operator learns immediately, rather than
    only at their next `deploy` (which hard-gates on it)."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "meet", "managed", "0.1.0"),
                UnitState("drive", "prod", "drive", "managed", "0.5.0"),
            ],
        )
    )
    _set_flags(
        monkeypatch,
        tmp_path,
        [
            {
                "version": "0.3.0",
                "apps": ["meet"],
                "reason": "meet 1.5 adds mandatory recording env vars",
                "link": "https://example.org/changelog#v030",
            }
        ],
    )

    mocker.patch.object(upgrade_mod.shutil, "which", return_value=None)
    mocker.patch("importlib.metadata.version", return_value="0.0.99")
    warn_spy = mocker.patch.object(ui, "warn")

    upgrade_mod.upgrade()

    warn_msgs = " ".join(str(c.args[0]) for c in warn_spy.call_args_list if c.args)
    assert "meet" in warn_msgs
    assert "drive" not in warn_msgs  # 0.5.0 already outranks the 0.3.0 flag
    assert "st-cli bootstrap" in warn_msgs


def test_upgrade_no_change_does_not_report_rebootstraps(repo, mocker):
    """The no-op (no version change) path returns before any rebootstrap
    reporting — it must not run at all."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    mocker.patch.object(upgrade_mod.shutil, "which", return_value="/usr/bin/pipx")
    mocker.patch.object(
        upgrade_mod.subprocess, "run", return_value=mocker.MagicMock(returncode=0)
    )
    mocker.patch("importlib.metadata.version", return_value="0.0.20")
    report_spy = mocker.patch.object(upgrade_mod, "_report_pending_rebootstraps")

    upgrade_mod.upgrade()

    report_spy.assert_not_called()


def test_upgrade_rebootstrap_reporting_failure_does_not_break_upgrade(repo, mocker):
    """A failure while checking for pending rebootstraps must never turn an
    otherwise successful upgrade into a failure — it's purely best-effort."""
    from st_cli.cmd import upgrade as upgrade_mod

    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    mocker.patch.object(upgrade_mod.shutil, "which", return_value=None)
    mocker.patch("importlib.metadata.version", return_value="0.0.99")
    mocker.patch.object(
        upgrade_mod.rebootstrap, "needed", side_effect=RuntimeError("boom")
    )

    upgrade_mod.upgrade()  # must not raise

    m = manifest.load_manifest()
    assert m.collection_version == "0.0.99"
