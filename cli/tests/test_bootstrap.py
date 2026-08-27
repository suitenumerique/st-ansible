"""Tests for st_cli.cmd.bootstrap: host validation, the _ask prompt helpers, and
the `bootstrap APP ENV -c/--component COMP` staged-rollout path.
"""

from __future__ import annotations

import pytest
from helpers import (
    docs_first_run_script,
    livekit_script,
    meet_first_run_script,
    messages_first_run_script,
    projects_first_run_script,
    script_questionary,
    seed_creds,
    seed_docs_yprovider_unit,
    seed_livekit_provider,
    with_answers,
)

from st_cli.cmd import bootstrap
from st_cli.core import appmeta, envrender, manifest, paths, prompts, tree, vault
from st_cli.core.errors import StCliError
from st_cli.core.secretbackend import AnsibleVaultBackend


def test_host_validation():
    from st_cli.core.prompts import _is_valid_host

    for good in (
        "10.0.0.5",
        "192.168.1.1",
        "::1",
        "meet.example.org",
        "host1",
        "k8s-node-3.lan",
    ):
        assert _is_valid_host(good), good
    for bad in ("10.1.1.a", "10.2.2.2.2.2", "999.1.1.1", "10.1.1", "", "bad host"):
        assert not _is_valid_host(bad), bad


def test_ask_placeholder_smoke(monkeypatch):
    """`_ask(..., placeholder=...)` passes placeholder=, not default=."""
    captured: dict = {}

    class _FakeQuestion:
        def ask(self):
            return "typed-value"

    def _fake_text(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return _FakeQuestion()

    monkeypatch.setattr(prompts.questionary, "text", _fake_text)

    result = bootstrap._ask("Pick a domain", placeholder="meet.example.org")
    assert result == "typed-value"
    assert "placeholder" in captured["kwargs"]
    assert "default" not in captured["kwargs"]  # placeholder path never sets default

    # and the classic default= path still works
    bootstrap._ask("DB_PORT", default="5432")
    assert captured["kwargs"].get("default") == "5432"
    assert "placeholder" not in captured["kwargs"]


def test_ask_default_prefills_editable_value_enter_accepts(monkeypatch):
    """`_ask(..., default=...)` passes default=, applies the required
    validator, and a typed value overrides the pre-filled default."""
    captured: dict = {}

    class _FakeQuestion:
        def __init__(self, answer):
            self._answer = answer

        def ask(self):
            return self._answer

    answer = {"v": ""}

    def _fake_text(prompt, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeQuestion(answer["v"])

    monkeypatch.setattr(prompts.questionary, "text", _fake_text)

    # .ask() returning the prefilled value simulates pressing Enter to accept it.
    answer["v"] = "redis://redis:6379/0"
    assert bootstrap._ask("REDIS_URL", "redis://redis:6379/0") == "redis://redis:6379/0"
    assert captured["kwargs"]["default"] == "redis://redis:6379/0"
    assert "placeholder" not in captured["kwargs"]
    assert captured["kwargs"]["validate"] is prompts._require  # required path applied

    # a typed value overrides the default
    answer["v"] = "redis://other:6379/1"
    assert bootstrap._ask("REDIS_URL", "redis://redis:6379/0") == "redis://other:6379/1"


def test_bootstrap_component_livekit_deploys_provider_only(repo, monkeypatch):
    """`bootstrap -c livekit` writes the livekit provider unit and bundles the
    egress unit, without writing the core or asking to bootstrap livekit."""
    seed_creds(repo)  # writes .vault-pass, so the vault-backend prompt does not fire
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            *livekit_script(host="10.0.0.1", public_domain=True),
        ],
    )

    bootstrap.bootstrap("meet", "prod", component="livekit")

    assert paths.vars_path("meet", "prod", "livekit").exists()
    lv = tree.load_vars("meet", "prod", "livekit")
    assert lv["st_meet_livekit_domain"] == "livekit.example.org"
    assert lv["st_meet_livekit_turn_domain"] == "turn.example.org"
    # st_meet_public_host derives from DOMAIN in apply_component_vars, the single
    # place the role reads it to build the LiveKit recording webhook URL.
    assert lv["st_meet_public_host"] == "meet.example.org"
    assert lv["st_meet_cadvisor_enabled"] is True  # cadvisor prompt yields a real bool
    # a single co-located node uses local valkey, not an external redis.
    assert lv["st_meet_livekit_valkey_enabled"] is True
    assert lv["st_meet_livekit_redis_address"] == "127.0.0.1:6379"
    assert vault.is_encrypted(paths.vault_path("meet", "prod", "livekit"))
    lvault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    assert "st_meet_livekit_api_key" in lvault
    assert "st_meet_livekit_api_secret" in lvault
    assert "10.0.0.1" in (repo / "meet/prod/livekit/hosts").read_text()

    # egress bundles in, co-located on the livekit host
    assert paths.vars_path("meet", "prod", "egress").exists()
    ev = tree.load_vars("meet", "prod", "egress")
    assert ev["st_meet_livekit_domain"] == "livekit.example.org"
    assert ev["st_meet_livekit_redis_address"] == "127.0.0.1:6379"
    assert ev["st_meet_cadvisor_enabled"] is True
    assert "10.0.0.1" in (repo / "meet/prod/egress/hosts").read_text()
    assert vault.is_encrypted(paths.vault_path("meet", "prod", "egress"))
    evault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert evault["st_meet_livekit_api_key"] == lvault["st_meet_livekit_api_key"]
    assert evault["st_meet_livekit_api_secret"] == lvault["st_meet_livekit_api_secret"]

    # component mode assumes deploy, so no dependency select fires.
    assert not any("Bootstrap livekit now?" in msg for msg, _ in sq.select_calls)

    assert not paths.vars_path("meet", "prod", "meet").exists()
    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert "livekit" in by_comp and by_comp["livekit"].mode == "managed"
    assert "egress" in by_comp and by_comp["egress"].mode == "managed"
    assert "meet" not in by_comp


def test_bootstrap_livekit_external_redis(repo, monkeypatch):
    """livekit and egress on different hosts prompt one shared redis address,
    accepted verbatim, and store it on both units."""
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        meet_first_run_script(smtp=False, db_mode="url", livekit="Yes — bootstrap now")
        # egress hosts are asked right after livekit hosts, before LiveKit
        # domain/TURN, because a different host skips co-location.
        # No "Public domain for meet" prompt: _ensure_meet_domain sees DOMAIN set.
        + livekit_script(host="10.0.0.1", egress_host="10.0.0.2", confirm_egress=False)
        + [
            # the topology check fails, so the redis-address prompt fires after
            # the livekit cadvisor confirm. A non-host:port value pins the
            # no-validation rule.
            ("text", "Redis address shared by livekit and egress", "whatever-redis"),
            ("text", "Redis username shared by livekit and egress", "redisuser"),
            ("password", "Redis password shared by livekit and egress", "redispass123"),
            ("confirm", "egress", True),  # egress cadvisor
        ],
    )

    bootstrap.bootstrap("meet", "prod")

    # valkey stays disabled, and a non-host:port address is accepted verbatim.
    lv = tree.load_vars("meet", "prod", "livekit")
    assert lv["st_meet_livekit_valkey_enabled"] is False
    assert lv["st_meet_livekit_redis_address"] == "whatever-redis"
    assert lv["st_meet_livekit_redis_username"] == "redisuser"
    # egress adopts the same redis address and username, so both units share one redis.
    ev = tree.load_vars("meet", "prod", "egress")
    assert ev["st_meet_livekit_redis_address"] == "whatever-redis"
    assert ev["st_meet_livekit_redis_username"] == "redisuser"
    assert ev["st_meet_livekit_domain"] == "livekit.example.org"
    # egress is not co-located: it keeps its own hosts file with 10.0.0.2.
    assert "10.0.0.2" in (repo / "meet/prod/egress/hosts").read_text()
    assert "10.0.0.2" not in (repo / "meet/prod/livekit/hosts").read_text()

    # the redis password mirrors into egress's own vault, equal to livekit's value.
    lvault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    evault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert lvault["st_meet_livekit_redis_password"] == "redispass123"
    assert (
        evault["st_meet_livekit_redis_password"]
        == lvault["st_meet_livekit_redis_password"]
    )

    # no leftover script guards that the redis-address prompt fired exactly once.
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert by_comp["meet"].mode == "managed"
    assert by_comp["livekit"].mode == "managed"
    assert by_comp["egress"].mode == "managed"


def test_bootstrap_component_livekit_external_redis_blank_auth(repo, monkeypatch):
    """A blank redis username/password is legal for an unauthenticated
    external redis: both are dropped entirely, not stored as empty strings."""
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            # egress on a different host skips co-location, so the external-redis
            # address, username, and password prompts fire.
            *livekit_script(
                host="10.0.0.1",
                egress_host="10.0.0.2",
                public_domain=True,
                confirm_egress=False,
            ),
            # a blank username and password are both legal for an unauthenticated redis.
            (
                "text",
                "Redis address shared by livekit and egress",
                "redis.example.org:6379",
            ),
            ("text", "Redis username shared by livekit and egress", ""),
            ("password", "Redis password shared by livekit and egress", ""),
            ("confirm", "egress", True),  # egress cadvisor
        ],
    )

    bootstrap.bootstrap("meet", "prod", component="livekit")

    # valkey stays disabled, the external address stores verbatim, and the blank
    # username omits the var entirely, per the truthiness check in _bundle_egress.
    lv = tree.load_vars("meet", "prod", "livekit")
    assert lv["st_meet_livekit_valkey_enabled"] is False
    assert lv["st_meet_livekit_redis_address"] == "redis.example.org:6379"
    assert "st_meet_livekit_redis_username" not in lv

    # egress reuses the same truthiness check when it builds its own vars.yml.
    ev = tree.load_vars("meet", "prod", "egress")
    assert "st_meet_livekit_redis_username" not in ev

    # the blank password is dropped entirely: no password key in either vault.
    lvault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    assert "st_meet_livekit_redis_password" not in lvault
    evault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert "st_meet_livekit_redis_password" not in evault
    assert evault["st_meet_livekit_api_key"] == lvault["st_meet_livekit_api_key"]
    assert evault["st_meet_livekit_api_secret"] == lvault["st_meet_livekit_api_secret"]

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"


