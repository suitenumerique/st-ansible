"""Tests for st_cli.core.generate — the trashable .st-cli/ scaffolding renderer."""

from __future__ import annotations

from ruamel.yaml.scalarstring import LiteralScalarString

from st_cli.core import generate, manifest, paths, tree
from st_cli.core.models import SecretConfig, StCliManifest, UnitState

from helpers import seed_creds, seed_meet_unit


# --------------------------------------------------------------------------- playbook + vars_files


def test_generate_two_phase_playbook_and_vault_in_vars_files(repo):
    seed_meet_unit(repo)
    # a vault.yml exists for this unit → must be added to vars_files
    vp = paths.vault_path("meet", "prod", "meet")
    vp.write_text("$ANSIBLE_VAULT;1.1;AES256\n3030\n")

    generate.generate_all("meet", "prod")
    pb = generate.playbook_path("meet", "prod", "meet").read_text()
    assert "become_user: root" in pb  # base task
    assert "become_user: meet" in pb  # deploy task
    assert "tasks_from: deploy.yml" in pb
    assert "tags: ['base']" in pb and "tags: ['deploy']" in pb
    assert "serial: 1" in pb  # hosts roll out one at a time
    assert "st_meet_enabled: true" in pb  # enabled injected inline on deploy task
    assert str((repo / "meet/prod/meet/vars.yml").resolve()) in pb
    assert str(vp.resolve()) in pb  # vault.yml loaded alongside vars.yml
    # vars.yml must NOT carry the enabled flag (it belongs on the deploy task)
    assert "st_meet_enabled" not in (repo / "meet/prod/meet/vars.yml").read_text()

    cfg = (repo / ".st-cli/ansible.cfg").read_text()
    assert "collections_path = " in cfg
    gi = (repo / ".gitignore").read_text()
    assert ".st-cli/" in gi and ".vault-pass" in gi
    # without a collection_source override, the git tag pin is rendered
    req = (repo / ".st-cli/galaxy-requirements.yml").read_text()
    assert "type: git" in req
    assert "0.0.19" in req


def test_reuse_unit_deploys(repo):
    """A unit in `reuse` mode is still rendered to a playbook (deployed)."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("meet", "prod", "meet", "managed"),
                UnitState("meet", "prod", "livekit", "reuse"),
            ],
        )
    )
    for c in ("meet", "livekit"):
        tree.save_vars("meet", "prod", c, tree.load_vars("meet", "prod", c))
        tree.write_hosts("meet", "prod", c, c, ["10.0.0.1"])
    generate.generate_all("meet", "prod")
    assert generate.playbook_path("meet", "prod", "meet").exists()
    assert generate.playbook_path(
        "meet", "prod", "livekit"
    ).exists()  # reuse ⇒ deployed


def test_generate_includes_common_yml_first_in_vars_files(repo):
    """The rendered playbook lists common.yml BEFORE the component's vars.yml."""
    seed_meet_unit(repo)
    tree.ensure_common("meet", "prod")

    generate.generate_all("meet", "prod")
    pb = generate.playbook_path("meet", "prod", "meet").read_text()
    common_path_str = str(paths.common_path("meet", "prod").resolve())
    vars_path_str = str(paths.vars_path("meet", "prod", "meet").resolve())
    assert common_path_str in pb
    assert pb.index(common_path_str) < pb.index(vars_path_str)


