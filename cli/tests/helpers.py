"""Shared test helpers: config-tree seeding and a scripted questionary stand-in.

These are plain functions, not fixtures, so any test module can import and call
them. The ``repo`` fixture (a tmp_path cwd) lives in ``conftest.py``.
"""

from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path

import pytest
import ruamel.yaml

from st_cli.cmd import bootstrap
from st_cli.core import appmeta, manifest, paths, tree, upgrades, vault, writer
from st_cli.core.models import SecretConfig, StCliManifest, UnitState


def seed_creds(repo: Path) -> None:
    """Write a .vault-pass so the vault-password prompt is skipped."""
    (repo / ".vault-pass").write_text("testpass\n")


def set_flags(monkeypatch, tmp_path, flags: list[dict]) -> Path:
    """Point ``upgrades._RESOURCE`` at a temp flags file."""
    p = tmp_path / "upgrades.yml"
    y = ruamel.yaml.YAML(typ="safe")
    with p.open("w", encoding="utf-8") as fh:
        y.dump(flags, fh)
    monkeypatch.setattr(upgrades, "_RESOURCE", p)
    return p


def file_mode(path) -> int:
    """Return the permission bits of ``path``, stripped of the file-type bits."""
    return stat.S_IMODE(path.stat().st_mode)


def call_order_spy() -> tuple[list[str], Callable[[str, Callable], Callable]]:
    """Return a call-order recorder: a list, and a ``spy(name, real)`` factory.

    ``spy(name, real)`` returns a wrapper that appends ``name`` to the list,
    then calls ``real``. The list is fresh per call, for one test.
    """
    calls: list[str] = []

    def spy(name, real):
        def _impl(*args, **kwargs):
            calls.append(name)
            return real(*args, **kwargs)

        return _impl

    return calls, spy