def test_bootstrap_component_egress_standalone(repo, monkeypatch):
    """`bootstrap meet prod -c egress` with a livekit unit already on disk adopts
    livekit's domain and redis address instead of re-prompting for them."""
    seed_livekit_provider(repo)  # seeds livekit vars, redis addr, vault, and hosts
    lk_vars_before = (repo / "meet/prod/livekit/vars.yml").read_text()
    lk_vault_before = (repo / "meet/prod/livekit/vault.yml").read_bytes()

    sq = script_questionary(
        monkeypatch,
        [
            # setup_backend reuses the persisted ansible-vault choice from the
            # seeded livekit unit, so no "Secret backend:" select fires.
            ("text", "egress host(s)", "10.0.0.3"),
            ("confirm", "cadvisor", True),  # egress cadvisor
        ],
    )

    bootstrap.bootstrap("meet", "prod", component="egress")

    assert paths.vars_path("meet", "prod", "egress").exists()
    ev = tree.load_vars("meet", "prod", "egress")
    assert ev["st_meet_livekit_domain"] == "livekit.example.org"
    assert ev["st_meet_livekit_redis_address"] == "livekit-redis.example:6379"
    assert ev["st_meet_cadvisor_enabled"] is True
    assert "10.0.0.3" in (repo / "meet/prod/egress/hosts").read_text()

    lvault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    evault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert evault["st_meet_livekit_api_key"] == lvault["st_meet_livekit_api_key"]
    assert evault["st_meet_livekit_api_secret"] == lvault["st_meet_livekit_api_secret"]
    assert evault["st_meet_livekit_api_key"] == "real-token"
    assert evault["st_meet_livekit_api_secret"] == "real-secret"
    # external redis means valkey is disabled, so the redis password mirrors too.
    assert evault["st_meet_livekit_redis_password"] == "real-redis-pass"

    # component mode assumes deploy, so no dependency select fires.
    assert not any("Bootstrap egress now?" in msg for msg, _ in sq.select_calls)
    # no leftover script guards that adoption skips the redis-address prompt.
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    assert (repo / "meet/prod/livekit/vars.yml").read_text() == lk_vars_before
    assert (repo / "meet/prod/livekit/vault.yml").read_bytes() == lk_vault_before

    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert "egress" in by_comp and by_comp["egress"].mode == "managed"
    assert "livekit" in by_comp and by_comp["livekit"].mode == "managed"
    assert "meet" not in by_comp


def test_bootstrap_component_core_wires_deps_only(repo, monkeypatch):
    """A wire-only `bootstrap -c meet` after a livekit tree exists pulls
    LIVEKIT_* refs from livekit's vault without modifying livekit."""
    seed_livekit_provider(repo)
    livekit_vars_before = (repo / "meet/prod/livekit/vars.yml").read_text()
    livekit_vault_before = (repo / "meet/prod/livekit/vault.yml").read_bytes()

    # setup_backend reuses the persisted choice from the seeded livekit unit,
    # and wire-only reuses the dependency silently, so no select fires.
    sq = script_questionary(
        monkeypatch,
        meet_first_run_script(
            smtp=False, db_mode="url", secret_backend=False, livekit=None
        ),
    )
    bootstrap.bootstrap("meet", "prod", component="meet")

    assert paths.vars_path("meet", "prod", "meet").exists()
    assert tree.load_vars("meet", "prod", "meet")["st_meet_cadvisor_enabled"] is True
    core_vars = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "LIVEKIT_API_KEY={{ vault_livekit_api_key }}" in core_vars
    assert "LIVEKIT_API_SECRET={{ vault_livekit_api_secret }}" in core_vars
    assert "LIVEKIT_API_URL=wss://livekit.example.org" in core_vars
    assert vault.is_encrypted(paths.vault_path("meet", "prod", "meet"))
    cvault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "meet"))
    assert cvault["vault_livekit_api_key"] == "real-token"
    assert cvault["vault_livekit_api_secret"] == "real-secret"
    assert "REDIS_URL={{ vault_redis_url }}" in core_vars
    assert "CELERY_BROKER_URL={{ vault_redis_url }}" in core_vars
    assert cvault["vault_redis_url"] == "redis://redis:6379/0"
    assert "RECORDING_ENABLE=True" in core_vars

    assert (repo / "meet/prod/livekit/vars.yml").read_text() == livekit_vars_before
    assert (repo / "meet/prod/livekit/vault.yml").read_bytes() == livekit_vault_before

    dep_offers = [c for msg, c in sq.select_calls if "Bootstrap livekit now?" in msg]
    assert not dep_offers, "wire-only must reuse an existing provider without a select"

    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert "meet" in by_comp and by_comp["meet"].mode == "managed"
    assert "livekit" in by_comp and by_comp["livekit"].mode == "managed"


def test_bootstrap_meet_full_reuse_livekit_bundles_egress(repo, monkeypatch):
    """Reusing an existing livekit keeps egress in the deployment: `_reuse_egress`
    creates its tree from livekit's on-disk redis topology."""
    seed_livekit_provider(repo)
    sq = script_questionary(
        monkeypatch,
        meet_first_run_script(
            smtp=False,
            db_mode="url",
            secret_backend=False,
            livekit="Reuse existing in the repo",
        )
        + [
            ("confirm", "egress", True),  # egress cadvisor, created on reuse
        ],
    )

    bootstrap.bootstrap("meet", "prod")

    assert paths.vars_path("meet", "prod", "egress").exists()
    ev = tree.load_vars("meet", "prod", "egress")
    assert ev["st_meet_livekit_domain"] == "livekit.example.org"
    assert ev["st_meet_livekit_redis_address"] == "livekit-redis.example:6379"
    assert ev["st_meet_cadvisor_enabled"] is True
    # co-located on the livekit hosts, so the reuse path skips the egress-hosts prompt.
    assert "10.0.0.1" in (repo / "meet/prod/egress/hosts").read_text()
    evault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert evault["st_meet_livekit_api_key"] == "real-token"
    assert evault["st_meet_livekit_api_secret"] == "real-secret"
    assert evault["st_meet_livekit_redis_password"] == "real-redis-pass"
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert by_comp["meet"].mode == "managed"
    assert by_comp["livekit"].mode == "managed"
    assert by_comp["egress"].mode == "managed"


def test_bootstrap_meet_full_deploys_livekit_with_public_host(repo, monkeypatch):
    """`_ask_core` collects DOMAIN before the deps loop, so `_ensure_meet_domain`
    sees it already set and does not re-prompt "Public domain for meet"."""
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        meet_first_run_script(smtp=False, db_mode="url", livekit="Yes — bootstrap now")
        # egress hosts bundle into the livekit step, asked right after the
        # livekit hosts and before LiveKit domain/TURN: blank means co-locate.
        + livekit_script(host="10.0.0.1"),
    )

    bootstrap.bootstrap("meet", "prod")

    lv = tree.load_vars("meet", "prod", "livekit")
    assert lv["st_meet_livekit_domain"] == "livekit.example.org"
    assert lv["st_meet_livekit_turn_domain"] == "turn.example.org"
    assert lv["st_meet_public_host"] == "meet.example.org"
    assert lv["st_meet_cadvisor_enabled"] is True
    # a single co-located node uses local valkey.
    assert lv["st_meet_livekit_valkey_enabled"] is True
    assert lv["st_meet_livekit_redis_address"] == "127.0.0.1:6379"

    # egress bundles into the livekit step: it adopts the livekit ws domain and
    # local valkey address, and livekit's generated api creds mirror into its vault.
    assert paths.vars_path("meet", "prod", "egress").exists()
    ev = tree.load_vars("meet", "prod", "egress")
    assert ev["st_meet_livekit_domain"] == "livekit.example.org"
    assert ev["st_meet_livekit_redis_address"] == "127.0.0.1:6379"
    assert ev["st_meet_cadvisor_enabled"] is True
    lvault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    evault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert evault["st_meet_livekit_api_key"] == lvault["st_meet_livekit_api_key"]
    assert evault["st_meet_livekit_api_secret"] == lvault["st_meet_livekit_api_secret"]

    # st_meet_public_host is the single source of truth, written into the core
    # vars.yml from DOMAIN; recording is unconditionally enabled, with no prompt.
    # The env blob carries the verbatim {{ st_meet_public_host }} ref: it travels
    # through the answer value, and ansible resolves it at deploy.
    core_data = tree.load_vars("meet", "prod", "meet")
    assert core_data["st_meet_public_host"] == "meet.example.org"
    core_vars = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "LIVEKIT_API_KEY={{ vault_livekit_api_key }}" in core_vars
    assert "LIVEKIT_API_URL=wss://livekit.example.org" in core_vars
    assert "DJANGO_ALLOWED_HOSTS={{ st_meet_public_host }}" in core_vars
    assert "LOGIN_REDIRECT_URL=https://{{ st_meet_public_host }}/" in core_vars
    assert "RECORDING_ENABLE=True" in core_vars

    # DOMAIN is already set by _ask_core, so _ensure_meet_domain does not re-prompt.
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert by_comp["meet"].mode == "managed"
    assert by_comp["livekit"].mode == "managed"
    assert by_comp["egress"].mode == "managed"


def test_ask_core_meet_always_sets_recording_env(monkeypatch):
    """`_ask_core` for meet asks nothing about recording and always hardcodes
    RECORDING_ENABLE, RECORDING_OUTPUT_FOLDER, and the RECORDING_* block."""
    script_questionary(
        monkeypatch,
        [
            ("text", "Public domain for meet", "meet.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://meet"),
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
        ],
    )
    meta = appmeta.load_app("meet")
    answers = bootstrap._ask_core(meta, AnsibleVaultBackend())

    assert answers["RECORDING_ENABLE"] == "True"
    assert answers["RECORDING_OUTPUT_FOLDER"] == "recordings"
    # the download base URL references the st_meet_public_host ansible var, so the
    # operator changes the domain in one place and the {{ }} lands verbatim in the
    # env blob.
    assert (
        answers["RECORDING_DOWNLOAD_BASE_URL"]
        == "https://{{ st_meet_public_host }}/recording"
    )
    body = envrender.render_env("meet", "meet", answers)["st_meet_backend_env"]
    assert "RECORDING_ENABLE=True" in body
    assert "RECORDING_STORAGE_EVENT_ENABLE=False" in body
    assert "RECORDING_OUTPUT_FOLDER=recordings" in body
    assert (
        "RECORDING_DOWNLOAD_BASE_URL=https://{{ st_meet_public_host }}/recording"
        in body
    )


def test_ask_core_region_blank_clears_recovered_value_and_warns(monkeypatch, mocker):
    """A blank AWS_S3_REGION_NAME over a recovered value pops it and warns."""
    script_questionary(
        monkeypatch,
        [
            ("text", "Public domain for meet", "meet.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://meet"),
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
        ],
    )
    meta = appmeta.load_app("meet")
    warn_spy = mocker.patch.object(bootstrap.ui, "warn")

    answers = bootstrap._ask_core(
        meta, AnsibleVaultBackend(), {"AWS_S3_REGION_NAME": "fr-par"}
    )

    assert "AWS_S3_REGION_NAME" not in answers
    assert any("AWS_S3_REGION_NAME" in c.args[0] for c in warn_spy.call_args_list)