def test_generate_keycloak_two_phase_playbook(repo):
    """A keycloak unit renders a two-phase playbook importing the keycloak role,
    with st_keycloak_enabled injected on the deploy (app-user) task only."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("keycloak", "prod", "keycloak", "managed")]
        )
    )
    data = tree.load_vars("keycloak", "prod", "keycloak")
    data["st_keycloak_env"] = LiteralScalarString("KC_HOSTNAME=idp.example.org\n")
    tree.save_vars("keycloak", "prod", "keycloak", data)
    tree.write_hosts("keycloak", "prod", "keycloak", "keycloak", ["10.0.0.9"])

    generate.generate_all("keycloak", "prod")
    pb = generate.playbook_path("keycloak", "prod", "keycloak").read_text()
    assert "suitenumerique.st.keycloak" in pb
    assert "become_user: root" in pb  # base task
    assert "become_user: keycloak" in pb  # deploy task
    assert "st_keycloak_enabled: true" in pb  # enabled injected on deploy task
    assert (
        "st_keycloak_enabled"
        not in (repo / "keycloak/prod/keycloak/vars.yml").read_text()
    )


def test_generate_egress_two_phase_playbook(repo):
    """A meet/prod/egress unit renders a two-phase playbook importing the meet role
    (same role + user as livekit/meet core), targets `hosts: egress` (the egress
    inventory group), loads meet/prod/egress/vars.yml, and sets
    `st_meet_egress_enabled: true` ONLY on the deploy (app-user) task — so the base
    task stays base-only. egress is its own unit (NOT a worker → it owns its own
    vars/hosts, unlike the workers component which reuses the core's files)."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [UnitState("meet", "prod", "egress", "managed")],
        )
    )
    data = tree.load_vars("meet", "prod", "egress")
    data["st_meet_livekit_domain"] = "livekit.example.org"
    data["st_meet_livekit_redis_address"] = "127.0.0.1:6379"
    tree.save_vars("meet", "prod", "egress", data)
    tree.write_hosts("meet", "prod", "egress", "egress", ["10.0.0.3"])

    generate.generate_all("meet", "prod")
    pb = generate.playbook_path("meet", "prod", "egress").read_text()
    # same role + user as livekit/meet core (egress is a meet component)
    assert "suitenumerique.st.meet" in pb
    assert "become_user: root" in pb  # base task
    assert "become_user: meet" in pb  # deploy task (egress runs as the meet user)
    assert "serial: 1" in pb
    # targets the egress inventory group (egress owns its own hosts file)
    assert "hosts: egress" in pb
    # loads the egress unit's vars.yml
    assert str(paths.vars_path("meet", "prod", "egress").resolve()) in pb
    # enabled flag injected ONLY on the deploy task — base stays base-only
    assert "st_meet_egress_enabled: true" in pb
    assert (
        "st_meet_egress_enabled" not in (repo / "meet/prod/egress/vars.yml").read_text()
    )


# --------------------------------------------------------------------------- galaxy-requirements overrides


def test_generate_galaxy_requirements_tarball_override(repo, monkeypatch):
    """A local tarball via ST_CLI_COLLECTION_SOURCE replaces the git pin (name only, no type)."""
    seed_meet_unit(repo)
    tarball = repo / "foo.tar.gz"
    tarball.write_bytes(b"")
    monkeypatch.setenv("ST_CLI_COLLECTION_SOURCE", str(tarball))

    generate.generate_all("meet", "prod")
    req = (repo / ".st-cli/galaxy-requirements.yml").read_text()
    assert str(tarball) in req
    assert "type: git" not in req
    assert "type: dir" not in req


def test_generate_galaxy_requirements_dir_override(repo, monkeypatch):
    """A local source dir via ST_CLI_COLLECTION_SOURCE emits name + type: dir."""
    seed_meet_unit(repo)
    src_dir = repo / "coll-src"
    src_dir.mkdir()
    monkeypatch.setenv("ST_CLI_COLLECTION_SOURCE", str(src_dir))

    generate.generate_all("meet", "prod")
    req = (repo / ".st-cli/galaxy-requirements.yml").read_text()
    assert str(src_dir) in req
    assert "type: dir" in req
    assert "type: git" not in req


# --------------------------------------------------------------------------- ansible.cfg (remote_user + vault_password_file)