def seed_meet_unit(repo: Path) -> None:
    """Seed a managed meet/prod/meet unit with vars + hosts (ansible-vault backend)."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    data = tree.load_vars("meet", "prod", "meet")
    # enabled flag is NOT stored in vars.yml; the generated playbook injects it
    data["st_meet_backend_env"] = "DJANGO_CONFIGURATION=Production\n"
    tree.save_vars("meet", "prod", "meet", data)
    tree.write_hosts("meet", "prod", "meet", "meet", ["10.0.0.5"])


def seed_drive_unit(
    repo: Path,
    hosts=("10.0.0.1",),
    *,
    components: tuple[str, ...] = ("drive",),
    component_hosts: dict[str, list[str]] | None = None,
    groups: dict[str, list[str]] | None = None,
) -> None:
    """Seed one or more managed drive/prod units (ansible-vault backend).

    ``component_hosts`` overrides ``hosts`` per component; ``groups`` writes a
    ``write_groups`` split for ``components[0]`` instead of a flat hosts file.
    """
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19",
            "0.0.19",
            [UnitState("drive", "prod", c, "managed") for c in components],
        )
    )
    if groups is not None:
        tree.write_groups("drive", "prod", components[0], groups)
        return
    if component_hosts:
        for component, comp_hosts in component_hosts.items():
            tree.write_hosts("drive", "prod", component, component, list(comp_hosts))
        return
    if hosts:
        tree.write_hosts("drive", "prod", components[0], components[0], list(hosts))


def seed_scaffolding_artifacts() -> None:
    """Pre-create the 4 trashable .st-cli/ artifacts so a clean/no-clean assertion is
    meaningful."""
    paths.st_cli_dir().mkdir(parents=True, exist_ok=True)
    (paths.st_cli_dir() / "ansible.cfg").write_text("[defaults]\n")
    (paths.st_cli_dir() / "galaxy-requirements.yml").write_text("collections: []\n")
    paths.playbooks_dir().mkdir(parents=True, exist_ok=True)
    (paths.playbooks_dir() / "meet-prod-meet.yml").write_text("[]\n")
    paths.collections_dir().mkdir(parents=True, exist_ok=True)


def seed_livekit_provider(repo: Path) -> None:
    """Seed a bootstrapped meet/prod/livekit provider unit (vars/vault/hosts).

    The redis address is distinctive, not the co-located default, so a test can
    tell a real adoption apart from the fallback default.
    """
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("meet", "prod", "livekit", "managed")]
        )
    )
    data = tree.load_vars("meet", "prod", "livekit")
    data["st_meet_livekit_domain"] = "livekit.example.org"
    data["st_meet_livekit_turn_domain"] = "turn.example.org"
    data["st_meet_livekit_valkey_enabled"] = False
    data["st_meet_livekit_redis_address"] = "livekit-redis.example:6379"
    tree.save_vars("meet", "prod", "livekit", data)
    tree.write_hosts("meet", "prod", "livekit", "livekit", ["10.0.0.1"])
    vp = paths.vault_path("meet", "prod", "livekit")
    vp.parent.mkdir(parents=True, exist_ok=True)
    with vp.open("w", encoding="utf-8") as fh:
        tree.yaml().dump(
            {
                "st_meet_livekit_api_key": "real-token",
                "st_meet_livekit_api_secret": "real-secret",
                "st_meet_livekit_redis_password": "real-redis-pass",
            },
            fh,
        )
    vault.encrypt_file(vp)


def seed_docs_yprovider_unit(repo: Path) -> None:
    """Seed a bootstrapped docs/prod/yprovider unit (vars/vault/hosts).

    The vault carries distinctive secret values so a test can tell a kept unit's
    values apart from freshly generated ones.
    """
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.20", "0.0.20", [UnitState("docs", "prod", "yprovider", "managed")]
        )
    )
    data = tree.load_vars("docs", "prod", "yprovider")
    data["st_docs_yprovider_env"] = (
        "COLLABORATION_SERVER_SECRET={{ vault_collaboration_server_secret }}\n"
        "COLLABORATION_SERVER_ORIGIN=https://docs.example.org\n"
        "COLLABORATION_BACKEND_BASE_URL=https://docs.example.org\n"
        "Y_PROVIDER_API_KEY={{ vault_y_provider_api_key }}\n"
        "COLLABORATION_LOGGING=true\n"
    )
    tree.save_vars("docs", "prod", "yprovider", data)
    tree.write_hosts("docs", "prod", "yprovider", "yprovider", ["10.0.0.9"])
    vp = paths.vault_path("docs", "prod", "yprovider")
    vp.parent.mkdir(parents=True, exist_ok=True)
    with vp.open("w", encoding="utf-8") as fh:
        tree.yaml().dump(
            {
                "vault_collaboration_server_secret": "kept-collab-secret",
                "vault_y_provider_api_key": "kept-yprovider-key",
            },
            fh,
        )
    vault.encrypt_file(vp)


def seed_meet_egress_unit(repo: Path, hosts=("10.0.0.2",)) -> None:
    """Seed a bootstrapped meet/prod/egress unit standalone on ``hosts``.

    Call ``seed_livekit_provider`` first. It shares that unit's domain, redis
    address, and vault secrets, so a livekit replay that mirrors them is a
    byte no-op.
    """
    m = manifest.load_manifest()
    manifest.upsert_unit(m, UnitState("meet", "prod", "egress", "managed"))
    manifest.save_manifest(m)
    meta = appmeta.load_app("meet")
    data = tree.load_vars("meet", "prod", "egress")
    data["st_meet_livekit_domain"] = "livekit.example.org"
    data["st_meet_livekit_redis_address"] = "livekit-redis.example:6379"
    data.yaml_set_start_comment(
        writer.vars_header("meet", meta, meta.component("egress"))
    )
    tree.save_vars("meet", "prod", "egress", data)
    tree.write_hosts("meet", "prod", "egress", "egress", list(hosts))
    vp = paths.vault_path("meet", "prod", "egress")
    vp.parent.mkdir(parents=True, exist_ok=True)
    with vp.open("w", encoding="utf-8") as fh:
        tree.yaml().dump(
            {
                "st_meet_livekit_api_key": "real-token",
                "st_meet_livekit_api_secret": "real-secret",
                "st_meet_livekit_redis_password": "real-redis-pass",
            },
            fh,
        )
    vault.encrypt_file(vp)


def seed_hashi_livekit_provider(repo: Path) -> None:
    """Seed a bootstrapped meet/prod/livekit unit under the hashi_vault backend:
    lookup-ref secrets in vars.yml, no ``vault.yml``, no ``.vault-pass``.

    Co-located with the default redis address, so a standalone replay never
    touches the redis-topology prompts.
    """
    manifest.save_manifest(
        StCliManifest(
            "0.0.20",
            "0.0.20",
            [UnitState("meet", "prod", "livekit", "managed")],
            [SecretConfig("meet", "prod", "hashi_vault")],
        )
    )
    data = tree.load_vars("meet", "prod", "livekit")
    data["st_meet_livekit_domain"] = "livekit.example.org"
    data["st_meet_livekit_turn_domain"] = "turn.example.org"
    data["st_meet_livekit_api_key"] = (
        "{{ lookup('community.hashi_vault.hashi_vault', "
        "'kv/data/meet:LIVEKIT_API_KEY') }}"
    )
    data["st_meet_livekit_api_secret"] = (
        "{{ lookup('community.hashi_vault.hashi_vault', "
        "'kv/data/meet:LIVEKIT_API_SECRET') }}"
    )
    data["st_meet_livekit_valkey_enabled"] = True
    data["st_meet_livekit_redis_address"] = "127.0.0.1:6379"
    tree.save_vars("meet", "prod", "livekit", data)
    tree.write_hosts("meet", "prod", "livekit", "livekit", ["10.0.0.1"])


def seed_external_livekit_with_leftover_tree(repo: Path) -> None:
    """Seed a meet/prod/livekit unit recorded ``external``, with its local tree
    still on disk from before it was manually switched (recorded mode wins
    over tree presence).

    The leftover tree keeps its own, now-ignored domain, distinct from the
    core's, so a test can tell the two apart.
    """
    seed_creds(repo)
    with pytest.MonkeyPatch.context() as mp:
        script_questionary(
            mp,
            [
                ("select", "Secret backend:", "ansible-vault"),
                ("text", "meet host(s)", "10.0.0.5"),
                ("text", "Public domain for meet", "meet.example.org"),
                ("select", "Database configuration:", "discrete (DB_*)"),
                ("text", "DB_HOST", "db.example.org"),
                ("text", "DB_NAME", "meetdb"),
                ("text", "DB_USER", "meetuser"),
                ("password", "DB_PASSWORD", "dbpass123"),
                ("text", "DB_PORT", "5432"),
                ("text", "REDIS_URL", "redis://redis:6379/0"),
                ("text", "AWS_S3_ENDPOINT_URL", "https://s3.example.org"),
                ("text", "AWS_S3_ACCESS_KEY_ID", "accesskey"),
                ("password", "AWS_S3_SECRET_ACCESS_KEY", "secretkey"),
                ("text", "AWS_STORAGE_BUCKET_NAME", "meet-media"),
                ("text", "AWS_S3_REGION_NAME (optional)", ""),
                ("select", "Identity provider:", "keycloak"),
                ("text", "Keycloak base URL", "https://idp.example.org"),
                ("text", "Keycloak realm", "master"),
                ("text", "OIDC_RP_CLIENT_ID", "meet-client-id"),
                ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
                ("confirm", "Configure transactional email (SMTP) settings?", False),
                ("confirm", "cadvisor", True),
                ("select", "Bootstrap livekit now?", "Yes — bootstrap now"),
                ("text", "livekit host(s)", "10.0.0.1"),
                ("text", "egress (leave blank", ""),
                (
                    "text",
                    "LiveKit domain (e.g. livekit.example.org)",
                    "livekit.example.org",
                ),
                (
                    "text",
                    "LiveKit TURN domain (e.g. turn.example.org)",
                    "turn.example.org",
                ),
                ("confirm", "livekit", True),
                ("confirm", "egress", True),
            ],
        )
        bootstrap.bootstrap("meet", "prod")

    m = manifest.load_manifest()
    for u in m.units:
        if u.component == "livekit":
            u.mode = "external"
    manifest.save_manifest(m)

    core_vars_path = paths.vars_path("meet", "prod", "meet")
    text = core_vars_path.read_text()
    old = "LIVEKIT_API_URL=wss://livekit.example.org"
    assert old in text, f"fixture expects {old} in {core_vars_path}"
    text = text.replace(old, "LIVEKIT_API_URL=wss://external-livekit.example.org")
    core_vars_path.write_text(text)


def meet_first_run_script(
    *,
    smtp: bool,
    db_mode: str = "discrete",
    livekit: str | None = "No — bootstrap later",
    secret_backend: bool = True,
) -> list[tuple]:
    """Build a first-run meet core script.

    ``db_mode`` picks ``"discrete"`` or ``"url"`` DB prompts.
    ``secret_backend=False`` skips the backend select; ``livekit=None`` skips
    the trailing select.
    """
    script = []
    if secret_backend:
        script.append(("select", "Secret backend:", "ansible-vault"))
    script.append(("text", "meet host(s)", "10.0.0.5"))
    script.append(("text", "Public domain for meet", "meet.example.org"))
    if db_mode == "discrete":
        script += [
            ("select", "Database configuration:", "discrete (DB_*)"),
            ("text", "DB_HOST", "db.example.org"),
            ("text", "DB_NAME", "meetdb"),
            ("text", "DB_USER", "meetuser"),
            ("password", "DB_PASSWORD", "dbpass123"),
            ("text", "DB_PORT", "5432"),
        ]
    else:
        script += [
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://meet"),
        ]
    script += [
        ("text", "REDIS_URL", "redis://redis:6379/0"),
        ("text", "AWS_S3_ENDPOINT_URL", "https://s3.example.org"),
        ("text", "AWS_S3_ACCESS_KEY_ID", "accesskey"),
        ("password", "AWS_S3_SECRET_ACCESS_KEY", "secretkey"),
        ("text", "AWS_STORAGE_BUCKET_NAME", "meet-media"),
        ("text", "AWS_S3_REGION_NAME (optional)", ""),
        ("select", "Identity provider:", "keycloak"),
        ("text", "Keycloak base URL", "https://idp.example.org"),
        ("text", "Keycloak realm", "master"),
        ("text", "OIDC_RP_CLIENT_ID", "meet-client-id"),
        ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
    ]
    if smtp:
        script += [
            ("confirm", "Configure transactional email (SMTP) settings?", True),
            ("text", "DJANGO_EMAIL_HOST", "smtp.example.org"),
            ("text", "DJANGO_EMAIL_PORT", "587"),
            ("text", "DJANGO_EMAIL_HOST_USER (optional)", "smtpuser"),
            ("password", "DJANGO_EMAIL_HOST_PASSWORD", "smtppass"),
            ("confirm", "DJANGO_EMAIL_USE_TLS?", True),
            ("confirm", "DJANGO_EMAIL_USE_SSL?", False),
            ("text", "DJANGO_EMAIL_FROM", "noreply@example.org"),
            ("text", "DJANGO_EMAIL_BRAND_NAME (optional)", "MeetBrand"),
        ]
    else:
        script.append(
            ("confirm", "Configure transactional email (SMTP) settings?", False)
        )
    script.append(("confirm", "cadvisor", True))
    if livekit is not None:
        script.append(("select", "Bootstrap livekit now?", livekit))
    return script


def livekit_script(
    host="10.0.0.1",
    *,
    egress_host="",
    ask_now=False,
    public_domain=False,
    confirm_livekit=True,
    confirm_egress=True,
) -> list[tuple]:
    """Build the livekit/egress bootstrap prompt fragment shared by many tests.

    ``ask_now``, ``public_domain``, ``confirm_livekit`` and ``confirm_egress``
    each toggle one optional prompt in the fragment, independently.
    """
    script = []
    if ask_now:
        script.append(("select", "Bootstrap livekit now?", "Yes — bootstrap now"))
    script += [
        ("text", "livekit host(s)", host),
        ("text", "egress (leave blank", egress_host),
        ("text", "LiveKit domain (e.g. livekit.example.org)", "livekit.example.org"),
        ("text", "LiveKit TURN domain (e.g. turn.example.org)", "turn.example.org"),
    ]
    if public_domain:
        script.append(
            (
                "text",
                "Public domain for meet (for the LiveKit recording webhook)",
                "meet.example.org",
            )
        )
    if confirm_livekit:
        script.append(("confirm", "livekit", True))
    if confirm_egress:
        script.append(("confirm", "egress", True))
    return script


def drive_first_run_script() -> list[tuple]:
    return [
        ("select", "Secret backend:", "ansible-vault"),
        ("text", "drive host(s)", "10.0.0.10"),
        ("text", "workers (leave blank", ""),
        ("text", "Public domain for drive", "drive.example.org"),
        ("select", "Database configuration:", "DATABASE_URL"),
        ("text", "DATABASE_URL", "postgres://drive"),
        ("text", "REDIS_URL", "redis://redis:6379/0"),
        ("text", "AWS_S3_ENDPOINT_URL", "https://s3.fr-par.scw.cloud"),
        ("text", "AWS_S3_ACCESS_KEY_ID", "driveaccess"),
        ("password", "AWS_S3_SECRET_ACCESS_KEY", "drivesecretkey"),
        ("text", "AWS_STORAGE_BUCKET_NAME", "drive-media"),
        ("text", "AWS_S3_REGION_NAME (optional)", "fr-par"),
        ("select", "Identity provider:", "keycloak"),
        ("text", "Keycloak base URL", "https://idp.example.org"),
        ("text", "Keycloak realm", "master"),
        ("text", "OIDC_RP_CLIENT_ID", "drive-client-id"),
        ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
        ("confirm", "Configure transactional email (SMTP) settings?", False),
        ("confirm", "cadvisor", True),
        ("select", "Bootstrap collabora now?", "No — bootstrap later"),
    ]


def messages_first_run_script(
    *,
    db_mode: str = "url",
    blobs_offload: bool = False,
    outbound: str = "direct",
) -> list[tuple]:
    """Build a first-run messages core script, up to and including the "cadvisor"
    confirm."""
    script = [("select", "Secret backend:", "ansible-vault")]
    script += [
        ("text", "messages host(s)", "10.0.0.4"),
        ("text", "workers (leave blank", ""),
        ("text", "Public domain for messages", "messages.example.org"),
    ]
    if db_mode == "discrete":
        script += [
            ("select", "Database configuration:", "discrete (DB_*)"),
            ("text", "DB_HOST", "msgdb.example.org"),
            ("text", "DB_NAME", "messagesdb"),
            ("text", "DB_USER", "messagesuser"),
            ("password", "DB_PASSWORD", "msgdbpass"),
            ("text", "DB_PORT", "5432"),
        ]
    else:
        script += [
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://messages"),
        ]
    script += [
        ("text", "REDIS_URL", "redis://redis:6379/0"),
        ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", "https://s3.example.org"),
        ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", "msg-imports"),
        ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", "impkey"),
        ("password", "STORAGE_MESSAGE_IMPORTS_SECRET_KEY", "impsecret"),
        ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ""),
        ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
        ("confirm", "Enable blobs offloading", blobs_offload),
    ]
    if blobs_offload:
        script += [
            ("text", "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_BLOBS_BUCKET_NAME", "msg-blobs"),
            ("text", "STORAGE_MESSAGE_BLOBS_ACCESS_KEY", "blobkey"),
            ("password", "STORAGE_MESSAGE_BLOBS_SECRET_KEY", "blobsecret"),
            ("text", "STORAGE_MESSAGE_BLOBS_REGION_NAME", ""),
        ]
    script += [
        ("text", "OPENSEARCH_URL", "http://opensearch:9200"),
        ("text", "MESSAGES_TECHNICAL_DOMAIN", "mail.example.org"),
        ("select", "Identity provider:", "keycloak"),
        ("text", "Keycloak base URL", "https://idp.example.org"),
        ("text", "Keycloak realm", "master"),
        ("text", "OIDC_RP_CLIENT_ID", "messages-client-id"),
        ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
        ("select", "Outbound mail mode", outbound),
    ]
    if outbound == "relay":
        script += [
            ("text", "MTA_OUT_RELAY_HOST", "smtp.example.org:587"),
            ("text", "MTA_OUT_RELAY_USERNAME", "relayuser"),
            ("password", "MTA_OUT_RELAY_PASSWORD", "relaypass"),
        ]
    script.append(("confirm", "cadvisor", True))
    return script


def docs_first_run_script(
    *,
    smtp: bool = False,
    yprovider: str | None = "No — bootstrap later",
    secret_backend: bool = True,
) -> list[tuple]:
    """Build a first-run docs core script.

    ``yprovider=None`` omits the trailing "Bootstrap yprovider now?" select.
    """
    script = []
    if secret_backend:
        script.append(("select", "Secret backend:", "ansible-vault"))
    script += [
        ("text", "docs host(s)", "10.0.0.5"),
        ("text", "workers (leave blank", ""),
        ("text", "Public domain for docs", "docs.example.org"),
        ("select", "Database configuration:", "DATABASE_URL"),
        ("text", "DATABASE_URL", "postgres://docs"),
        ("text", "REDIS_URL", "redis://redis:6379/0"),
        ("text", "AWS_S3_ENDPOINT_URL", "https://s3.example.org"),
        ("text", "AWS_S3_ACCESS_KEY_ID", "accesskey"),
        ("password", "AWS_S3_SECRET_ACCESS_KEY", "secretkey"),
        ("text", "AWS_STORAGE_BUCKET_NAME", "docs-media"),
        ("text", "AWS_S3_REGION_NAME (optional)", ""),
        ("select", "Identity provider:", "keycloak"),
        ("text", "Keycloak base URL", "https://idp.example.org"),
        ("text", "Keycloak realm", "master"),
        ("text", "OIDC_RP_CLIENT_ID", "docs-client-id"),
        ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
    ]
    if smtp:
        script += [
            ("confirm", "Configure transactional email (SMTP) settings?", True),
            ("text", "DJANGO_EMAIL_HOST", "smtp.example.org"),
            ("text", "DJANGO_EMAIL_PORT", "587"),
            ("text", "DJANGO_EMAIL_HOST_USER (optional)", ""),
            ("password", "DJANGO_EMAIL_HOST_PASSWORD", "smtp-pass"),
            ("confirm", "DJANGO_EMAIL_USE_TLS?", True),
            ("confirm", "DJANGO_EMAIL_USE_SSL?", False),
            ("text", "DJANGO_EMAIL_FROM", "noreply@docs.example.org"),
            ("text", "DJANGO_EMAIL_BRAND_NAME (optional)", ""),
        ]
    else:
        script.append(
            ("confirm", "Configure transactional email (SMTP) settings?", False)
        )
    script.append(("confirm", "cadvisor", True))
    if yprovider is not None:
        script.append(("select", "Bootstrap yprovider now?", yprovider))
    return script


def projects_first_run_script(
    *,
    host: str = "10.0.0.7",
    domain: str = "projects.example.org",
    database_url: str = "postgresql://u:p@db.example.org:5432/projects",
    oidc_provider: str = "keycloak",
    custom_issuer: str = "",
    org_claim: str = "",
    scaling: bool = False,
    s3: bool = True,
    smtp: bool = False,
    cadvisor: bool = True,
) -> list[tuple]:
    """Build a first-run projects script: a Sails app, so no DB-mode select and
    no dependency loop.

    ``oidc_provider`` is ``"keycloak"``, ``"custom"`` (typed issuer URL), or
    any other value (no extra prompt). ``scaling=True`` forces S3 on.
    """
    script = [("select", "Secret backend:", "ansible-vault")]
    script += [
        ("text", "projects host(s)", host),
        ("text", "Public domain for projects", domain),
        ("text", "DATABASE_URL", database_url),
        ("select", "Identity provider:", oidc_provider),
    ]
    if oidc_provider == "keycloak":
        script += [
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "st"),
        ]
    elif oidc_provider == "custom":
        script.append(("text", "OIDC issuer URL", custom_issuer))
    script += [
        ("text", "OIDC_CLIENT_ID", "projects"),
        ("password", "OIDC_CLIENT_SECRET", "oidcsecret"),
        ("text", "ORGANIZATION_ID_CLAIM", org_claim),
        ("confirm", "horizontal scaling", scaling),
    ]
    if scaling:
        script.append(("text", "REDIS_URL", "redis://:pw@redis.example.org:6379/0"))
    else:
        script.append(("confirm", "Configure S3 object storage", s3))
    if scaling or s3:
        script += [
            ("text", "S3_ENDPOINT", "https://s3.fr-par.scw.cloud"),
            ("text", "S3_REGION", "fr-par"),
            ("text", "S3_ACCESS_KEY_ID", "AKID"),
            ("password", "S3_SECRET_ACCESS_KEY", "s3secret"),
            ("text", "S3_BUCKET", "projects"),
            ("confirm", "S3_FORCE_PATH_STYLE", True),
        ]
    script.append(("confirm", "Configure transactional email", smtp))
    if smtp:
        script += [
            ("text", "SMTP_HOST", "smtp.example.org"),
            ("text", "SMTP_PORT", "587"),
            ("confirm", "SMTP_SECURE", True),
            ("text", "SMTP_USER", "mailer@example.org"),
            ("password", "SMTP_PASSWORD", "smtppass"),
            ("text", "SMTP_FROM", '"Projects" <noreply@example.org>'),
        ]
    script.append(("confirm", "cadvisor", cadvisor))
    return script


def with_answers(script: list[tuple], overrides: dict[str, object]) -> list[tuple]:
    """Return a copy of ``script`` with each tuple's answer replaced when a
    key in ``overrides`` is a substring of that tuple's prompt."""
    result = []
    for kind, sub, ans in script:
        for key, new_ans in overrides.items():
            if key in sub:
                ans = new_ans
                break
        result.append((kind, sub, ans))
    return result