def test_bootstrap_keycloak_writes_env_blob_and_vault(repo, monkeypatch):
    """`bootstrap keycloak prod` writes the st_keycloak_env blob with
    {{ vault_* }} refs for the two passwords and registers the unit."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "keycloak host(s)", "10.0.0.9"),
            ("text", "Public domain for keycloak", "idp.example.org"),
            ("text", "Database host", "db.example.org"),
            ("text", "Database port", "5432"),
            ("text", "Database name", "keycloak"),
            ("text", "Database user", "keycloak"),
            ("password", "KC_DB_PASSWORD", "dbsecret"),
            ("text", "Bootstrap admin username", "admin"),
            ("password", "KC_BOOTSTRAP_ADMIN_PASSWORD", "adminsecret"),
            ("confirm", "cadvisor", True),
        ],
    )

    bootstrap.bootstrap("keycloak", "prod")

    assert paths.vars_path("keycloak", "prod", "keycloak").exists()
    assert (
        tree.load_vars("keycloak", "prod", "keycloak")["st_keycloak_cadvisor_enabled"]
        is True
    )
    body = (repo / "keycloak/prod/keycloak/vars.yml").read_text()
    assert "KC_DB_URL=jdbc:postgresql://db.example.org:5432/keycloak" in body
    assert "KC_HOSTNAME=idp.example.org" in body
    assert "KC_DB_PASSWORD={{ vault_kc_db_password }}" in body
    assert "KC_BOOTSTRAP_ADMIN_PASSWORD={{ vault_kc_bootstrap_admin_password }}" in body
    assert "KC_DB=" not in body  # baked into the image
    assert "st_keycloak_enabled" not in body  # enabled flag lives on the deploy task

    assert vault.is_encrypted(paths.vault_path("keycloak", "prod", "keycloak"))
    kvault = vault.decrypt_to_dict(paths.vault_path("keycloak", "prod", "keycloak"))
    assert kvault["vault_kc_db_password"] == "dbsecret"
    assert kvault["vault_kc_bootstrap_admin_password"] == "adminsecret"

    assert "10.0.0.9" in (repo / "keycloak/prod/keycloak/hosts").read_text()
    m = manifest.load_manifest()
    assert [u.component for u in m.units] == ["keycloak"]


def test_bootstrap_projects_writes_env_blob_and_vault(repo, monkeypatch):
    """`bootstrap projects prod` writes the st_projects_env blob with
    OIDC-enforced SSO defaults and {{ vault_* }} refs for every secret."""
    seed_creds(repo)
    sq = script_questionary(monkeypatch, projects_first_run_script())

    bootstrap.bootstrap("projects", "prod")

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert paths.vars_path("projects", "prod", "projects").exists()
    assert (
        tree.load_vars("projects", "prod", "projects")["st_projects_cadvisor_enabled"]
        is True
    )
    body = (repo / "projects/prod/projects/vars.yml").read_text()
    assert "BASE_URL=https://projects.example.org" in body
    assert "OIDC_ISSUER=https://idp.example.org/realms/st" in body
    assert "OIDC_ENFORCED=true" in body  # SSO only, no local accounts
    # keycloak keeps the generic OIDC defaults: the ProConnect-only overrides for
    # signed userinfo and per-claim scopes must not leak into a keycloak setup.
    assert "OIDC_SCOPES=openid email profile" in body
    assert "OIDC_FULLNAME_ATTRIBUTES=name" in body
    assert "OIDC_USERINFO_SIGNED_RESPONSE_ALG" not in body
    assert "S3_ENDPOINT=https://s3.fr-par.scw.cloud" in body
    assert "SECRET_KEY={{ vault_secret_key }}" in body
    assert "DATABASE_URL={{ vault_database_url }}" in body
    assert "OIDC_CLIENT_SECRET={{ vault_oidc_client_secret }}" in body
    assert "S3_SECRET_ACCESS_KEY={{ vault_s3_secret_access_key }}" in body
    assert "SMTP_HOST" not in body  # SMTP declined
    assert "REDIS_URL" not in body  # scaling declined, single-instance default
    assert "st_projects_enabled" not in body  # enabled flag lives on the deploy task

    assert vault.is_encrypted(paths.vault_path("projects", "prod", "projects"))
    pvault = vault.decrypt_to_dict(paths.vault_path("projects", "prod", "projects"))
    assert (
        pvault["vault_database_url"] == "postgresql://u:p@db.example.org:5432/projects"
    )
    assert pvault["vault_oidc_client_secret"] == "oidcsecret"
    assert pvault["vault_s3_secret_access_key"] == "s3secret"
    assert pvault["vault_secret_key"]  # generated

    assert "10.0.0.7" in (repo / "projects/prod/projects/hosts").read_text()
    m = manifest.load_manifest()
    assert [u.component for u in m.units] == ["projects"]
    assert m.units[0].mode == "managed"


def test_bootstrap_file_scanner_writes_env_blob_and_vault(repo, monkeypatch):
    """Full `bootstrap file-scanner prod` runs the file-scanner questionnaire (no
    DOMAIN/DB/S3/OIDC prompts): the env blob wires the bundled clamav/redis compose
    services, carries the caller public keys verbatim, refs the generated webhook
    signing seed + /metrics token as {{ vault_* }}, and skips ALLOWED_URL_HOSTS
    when left blank. The generated secrets land in vault.yml as 32 random bytes
    base64url (43 chars — a valid Ed25519 seed for JWT_SIGNING_KEY)."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "file-scanner host(s)", "10.0.0.20"),
            ("text", "JWT_ISSUER_KEYS", "transferts:pubkeyAAA"),
            ("text", "JWT_SIGNING_KID", "v1"),
            ("confirm", "PROMETHEUS_API_KEY", True),
            ("text", "ALLOWED_URL_HOSTS", ""),
            ("text", "SSRF_ALLOWED_HOSTS", ""),
            ("confirm", "cadvisor", True),
        ],
    )

    bootstrap.bootstrap("file-scanner", "prod")

    body = (repo / "file-scanner/prod/file-scanner/vars.yml").read_text()
    # dash-normalised cadvisor toggle (st_file-scanner_* would be an invalid var)
    assert "st_file_scanner_cadvisor_enabled" in body
    assert "st_file-scanner" not in body
    # fixed wiring to the in-compose clamav/redis services
    assert "CLAMAV_HOSTS=clamav:3310" in body
    assert "WORKER_BROKER_URL=redis://redis:6379/0" in body
    assert "JWT_ISSUER_KEYS=transferts:pubkeyAAA" in body
    assert "JWT_SIGNING_KEY={{ vault_jwt_signing_key }}" in body
    assert "JWT_SIGNING_KID=v1" in body
    assert "PROMETHEUS_API_KEY={{ vault_prometheus_api_key }}" in body
    # left blank → keys not emitted (the app keeps its no-allowlist defaults)
    assert "ALLOWED_URL_HOSTS" not in body
    assert "SSRF_ALLOWED_HOSTS" not in body

    assert vault.is_encrypted(paths.vault_path("file-scanner", "prod", "file-scanner"))
    fvault = vault.decrypt_to_dict(
        paths.vault_path("file-scanner", "prod", "file-scanner")
    )
    assert len(fvault["vault_jwt_signing_key"]) == 43  # token_urlsafe(32)
    assert len(fvault["vault_prometheus_api_key"]) == 43

    assert "10.0.0.20" in (repo / "file-scanner/prod/file-scanner/hosts").read_text()
    m = manifest.load_manifest()
    assert [u.component for u in m.units] == ["file-scanner"]
    assert m.units[0].mode == "managed"


def test_ask_file_scanner_allowlist_reuses_hosts_for_ssrf(repo, monkeypatch):
    """With ALLOWED_URL_HOSTS set, the SSRF question becomes a yes/no reusing the
    same list (no re-typing): answering yes emits both keys with one value.
    Declining the /metrics confirm leaves PROMETHEUS_API_KEY out entirely."""
    script_questionary(
        monkeypatch,
        [
            ("text", "JWT_ISSUER_KEYS", "transferts:pubkeyAAA"),
            ("text", "JWT_SIGNING_KID", "v1"),
            ("confirm", "PROMETHEUS_API_KEY", False),
            ("text", "ALLOWED_URL_HOSTS", "s3.fr-par.scw.cloud"),
            ("confirm", "private IPs", True),
        ],
    )
    meta = appmeta.load_app("file-scanner")
    answers = bootstrap._ask_file_scanner(meta, AnsibleVaultBackend())

    assert answers["SSRF_ALLOWED_HOSTS"] == "s3.fr-par.scw.cloud"
    body = envrender.render_env("file-scanner", "file-scanner", answers)[
        "st_file_scanner_env"
    ]
    assert "ALLOWED_URL_HOSTS=s3.fr-par.scw.cloud" in body
    assert "SSRF_ALLOWED_HOSTS=s3.fr-par.scw.cloud" in body
    assert "PROMETHEUS_API_KEY" not in body


def test_bootstrap_component_invalid_raises(repo, monkeypatch):
    """`bootstrap -c foo` raises StCliError mentioning the valid targets."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
        ],
    )
    with pytest.raises(StCliError, match="valid targets"):
        bootstrap.bootstrap("meet", "prod", component="foo")


def test_bootstrap_component_workers_not_implemented_raises(repo, monkeypatch):
    """`--component workers` on meet raises StCliError because workers is not
    implemented."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
        ],
    )
    with pytest.raises(StCliError, match="valid targets") as exc:
        bootstrap.bootstrap("meet", "prod", component="workers")
    assert "livekit" in str(exc.value)
    assert "meet" in str(exc.value)


