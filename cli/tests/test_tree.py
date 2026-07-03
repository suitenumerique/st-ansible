"""Tests for st_cli.core.tree — the committed config tree (hosts INI, vars.yml, common.yml)."""

from __future__ import annotations

import re

from ruamel.yaml.scalarstring import LiteralScalarString

from st_cli.core import paths, tree


# --------------------------------------------------------------------------- hosts / groups


def test_read_hosts_parses_ini(repo):
    tree.write_hosts("meet", "prod", "meet", "meet", ["10.0.0.5", "10.0.0.6"])
    assert tree.read_hosts("meet", "prod", "meet") == ["10.0.0.5", "10.0.0.6"]


def test_write_groups_and_read_hosts_by_group(repo):
    """write_groups emits one [group] section per non-empty group; read_hosts(group=...)
    returns only that group's hosts and read_hosts() returns all across all groups."""
    tree.write_groups(
        "drive",
        "prod",
        "drive",
        {"drive": ["10.0.0.1", "10.0.0.2"], "workers": ["10.0.0.9"]},
    )
    raw = (repo / "drive/prod/drive/hosts").read_text()
    assert "[drive]" in raw and "[workers]" in raw
    assert tree.read_hosts("drive", "prod", "drive", group="drive") == [
        "10.0.0.1",
        "10.0.0.2",
    ]
    assert tree.read_hosts("drive", "prod", "drive", group="workers") == ["10.0.0.9"]
    # no group ⇒ all hosts across all groups
    assert tree.read_hosts("drive", "prod", "drive") == [
        "10.0.0.1",
        "10.0.0.2",
        "10.0.0.9",
    ]
    # an unknown group yields no hosts rather than everything
    assert tree.read_hosts("drive", "prod", "drive", group="nope") == []

    # empty groups are omitted entirely (blank worker IPs ⇒ no [workers] section)
    tree.write_groups("drive", "prod", "drive", {"drive": ["10.0.0.1"], "workers": []})
    raw2 = (repo / "drive/prod/drive/hosts").read_text()
    assert "[drive]" in raw2 and "[workers]" not in raw2
    assert tree.read_hosts("drive", "prod", "drive", group="workers") == []


# --------------------------------------------------------------------------- inventory (alias, ip)


def test_read_inventory_returns_alias_ip_pairs(repo):
    """read_inventory returns (alias, ansible_host) pairs, group-filtered."""
    tree.write_hosts("meet", "prod", "meet", "meet", ["10.0.0.5", "10.0.0.6"])
    assert tree.read_inventory("meet", "prod", "meet") == [
        ("meet1", "10.0.0.5"),
        ("meet2", "10.0.0.6"),
    ]

    tree.write_groups(
        "drive", "prod", "drive", {"drive": ["10.0.0.1"], "workers": ["10.0.0.9"]}
    )
    assert tree.read_inventory("drive", "prod", "drive", group="workers") == [
        ("workers1", "10.0.0.9")
    ]
    assert tree.read_inventory("drive", "prod", "drive") == [
        ("drive1", "10.0.0.1"),
        ("workers1", "10.0.0.9"),
    ]


def test_find_host_matches_alias_only(repo):
    """find_host matches the inventory alias, never the ip."""
    entries = [("meet1", "10.0.0.5"), ("meet2", "10.0.0.6")]
    assert tree.find_host(entries, "meet2") == ("meet2", "10.0.0.6")
    assert tree.find_host(entries, "10.0.0.6") is None  # an ip is not an alias
    assert tree.find_host(entries, "nope") is None


def test_component_inventory_worker_group_rule(repo):
    """component_inventory targets the core group for the core, and the worker's own
    [workers] group (in the core hosts file) for the worker."""
    from st_cli.core import appmeta

    meta = appmeta.load_app("meet")
    tree.write_groups(
        "meet", "prod", "meet", {"meet": ["10.0.0.5"], "workers": ["10.0.0.9"]}
    )
    assert tree.component_inventory("meet", "prod", meta, meta.core()) == [
        ("meet1", "10.0.0.5")
    ]
    assert tree.component_inventory("meet", "prod", meta, meta.worker()) == [
        ("workers1", "10.0.0.9")
    ]


# --------------------------------------------------------------------------- vars.yml


def test_vars_yml_stays_plaintext_with_vault_refs(repo):
    data = tree.load_vars("meet", "prod", "meet")
    data["st_meet_enabled"] = True
    data["st_meet_backend_env"] = LiteralScalarString(
        "DJANGO_SECRET_KEY={{ vault_django_secret_key }}\n"
    )
    tree.save_vars("meet", "prod", "meet", data)
    raw = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "{{ vault_django_secret_key }}" in raw
    assert "$ANSIBLE_VAULT" not in raw  # vars.yml is never encrypted


# --------------------------------------------------------------------------- common.yml


def test_ensure_common_seeds_then_preserves_edits(repo):
    """ensure_common writes an empty common.yml once; later calls never overwrite."""
    assert not paths.common_path("meet", "prod").exists()
    tree.ensure_common("meet", "prod")
    p = paths.common_path("meet", "prod")
    assert p.exists()
    assert "---" in p.read_text()

    # hand-edit the file — a second ensure_common call must leave it untouched.
    edited = "# my custom content\n---\nst_meet_uid: 1234\n"
    p.write_text(edited)
    tree.ensure_common("meet", "prod")
    assert p.read_text() == edited