class _AcceptDefault:
    """Sentinel script answer: "press Enter" on whatever ``default=`` the prompt
    call was given.

    Rebootstrap tests use it to script an Enter-through run without
    hardcoding the recovered value at every single prompt.
    """

    def __repr__(self) -> str:
        return "ACCEPT_DEFAULT"


ACCEPT_DEFAULT = _AcceptDefault()


class FakeQuestion:
    """A questionary Question stand-in returning a canned answer from .ask()."""

    def __init__(self, answer):
        self._answer = answer

    def ask(self):
        return self._answer


class ScriptedQuestionary:
    """Replaces questionary.text/password/confirm/select with canned answers.

    Each script is a ``(kind, substring, answer)`` tuple; the first whose
    ``kind`` matches and whose ``substring`` is in the prompt is consumed.
    """

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.select_calls: list[tuple[str, list[str]]] = []

    def _consume(self, kind, prompt):
        for i, (k, sub, _ans) in enumerate(self._scripts):
            if k == kind and sub in prompt:
                return self._scripts.pop(i)[2]
        raise AssertionError(
            f"unexpected questionary.{kind} prompt: {prompt!r}\n"
            f"remaining scripts: {self._scripts}"
        )

    def text(self, prompt, **kwargs):
        ans = self._consume("text", prompt)
        if ans is ACCEPT_DEFAULT:
            return FakeQuestion(kwargs.get("default", ""))
        return FakeQuestion(ans)

    def password(self, prompt, **kwargs):
        return FakeQuestion(self._consume("password", prompt))

    def confirm(self, prompt, **kwargs):
        # The pre-questionnaire readiness gate auto-passes (yes) without consuming
        # a script, so every full/core/workers-run test needn't script it.
        if "ready" in prompt.lower():
            return FakeQuestion(True)
        ans = self._consume("confirm", prompt)
        if ans is ACCEPT_DEFAULT:
            return FakeQuestion(kwargs.get("default", False))
        return FakeQuestion(ans)

    def select(self, message, choices, **kwargs):
        self.select_calls.append((message, list(choices)))
        ans = self._consume("select", message)
        if ans is ACCEPT_DEFAULT:
            return FakeQuestion(kwargs.get("default"))
        return FakeQuestion(ans)