def test_bootstrap_summary_mentions_secrets_for_ansible_vault(repo, monkeypatch, capfd):
    """The end-of-bootstrap summary mentions `st-cli secrets` and the
    .vault-pass reminder for an ansible-vault app and env."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "keycloak host(s)", "10.0.0.9"),
            ("text", "Public domain for keycloak", "idp.example.org"),
            ("text", "Database host", "db.example.org"),
            ("text", "Database port", "5432"),
            ("text", "Database name", "keycloak"),
            ("text", "Database user", "keycloak"),
            ("password", "KC_DB_PASSWORD", "dbsecret"),
            ("text", "Bootstrap admin username", "admin"),
            ("password", "KC_BOOTSTRAP_ADMIN_PASSWORD", "adminsecret"),
            ("confirm", "cadvisor", True),
        ],
    )
    bootstrap.bootstrap("keycloak", "prod")

    out = capfd.readouterr().out
    # The summary starts after "Bootstrapped <app>/<env>.". Strip the panel
    # side borders to rejoin the wrapped lines.
    summary = out.split("Bootstrapped keycloak/prod.", 1)[1]
    flat = " ".join(summary.replace("│", " ").split())
    assert "Next steps" in flat
    assert "st-cli secrets keycloak prod" in flat
    assert ".vault-pass" in flat
    assert "Back up and share" in flat


def test_bootstrap_summary_no_secrets_hint_for_hashi_vault(repo, monkeypatch, capfd):
    """The end-of-bootstrap summary omits `st-cli secrets` and .vault-pass for
    a hashi_vault app and env, because secrets live in OpenBao."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "hashi_vault (OpenBao)"),
            ("text", "OpenBao / Vault URL", "https://vault.example:8200"),
            ("confirm", "Skip TLS verification?", False),
            ("text", "keycloak host(s)", "10.0.0.9"),
            ("text", "Public domain for keycloak", "idp.example.org"),
            ("text", "Database host", "db.example.org"),
            ("text", "Database port", "5432"),
            ("text", "Database name", "keycloak"),
            ("text", "Database user", "keycloak"),
            (
                "text",
                "KC_DB_PASSWORD",
                "kv/data/keycloak:db_password",
            ),
            ("text", "Bootstrap admin username", "admin"),
            (
                "text",
                "KC_BOOTSTRAP_ADMIN_PASSWORD",
                "kv/data/keycloak:admin_password",
            ),
            ("confirm", "cadvisor", True),
        ],
    )
    bootstrap.bootstrap("keycloak", "prod")

    out = capfd.readouterr().out
    # The summary starts after "Bootstrapped <app>/<env>.". Strip the panel
    # side borders to rejoin the wrapped lines.
    summary = out.split("Bootstrapped keycloak/prod.", 1)[1]
    flat = " ".join(summary.replace("│", " ").split())
    assert "st-cli secrets" not in flat
    # hashi mode has no .vault-pass, so the backup/share step is absent too.
    assert ".vault-pass" not in flat


def test_bootstrap_messages_optional_deps_skippable(repo, monkeypatch):
    """Declining the optional `mpa` and `socks-proxy` deps registers no
    vars.yml or manifest unit for them; `mta-in` and the core still bootstrap."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        messages_first_run_script()
        + [
            ("select", "Bootstrap mta-in now?", "Yes — bootstrap now"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            # mta-in's shared rule is `generate: secret`, so no prompt for it;
            # the helper still prompts MYHOSTNAME for the mta-in env blob.
            ("text", "MYHOSTNAME", "mx.example.org"),
            ("confirm", "cadvisor", True),  # mta-in cadvisor
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
            ("select", "Bootstrap socks-proxy now?", "No — bootstrap later"),
        ],
    )

    bootstrap.bootstrap("messages", "prod")

    assert not paths.vars_path("messages", "prod", "mpa").exists()
    assert not paths.vars_path("messages", "prod", "socks-proxy").exists()

    assert paths.vars_path("messages", "prod", "mta-in").exists()
    assert paths.vars_path("messages", "prod", "messages").exists()

    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert "mta-in" in by_comp and by_comp["mta-in"].mode == "managed"
    assert "messages" in by_comp and by_comp["messages"].mode == "managed"
    assert "mpa" not in by_comp
    assert "socks-proxy" not in by_comp


def test_bootstrap_messages_provider_vars_deploy(repo, monkeypatch):
    """Deploying mta-in, mpa, and socks-proxy renders each provider's env blob
    and mirrors its secrets into every vault."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        messages_first_run_script()
        + [
            ("select", "Bootstrap mta-in now?", "Yes — bootstrap now"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            ("text", "MYHOSTNAME", "mx.example.org"),
            ("confirm", "cadvisor", True),  # mta-in cadvisor
            ("select", "Bootstrap mpa now?", "Yes — bootstrap now"),
            ("text", "mpa host(s)", "10.0.0.8"),
            (
                "confirm",
                "cadvisor",
                True,
            ),  # mpa cadvisor, secrets generated not prompted
            ("select", "Bootstrap socks-proxy now?", "Yes — bootstrap now"),
            ("text", "socks-proxy host(s)", "10.0.0.6"),
            ("text", "PROXY_EXTERNAL", "eth0"),
            ("text", "PROXY_INTERNAL_PORT", "50405"),
            ("confirm", "cadvisor", True),  # socks-proxy cadvisor
        ],
    )

    bootstrap.bootstrap("messages", "prod")

    mtain_env = tree.load_vars("messages", "prod", "mta-in")["st_messages_mta_in_env"]
    assert "MDA_API_SECRET={{ vault_mda_api_secret }}" in mtain_env
    assert "MDA_API_BASE_URL=https://messages.example.org/api/v1.0/" in mtain_env
    assert "MYHOSTNAME=mx.example.org" in mtain_env

    # MDA_API_SECRET mirrors into both mta-in's and messages' vaults with the same
    # value.
    mtain_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "mta-in"))
    msgs_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "messages"))
    assert "vault_mda_api_secret" in mtain_vault
    assert "vault_mda_api_secret" in msgs_vault
    assert mtain_vault["vault_mda_api_secret"] == msgs_vault["vault_mda_api_secret"]

    # mpa secrets follow the vault-ref split: vars.yml carries {{ vault_mpa_* }}
    # refs, and the real values live under vault_mpa_* in mpa's vault.yml.
    mpa_vars = tree.load_vars("messages", "prod", "mpa")
    assert mpa_vars["st_messages_mpa_auth_bearer"] == "{{ vault_mpa_auth_bearer }}"
    assert (
        mpa_vars["st_messages_mpa_rspamd_controller_password"]
        == "{{ vault_mpa_rspamd_controller_password }}"
    )
    mpa_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "mpa"))
    assert "vault_mpa_auth_bearer" in mpa_vault
    assert "vault_mpa_rspamd_controller_password" in mpa_vault

    sp_env = tree.load_vars("messages", "prod", "socks-proxy")[
        "st_messages_socks_proxy_env"
    ]
    assert "PROXY_EXTERNAL=eth0" in sp_env
    assert "PROXY_INTERNAL_PORT=50405" in sp_env
    assert "PROXY_USERS={{ vault_proxy_users }}" in sp_env
    sp_vault = vault.decrypt_to_dict(
        paths.vault_path("messages", "prod", "socks-proxy")
    )
    assert "vault_proxy_users" in sp_vault
    assert sp_vault["vault_proxy_users"].startswith("messages:")

    core_vars = (repo / "messages/prod/messages/vars.yml").read_text()
    assert (
        "MTA_OUT_DIRECT_PROXIES=socks5s://{{ vault_proxy_users }}@10.0.0.6:50405"
        in core_vars
    )
    assert "vault_proxy_users" in msgs_vault

    # a single mpa host needs no LB prompt: rspamd_url embeds the mpa host and the
    # role-default caddy port ref, and rspamd_auth embeds the mirrored bearer ref.
    assert (
        'SPAM_CONFIG={"rspamd_url": "http://10.0.0.8:{{ st_messages_mpa_caddy_port }}", '
        '"rspamd_auth": "Bearer {{ vault_mpa_auth_bearer }}", '
        '"inbound_auth": "rspamd"}' in core_vars
    )

    # the auth bearer mirrors into the messages vault under the same name, so the
    # SPAM_CONFIG ref resolves there and equals the value in the mpa vault.
    assert "vault_mpa_auth_bearer" in msgs_vault
    assert msgs_vault["vault_mpa_auth_bearer"] == mpa_vault["vault_mpa_auth_bearer"]


def test_bootstrap_messages_mta_in_standalone_prompts_mda_api_secret(repo, monkeypatch):
    """`-c mta-in` with no existing messages core vault prompts the operator
    for MDA_API_SECRET instead of leaking a literal placeholder into vars.yml."""
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            # standalone -c mta-in leaves answers["DOMAIN"] unset, so it prompts.
            ("text", "Public domain for messages", "messages.example.org"),
            ("text", "MYHOSTNAME", "mx.example.org"),
            # no core vault on disk, so MDA_API_SECRET is prompted, not leaked as a
            # placeholder.
            ("password", "MDA_API_SECRET", "shared-secret-from-core"),
            ("confirm", "cadvisor", True),  # mta-in cadvisor
        ],
    )

    bootstrap.bootstrap("messages", "prod", component="mta-in")

    # the env blob carries a real {{ vault_mda_api_secret }} ref, not the literal
    # placeholder a silent-skip regression would leave behind.
    mtain_env = tree.load_vars("messages", "prod", "mta-in")["st_messages_mta_in_env"]
    assert "MDA_API_SECRET={{ vault_mda_api_secret }}" in mtain_env
    assert "{MDA_API_SECRET}" not in mtain_env
    assert "MDA_API_BASE_URL=https://messages.example.org/api/v1.0/" in mtain_env
    assert "MYHOSTNAME=mx.example.org" in mtain_env

    assert vault.is_encrypted(paths.vault_path("messages", "prod", "mta-in"))
    mtain_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "mta-in"))
    assert mtain_vault["vault_mda_api_secret"] == "shared-secret-from-core"

    # a regression that skips the prompt would leave this script unconsumed.
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    assert not paths.vars_path("messages", "prod", "messages").exists()

    m = manifest.load_manifest()
    assert [u.component for u in m.units] == ["mta-in"]
    assert m.units[0].mode == "managed"