def test_generate_ansible_cfg_remote_user_from_env(repo, monkeypatch):
    """ST_CLI_SSH_USER set → the generated ansible.cfg remote_user comes from it
    (the deploy path)."""
    seed_meet_unit(repo)
    monkeypatch.setenv("ST_CLI_SSH_USER", "deployer")

    generate.generate_all("meet", "prod")
    cfg = (repo / ".st-cli/ansible.cfg").read_text()
    assert "remote_user = deployer" in cfg


def test_generate_ansible_cfg_uses_repo_root_vault_pass_by_default(repo):
    """The generated ansible.cfg points at the repo-root ``.vault-pass``
    (the default, now unconditional — not ``.st-cli/.vault-pass``)."""
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_backend_env"] = "DJANGO_CONFIGURATION=Production\n"
    tree.save_vars("meet", "prod", "meet", data)
    tree.write_hosts("meet", "prod", "meet", "meet", ["10.0.0.5"])

    generate.generate_all("meet", "prod")

    cfg = (repo / ".st-cli/ansible.cfg").read_text()
    assert str(repo / ".vault-pass") in cfg
    assert ".st-cli/.vault-pass" not in cfg


def _seed_meet_unit_no_local(repo):
    """Seed the meet/prod/meet manifest + vars + hosts with NO local config file.

    Mirrors ``seed_meet_unit`` but skips ``seed_creds`` so no gitignored local
    file is written — the CI scenario where the ssh user comes from the env var.
    """
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_backend_env"] = "DJANGO_CONFIGURATION=Production\n"
    tree.save_vars("meet", "prod", "meet", data)
    tree.write_hosts("meet", "prod", "meet", "meet", ["10.0.0.5"])


def test_generate_ok_without_local_file_omits_remote_user(repo, monkeypatch):
    """No local config file + no ST_CLI_SSH_USER → generate succeeds and ansible.cfg
    OMITS remote_user (ansible defers to the ssh config chain instead of root)."""
    _seed_meet_unit_no_local(repo)
    monkeypatch.delenv("ST_CLI_SSH_USER", raising=False)

    generate.generate_all("meet", "prod")

    cfg = (repo / ".st-cli/ansible.cfg").read_text()
    assert "remote_user" not in cfg


# --------------------------------------------------------------------------- secret backend → scaffolding