class DefaultsQuestionary(ScriptedQuestionary):
    """Answers ``ACCEPT_DEFAULT`` to every prompt that no script covers.

    An unscripted ``password`` prompt, or a required ``text`` prompt without a
    default, raises. A hashi_vault leg must check ``asked`` for its secrets.
    """

    def __init__(self, scripts):
        super().__init__(scripts)
        self.prompts: list[tuple[str, str]] = []

    def _consume(self, kind, prompt):
        self.prompts.append((kind, prompt))
        for i, (k, sub, _ans) in enumerate(self._scripts):
            if k == kind and sub in prompt:
                return self._scripts.pop(i)[2]
        if kind == "password":
            raise AssertionError(f"unexpected questionary.password prompt: {prompt!r}")
        return ACCEPT_DEFAULT

    def text(self, prompt, **kwargs):
        from st_cli.core import prompts

        scripted = any(k == "text" and sub in prompt for k, sub, _ in self._scripts)
        required = kwargs.get("validate") is prompts._require
        if not scripted and required and not kwargs.get("default"):
            raise AssertionError(
                f"unexpected required questionary.text prompt: {prompt!r}"
            )
        return super().text(prompt, **kwargs)

    def asked(self, kind, substring) -> bool:
        return any(k == kind and substring in p for k, p in self.prompts)


def _patch_questionary(monkeypatch, sq):
    from st_cli.core import prompts

    monkeypatch.setattr(prompts.questionary, "text", sq.text)
    monkeypatch.setattr(prompts.questionary, "password", sq.password)
    monkeypatch.setattr(prompts.questionary, "confirm", sq.confirm)
    monkeypatch.setattr(prompts.questionary, "select", sq.select)
    return sq


def accept_defaults(monkeypatch, scripts=()) -> DefaultsQuestionary:
    """Script an Enter-through run; ``scripts`` overrides selected prompts."""
    return _patch_questionary(monkeypatch, DefaultsQuestionary(scripts))


def script_questionary(monkeypatch, scripts) -> ScriptedQuestionary:
    """Patch ``st_cli.core.prompts.questionary`` with scripted responses.

    Every interactive call reaches questionary through that shared surface,
    so this one patch covers the whole bootstrap flow.
    """
    return _patch_questionary(monkeypatch, ScriptedQuestionary(scripts))