def test_bootstrap_messages_socks_proxy_hashi_vault_derives_mta_proxies(
    repo, monkeypatch
):
    """hashi_vault socks-proxy derives MTA_OUT_DIRECT_PROXIES from the
    PROXY_USERS lookup term instead of prompting for it."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "hashi_vault (OpenBao)"),
            ("text", "OpenBao / Vault URL", "https://vault.example:8200"),
            ("confirm", "Skip TLS verification?", False),
            # messages has workers, so the core hosts step adds an optional prompt.
            ("text", "messages host(s)", "10.0.0.4"),
            ("text", "workers (leave blank", ""),
            ("text", "Public domain for messages", "messages.example.org"),
            (
                "text",
                "DJANGO_SECRET_KEY",
                "@openbao(kv/data/messages:django_secret_key)",
            ),
            ("select", "Database configuration:", "DATABASE_URL"),
            (
                "text",
                "DATABASE_URL",
                "@openbao(kv/data/messages:database_url)",
            ),
            ("text", "REDIS_URL", "@openbao(kv/data/messages:redis_url)"),
            (
                "text",
                "MDA_API_SECRET",
                "@openbao(kv/data/messages:mda_api_secret)",
            ),
            (
                "text",
                "SALT_KEY",
                "@openbao(kv/data/messages:salt_key)",
            ),
            # messages-only S3: the imports bucket always prompts, blobs offload here
            # declines.
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", "msg-imports"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", "impkey"),
            (
                "text",
                "STORAGE_MESSAGE_IMPORTS_SECRET_KEY",
                "@openbao(kv/data/messages:imports_secret_key)",
            ),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ""),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
            ("confirm", "Enable blobs offloading", False),
            ("text", "OPENSEARCH_URL", "http://opensearch:9200"),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", "mail.example.org"),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "messages-client-id"),
            (
                "text",
                "OIDC_RP_CLIENT_SECRET",
                "@openbao(kv/data/messages:oidc_secret)",
            ),
            ("select", "Outbound mail mode", "direct"),
            ("confirm", "cadvisor", True),  # core cadvisor, last core question
            ("select", "Bootstrap mta-in now?", "Yes — bootstrap now"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            ("text", "MYHOSTNAME", "mx.example.org"),
            ("confirm", "cadvisor", True),  # mta-in cadvisor
            ("select", "Bootstrap mpa now?", "Yes — bootstrap now"),
            ("text", "mpa host(s)", "10.0.0.8"),
            (
                "text",
                "st_messages_mpa_auth_bearer",
                "@openbao(kv/data/messages:mpa_auth_bearer)",
            ),
            (
                "text",
                "st_messages_mpa_rspamd_controller_password",
                "@openbao(kv/data/messages:mpa_rspamd_controller_password)",
            ),
            ("confirm", "cadvisor", True),  # mpa cadvisor
            ("select", "Bootstrap socks-proxy now?", "Yes — bootstrap now"),
            ("text", "socks-proxy host(s)", "10.0.0.6"),
            ("text", "PROXY_EXTERNAL", "eth0"),
            ("text", "PROXY_INTERNAL_PORT", "50405"),
            # exactly one PROXY_USERS prompt fires; MTA_OUT_DIRECT_PROXIES is derived.
            ("text", "PROXY_USERS", "@openbao(kv/data/messages:proxy_users)"),
            ("confirm", "cadvisor", True),  # socks-proxy cadvisor
        ],
    )

    bootstrap.bootstrap("messages", "prod")

    sp_env = tree.load_vars("messages", "prod", "socks-proxy")[
        "st_messages_socks_proxy_env"
    ]
    assert "PROXY_EXTERNAL=eth0" in sp_env
    assert "PROXY_INTERNAL_PORT=50405" in sp_env
    assert (
        "PROXY_USERS={{ lookup('community.hashi_vault.hashi_vault', "
        "'kv/data/messages:proxy_users') }}" in sp_env
    )

    # hashi_vault is reference-only, so socks-proxy gets no vault.yml.
    assert not paths.vault_path("messages", "prod", "socks-proxy").exists()

    # the same PROXY_USERS lookup term is derived from answers, never re-prompted.
    core_vars = (repo / "messages/prod/messages/vars.yml").read_text()
    assert (
        "MTA_OUT_DIRECT_PROXIES=socks5s://{{ lookup('community.hashi_vault.hashi_vault', "
        "'kv/data/messages:proxy_users') }}@10.0.0.6:50405" in core_vars
    )

    # SPAM_CONFIG is constructed, never prompted: a single mpa host derives the
    # rspamd_url, and the auth bearer reuses the st_messages_mpa_auth_bearer lookup.
    assert (
        'SPAM_CONFIG={"rspamd_url": "http://10.0.0.8:{{ st_messages_mpa_caddy_port }}", '
        '"rspamd_auth": "Bearer {{ lookup(\'community.hashi_vault.hashi_vault\', '
        "'kv/data/messages:mpa_auth_bearer') }}\", "
        '"inbound_auth": "rspamd"}' in core_vars
    )


def test_bootstrap_messages_storage_blobs_offload(repo, monkeypatch):
    """Blobs offload enabled writes the offload bucket, MESSAGES_BLOBS_OFFLOAD_ENABLED
    and MESSAGES_BLOBS_ENCRYPT_KEYS, plus the three secrets to the vault."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        messages_first_run_script(blobs_offload=True)
        + [
            ("select", "Bootstrap mta-in now?", "Yes — bootstrap now"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            ("text", "MYHOSTNAME", "mx.example.org"),
            ("confirm", "cadvisor", True),  # mta-in cadvisor
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
            ("select", "Bootstrap socks-proxy now?", "No — bootstrap later"),
        ],
    )

    bootstrap.bootstrap("messages", "prod")

    core_vars = (repo / "messages/prod/messages/vars.yml").read_text()
    assert "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL=https://s3.example.org" in core_vars
    assert "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME=msg-imports" in core_vars
    assert "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY=impkey" in core_vars
    assert (
        "STORAGE_MESSAGE_IMPORTS_SECRET_KEY={{ vault_storage_message_imports_secret_key }}"
        in core_vars
    )
    assert "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY=3600" in core_vars
    assert "MESSAGES_BLOBS_OFFLOAD_ENABLED=1" in core_vars
    assert (
        'MESSAGES_BLOBS_ENCRYPT_KEYS={"1": {"algo": "aes-gcm", "secret": "{{ vault_messages_blobs_encrypt_key }}", "active": true}}'
        in core_vars
    )
    assert (
        "STORAGE_MESSAGE_BLOBS_SECRET_KEY={{ vault_storage_message_blobs_secret_key }}"
        in core_vars
    )
    assert "SALT_KEY={{ vault_salt_key }}" in core_vars
    # OPENSEARCH_URL is mandatory: it always renders into the messages backend env.
    assert "OPENSEARCH_URL=http://opensearch:9200" in core_vars
    assert "MESSAGES_TECHNICAL_DOMAIN=mail.example.org" in core_vars

    msgs_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "messages"))
    assert "vault_storage_message_imports_secret_key" in msgs_vault
    assert msgs_vault["vault_storage_message_imports_secret_key"] == "impsecret"
    assert "vault_storage_message_blobs_secret_key" in msgs_vault
    assert msgs_vault["vault_storage_message_blobs_secret_key"] == "blobsecret"
    assert "vault_messages_blobs_encrypt_key" in msgs_vault
    assert len(msgs_vault["vault_messages_blobs_encrypt_key"]) >= 32
    assert "vault_salt_key" in msgs_vault


def test_bootstrap_messages_relay_outbound_mode(repo, monkeypatch):
    """Relay outbound mode collects the SMTP smarthost + credentials and
    suppresses the socks-proxy dependency prompt entirely."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        messages_first_run_script(outbound="relay")
        + [
            ("select", "Bootstrap mta-in now?", "Yes — bootstrap now"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            ("text", "MYHOSTNAME", "mx.example.org"),
            ("confirm", "cadvisor", True),  # mta-in cadvisor
            ("select", "Bootstrap mpa now?", "Yes — bootstrap now"),
            ("text", "mpa host(s)", "10.0.0.8"),
            ("confirm", "cadvisor", True),  # mpa cadvisor, secrets are generated
            # relay mode skips the socks-proxy select; script_questionary errors
            # on any unscripted prompt, so a regression would fail loudly here.
        ],
    )

    bootstrap.bootstrap("messages", "prod")

    core_vars = (repo / "messages/prod/messages/vars.yml").read_text()
    assert "MTA_OUT_MODE=relay" in core_vars
    assert "MTA_OUT_RELAY_HOST=smtp.example.org:587" in core_vars
    assert "MTA_OUT_RELAY_USERNAME=relayuser" in core_vars
    assert "MTA_OUT_RELAY_PASSWORD={{ vault_mta_out_relay_password }}" in core_vars

    # relay mode suppresses socks-proxy: no unit vars, and no MTA_OUT_DIRECT_PROXIES,
    # since only the socks-proxy helper computes that var.
    assert not paths.vars_path("messages", "prod", "socks-proxy").exists()
    assert "MTA_OUT_DIRECT_PROXIES" not in core_vars

    msgs_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "messages"))
    assert "vault_mta_out_relay_password" in msgs_vault


def test_bootstrap_intro_guidance_for_core_not_provider(repo, monkeypatch, capfd):
    """Pre-questionnaire guidance (arch-docs URL + the app-tailored 'Requirements'
    checklist) is printed before the 'Bootstrapped' line for a full/core/workers
    run, and is ABSENT for a provider-only `-c <provider>` run. keycloak's list
    keeps PostgreSQL but drops the Redis/S3/ProConnect lines (it IS the IdP)."""
    seed_creds(repo)
    # keycloak full run: guidance appears before "Bootstrapped".
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "keycloak host(s)", "10.0.0.9"),
            ("text", "Public domain for keycloak", "idp.example.org"),
            ("text", "Database host", "db.example.org"),
            ("text", "Database port", "5432"),
            ("text", "Database name", "keycloak"),
            ("text", "Database user", "keycloak"),
            ("password", "KC_DB_PASSWORD", "dbsecret"),
            ("text", "Bootstrap admin username", "admin"),
            ("password", "KC_BOOTSTRAP_ADMIN_PASSWORD", "adminsecret"),
            ("confirm", "cadvisor", True),
        ],
    )
    bootstrap.bootstrap("keycloak", "prod")

    out = capfd.readouterr().out
    intro = out.split("Bootstrapped keycloak/prod.", 1)[0]
    flat = " ".join(intro.replace("│", " ").split())
    assert "st-ansible/tree/main/docs/02-keycloak" in flat
    assert "Requirements" in flat
    # The checklist is app-tailored (apps/<app>.yml `requirements`): keycloak
    # needs only a database — it IS the identity provider, and uses no Redis/S3 —
    # so the OIDC/ProConnect pointer and the Redis/S3 lines must NOT be shown here.
    assert "PostgreSQL" in flat
    assert "partenaires.proconnect.gouv.fr" not in flat
    assert "Redis" not in flat and "S3" not in flat

    # meet livekit provider run: guidance is absent.
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            # standalone -c livekit runs no _ask_core, so DOMAIN is unset and
            # _ensure_meet_domain prompts for it to build st_meet_public_host.
            *livekit_script(host="10.0.0.1", public_domain=True),
        ],
    )
    bootstrap.bootstrap("meet", "prod", component="livekit")

    out2 = capfd.readouterr().out
    flat2 = " ".join(out2.replace("│", " ").split())
    assert "Requirements" not in flat2
    assert "partenaires.proconnect.gouv.fr" not in flat2


def test_bootstrap_intro_requirements_tailored_for_file_scanner(
    repo, monkeypatch, capfd
):
    """An app with a manifest ``requirements:`` list gets a tailored checklist in
    the Requirements box: file-scanner shows IPs + caller public keys and NONE of
    the generic PostgreSQL/Redis/S3/ProConnect lines (its stack is self-contained)."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "file-scanner host(s)", "10.0.0.20"),
            ("text", "JWT_ISSUER_KEYS", "transferts:pubkeyAAA"),
            ("text", "JWT_SIGNING_KID", "v1"),
            ("confirm", "PROMETHEUS_API_KEY", True),
            ("text", "ALLOWED_URL_HOSTS", ""),
            ("text", "SSRF_ALLOWED_HOSTS", ""),
            ("confirm", "cadvisor", True),
        ],
    )
    bootstrap.bootstrap("file-scanner", "prod")

    out = capfd.readouterr().out
    intro = out.split("Bootstrapped file-scanner/prod.", 1)[0]
    flat = " ".join(intro.replace("│", " ").split())
    assert "Requirements" in flat
    assert "JWT_ISSUER_KEYS" in flat
    assert "PostgreSQL" not in flat
    assert "S3" not in flat
    assert "partenaires.proconnect.gouv.fr" not in flat