def test_generate_hashi_vault_backend_emits_collection_and_drops_vault_pw(repo):
    """In hashi_vault mode: galaxy-requirements.yml adds community.hashi_vault and
    ansible.cfg omits vault_password_file."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.20",
            "0.0.20",
            [UnitState("meet", "prod", "meet", "managed")],
            secrets=[SecretConfig("meet", "prod", "hashi_vault")],
        )
    )
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_backend_env"] = LiteralScalarString(
        "DJANGO_SECRET_KEY={{ lookup('community.hashi_vault.hashi_vault', 'kv/data/meet:django') }}\n"
    )
    tree.save_vars("meet", "prod", "meet", data)
    tree.write_hosts("meet", "prod", "meet", "meet", ["10.0.0.5"])

    generate.generate_all("meet", "prod")
    req = (repo / ".st-cli/galaxy-requirements.yml").read_text()
    assert "community.hashi_vault" in req
    cfg = (repo / ".st-cli/ansible.cfg").read_text()
    assert "vault_password_file" not in cfg


def test_generate_ansible_vault_backend_keeps_vault_password_file(repo):
    """The default (no secrets: block) still emits vault_password_file."""
    seed_meet_unit(repo)  # no secrets: block → ansible-vault
    generate.generate_all("meet", "prod")
    cfg = (repo / ".st-cli/ansible.cfg").read_text()
    assert "vault_password_file" in cfg
    req = (repo / ".st-cli/galaxy-requirements.yml").read_text()
    assert "community.hashi_vault" not in req


# --------------------------------------------------------------------------- workers


def test_generate_workers_reuses_core_files(repo):
    """The workers playbook reuses the core unit's vars/hosts and only flips the enabled flag.

    Workers own no vars.yml/vault.yml/hosts — only the core drive/prod/drive unit
    is seeded. The generated workers playbook must target the core group, load the
    core's vars.yml, and set st_drive_workers_enabled on its deploy task.
    """
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("drive", "prod", "drive", "managed"),
                UnitState("drive", "prod", "workers", "managed"),
            ],
        )
    )
    # seed ONLY the core unit's files (no drive/prod/workers/ dir)
    data = tree.load_vars("drive", "prod", "drive")
    data["st_drive_backend_env"] = LiteralScalarString("REDIS_URL=redis://r/0\n")
    tree.save_vars("drive", "prod", "drive", data)
    tree.write_hosts("drive", "prod", "drive", "drive", ["10.0.0.1"])

    generate.generate_all("drive", "prod")
    pb = generate.playbook_path("drive", "prod", "workers").read_text()
    assert "hosts: drive" in pb  # core's inventory group
    assert "hosts: drive_workers" not in pb
    assert "st_drive_workers_enabled: true" in pb  # worker deploy flag
    assert (
        str(paths.vars_path("drive", "prod", "drive").resolve()) in pb
    )  # core vars reused
    assert not paths.vars_path(
        "drive", "prod", "workers"
    ).exists()  # no workers dir written


def test_generate_workers_targets_workers_group_when_present(repo):
    """With a [workers] group seeded, the workers playbook targets `hosts: workers`;
    without one it falls back to `hosts: drive` (the co-located case, also covered
    by test_generate_workers_reuses_core_files). vars_files still point at the core."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [
                UnitState("drive", "prod", "drive", "managed"),
                UnitState("drive", "prod", "workers", "managed"),
            ],
        )
    )
    data = tree.load_vars("drive", "prod", "drive")
    data["st_drive_backend_env"] = LiteralScalarString("REDIS_URL=redis://r/0\n")
    tree.save_vars("drive", "prod", "drive", data)
    # split: core group + a dedicated [workers] group on a separate IP
    tree.write_groups(
        "drive", "prod", "drive", {"drive": ["10.0.0.1"], "workers": ["10.0.0.2"]}
    )

    generate.generate_all("drive", "prod")
    wpb = generate.playbook_path("drive", "prod", "workers").read_text()
    assert "hosts: workers" in wpb  # worker → its own group
    assert "st_drive_workers_enabled: true" in wpb  # worker deploy flag
    assert (
        str(paths.vars_path("drive", "prod", "drive").resolve()) in wpb
    )  # core vars reused
    cpb = generate.playbook_path("drive", "prod", "drive").read_text()
    assert "hosts: drive" in cpb  # core still targets its own group
    assert "hosts: workers" not in cpb
    assert not paths.vars_path(
        "drive", "prod", "workers"
    ).exists()  # no workers dir written


# --------------------------------------------------------------------------- stale-playbook cleanup


def test_generate_stale_cleanup_preserves_sibling_env_playbook(repo):
    """Stale-playbook cleanup only removes THIS (app, env)'s playbooks.

    The ``{app}-{env}-*.yml`` glob spans dashes, so a bare unlink would also
    clobber a sibling file whose name extends this env (e.g. env 'prod' glob
    matching 'meet-prod-staging-backend.yml'). The derived component
    ('staging-backend') is not a real meet component key, so it must survive a
    generate for env 'prod' — while a stale real-component playbook is still
    regenerated (cleaned up + rewritten).
    """
    seed_meet_unit(repo)
    paths.playbooks_dir().mkdir(parents=True, exist_ok=True)
    sibling = paths.playbooks_dir() / "meet-prod-staging-backend.yml"
    sibling.write_text("[]\n")
    stale_real = paths.playbooks_dir() / "meet-prod-meet.yml"
    stale_real.write_text("STALE\n")

    generate.generate_all("meet", "prod")

    assert sibling.exists()  # sibling-env file untouched (not a real component)
    # the real stale playbook was regenerated (overwritten, not left as 'STALE')
    assert stale_real.exists()
    assert stale_real.read_text() != "STALE\n"