# --------------------------------------------------------------------------- ssh scaffold


def test_ensure_ssh_scaffold_seeds_config_and_known_hosts(repo):
    """ensure_ssh_scaffold writes ssh/config + ssh/known_hosts once with seed content."""
    assert not paths.ssh_config_path().exists()
    assert not paths.ssh_known_hosts_path().exists()
    tree.ensure_ssh_scaffold()
    cfg = paths.ssh_config_path()
    kh = paths.ssh_known_hosts_path()
    assert cfg.exists() and kh.exists()
    cfg_text = cfg.read_text()
    kh_text = kh.read_text()
    assert "ProxyJump" in cfg_text  # the commented bastion example
    assert "known_hosts" in kh_text


def test_ensure_ssh_scaffold_seeds_no_active_host_star(repo):
    """No active `Host *` block is seeded — only `#`-commented mentions — so the file is
    safe to Include into a user's personal ~/.ssh/config (a `Host *` would override their
    global ssh defaults)."""
    tree.ensure_ssh_scaffold()
    cfg_text = paths.ssh_config_path().read_text()
    assert not re.search(r"^\s*Host\s+\*", cfg_text, re.MULTILINE)


def test_ensure_ssh_scaffold_is_idempotent(repo):
    """A second call never overwrites a hand-edited ssh/config (mirrors ensure_common)."""
    tree.ensure_ssh_scaffold()
    cfg = paths.ssh_config_path()
    sentinel = "SENTINEL-DO-NOT-OVERWRITE\n"
    cfg.write_text(sentinel)
    tree.ensure_ssh_scaffold()
    assert cfg.read_text() == sentinel


def test_ssh_config_local_is_gitignored(repo):
    """ssh/config.local is GITIGNORED (per-operator ssh identity, never committed)."""
    assert "ssh/config.local" in tree._GITIGNORE_ENTRIES
    tree.ensure_gitignore()
    gi = (repo / ".gitignore").read_text()
    assert "ssh/config.local" in gi


def test_ensure_ssh_scaffold_seeds_config_local(repo):
    """ensure_ssh_scaffold writes ssh/config.local; an untouched seed is a no-op
    (every non-blank line is a comment)."""
    assert not paths.ssh_config_local_path().exists()
    tree.ensure_ssh_scaffold()
    local_cfg = paths.ssh_config_local_path()
    assert local_cfg.exists()
    text = local_cfg.read_text()
    for line in text.splitlines():
        assert line.startswith("#") or not line.strip()
    assert "User" in text  # the commented Host * template mentions User


def test_ensure_ssh_scaffold_preserves_edited_config_local(repo):
    """A second call never overwrites a hand-edited ssh/config.local."""
    tree.ensure_ssh_scaffold()
    local_cfg = paths.ssh_config_local_path()
    sentinel = "Host *\n    User alice\n"
    local_cfg.write_text(sentinel)
    tree.ensure_ssh_scaffold()
    assert local_cfg.read_text() == sentinel


def test_ensure_ssh_scaffold_tightens_config_local_perms(repo):
    """ssh rejects a group/other-writable client config ("Bad owner or permissions").
    ensure_ssh_scaffold normalises config.local to 0600 on every pass — even for a
    pre-existing, hand-edited file — without touching its content."""
    tree.ensure_ssh_scaffold()
    local_cfg = paths.ssh_config_local_path()
    sentinel = "Host *\n    CertificateFile /ssh-cert.pub\n"
    local_cfg.write_text(sentinel)
    local_cfg.chmod(0o664)  # loose umask / hand-edited → ssh would refuse it
    tree.ensure_ssh_scaffold()
    assert local_cfg.stat().st_mode & 0o777 == 0o600
    assert local_cfg.read_text() == sentinel  # content preserved


def test_ensure_ssh_scaffold_tightens_config_perms(repo):
    """ssh refuses a group/other-writable Included config ("Bad owner or permissions").
    Git doesn't track modes beyond the exec bit, so a checkout under umask 002 leaves
    the COMMITTED ssh/config group-writable (0664). ensure_ssh_scaffold must strip the
    group/other WRITE bits (→ 0644) on every pass — even for a pre-existing file —
    without touching its content."""
    tree.ensure_ssh_scaffold()
    cfg = paths.ssh_config_path()
    sentinel = "Host bastion\n    HostName bastion.example.org\n"
    cfg.write_text(sentinel)
    cfg.chmod(0o664)  # simulates a loose-umask checkout → ssh would refuse it
    tree.ensure_ssh_scaffold()
    mode = cfg.stat().st_mode & 0o777
    assert mode == 0o644
    assert mode & 0o022 == 0  # no group/other write bit
    assert cfg.read_text() == sentinel  # content preserved


def test_ssh_dir_is_not_gitignored(repo):
    """ssh/ is COMMITTED (not in _GITIGNORE_ENTRIES) — host-key/bastion config is not
    secret and the inventory IPs are already tracked in the committed hosts files."""
    assert "ssh/" not in tree._GITIGNORE_ENTRIES
    assert "ssh" not in tree._GITIGNORE_ENTRIES