def test_confirm_ready_gate_aborts_on_decline_or_interrupt(monkeypatch):
    """Declining the readiness gate, or Ctrl+C/EOF, raises StCliError."""

    class _Q:
        def __init__(self, ans):
            self._ans = ans

        def ask(self):
            return self._ans

    # a yes answer does not raise.
    monkeypatch.setattr(prompts.questionary, "confirm", lambda *a, **k: _Q(True))
    prompts._confirm_ready("ready?")

    # a no answer aborts.
    monkeypatch.setattr(prompts.questionary, "confirm", lambda *a, **k: _Q(False))
    with pytest.raises(StCliError):
        prompts._confirm_ready("ready?")

    # Ctrl+C or EOF makes .ask() return None, which also aborts.
    monkeypatch.setattr(prompts.questionary, "confirm", lambda *a, **k: _Q(None))
    with pytest.raises(StCliError):
        prompts._confirm_ready("ready?")


def test_ask_core_sets_login_redirect_url_failure_for_non_drive(monkeypatch):
    """`_ask_core` sets LOGIN_REDIRECT_URL_FAILURE so the failed OIDC login
    redirects to https://<domain>/ instead of the literal string None."""
    script_questionary(
        monkeypatch,
        [
            ("text", "Public domain for meet", "meet.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://meet"),
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
        ],
    )
    meta = appmeta.load_app("meet")
    answers = bootstrap._ask_core(meta, AnsibleVaultBackend())

    # meet reassigns the redirect URLs to the st_meet_public_host ansible var, the
    # single source of truth, mirroring drive's st_drive_public_host override.
    # DJANGO_ALLOWED_HOSTS and the CSRF/CORS origins get the same treatment.
    assert answers["LOGIN_REDIRECT_URL"] == "https://{{ st_meet_public_host }}/"
    assert answers["LOGIN_REDIRECT_URL_FAILURE"] == "https://{{ st_meet_public_host }}/"
    assert answers["DJANGO_ALLOWED_HOSTS"] == "{{ st_meet_public_host }}"

    # the env template prints answers.SOMEKEY verbatim, so jinja2 does not
    # re-evaluate the {{ }} ref; ansible resolves it at deploy.
    body = envrender.render_env("meet", "meet", answers)["st_meet_backend_env"]
    assert "LOGIN_REDIRECT_URL=https://{{ st_meet_public_host }}/" in body
    assert "LOGIN_REDIRECT_URL_FAILURE=https://{{ st_meet_public_host }}/" in body
    assert "RECORDING_ENABLE=True" in body


def test_bootstrap_docs_full_deploys_yprovider(repo, monkeypatch):
    """Full `bootstrap docs prod` deploying yprovider on a single host generates
    and mirrors the shared secrets without re-prompting for them."""
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        docs_first_run_script(smtp=True, yprovider="Yes — bootstrap now")
        + [
            ("text", "yprovider host(s)", "10.0.0.9"),
            # no "Public domain for docs" prompt: _ensure_domain sees DOMAIN set.
            # no secret prompts: they mirror straight from the core's buffer.
            ("confirm", "cadvisor", True),  # yprovider cadvisor
        ],
    )

    bootstrap.bootstrap("docs", "prod")

    # caddy expands {$CADDY_YPROVIDER_ENDPOINTS} at parse time, so the endpoint
    # list lands in the caddy env blob, not as an ansible var.
    core_data = tree.load_vars("docs", "prod", "docs")
    assert core_data["st_docs_public_host"] == "docs.example.org"
    assert "st_docs_yprovider_endpoints" not in core_data
    assert (
        "CADDY_YPROVIDER_ENDPOINTS=10.0.0.9:50601" in (core_data["st_docs_caddy_env"])
    )

    core_vars = (repo / "docs/prod/docs/vars.yml").read_text()
    # the upstream docs Django package is named "impress", not "docs".
    assert "DJANGO_SETTINGS_MODULE=impress.settings" in core_vars
    assert "DJANGO_SETTINGS_MODULE=docs.settings" not in core_vars
    assert "DJANGO_ALLOWED_HOSTS={{ st_docs_public_host }}" in core_vars
    assert (
        'OIDC_REDIRECT_ALLOWED_HOSTS=["https://{{ st_docs_public_host }}"]' in core_vars
    )
    assert (
        "COLLABORATION_WS_URL=wss://{{ st_docs_public_host }}/collaboration/ws/"
        in core_vars
    )
    assert (
        "COLLABORATION_API_URL=https://{{ st_docs_public_host }}/collaboration/api/"
        in core_vars
    )
    assert (
        "COLLABORATION_SERVER_SECRET={{ vault_collaboration_server_secret }}"
        in core_vars
    )
    assert "Y_PROVIDER_API_KEY={{ vault_y_provider_api_key }}" in core_vars
    assert "Y_PROVIDER_API_BASE_URL=http://10.0.0.9:50601/api/" in core_vars
    assert "# Backend-only (the browser never calls it)" in core_vars
    assert "CONVERSION_UPLOAD_ENABLED=true" in core_vars
    assert "DOCSPEC_API_URL=http://docspec:4000/conversion" in core_vars
    assert (
        "DJANGO_EMAIL_LOGO_IMG=https://{{ st_docs_public_host }}"
        "/assets/logo-suite-numerique.png" in core_vars
    )
    assert "DJANGO_EMAIL_URL_APP=https://{{ st_docs_public_host }}" in core_vars

    core_vault = vault.decrypt_to_dict(paths.vault_path("docs", "prod", "docs"))
    assert "vault_collaboration_server_secret" in core_vault
    assert "vault_y_provider_api_key" in core_vault

    yp_env = tree.load_vars("docs", "prod", "yprovider")["st_docs_yprovider_env"]
    assert "COLLABORATION_SERVER_SECRET={{ vault_collaboration_server_secret }}" in (
        yp_env
    )
    assert "COLLABORATION_SERVER_ORIGIN=https://docs.example.org" in yp_env
    assert "COLLABORATION_BACKEND_BASE_URL=https://docs.example.org" in yp_env
    assert "Y_PROVIDER_API_KEY={{ vault_y_provider_api_key }}" in yp_env
    assert "COLLABORATION_LOGGING=true" in yp_env

    # the secrets mirrored into yprovider's own vault equal the core's values.
    yp_vault = vault.decrypt_to_dict(paths.vault_path("docs", "prod", "yprovider"))
    assert (
        yp_vault["vault_collaboration_server_secret"]
        == core_vault["vault_collaboration_server_secret"]
    )
    assert (
        yp_vault["vault_y_provider_api_key"] == core_vault["vault_y_provider_api_key"]
    )

    assert "10.0.0.9" in (repo / "docs/prod/yprovider/hosts").read_text()
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert by_comp["docs"].mode == "managed"
    assert by_comp["yprovider"].mode == "managed"


def test_bootstrap_docs_yprovider_standalone_prompts_secrets(repo, monkeypatch):
    """`bootstrap docs prod -c yprovider` with no existing docs core prompts
    DOMAIN and both core-owned secrets."""
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "yprovider host(s)", "10.0.0.9"),
            (
                "text",
                "Public domain for docs (for the collaboration server origin)",
                "docs.example.org",
            ),
            (
                "password",
                "COLLABORATION_SERVER_SECRET (shared with the docs core — must match it)",
                "shared-collab-secret",
            ),
            (
                "password",
                "Y_PROVIDER_API_KEY (shared with the docs core — must match it)",
                "shared-yprovider-key",
            ),
            ("confirm", "cadvisor", True),  # yprovider cadvisor
        ],
    )

    bootstrap.bootstrap("docs", "prod", component="yprovider")

    yp_env = tree.load_vars("docs", "prod", "yprovider")["st_docs_yprovider_env"]
    assert "COLLABORATION_SERVER_SECRET={{ vault_collaboration_server_secret }}" in (
        yp_env
    )
    assert "COLLABORATION_SERVER_ORIGIN=https://docs.example.org" in yp_env

    yp_vault = vault.decrypt_to_dict(paths.vault_path("docs", "prod", "yprovider"))
    assert yp_vault["vault_collaboration_server_secret"] == "shared-collab-secret"
    assert yp_vault["vault_y_provider_api_key"] == "shared-yprovider-key"

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    assert not paths.vars_path("docs", "prod", "docs").exists()
    m = manifest.load_manifest()
    assert [u.component for u in m.units] == ["yprovider"]


def test_bootstrap_docs_reuse_yprovider_adopts_kept_secrets(repo, monkeypatch):
    """Reusing an existing yprovider unit makes the docs core adopt its kept
    COLLABORATION_SERVER_SECRET and Y_PROVIDER_API_KEY instead of generating fresh ones.
    """
    seed_docs_yprovider_unit(repo)
    yp_vault_before = (repo / "docs/prod/yprovider/vault.yml").read_bytes()
    sq = script_questionary(
        monkeypatch,
        docs_first_run_script(
            secret_backend=False, yprovider="Reuse existing in the repo"
        ),
    )

    bootstrap.bootstrap("docs", "prod")

    core_vault = vault.decrypt_to_dict(paths.vault_path("docs", "prod", "docs"))
    assert core_vault["vault_collaboration_server_secret"] == "kept-collab-secret"
    assert core_vault["vault_y_provider_api_key"] == "kept-yprovider-key"

    # the computed core values rebuild from the kept unit's hosts file.
    core_data = tree.load_vars("docs", "prod", "docs")
    assert (
        "CADDY_YPROVIDER_ENDPOINTS=10.0.0.9:50601" in (core_data["st_docs_caddy_env"])
    )
    core_vars = (repo / "docs/prod/docs/vars.yml").read_text()
    assert "Y_PROVIDER_API_BASE_URL=http://10.0.0.9:50601/api/" in core_vars
    assert "# Backend-only (the browser never calls it)" in core_vars

    # the kept yprovider unit is untouched
    assert (repo / "docs/prod/yprovider/vault.yml").read_bytes() == yp_vault_before

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    m = manifest.load_manifest()
    by_comp = {u.component: u.mode for u in m.units}
    assert by_comp["yprovider"] == "managed"
    assert by_comp["docs"] == "managed"


def test_bootstrap_docs_yprovider_external_prompts_endpoints(repo, monkeypatch):
    """An external yprovider prompts for its endpoints, base URL, and secrets,
    which overwrite the values `_ask_core` generated."""
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        docs_first_run_script(yprovider="Already deployed (enter URL + keys)")
        + [
            (
                "text",
                "yprovider endpoints (host:port, comma-separated)",
                "10.0.0.9:50601, 10.0.0.10:50601",
            ),
            (
                "text",
                "Y_PROVIDER_API_BASE_URL (backend-only conversion API)",
                "http://yprovider.internal:50601/api/",
            ),
            ("password", "COLLABORATION_SERVER_SECRET", "ext-collab-secret"),
            ("password", "Y_PROVIDER_API_KEY", "ext-yprovider-key"),
        ],
    )

    bootstrap.bootstrap("docs", "prod")

    core_data = tree.load_vars("docs", "prod", "docs")
    assert (
        "CADDY_YPROVIDER_ENDPOINTS=10.0.0.9:50601 10.0.0.10:50601"
        in core_data["st_docs_caddy_env"]
    )

    core_vars = (repo / "docs/prod/docs/vars.yml").read_text()
    assert "Y_PROVIDER_API_BASE_URL=http://yprovider.internal:50601/api/" in core_vars

    core_vault = vault.decrypt_to_dict(paths.vault_path("docs", "prod", "docs"))
    assert core_vault["vault_collaboration_server_secret"] == "ext-collab-secret"
    assert core_vault["vault_y_provider_api_key"] == "ext-yprovider-key"

    assert not paths.vars_path("docs", "prod", "yprovider").exists()

    m = manifest.load_manifest()
    by_comp = {u.component: u.mode for u in m.units}
    assert by_comp["yprovider"] == "external"
    assert by_comp["docs"] == "managed"

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"


def test_bootstrap_docs_yprovider_skip_leaves_endpoints_empty(repo, monkeypatch):
    """Skipping yprovider sets no endpoint or secret, and the core caddy env
    renders CADDY_YPROVIDER_ENDPOINTS as empty."""
    seed_creds(repo)
    sq = script_questionary(monkeypatch, docs_first_run_script())

    bootstrap.bootstrap("docs", "prod")

    core_data = tree.load_vars("docs", "prod", "docs")
    assert "CADDY_YPROVIDER_ENDPOINTS=\n" in core_data["st_docs_caddy_env"]

    m = manifest.load_manifest()
    by_comp = {u.component: u.mode for u in m.units}
    assert "yprovider" not in by_comp
    assert by_comp["docs"] == "managed"

    assert not paths.vars_path("docs", "prod", "yprovider").exists()
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"


def test_docs_yprovider_endpoints_colocated_single_host():
    """core and yprovider on the same single host use the podman host alias
    host.containers.internal."""
    answers = {"_core_hosts": ["10.0.0.5"]}
    assert (
        bootstrap._docs_yprovider_endpoints(
            answers, "docs", "prod", "docs", ["10.0.0.5"]
        )
        == "host.containers.internal:50601"
    )


def test_docs_yprovider_endpoints_distinct_hosts():
    answers = {"_core_hosts": ["10.0.0.5"]}
    assert (
        bootstrap._docs_yprovider_endpoints(
            answers, "docs", "prod", "docs", ["10.0.0.9", "10.0.0.10"]
        )
        == "10.0.0.9:50601 10.0.0.10:50601"
    )


def test_docs_yprovider_endpoints_multi_colocated_keeps_real_hosts():
    """several co-located hosts keep the real IPs, so every caddy shares one
    identical list and routes a room to the same node."""
    answers = {"_core_hosts": ["10.0.0.5", "10.0.0.6"]}
    assert (
        bootstrap._docs_yprovider_endpoints(
            answers, "docs", "prod", "docs", ["10.0.0.5", "10.0.0.6"]
        )
        == "10.0.0.5:50601 10.0.0.6:50601"
    )


def test_docs_yprovider_endpoints_reads_core_hosts_from_disk(repo):
    """standalone and reuse runs carry no stash, so the core hosts file on disk
    decides."""
    tree.write_hosts("docs", "prod", "docs", "docs", ["10.0.0.5"])
    assert (
        bootstrap._docs_yprovider_endpoints({}, "docs", "prod", "docs", ["10.0.0.5"])
        == "host.containers.internal:50601"
    )


def test_docs_yprovider_endpoints_rejects_empty_hosts(repo):
    with pytest.raises(StCliError):
        bootstrap._docs_yprovider_endpoints({}, "docs", "prod", "docs", [])


# --------------------------------------------------------------------------- projects


def test_requirements_checklist_is_app_tailored(capsys, monkeypatch):
    """The pre-questionnaire Requirements panel renders the app's own
    ``requirements`` checklist (apps/<app>.yml): projects (a Sails app) must NOT
    be told to prepare a Redis or S3 as hard prerequisites — they only appear in
    its "Optionally:" scaling line — while the Django apps still list them."""
    monkeypatch.setattr(bootstrap, "_confirm_ready", lambda *a, **k: None)

    bootstrap._print_bootstrap_intro(appmeta.load_app("projects"))
    flat = " ".join(capsys.readouterr().out.replace("│", " ").split())
    assert "PostgreSQL" in flat and "Identity provider" in flat
    assert "Redis host and credentials" not in flat
    assert "S3 endpoint" not in flat
    assert "Optionally" in flat  # the scaling S3/Redis prep is opt-in only

    bootstrap._print_bootstrap_intro(appmeta.load_app("keycloak"))
    flat = " ".join(capsys.readouterr().out.replace("│", " ").split())
    assert "PostgreSQL" in flat
    assert "Redis" not in flat and "S3" not in flat

    bootstrap._print_bootstrap_intro(appmeta.load_app("meet"))
    assert "Redis" in capsys.readouterr().out


def test_bootstrap_projects_oidc_provider_switch_proconnect(repo, monkeypatch):
    """projects gets the same identity-provider switch as the Django apps, but derives a
    single OIDC_ISSUER without an extra URL prompt because the issuer is bundled.
    """
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        projects_first_run_script(
            database_url="postgresql://u:p@db/projects",
            oidc_provider="proconnect-integ",
            s3=False,
            cadvisor=False,
        ),
    )

    bootstrap.bootstrap("projects", "prod")

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    body = (repo / "projects/prod/projects/vars.yml").read_text()
    assert "OIDC_ISSUER=https://fca.integ01.dev-agentconnect.fr/api/v2" in body, body
    # the provider choice itself is questionnaire state, never an env key.
    assert "OIDC_PROVIDER" not in body
    assert "S3_" not in body
    # ProConnect returns the userinfo as a signed JWT and exposes
    # given_name/usual_name/email/siret via per-claim scopes, not the generic
    # profile scope and name claim. Without these, login loops back.
    assert "OIDC_SCOPES=openid given_name usual_name email siret" in body
    assert "OIDC_USERINFO_SIGNED_RESPONSE_ALG=RS256" in body
    assert "OIDC_FULLNAME_ATTRIBUTES=given_name,usual_name" in body


def test_bootstrap_projects_custom_oidc_org_mode_and_smtp(repo, monkeypatch):
    """Selecting the `custom` OIDC provider, org mode, and SMTP with a user
    exercises the questionnaire branches the happy-path tests skip."""
    seed_creds(repo)
    warns: list[str] = []
    monkeypatch.setattr(bootstrap.ui, "warn", warns.append)
    sq = script_questionary(
        monkeypatch,
        # a trailing slash on the custom issuer exercises the rstrip normalisation.
        projects_first_run_script(
            host="10.0.0.8",
            database_url="postgresql://u:p@db/projects",
            oidc_provider="custom",
            custom_issuer="https://sso.example.org/realms/lst/",
            org_claim="organization_id",
            s3=False,
            smtp=True,
            cadvisor=False,
        ),
    )

    bootstrap.bootstrap("projects", "prod")

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    body = (repo / "projects/prod/projects/vars.yml").read_text()
    # custom issuer: typed verbatim, trailing slash stripped
    assert "OIDC_ISSUER=https://sso.example.org/realms/lst\n" in body
    assert "ORGANIZATION_ID_CLAIM=organization_id" in body  # org mode
    assert "SMTP_HOST=smtp.example.org" in body
    assert "SMTP_PORT=587" in body
    assert "SMTP_SECURE=true" in body
    assert "SMTP_USER=mailer@example.org" in body
    # secret becomes a vault ref
    assert "SMTP_PASSWORD={{ vault_smtp_password }}" in body
    assert "SMTP_FROM=" in body
    assert "S3_" not in body  # S3 declined, local storage
    # declining S3 warns that local storage rules out multi-instance later
    assert any("S3" in w for w in warns), warns

    pvault = vault.decrypt_to_dict(paths.vault_path("projects", "prod", "projects"))
    assert pvault["vault_smtp_password"] == "smtppass"
    assert pvault["vault_oidc_client_secret"] == "oidcsecret"


def test_bootstrap_projects_scaling_redis_enforces_s3(repo, monkeypatch):
    """Accepting horizontal scaling routes REDIS_URL through the secret backend
    and makes the S3 questionnaire run unconditionally."""
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        # scaling skips the "Configure S3 object storage" opt-out confirm.
        # S3_REGION is blank here, unlike the builder's "fr-par" default.
        with_answers(
            projects_first_run_script(
                host="10.0.0.7, 10.0.0.8",
                database_url="postgresql://u:p@db/projects",
                scaling=True,
                cadvisor=False,
            ),
            {"S3_REGION": ""},
        ),
    )

    bootstrap.bootstrap("projects", "prod")

    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"
    body = (repo / "projects/prod/projects/vars.yml").read_text()
    assert "REDIS_URL={{ vault_redis_url }}" in body  # secret becomes a vault ref
    assert "S3_ENDPOINT=https://s3.fr-par.scw.cloud" in body  # enforced S3
    pvault = vault.decrypt_to_dict(paths.vault_path("projects", "prod", "projects"))
    assert pvault["vault_redis_url"] == "redis://:pw@redis.example.org:6379/0"
    assert pvault["vault_s3_secret_access_key"] == "s3secret"


def _render_caddy_parts(protocol: str, host: str, **names) -> tuple[str, str]:
    """Evaluate the two expressions like Ansible does, with its urlsplit filter."""
    from urllib.parse import urlsplit as _urlsplit

    import jinja2

    env = jinja2.Environment()
    env.filters["urlsplit"] = lambda url, part: getattr(_urlsplit(url), part)
    return (
        env.from_string(protocol).render(**names),
        env.from_string(host).render(**names),
    )


def test_caddy_s3_parts_plain_endpoint():
    assert bootstrap.caddy_s3_parts("https://s3.example.org:9000") == (
        "https",
        "s3.example.org:9000",
    )
    assert bootstrap.caddy_s3_parts("http://minio.local") == ("http", "minio.local")


def test_valid_s3_endpoint_requires_a_scheme_on_a_literal():
    assert bootstrap.valid_s3_endpoint("https://s3.example.org") is True
    assert bootstrap.valid_s3_endpoint("http://minio.local:9000") is True
    assert bootstrap.valid_s3_endpoint("{{ lookup('x', 'y') }}") is True
    assert "http://" in bootstrap.valid_s3_endpoint("s3.example.org")
    assert "http://" in bootstrap.valid_s3_endpoint("")


def test_caddy_s3_parts_lookup_endpoint_defers_the_split_to_ansible():
    endpoint = "https://{{ lookup('community.hashi_vault.hashi_vault', 'kv/data/drive:s3_host') }}"
    protocol, host = bootstrap.caddy_s3_parts(endpoint)
    assert "urlsplit('scheme')" in protocol
    assert "urlsplit('netloc')" in host
    assert "{{" not in host[2:]  # one expression, no nested braces

    rendered = _render_caddy_parts(
        protocol, host, lookup=lambda _plugin, _term: "minio.example.org:9000/"
    )
    assert rendered == ("https", "minio.example.org:9000")


def test_caddy_s3_parts_lookup_endpoint_with_scheme_in_the_secret():
    """The vault value feeds AWS_S3_ENDPOINT_URL, so it carries the scheme."""
    protocol, host = bootstrap.caddy_s3_parts("{{ s3_endpoint }}")
    assert (protocol, host) == (
        "{{ (s3_endpoint) | urlsplit('scheme') }}",
        "{{ (s3_endpoint) | urlsplit('netloc') }}",
    )
    rendered = _render_caddy_parts(
        protocol, host, s3_endpoint="http://minio.example.org:9000"
    )
    assert rendered == ("http", "minio.example.org:9000")


# --------------------------------------------------------------------------- transfers


def test_ask_core_transfers_overrides_settings_bucket_and_s3_origin(monkeypatch):
    """`_ask_core` for transfers diverges from the django-lasuite defaults in a few
    app-specific ways: the Django settings module is the French package name
    `transferts.settings` (not `transfers.settings`); the S3 endpoint origin is
    mirrored into TRANSFERTS_FRONTEND_S3_ORIGIN (the frontend Caddy CSP needs it) and
    USE_X_FORWARDED_FOR is enabled (transfers sits behind the frontend proxy). The
    optional DRIVE_BASE_URL is emitted only when answered, and the sender address is
    exposed as DEFAULT_FROM_EMAIL. The frontend Caddy env points at the backend
    service. Object storage uses the standard AWS_STORAGE_BUCKET_NAME (via base)."""
    script_questionary(
        monkeypatch,
        [
            ("text", "Public domain for transfers", "transfers.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://transfers"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            ("text", "AWS_S3_ENDPOINT_URL", "https://s3.fr-par.scw.cloud"),
            ("text", "AWS_S3_ACCESS_KEY_ID", "accesskey"),
            ("password", "AWS_S3_SECRET_ACCESS_KEY", "secretkey"),
            ("text", "AWS_STORAGE_BUCKET_NAME", "transfers-prod"),
            ("text", "AWS_S3_REGION_NAME (optional)", "fr-par"),
            ("text", "DRIVE_BASE_URL", "https://drive.example.org"),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "transfers-client"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            ("confirm", "Configure transactional email (SMTP) settings?", True),
            ("text", "DJANGO_EMAIL_HOST", "smtp.example.org"),
            ("text", "DJANGO_EMAIL_PORT", "587"),
            ("text", "DJANGO_EMAIL_HOST_USER (optional)", ""),
            ("password", "DJANGO_EMAIL_HOST_PASSWORD", "smtp-pass"),
            ("confirm", "DJANGO_EMAIL_USE_TLS?", True),
            ("confirm", "DJANGO_EMAIL_USE_SSL?", False),
            ("text", "DJANGO_EMAIL_FROM", "noreply@example.org"),
            ("text", "DJANGO_EMAIL_BRAND_NAME (optional)", ""),
            ("confirm", "file-scanner", False),
        ],
    )
    meta = appmeta.load_app("transfers")
    answers = bootstrap._ask_core(meta, AnsibleVaultBackend())

    # Django package is `transferts` (French spelling), not the st-cli app name.
    assert answers["DJANGO_SETTINGS_MODULE"] == "transferts.settings"
    # bucket uses the django-lasuite default AWS_STORAGE_BUCKET_NAME (via base)
    assert answers["AWS_STORAGE_BUCKET_NAME"] == "transfers-prod"
    assert answers["AWS_S3_SIGNATURE_VERSION"] == "s3v4"
    assert answers["TRANSFERTS_FRONTEND_S3_ORIGIN"] == "https://s3.fr-par.scw.cloud"
    assert answers["USE_X_FORWARDED_FOR"] == "true"
    assert answers["DRIVE_BASE_URL"] == "https://drive.example.org"

    backend = envrender.render_env("transfers", "transfers", answers)
    body = backend["st_transfers_backend_env"]
    assert "DJANGO_SETTINGS_MODULE=transferts.settings" in body
    assert "AWS_STORAGE_BUCKET_NAME=transfers-prod" in body
    assert "AWS_S3_SIGNATURE_VERSION=s3v4" in body
    assert "USE_X_FORWARDED_FOR=true" in body
    assert "DRIVE_BASE_URL=https://drive.example.org" in body
    # sender comes from the base DJANGO_EMAIL_FROM (transfers maps DEFAULT_FROM_EMAIL
    # onto that env var), so the overlay adds no DEFAULT_FROM_EMAIL of its own
    assert "DJANGO_EMAIL_FROM=noreply@example.org" in body
    assert "DEFAULT_FROM_EMAIL=" not in body

    frontend = backend["st_transfers_frontend_env"]
    assert "TRANSFERTS_FRONTEND_BACKEND_SERVER=transfers-backend:8000" in frontend
    assert "TRANSFERTS_FRONTEND_S3_ORIGIN=https://s3.fr-par.scw.cloud" in frontend


def test_ask_core_transfers_drive_url_optional(monkeypatch):
    """DRIVE_BASE_URL is optional: leaving it blank omits the key entirely, so the
    rendered backend env carries no DRIVE_BASE_URL line (integration stays off)."""
    script_questionary(
        monkeypatch,
        [
            ("text", "Public domain for transfers", "transfers.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://transfers"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            ("text", "AWS_S3_ENDPOINT_URL", "https://s3.fr-par.scw.cloud"),
            ("text", "AWS_S3_ACCESS_KEY_ID", "accesskey"),
            ("password", "AWS_S3_SECRET_ACCESS_KEY", "secretkey"),
            ("text", "AWS_STORAGE_BUCKET_NAME", "transfers-prod"),
            ("text", "AWS_S3_REGION_NAME (optional)", ""),
            ("text", "DRIVE_BASE_URL", ""),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "transfers-client"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            ("confirm", "Configure transactional email (SMTP) settings?", False),
            ("confirm", "file-scanner", False),
        ],
    )
    meta = appmeta.load_app("transfers")
    answers = bootstrap._ask_core(meta, AnsibleVaultBackend())
    assert "DRIVE_BASE_URL" not in answers
    # file-scanner declined → no CLAMAV/SCAN keys at all (app keeps its disabled default)
    assert "CLAMAV_SCAN_ENABLED" not in answers
    body = envrender.render_env("transfers", "transfers", answers)[
        "st_transfers_backend_env"
    ]
    assert "DRIVE_BASE_URL" not in body
    assert "CLAMAV" not in body and "SCAN_JWT" not in body


def test_ask_core_transfers_file_scanner_enabled(monkeypatch):
    """Accepting the file-scanner questionnaire enables the ClamAV integration: the
    CLAMAV_*/SCAN_* keys are set, the EdDSA signing key is routed through the secret
    backend (a vault ref, not the raw value), and the whole block renders into the
    backend env."""
    script_questionary(
        monkeypatch,
        [
            ("text", "Public domain for transfers", "transfers.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://transfers"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            ("text", "AWS_S3_ENDPOINT_URL", "https://s3.fr-par.scw.cloud"),
            ("text", "AWS_S3_ACCESS_KEY_ID", "accesskey"),
            ("password", "AWS_S3_SECRET_ACCESS_KEY", "secretkey"),
            ("text", "AWS_STORAGE_BUCKET_NAME", "transfers-prod"),
            ("text", "AWS_S3_REGION_NAME (optional)", ""),
            ("text", "DRIVE_BASE_URL", ""),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "transfers-client"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            ("confirm", "Configure transactional email (SMTP) settings?", False),
            ("confirm", "file-scanner", True),
            ("text", "CLAMAV_SERVICE_URL", "http://clamav_rest:8090"),
            ("text", "SCAN_WEBHOOK_BASE_URL", "http://transfers-backend:8000"),
            ("password", "SCAN_JWT_PRIVATE_KEY", "eddsa-private-key"),
            ("text", "SCAN_JWT_ISSUER", "transferts"),
            ("text", "SCAN_JWT_AUDIENCE", "file-scanner"),
            ("text", "SCAN_JWT_TTL", "300"),
            ("text", "SCAN_MAX_FILE_SIZE", "2147483648"),
            ("text", "SCAN_PRESIGNED_URL_EXPIRY", "3600"),
            ("text", "SCAN_PENDING_REAP_MINUTES", "15"),
        ],
    )
    meta = appmeta.load_app("transfers")
    answers = bootstrap._ask_core(meta, AnsibleVaultBackend())
    assert answers["CLAMAV_SCAN_ENABLED"] == "true"
    assert answers["CLAMAV_SERVICE_URL"] == "http://clamav_rest:8090"
    assert answers["SCAN_WEBHOOK_BASE_URL"] == "http://transfers-backend:8000"
    # the EdDSA key is a secret → a vault ref, not the raw value
    assert answers["SCAN_JWT_PRIVATE_KEY"].startswith("{{ vault")
    assert "eddsa-private-key" not in answers["SCAN_JWT_PRIVATE_KEY"]

    body = envrender.render_env("transfers", "transfers", answers)[
        "st_transfers_backend_env"
    ]
    assert "CLAMAV_SCAN_ENABLED=true" in body
    assert "CLAMAV_SERVICE_URL=http://clamav_rest:8090" in body
    assert "SCAN_WEBHOOK_BASE_URL=http://transfers-backend:8000" in body
    assert "SCAN_JWT_ISSUER=transferts" in body
    assert "SCAN_JWT_AUDIENCE=file-scanner" in body
    assert "SCAN_JWT_TTL=300" in body
    assert "SCAN_JWT_PRIVATE_KEY={{ vault_scan_jwt_private_key }}" in body
