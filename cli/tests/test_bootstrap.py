"""Tests for st_cli.cmd.bootstrap — host validation, the _ask prompt helpers, and
the `bootstrap APP ENV -c/--component COMP` staged-rollout path.

`-c COMP` scaffolds ONLY component COMP's unit, so a provider can be rolled out
before the core. No flag = the full interactive questionnaire.
"""

from __future__ import annotations

import pytest
from helpers import script_questionary, seed_creds, seed_livekit_provider

from st_cli.cmd import bootstrap
from st_cli.core import appmeta, envrender, manifest, paths, prompts, tree, vault
from st_cli.core.errors import StCliError
from st_cli.core.secretbackend import AnsibleVaultBackend

# --------------------------------------------------------------------------- host validation


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


# --------------------------------------------------------------------------- _ask / _text_question


def test_ask_placeholder_smoke(monkeypatch):
    """_ask(..., placeholder='hint') builds a questionary Question passing
    placeholder= (not default=) and returns the typed value (no real TTY)."""
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
    """default= renders as questionary's NATIVE editable pre-filled value (not a
    grey ghost): ``default`` IS in the questionary kwargs, ``placeholder`` is NOT,
    the required validator is applied, and pressing Enter (questionary returns the
    prefilled default) yields that default. A typed value wins."""
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

    # native prefill: default IS in kwargs, placeholder is NOT, validate is _require
    # (the required path is applied). Pressing Enter on a prefilled field returns
    # the default — simulated by having .ask() return the prefilled value.
    answer["v"] = "redis://redis:6379/0"
    assert bootstrap._ask("REDIS_URL", "redis://redis:6379/0") == "redis://redis:6379/0"
    assert captured["kwargs"]["default"] == "redis://redis:6379/0"
    assert "placeholder" not in captured["kwargs"]
    assert captured["kwargs"]["validate"] is prompts._require  # required path applied

    # a typed value overrides the default
    answer["v"] = "redis://other:6379/1"
    assert bootstrap._ask("REDIS_URL", "redis://redis:6379/0") == "redis://other:6379/1"


# --------------------------------------------------------------------------- bootstrap --component


def test_bootstrap_component_livekit_deploys_provider_only(repo, monkeypatch):
    """`bootstrap -c livekit` writes the livekit provider unit AND bundles the
    egress unit into the livekit step (egress hosts co-located on the livekit host →
    local valkey: ``st_meet_livekit_valkey_enabled=True`` and both redis addresses
    ``127.0.0.1:6379``); the core meet/vars.yml is NOT written and no meet unit is
    registered. The "Bootstrap livekit now?" select is NOT asked — the user
    explicitly asked to bootstrap that provider, so the deploy path is taken directly.
    Livekit's generated api key/secret are mirrored into egress's own vault (raw under
    the ``st_meet_livekit_api_key``/``st_meet_livekit_api_secret`` var names, reusing
    livekit's own names — the role reads them directly from vault.yml). Both livekit
    and egress register as managed."""
    seed_creds(repo)  # writes .vault-pass (skips the vault prompt)
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "livekit host(s)", "10.0.0.1"),
            # egress hosts (bundled into the livekit step) are now asked right after
            # the livekit hosts, BEFORE the LiveKit domain/TURN prompts: blank →
            # co-locate on 10.0.0.1
            ("text", "egress (leave blank", ""),
            (
                "text",
                "LiveKit domain (e.g. livekit.example.org)",
                "livekit.example.org",
            ),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", "turn.example.org"),
            # standalone `bootstrap -c livekit` → answers is empty, so
            # _ensure_meet_domain prompts DOMAIN for the livekit unit's vars.yml
            # (single source of truth — the role derives the webhook URL from it).
            (
                "text",
                "Public domain for meet (for the LiveKit recording webhook)",
                "meet.example.org",
            ),
            ("confirm", "livekit", True),  # livekit cadvisor
            ("confirm", "egress", True),  # egress cadvisor (single co-located → valkey)
        ],
    )

    bootstrap.bootstrap("meet", "prod", component="livekit")

    # livekit provider unit written
    assert paths.vars_path("meet", "prod", "livekit").exists()
    lv = tree.load_vars("meet", "prod", "livekit")
    assert lv["st_meet_livekit_domain"] == "livekit.example.org"
    assert lv["st_meet_livekit_turn_domain"] == "turn.example.org"
    # the livekit unit's st_meet_public_host is derived from DOMAIN in
    # apply_component_vars (single source of truth — the role derives the
    # LiveKit recording webhook URL from it, no CLI-side webhook URL var).
    assert lv["st_meet_public_host"] == "meet.example.org"
    assert lv["st_meet_cadvisor_enabled"] is True  # cadvisor prompt → real YAML bool
    # bundled redis topology (single co-located node → local valkey)
    assert lv["st_meet_livekit_valkey_enabled"] is True
    assert lv["st_meet_livekit_redis_address"] == "127.0.0.1:6379"
    assert vault.is_encrypted(paths.vault_path("meet", "prod", "livekit"))
    lvault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    assert "st_meet_livekit_api_key" in lvault
    assert "st_meet_livekit_api_secret" in lvault
    assert "10.0.0.1" in (repo / "meet/prod/livekit/hosts").read_text()

    # egress unit written (bundled) — co-located on the livekit host
    assert paths.vars_path("meet", "prod", "egress").exists()
    ev = tree.load_vars("meet", "prod", "egress")
    assert ev["st_meet_livekit_domain"] == "livekit.example.org"
    assert ev["st_meet_livekit_redis_address"] == "127.0.0.1:6379"
    assert ev["st_meet_cadvisor_enabled"] is True
    # co-located: egress hosts == livekit hosts
    assert "10.0.0.1" in (repo / "meet/prod/egress/hosts").read_text()
    # creds mirrored from livekit's vault into egress's vault (EQUAL to livekit's)
    assert vault.is_encrypted(paths.vault_path("meet", "prod", "egress"))
    evault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert evault["st_meet_livekit_api_key"] == lvault["st_meet_livekit_api_key"]
    assert evault["st_meet_livekit_api_secret"] == lvault["st_meet_livekit_api_secret"]

    # the "Bootstrap livekit now?" select was NOT asked (assume_deploy)
    assert not any("Bootstrap livekit now?" in msg for msg, _ in sq.select_calls)

    # core NOT written / NOT registered
    assert not paths.vars_path("meet", "prod", "meet").exists()
    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert "livekit" in by_comp and by_comp["livekit"].mode == "managed"
    assert "egress" in by_comp and by_comp["egress"].mode == "managed"
    assert "meet" not in by_comp


def test_bootstrap_livekit_external_redis(repo, monkeypatch):
    """`bootstrap meet prod` (full) with livekit and egress on DIFFERENT hosts: the
    single-co-located-node test fails (`sorted(livekit_hosts) != sorted(egress_hosts)`)
    so ``st_meet_livekit_valkey_enabled=False`` and the "Redis address shared by
    livekit and egress (host:port)" prompt fires, followed by the redis username +
    password prompts. Any non-empty string is accepted VERBATIM with NO host:port
    validation (per the topology rule), so a clearly non-host:port value
    (``whatever-redis``) is stored VERBATIM in BOTH the livekit unit's and the
    egress unit's ``st_meet_livekit_redis_address`` var (egress reuses the same
    livekit var) — guaranteeing they share one redis. Egress hosts are prompted explicitly
    (NOT blank) so the co-location short-circuit does not fire. The redis username is
    adopted verbatim by egress (plaintext); the password is mirrored (encrypted)
    into egress's own vault alongside the api key/secret."""
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "meet host(s)", "10.0.0.5"),
            # _ask_core for meet
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
            ("confirm", "cadvisor", True),  # meet core cadvisor
            # deps loop — livekit: deploy
            ("select", "Bootstrap livekit now?", "Yes — bootstrap now"),
            ("text", "livekit host(s)", "10.0.0.1"),
            # egress hosts asked right after livekit hosts, BEFORE LiveKit
            # domain/TURN; on a DIFFERENT host → co-location does not fire
            ("text", "egress (leave blank", "10.0.0.2"),
            (
                "text",
                "LiveKit domain (e.g. livekit.example.org)",
                "livekit.example.org",
            ),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", "turn.example.org"),
            # NO "Public domain for meet" — _ensure_meet_domain sees DOMAIN set
            ("confirm", "livekit", True),  # livekit cadvisor
            # the topology check fails → the "Redis address …" prompt fires (after
            # the livekit cadvisor confirm), followed by username + password; feed
            # a non-host:port string to pin the no-validation rule (stored verbatim).
            ("text", "Redis address shared by livekit and egress", "whatever-redis"),
            ("text", "Redis username shared by livekit and egress", "redisuser"),
            ("password", "Redis password shared by livekit and egress", "redispass123"),
            ("confirm", "egress", True),  # egress cadvisor
        ],
    )

    bootstrap.bootstrap("meet", "prod")

    # livekit: valkey DISABLED, the external redis address stored VERBATIM (no
    # host:port validation — a clearly non-host:port string is accepted as-is);
    # the redis username is stored in plaintext.
    lv = tree.load_vars("meet", "prod", "livekit")
    assert lv["st_meet_livekit_valkey_enabled"] is False
    assert lv["st_meet_livekit_redis_address"] == "whatever-redis"
    assert lv["st_meet_livekit_redis_username"] == "redisuser"
    # egress: adopts the SAME redis address + username (guaranteed to share one
    # redis with livekit)
    ev = tree.load_vars("meet", "prod", "egress")
    assert ev["st_meet_livekit_redis_address"] == "whatever-redis"
    assert ev["st_meet_livekit_redis_username"] == "redisuser"
    assert ev["st_meet_livekit_domain"] == "livekit.example.org"
    # egress NOT co-located: it has its own hosts file with 10.0.0.2
    assert "10.0.0.2" in (repo / "meet/prod/egress/hosts").read_text()
    assert "10.0.0.2" not in (repo / "meet/prod/livekit/hosts").read_text()

    # the redis password is mirrored (encrypted) into egress's own vault, equal
    # to the livekit-side value (livekit vault.yml holds it too).
    lvault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    evault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert lvault["st_meet_livekit_redis_password"] == "redispass123"
    assert (
        evault["st_meet_livekit_redis_password"]
        == lvault["st_meet_livekit_redis_password"]
    )

    # every scripted answer was consumed — including the "Redis address …"
    # prompt (the topology prompt fired; no leftover script means no extra prompt
    # and no missing prompt). Regression guard for both the prompt-fires path
    # and the no-validation rule (a non-host:port string was accepted as-is).
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    # meet + livekit + egress all registered as managed
    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert by_comp["meet"].mode == "managed"
    assert by_comp["livekit"].mode == "managed"
    assert by_comp["egress"].mode == "managed"


def test_bootstrap_component_livekit_external_redis_blank_auth(repo, monkeypatch):
    """`bootstrap -c livekit` with egress on a DIFFERENT host (so the co-location
    short-circuit does not fire, exactly like ``test_bootstrap_livekit_external_redis``)
    but the redis username AND password left BLANK — legal for an unauthenticated
    external redis (``_ask``/``_password`` are called with ``required=False`` for
    both). A blank username is dropped entirely: ``if username:`` in
    ``_bundle_egress`` is a truthiness check, so ``""`` never lands in either the
    livekit or the egress vars.yml. A blank password is dropped the same way: it
    is stored only when it is truthy, so an empty string is never written to
    ``backend.var_secret`` and never mirrored into egress's vault — an
    unauthenticated external redis carries no password key in either vault, only
    the mirrored api key/secret."""
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "livekit host(s)", "10.0.0.1"),
            # egress on a DIFFERENT host — co-location short-circuit does not
            # fire, so the external-redis address/username/password prompts fire.
            ("text", "egress (leave blank", "10.0.0.2"),
            (
                "text",
                "LiveKit domain (e.g. livekit.example.org)",
                "livekit.example.org",
            ),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", "turn.example.org"),
            (
                "text",
                "Public domain for meet (for the LiveKit recording webhook)",
                "meet.example.org",
            ),
            ("confirm", "livekit", True),  # livekit cadvisor
            # the topology check fails → the "Redis address …" prompt fires
            # (after the livekit cadvisor confirm), followed by a BLANK username
            # and a BLANK password — both legal for an unauthenticated redis.
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

    # livekit: valkey DISABLED, the external redis address stored; the blank
    # username omits the var entirely (truthiness check in _bundle_egress).
    lv = tree.load_vars("meet", "prod", "livekit")
    assert lv["st_meet_livekit_valkey_enabled"] is False
    assert lv["st_meet_livekit_redis_address"] == "redis.example.org:6379"
    assert "st_meet_livekit_redis_username" not in lv

    # egress: same blank-username omission (it reuses the same truthiness check
    # when building its own vars.yml).
    ev = tree.load_vars("meet", "prod", "egress")
    assert "st_meet_livekit_redis_username" not in ev

    # the blank password is dropped entirely: no password key in either vault,
    # only the mirrored api key/secret (both vaults still exist).
    lvault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    assert "st_meet_livekit_redis_password" not in lvault
    evault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert "st_meet_livekit_redis_password" not in evault
    assert evault["st_meet_livekit_api_key"] == lvault["st_meet_livekit_api_key"]
    assert evault["st_meet_livekit_api_secret"] == lvault["st_meet_livekit_api_secret"]

    # every scripted answer was consumed — including both blank prompts.
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"


def test_bootstrap_component_egress_standalone(repo, monkeypatch):
    """`bootstrap meet prod -c egress` with a livekit unit ALREADY on disk: egress
    ADOPTS livekit's already-decided domain + redis address (it does NOT re-prompt or
    re-decide topology — guaranteed to share the same redis the livekit unit was
    bootstrapped with). Livekit's api key/secret are mirrored from livekit's ON-DISK
    vault into egress's own vault (equal to livekit's). The "Bootstrap egress now?"
    select is NOT asked (assume_deploy — the user explicitly asked to bootstrap
    egress). ONLY egress is registered (livekit was already registered by the seed)."""
    seed_livekit_provider(repo)  # seeds livekit vars (incl. redis addr) + vault + hosts
    lk_vars_before = (repo / "meet/prod/livekit/vars.yml").read_text()
    lk_vault_before = (repo / "meet/prod/livekit/vault.yml").read_bytes()

    sq = script_questionary(
        monkeypatch,
        [
            # setup_backend reuses the persisted ansible-vault choice silently
            # (the seed registered a livekit unit) — no "Secret backend:" select.
            ("text", "egress host(s)", "10.0.0.3"),
            # egress db/db are never prompted — egress has no vars/env_render block
            # NO "Redis address" prompt (topology is ADOPTED, not re-decided)
            ("confirm", "cadvisor", True),  # egress cadvisor
        ],
    )

    bootstrap.bootstrap("meet", "prod", component="egress")

    # egress unit written, ADOPTING the on-disk livekit unit's domain + redis address
    assert paths.vars_path("meet", "prod", "egress").exists()
    ev = tree.load_vars("meet", "prod", "egress")
    assert ev["st_meet_livekit_domain"] == "livekit.example.org"
    assert ev["st_meet_livekit_redis_address"] == "livekit-redis.example:6379"
    assert ev["st_meet_cadvisor_enabled"] is True
    # egress hosts written (not adopted — the deploy tail writes them from _ask_hosts)
    assert "10.0.0.3" in (repo / "meet/prod/egress/hosts").read_text()

    # creds mirrored from livekit's ON-DISK vault into egress's own vault
    lvault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    evault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert evault["st_meet_livekit_api_key"] == lvault["st_meet_livekit_api_key"]
    assert evault["st_meet_livekit_api_secret"] == lvault["st_meet_livekit_api_secret"]
    assert evault["st_meet_livekit_api_key"] == "real-token"
    assert evault["st_meet_livekit_api_secret"] == "real-secret"
    # external redis (valkey disabled) → the redis password is mirrored too
    assert evault["st_meet_livekit_redis_password"] == "real-redis-pass"

    # the "Bootstrap egress now?" select was NOT asked (assume_deploy)
    assert not any("Bootstrap egress now?" in msg for msg, _ in sq.select_calls)
    # no "Redis address …" prompt (topology adopted, not re-decided)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    # livekit unit/files NOT modified by the standalone egress run
    assert (repo / "meet/prod/livekit/vars.yml").read_text() == lk_vars_before
    assert (repo / "meet/prod/livekit/vault.yml").read_bytes() == lk_vault_before

    # manifest: the livekit unit was ALREADY registered by the seed; egress is added.
    # ONLY egress is registered by THIS bootstrap call (livekit's unit is preserved).
    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert "egress" in by_comp and by_comp["egress"].mode == "managed"
    assert "livekit" in by_comp and by_comp["livekit"].mode == "managed"
    # the core was NOT bootstrapped in a standalone -c egress run
    assert "meet" not in by_comp


def test_bootstrap_component_core_wires_deps_only(repo, monkeypatch):
    """`bootstrap -c meet` (wire-only) after a livekit tree exists: writes the
    core vars/vault/hosts with LIVEKIT_* refs pulled from livekit's vault (reuse);
    the livekit unit/files are NOT modified; no "Yes — bootstrap now" path taken."""
    seed_livekit_provider(repo)
    livekit_vars_before = (repo / "meet/prod/livekit/vars.yml").read_text()
    livekit_vault_before = (repo / "meet/prod/livekit/vault.yml").read_bytes()

    sq = script_questionary(
        monkeypatch,
        [
            # core hosts (asked before _ask_core), then the core questionnaire.
            # NB: no "Secret backend:" select here — a livekit unit was seeded by
            # seed_livekit_provider, so setup_backend reuses the persisted
            # ansible-vault choice silently (no prompt).
            ("text", "meet host(s)", "10.0.0.5"),
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
            ("confirm", "cadvisor", True),
            # dep: wire-only + existing livekit — reused automatically, no select.
        ],
    )
    bootstrap.bootstrap("meet", "prod", component="meet")

    # core written with LIVEKIT refs (reuse from livekit's vault)
    assert paths.vars_path("meet", "prod", "meet").exists()
    assert tree.load_vars("meet", "prod", "meet")["st_meet_cadvisor_enabled"] is True
    core_vars = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "LIVEKIT_API_KEY={{ vault_livekit_api_key }}" in core_vars
    assert "LIVEKIT_API_SECRET={{ vault_livekit_api_secret }}" in core_vars
    assert "LIVEKIT_API_URL=wss://livekit.example.org" in core_vars
    # the real values pulled from livekit's vault land in the core's vault.yml
    assert vault.is_encrypted(paths.vault_path("meet", "prod", "meet"))
    cvault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "meet"))
    assert cvault["vault_livekit_api_key"] == "real-token"
    assert cvault["vault_livekit_api_secret"] == "real-secret"
    assert "REDIS_URL={{ vault_redis_url }}" in core_vars
    assert "CELERY_BROKER_URL={{ vault_redis_url }}" in core_vars
    assert cvault["vault_redis_url"] == "redis://redis:6379/0"

    # recording is always on now (no confirm) → RECORDING_* lines are always
    # present in the meet backend env
    assert "RECORDING_ENABLE=True" in core_vars

    # livekit unit/files NOT modified
    assert (repo / "meet/prod/livekit/vars.yml").read_text() == livekit_vars_before
    assert (repo / "meet/prod/livekit/vault.yml").read_bytes() == livekit_vault_before

    # wire-only + existing livekit: reused automatically, no dep select at all
    dep_offers = [c for msg, c in sq.select_calls if "Bootstrap livekit now?" in msg]
    assert not dep_offers, "wire-only must reuse an existing provider without a select"

    # both units registered; livekit mode unchanged (managed)
    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert "meet" in by_comp and by_comp["meet"].mode == "managed"
    assert "livekit" in by_comp and by_comp["livekit"].mode == "managed"


def test_bootstrap_meet_full_reuse_livekit_bundles_egress(repo, monkeypatch):
    """Full `bootstrap meet prod` (no -c) where livekit ALREADY exists and the
    operator picks "Reuse existing": the reused livekit keeps egress in the
    deployment. Egress has no tree yet, so `_reuse_egress` creates it from
    livekit's on-disk redis topology (external redis: valkey disabled), co-located
    on the livekit hosts, and meet + livekit + egress all register as managed."""
    seed_livekit_provider(repo)
    sq = script_questionary(
        monkeypatch,
        [
            ("text", "meet host(s)", "10.0.0.5"),
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
            ("confirm", "cadvisor", True),  # meet core cadvisor
            # deps loop — livekit exists: reuse it (still deployed)
            ("select", "Bootstrap livekit now?", "Reuse existing in the repo"),
            ("confirm", "egress", True),  # egress cadvisor (created on reuse)
        ],
    )

    bootstrap.bootstrap("meet", "prod")

    # egress unit created on the reuse path, adopting livekit's on-disk topology
    assert paths.vars_path("meet", "prod", "egress").exists()
    ev = tree.load_vars("meet", "prod", "egress")
    assert ev["st_meet_livekit_domain"] == "livekit.example.org"
    assert ev["st_meet_livekit_redis_address"] == "livekit-redis.example:6379"
    assert ev["st_meet_cadvisor_enabled"] is True
    # co-located on the livekit hosts (no egress-hosts prompt on the reuse path)
    assert "10.0.0.1" in (repo / "meet/prod/egress/hosts").read_text()
    # livekit's creds mirrored (incl. the external-redis password) into egress vault
    evault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert evault["st_meet_livekit_api_key"] == "real-token"
    assert evault["st_meet_livekit_api_secret"] == "real-secret"
    assert evault["st_meet_livekit_redis_password"] == "real-redis-pass"
    # no unconsumed scripts (reuse path prompts nothing beyond egress cadvisor)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    # meet + livekit + egress all registered as managed
    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert by_comp["meet"].mode == "managed"
    assert by_comp["livekit"].mode == "managed"
    assert by_comp["egress"].mode == "managed"


def test_bootstrap_meet_full_deploys_livekit_with_public_host(repo, monkeypatch):
    """Full `bootstrap meet prod` (no -c): `_ask_core` collects DOMAIN BEFORE the
    deps loop, so the livekit dep's deploy path runs `_ensure_meet_domain`, sees
    answers["DOMAIN"] already set, and does NOT re-prompt — ScriptedQuestionary
    would error on any unscripted "Public domain for meet" prompt. The livekit
    dep's deploy path BUNDLES egress (egress hosts co-located on the livekit host →
    local valkey: ``st_meet_livekit_valkey_enabled=True`` and both redis addresses
    ``127.0.0.1:6379``); livekit's generated api creds are mirrored into egress's
    own vault. Only the livekit vars.yml (and the meet core env blob) carry
    `st_meet_public_host == "meet.example.org"` (single source of truth — the role
    derives every public-facing URL, incl. the LiveKit recording webhook, from it) —
    egress has no `vars` block of its own, so its vars.yml carries no such key. The
    core backend env blob carries the verbatim ``{{ st_meet_public_host }}`` ref for
    DJANGO_ALLOWED_HOSTS / LOGIN_REDIRECT_URL, recording is always on (RECORDING_*
    env is always emitted, never prompted), and meet + livekit + egress all
    register as managed."""
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "meet host(s)", "10.0.0.5"),
            # _ask_core for meet
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
            ("confirm", "cadvisor", True),  # meet core cadvisor
            # deps loop — livekit (no existing tree): take the deploy path
            ("select", "Bootstrap livekit now?", "Yes — bootstrap now"),
            ("text", "livekit host(s)", "10.0.0.1"),
            # egress hosts (bundled into the livekit step) asked right after the
            # livekit hosts, BEFORE LiveKit domain/TURN: blank → co-locate
            ("text", "egress (leave blank", ""),
            (
                "text",
                "LiveKit domain (e.g. livekit.example.org)",
                "livekit.example.org",
            ),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", "turn.example.org"),
            # NO "Public domain for meet" — _ensure_meet_domain sees DOMAIN set
            ("confirm", "livekit", True),  # livekit cadvisor
            ("confirm", "egress", True),  # egress cadvisor (single co-located → valkey)
        ],
    )

    bootstrap.bootstrap("meet", "prod")

    # livekit vars.yml: st_meet_public_host derived from DOMAIN (no extra prompt)
    lv = tree.load_vars("meet", "prod", "livekit")
    assert lv["st_meet_livekit_domain"] == "livekit.example.org"
    assert lv["st_meet_livekit_turn_domain"] == "turn.example.org"
    assert lv["st_meet_public_host"] == "meet.example.org"
    assert lv["st_meet_cadvisor_enabled"] is True
    # bundled redis topology (single co-located node → local valkey)
    assert lv["st_meet_livekit_valkey_enabled"] is True
    assert lv["st_meet_livekit_redis_address"] == "127.0.0.1:6379"

    # egress vars.yml + vault.yml written (bundled into the livekit step): egress
    # adopts the livekit ws domain + the local valkey address (single co-located
    # node); livekit's generated api creds are mirrored into egress's own vault.
    assert paths.vars_path("meet", "prod", "egress").exists()
    ev = tree.load_vars("meet", "prod", "egress")
    assert ev["st_meet_livekit_domain"] == "livekit.example.org"
    assert ev["st_meet_livekit_redis_address"] == "127.0.0.1:6379"
    assert ev["st_meet_cadvisor_enabled"] is True
    lvault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "livekit"))
    evault = vault.decrypt_to_dict(paths.vault_path("meet", "prod", "egress"))
    assert evault["st_meet_livekit_api_key"] == lvault["st_meet_livekit_api_key"]
    assert evault["st_meet_livekit_api_secret"] == lvault["st_meet_livekit_api_secret"]

    # meet core: single source of truth (st_meet_public_host — written into the
    # core vars.yml from DOMAIN) + LIVEKIT_* refs pulled from the livekit secrets;
    # recording is unconditionally enabled (no prompt — see _set_meet_recording).
    # The env blob carries the verbatim {{ st_meet_public_host }} ref (the {{ }}
    # travels through the answer value; ANSIBLE resolves it at deploy).
    core_data = tree.load_vars("meet", "prod", "meet")
    assert core_data["st_meet_public_host"] == "meet.example.org"
    core_vars = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "LIVEKIT_API_KEY={{ vault_livekit_api_key }}" in core_vars
    assert "LIVEKIT_API_URL=wss://livekit.example.org" in core_vars
    assert "DJANGO_ALLOWED_HOSTS={{ st_meet_public_host }}" in core_vars
    assert "LOGIN_REDIRECT_URL=https://{{ st_meet_public_host }}/" in core_vars
    assert "RECORDING_ENABLE=True" in core_vars

    # the _ensure_meet_domain prompt did NOT fire (DOMAIN already set by _ask_core)
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    # meet + livekit + egress all registered as managed
    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert by_comp["meet"].mode == "managed"
    assert by_comp["livekit"].mode == "managed"
    assert by_comp["egress"].mode == "managed"


def test_ask_core_meet_always_sets_recording_env(monkeypatch):
    """`_ask_core` for meet no longer asks anything about recording — it is always
    on. This test's script contains NOTHING recording-related (no confirm, no
    RECORDING_OUTPUT_FOLDER text prompt): if a recording prompt is ever
    reintroduced, ``ScriptedQuestionary`` raises AssertionError on the unexpected
    prompt and this test fails. That makes it the regression test for the whole
    "remove both recording prompts" change.

    The answers still carry RECORDING_ENABLE=True, RECORDING_OUTPUT_FOLDER=
    recordings (now hardcoded, not asked) and RECORDING_DOWNLOAD_BASE_URL pointing
    at the st_meet_public_host ansible var (single source of truth — same var
    DJANGO_ALLOWED_HOSTS / the redirects reference; the {{ }} travels verbatim
    through the answer into the env blob). The rendered meet backend env contains
    the full RECORDING_* block — incl. RECORDING_STORAGE_EVENT_ENABLE=False
    (pinning the LiveKit webhook completion path). S3 is reused from the existing
    _ask_core prompts."""
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
    # the download base URL references the st_meet_public_host ansible var so the
    # operator changes the domain in ONE place ({{ }} lands verbatim in the env blob).
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


def test_bootstrap_keycloak_writes_env_blob_and_vault(repo, monkeypatch):
    """Full `bootstrap keycloak prod` runs the keycloak-specific questionnaire
    (no DOMAIN/Redis/S3/OIDC prompts): it writes the st_keycloak_env blob with
    {{ vault_* }} refs for the two passwords, encrypts them into vault.yml,
    writes the hosts, and registers the keycloak unit."""
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
    assert m.units[0].mode == "managed"


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
    """`--component workers` on meet (workers not implemented) → StCliError
    listing the valid targets (workers is excluded from the set)."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
        ],
    )
    with pytest.raises(StCliError, match="valid targets") as exc:
        bootstrap.bootstrap("meet", "prod", component="workers")
    # workers is not a valid target for meet (not implemented)
    assert "livekit" in str(exc.value)
    assert "meet" in str(exc.value)


# --------------------------------------------------------------------------- post-run secrets hint


def test_bootstrap_summary_mentions_secrets_for_ansible_vault(repo, monkeypatch, capfd):
    """The end-of-bootstrap summary mentions `st-cli secrets` for an
    ansible-vault (app, env) — both the back-up/share reminder and the
    `st-cli secrets <app> <env>` hint. The intro ui.note is NOT part of the
    summary, so we split the output at the "Bootstrapped" success line."""
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
    # Isolate the _print_summary output (starts at "Bootstrapped <app>/<env>").
    # Strip the "Next steps" panel's │ side-borders so wrapped lines rejoin.
    summary = out.split("Bootstrapped keycloak/prod.", 1)[1]
    flat = " ".join(summary.replace("│", " ").split())
    assert "Next steps" in flat
    assert "st-cli secrets keycloak prod" in flat
    assert ".vault-pass" in flat
    assert "Back up and share" in flat


def test_bootstrap_summary_no_secrets_hint_for_hashi_vault(repo, monkeypatch, capfd):
    """The end-of-bootstrap summary does NOT mention `st-cli secrets` for a
    hashi_vault (app, env) — secrets live in OpenBao, not a local vault.yml.
    The intro ui.note (which mentions `st-cli secrets` for all backends) is
    NOT part of the summary, so we split at the "Bootstrapped" success line."""
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
    # Isolate the _print_summary output (starts at "Bootstrapped <app>/<env>").
    # Strip the "Next steps" panel's │ side-borders so wrapped lines rejoin.
    summary = out.split("Bootstrapped keycloak/prod.", 1)[1]
    flat = " ".join(summary.replace("│", " ").split())
    assert "st-cli secrets" not in flat
    # the .vault-pass backup/share step is also absent (no .vault-pass in hashi mode)
    assert ".vault-pass" not in flat


# --------------------------------------------------------------------------- optional deps (messages)


def test_bootstrap_messages_optional_deps_skippable(repo, monkeypatch):
    """A full `bootstrap messages prod` lets the operator SKIP the optional
    `mpa` and `socks-proxy` deps (answer No to the pre-gate confirm). Neither a
    vars.yml nor a manifest unit is recorded for them; the required `mta-in` dep
    and the `messages` core are bootstrapped as usual. The operator can add the
    skipped deps later via `st-cli bootstrap -c <dep>`."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            # core hosts, then the optional worker-hosts prompt (messages has workers)
            ("text", "messages host(s)", "10.0.0.4"),
            ("text", "workers (leave blank", ""),  # co-locate workers on the core hosts
            # core questionnaire (_ask_core)
            ("text", "Public domain for messages", "messages.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://messages"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            # messages does NOT use the generic AWS_S3_* storage — no S3 prompts here.
            # messages-only S3: imports bucket (always prompted) + blobs offload (declined)
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", "msg-imports"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", "impkey"),
            ("password", "STORAGE_MESSAGE_IMPORTS_SECRET_KEY", "impsecret"),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ""),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
            ("confirm", "Enable blobs offloading", False),
            ("text", "OPENSEARCH_URL", "http://opensearch:9200"),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", "mail.example.org"),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "messages-client-id"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            # no SMTP confirm — messages is NOT in _EMAIL_APPS
            ("select", "Outbound mail mode", "direct"),
            ("confirm", "cadvisor", True),  # core cadvisor (last core question)
            # deps loop — mta-in (required): take the deploy path
            ("select", "Bootstrap mta-in now?", "Yes — bootstrap now"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            # mta-in shared rule is `generate: secret` → auto-generated, no prompt;
            # the helper now prompts MYHOSTNAME for the mta-in env blob
            ("text", "MYHOSTNAME", "mx.example.org"),
            ("confirm", "cadvisor", True),  # mta-in cadvisor
            # deps loop — mpa (optional): skip
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
            # deps loop — socks-proxy (optional): skip
            ("select", "Bootstrap socks-proxy now?", "No — bootstrap later"),
        ],
    )

    bootstrap.bootstrap("messages", "prod")

    # optional deps skipped → no vars.yml written for them
    assert not paths.vars_path("messages", "prod", "mpa").exists()
    assert not paths.vars_path("messages", "prod", "socks-proxy").exists()

    # required dep + core bootstrapped
    assert paths.vars_path("messages", "prod", "mta-in").exists()
    assert paths.vars_path("messages", "prod", "messages").exists()

    # manifest: mta-in + messages registered; mpa / socks-proxy NOT registered
    m = manifest.load_manifest()
    by_comp = {u.component: u for u in m.units}
    assert "mta-in" in by_comp and by_comp["mta-in"].mode == "managed"
    assert "messages" in by_comp and by_comp["messages"].mode == "managed"
    assert "mpa" not in by_comp
    assert "socks-proxy" not in by_comp


def test_bootstrap_messages_provider_vars_deploy(repo, monkeypatch):
    """Full `bootstrap messages prod` deploying mta-in + mpa + socks-proxy: each
    provider's st_messages_<comp>_env blob is rendered from the helper-collected
    answers, the shared MDA_API_SECRET is mirrored into both mta-in's and messages'
    vaults, mpa's rspamd controller password is generated, and the computed
    MTA_OUT_DIRECT_PROXIES consumer value (with its vault_proxy_users ref) lands
    in the messages backend env."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            # core hosts, then optional worker-hosts prompt (messages has workers)
            ("text", "messages host(s)", "10.0.0.4"),
            ("text", "workers (leave blank", ""),  # co-locate workers on the core hosts
            # core questionnaire (_ask_core)
            ("text", "Public domain for messages", "messages.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://messages"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            # messages does NOT use the generic AWS_S3_* storage — no S3 prompts here.
            # messages-only S3: imports bucket (always prompted) + blobs offload (declined)
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", "msg-imports"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", "impkey"),
            ("password", "STORAGE_MESSAGE_IMPORTS_SECRET_KEY", "impsecret"),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ""),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
            ("confirm", "Enable blobs offloading", False),
            ("text", "OPENSEARCH_URL", "http://opensearch:9200"),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", "mail.example.org"),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "messages-client-id"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            # no SMTP confirm — messages is NOT in _EMAIL_APPS
            ("select", "Outbound mail mode", "direct"),
            ("confirm", "cadvisor", True),  # core cadvisor (last core question)
            # deps loop — mta-in (required): deploy
            ("select", "Bootstrap mta-in now?", "Yes — bootstrap now"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            ("text", "MYHOSTNAME", "mx.example.org"),
            ("confirm", "cadvisor", True),  # mta-in cadvisor
            # deps loop — mpa (optional): deploy
            ("select", "Bootstrap mpa now?", "Yes — bootstrap now"),
            ("text", "mpa host(s)", "10.0.0.8"),
            (
                "confirm",
                "cadvisor",
                True,
            ),  # mpa cadvisor (secrets generated, not prompted)
            # deps loop — socks-proxy (optional): deploy
            ("select", "Bootstrap socks-proxy now?", "Yes — bootstrap now"),
            ("text", "socks-proxy host(s)", "10.0.0.6"),
            ("text", "PROXY_EXTERNAL", "eth0"),
            ("text", "PROXY_INTERNAL_PORT", "50405"),
            ("confirm", "cadvisor", True),  # socks-proxy cadvisor
        ],
    )

    bootstrap.bootstrap("messages", "prod")

    # mta-in: env blob rendered from helper-collected answers
    mtain_env = tree.load_vars("messages", "prod", "mta-in")["st_messages_mta_in_env"]
    assert "MDA_API_SECRET={{ vault_mda_api_secret }}" in mtain_env
    assert "MDA_API_BASE_URL=https://messages.example.org/api/v1.0/" in mtain_env
    assert "MYHOSTNAME=mx.example.org" in mtain_env

    # MDA_API_SECRET mirrored into both mta-in's and messages' vaults (same value)
    mtain_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "mta-in"))
    msgs_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "messages"))
    assert "vault_mda_api_secret" in mtain_vault
    assert "vault_mda_api_secret" in msgs_vault
    assert mtain_vault["vault_mda_api_secret"] == msgs_vault["vault_mda_api_secret"]

    # mpa: secrets follow the vault-ref split — vars.yml carries {{ vault_mpa_* }}
    # refs, the real values live under vault_mpa_* in mpa's vault.yml.
    mpa_vars = tree.load_vars("messages", "prod", "mpa")
    assert mpa_vars["st_messages_mpa_auth_bearer"] == "{{ vault_mpa_auth_bearer }}"
    assert (
        mpa_vars["st_messages_mpa_rspamd_controller_password"]
        == "{{ vault_mpa_rspamd_controller_password }}"
    )
    mpa_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "mpa"))
    assert "vault_mpa_auth_bearer" in mpa_vault
    assert "vault_mpa_rspamd_controller_password" in mpa_vault

    # socks-proxy: env blob rendered from helper-collected answers
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

    # messages backend: computed MTA_OUT_DIRECT_PROXIES with vault_proxy_users ref
    core_vars = (repo / "messages/prod/messages/vars.yml").read_text()
    assert (
        "MTA_OUT_DIRECT_PROXIES=socks5s://{{ vault_proxy_users }}@10.0.0.6:50405"
        in core_vars
    )
    assert "vault_proxy_users" in msgs_vault

    # messages backend: computed SPAM_CONFIG JSON (mpa deployed on a single host →
    # no LB prompt; rspamd_url embeds the mpa host + the role-default caddy port ref,
    # rspamd_auth embeds the mirrored bearer ref resolved from the messages vault).
    assert (
        'SPAM_CONFIG={"rspamd_url": "http://10.0.0.8:{{ st_messages_mpa_caddy_port }}", '
        '"rspamd_auth": "Bearer {{ vault_mpa_auth_bearer }}", '
        '"inbound_auth": "rspamd"}' in core_vars
    )

    # the auth bearer is mirrored into the messages vault under the same
    # vault_mpa_auth_bearer name (so the {{ vault_mpa_auth_bearer }} ref in
    # SPAM_CONFIG resolves there) and equals the value in the mpa vault.
    assert "vault_mpa_auth_bearer" in msgs_vault
    assert msgs_vault["vault_mpa_auth_bearer"] == mpa_vault["vault_mpa_auth_bearer"]


def test_bootstrap_messages_mta_in_standalone_prompts_mda_api_secret(repo, monkeypatch):
    """`bootstrap messages prod -c mta-in` with NO existing messages core vault
    prompts the operator for MDA_API_SECRET (the core-owned secret is unavailable
    before the core is bootstrapped) and routes it through the backend so the
    resulting mta-in vars.yml carries a real {{ vault_mda_api_secret }} ref and
    the mta-in vault.yml holds the prompted value — NOT a literal
    'MDA_API_SECRET={MDA_API_SECRET}' placeholder leaking into the committed tree.
    Regression guard for the silent-skip bug in the ansible-vault branch."""
    seed_creds(repo)
    sq = script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            # standalone -c mta-in → answers["DOMAIN"] unset → prompt for it
            ("text", "Public domain for messages", "messages.example.org"),
            ("text", "MYHOSTNAME", "mx.example.org"),
            # NO core vault on disk → MDA_API_SECRET prompt (the fix)
            ("password", "MDA_API_SECRET", "shared-secret-from-core"),
            ("confirm", "cadvisor", True),  # mta-in cadvisor
        ],
    )

    bootstrap.bootstrap("messages", "prod", component="mta-in")

    # mta-in vars.yml: env blob carries a real {{ vault_mda_api_secret }} ref
    # (NOT the literal 'MDA_API_SECRET={MDA_API_SECRET}' placeholder that the
    # silent-skip bug left behind when answers[MDA_API_SECRET] was never set).
    mtain_env = tree.load_vars("messages", "prod", "mta-in")["st_messages_mta_in_env"]
    assert "MDA_API_SECRET={{ vault_mda_api_secret }}" in mtain_env
    assert "{MDA_API_SECRET}" not in mtain_env
    assert "MDA_API_BASE_URL=https://messages.example.org/api/v1.0/" in mtain_env
    assert "MYHOSTNAME=mx.example.org" in mtain_env

    # mta-in vault.yml: the prompted value is stored under vault_mda_api_secret
    assert vault.is_encrypted(paths.vault_path("messages", "prod", "mta-in"))
    mtain_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "mta-in"))
    assert mtain_vault["vault_mda_api_secret"] == "shared-secret-from-core"

    # every scripted answer was consumed — the MDA_API_SECRET password prompt
    # was actually issued (a regression-skip would leave this script leftover).
    assert not sq._scripts, f"unconsumed scripts: {sq._scripts}"

    # messages core unit NOT written (standalone -c mta-in)
    assert not paths.vars_path("messages", "prod", "messages").exists()

    # manifest: only mta-in registered
    m = manifest.load_manifest()
    assert [u.component for u in m.units] == ["mta-in"]
    assert m.units[0].mode == "managed"


def test_bootstrap_messages_socks_proxy_hashi_vault_derives_mta_proxies(
    repo, monkeypatch
):
    """hashi_vault `bootstrap messages prod` deploying socks-proxy derives
    MTA_OUT_DIRECT_PROXIES from the PROXY_USERS lookup term instead of prompting
    for it (the ansible-vault bug). The messages core env blob carries a
    self-contained OpenBao lookup embedded in each socks5s:// URL, no vault.yml
    is written for socks-proxy (reference-only), and exactly one OpenBao lookup
    for PROXY_USERS is prompted (the ScriptedQuestionary errors on any
    unexpected/mismatched prompt, so not scripting MTA_OUT_DIRECT_PROXIES is the
    regression guard)."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "hashi_vault (OpenBao)"),
            ("text", "OpenBao / Vault URL", "https://vault.example:8200"),
            ("confirm", "Skip TLS verification?", False),
            # core hosts + optional worker-hosts prompt (messages has workers)
            ("text", "messages host(s)", "10.0.0.4"),
            ("text", "workers (leave blank", ""),
            # core questionnaire (_ask_core, messages)
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
            # messages-only S3: imports bucket (always) + blobs offload (declined)
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
            ("confirm", "cadvisor", True),  # core cadvisor (last core question)
            # deps loop — mta-in (required): deploy
            ("select", "Bootstrap mta-in now?", "Yes — bootstrap now"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            ("text", "MYHOSTNAME", "mx.example.org"),
            ("confirm", "cadvisor", True),  # mta-in cadvisor
            # deps loop — mpa (optional): deploy
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
            # deps loop — socks-proxy (optional): deploy
            ("select", "Bootstrap socks-proxy now?", "Yes — bootstrap now"),
            ("text", "socks-proxy host(s)", "10.0.0.6"),
            ("text", "PROXY_EXTERNAL", "eth0"),
            ("text", "PROXY_INTERNAL_PORT", "50405"),
            # exactly one PROXY_USERS — NO MTA_OUT_DIRECT_PROXIES
            ("text", "PROXY_USERS", "@openbao(kv/data/messages:proxy_users)"),
            ("confirm", "cadvisor", True),  # socks-proxy cadvisor
        ],
    )

    bootstrap.bootstrap("messages", "prod")

    # socks-proxy: env blob rendered with the PROXY_USERS OpenBao lookup ref
    sp_env = tree.load_vars("messages", "prod", "socks-proxy")[
        "st_messages_socks_proxy_env"
    ]
    assert "PROXY_EXTERNAL=eth0" in sp_env
    assert "PROXY_INTERNAL_PORT=50405" in sp_env
    assert (
        "PROXY_USERS={{ lookup('community.hashi_vault.hashi_vault', "
        "'kv/data/messages:proxy_users') }}" in sp_env
    )

    # no vault.yml for socks-proxy (hashi is reference-only)
    assert not paths.vault_path("messages", "prod", "socks-proxy").exists()

    # messages core: derived MTA_OUT_DIRECT_PROXIES embedding the SAME PROXY_USERS
    # lookup term (never prompted in hashi mode — derived from answers["PROXY_USERS"]).
    core_vars = (repo / "messages/prod/messages/vars.yml").read_text()
    assert (
        "MTA_OUT_DIRECT_PROXIES=socks5s://{{ lookup('community.hashi_vault.hashi_vault', "
        "'kv/data/messages:proxy_users') }}@10.0.0.6:50405" in core_vars
    )

    # messages core: SPAM_CONFIG is CONSTRUCTED (never prompted) — single mpa host
    # derives the rspamd_url and the auth bearer is the SAME OpenBao lookup ref
    # entered for st_messages_mpa_auth_bearer.
    assert (
        'SPAM_CONFIG={"rspamd_url": "http://10.0.0.8:{{ st_messages_mpa_caddy_port }}", '
        '"rspamd_auth": "Bearer {{ lookup(\'community.hashi_vault.hashi_vault\', '
        "'kv/data/messages:mpa_auth_bearer') }}\", "
        '"inbound_auth": "rspamd"}' in core_vars
    )


def test_bootstrap_messages_storage_blobs_offload(repo, monkeypatch):
    """`bootstrap messages prod` with blobs offload enabled: the imports bucket
    vars are always prompted, the blobs offload bucket + MESSAGES_BLOBS_OFFLOAD_ENABLED
    + MESSAGES_BLOBS_ENCRYPT_KEYS (with its vault ref) land in the messages backend
    env, and the three secrets (imports secret, blobs secret, blobs encrypt key) are
    written to the messages vault (the encrypt key generated ≥32 chars)."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            # core hosts, then the optional worker-hosts prompt (messages has workers)
            ("text", "messages host(s)", "10.0.0.4"),
            ("text", "workers (leave blank", ""),  # co-locate workers on the core hosts
            # core questionnaire (_ask_core)
            ("text", "Public domain for messages", "messages.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://messages"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            # messages does NOT use the generic AWS_S3_* storage — no S3 prompts here.
            # messages-only S3: imports bucket (always) + blobs offload (enabled)
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", "msg-imports"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", "impkey"),
            ("password", "STORAGE_MESSAGE_IMPORTS_SECRET_KEY", "impsecret"),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ""),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
            ("confirm", "Enable blobs offloading", True),
            ("text", "OPENSEARCH_URL", "http://opensearch:9200"),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", "mail.example.org"),
            # blobs offload bucket
            ("text", "STORAGE_MESSAGE_BLOBS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_BLOBS_BUCKET_NAME", "msg-blobs"),
            ("text", "STORAGE_MESSAGE_BLOBS_ACCESS_KEY", "blobkey"),
            ("password", "STORAGE_MESSAGE_BLOBS_SECRET_KEY", "blobsecret"),
            ("text", "STORAGE_MESSAGE_BLOBS_REGION_NAME", ""),
            # MESSAGES_BLOBS_ENCRYPT_KEY is generated (ansible-vault) — no prompt
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "messages-client-id"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            # no SMTP confirm — messages is NOT in _EMAIL_APPS
            ("select", "Outbound mail mode", "direct"),
            ("confirm", "cadvisor", True),  # core cadvisor (last core question)
            # deps loop — mta-in (required): take the deploy path
            ("select", "Bootstrap mta-in now?", "Yes — bootstrap now"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            ("text", "MYHOSTNAME", "mx.example.org"),
            ("confirm", "cadvisor", True),  # mta-in cadvisor
            # deps loop — mpa (optional): skip
            ("select", "Bootstrap mpa now?", "No — bootstrap later"),
            # deps loop — socks-proxy (optional): skip
            ("select", "Bootstrap socks-proxy now?", "No — bootstrap later"),
        ],
    )

    bootstrap.bootstrap("messages", "prod")

    # messages backend vars.yml: imports bucket + blobs offload + encrypt keys
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
    # OPENSEARCH_URL is mandatory — always rendered into the messages backend env.
    assert "OPENSEARCH_URL=http://opensearch:9200" in core_vars
    assert "MESSAGES_TECHNICAL_DOMAIN=mail.example.org" in core_vars

    # messages vault: the three secrets (imports secret, blobs secret, encrypt key)
    msgs_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "messages"))
    assert "vault_storage_message_imports_secret_key" in msgs_vault
    assert msgs_vault["vault_storage_message_imports_secret_key"] == "impsecret"
    assert "vault_storage_message_blobs_secret_key" in msgs_vault
    assert msgs_vault["vault_storage_message_blobs_secret_key"] == "blobsecret"
    assert "vault_messages_blobs_encrypt_key" in msgs_vault
    assert len(msgs_vault["vault_messages_blobs_encrypt_key"]) >= 32
    assert "vault_salt_key" in msgs_vault


def test_bootstrap_messages_relay_outbound_mode(repo, monkeypatch):
    """`bootstrap messages prod` in relay outbound mode: the MTA_OUT_MODE select
    (asked at the end of _ask_core, after email, before the core cadvisor confirm)
    collects the external SMTP smarthost host + optional credentials and routes
    the password through the ansible-vault backend. Relay mode SUPPRESSES the
    socks-proxy dependency prompt (egress is the smarthost, not socks-proxy), so
    no socks-proxy prompt is scripted — ScriptedQuestionary errors on any
    unscripted prompt, which is the regression guard for the skip. mta-in and mpa
    are still bootstrapped as in the full deploy flow."""
    seed_creds(repo)
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            # core hosts, then the optional worker-hosts prompt (messages has workers)
            ("text", "messages host(s)", "10.0.0.4"),
            ("text", "workers (leave blank", ""),  # co-locate workers on the core hosts
            # core questionnaire (_ask_core)
            ("text", "Public domain for messages", "messages.example.org"),
            ("select", "Database configuration:", "DATABASE_URL"),
            ("text", "DATABASE_URL", "postgres://messages"),
            ("text", "REDIS_URL", "redis://redis:6379/0"),
            # messages-only S3: imports bucket (always) + blobs offload (declined)
            ("text", "STORAGE_MESSAGE_IMPORTS_ENDPOINT_URL", "https://s3.example.org"),
            ("text", "STORAGE_MESSAGE_IMPORTS_BUCKET_NAME", "msg-imports"),
            ("text", "STORAGE_MESSAGE_IMPORTS_ACCESS_KEY", "impkey"),
            ("password", "STORAGE_MESSAGE_IMPORTS_SECRET_KEY", "impsecret"),
            ("text", "STORAGE_MESSAGE_IMPORTS_REGION_NAME", ""),
            ("text", "STORAGE_MESSAGE_IMPORTS_EXPIRE_POLICY", "3600"),
            ("confirm", "Enable blobs offloading", False),
            ("text", "OPENSEARCH_URL", "http://opensearch:9200"),
            ("text", "MESSAGES_TECHNICAL_DOMAIN", "mail.example.org"),
            ("select", "Identity provider:", "keycloak"),
            ("text", "Keycloak base URL", "https://idp.example.org"),
            ("text", "Keycloak realm", "master"),
            ("text", "OIDC_RP_CLIENT_ID", "messages-client-id"),
            ("password", "OIDC_RP_CLIENT_SECRET", "oidc-secret"),
            # no SMTP confirm — messages is NOT in _EMAIL_APPS
            # outbound mail mode (asked at the end of _ask_core, before cadvisor)
            ("select", "Outbound mail mode", "relay"),
            ("text", "MTA_OUT_RELAY_HOST", "smtp.example.org:587"),
            ("text", "MTA_OUT_RELAY_USERNAME", "relayuser"),
            ("password", "MTA_OUT_RELAY_PASSWORD", "relaypass"),
            ("confirm", "cadvisor", True),  # core cadvisor (last core question)
            # deps loop — mta-in (required): deploy
            ("select", "Bootstrap mta-in now?", "Yes — bootstrap now"),
            ("text", "mta-in host(s)", "10.0.0.7"),
            ("text", "MYHOSTNAME", "mx.example.org"),
            ("confirm", "cadvisor", True),  # mta-in cadvisor
            # deps loop — mpa (optional): deploy
            ("select", "Bootstrap mpa now?", "Yes — bootstrap now"),
            ("text", "mpa host(s)", "10.0.0.8"),
            ("confirm", "cadvisor", True),  # mpa cadvisor (secrets generated)
            # deps loop — socks-proxy (optional): SKIPPED in relay mode — NOT scripted
            # (ScriptedQuestionary would error if the relay-mode skip regressed).
        ],
    )

    bootstrap.bootstrap("messages", "prod")

    # messages core vars.yml: the relay outbound env vars land in the
    # st_messages_backend_env blob (DIRECT mode would emit none of these).
    core_vars = (repo / "messages/prod/messages/vars.yml").read_text()
    assert "MTA_OUT_MODE=relay" in core_vars
    assert "MTA_OUT_RELAY_HOST=smtp.example.org:587" in core_vars
    assert "MTA_OUT_RELAY_USERNAME=relayuser" in core_vars
    assert "MTA_OUT_RELAY_PASSWORD={{ vault_mta_out_relay_password }}" in core_vars

    # relay mode suppresses socks-proxy: no socks-proxy unit vars written, and no
    # MTA_OUT_DIRECT_PROXIES (that var is computed by the socks-proxy helper).
    assert not paths.vars_path("messages", "prod", "socks-proxy").exists()
    assert "MTA_OUT_DIRECT_PROXIES" not in core_vars

    # messages vault: the relay password is routed through the backend.
    msgs_vault = vault.decrypt_to_dict(paths.vault_path("messages", "prod", "messages"))
    assert "vault_mta_out_relay_password" in msgs_vault


# --------------------------------------------------------------------------- pre-questionnaire intro guidance


def test_bootstrap_intro_guidance_for_core_not_provider(repo, monkeypatch, capfd):
    """Pre-questionnaire guidance (arch-docs URL + a 'Requirements' checklist with
    the ProConnect pointer) is printed before the 'Bootstrapped' line for a full/
    core/workers run, and is ABSENT for a provider-only `-c <provider>` run."""
    seed_creds(repo)
    # --- part 1: keycloak full run → guidance present before "Bootstrapped" ---
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
    assert "partenaires.proconnect.gouv.fr" in flat

    # --- part 2: meet livekit provider run → guidance absent ---
    script_questionary(
        monkeypatch,
        [
            ("select", "Secret backend:", "ansible-vault"),
            ("text", "livekit host(s)", "10.0.0.1"),
            # egress hosts (bundled into the livekit step) asked right after the
            # livekit hosts, BEFORE LiveKit domain/TURN: blank → co-locate
            ("text", "egress (leave blank", ""),
            (
                "text",
                "LiveKit domain (e.g. livekit.example.org)",
                "livekit.example.org",
            ),
            ("text", "LiveKit TURN domain (e.g. turn.example.org)", "turn.example.org"),
            # standalone -c livekit → _ensure_meet_domain prompts DOMAIN for the
            # livekit unit's st_meet_public_host (no _ask_core here, so DOMAIN
            # isn't set yet).
            (
                "text",
                "Public domain for meet (for the LiveKit recording webhook)",
                "meet.example.org",
            ),
            ("confirm", "livekit", True),  # livekit cadvisor
            ("confirm", "egress", True),  # egress cadvisor (single co-located → valkey)
        ],
    )
    bootstrap.bootstrap("meet", "prod", component="livekit")

    out2 = capfd.readouterr().out
    flat2 = " ".join(out2.replace("│", " ").split())
    assert "Requirements" not in flat2
    assert "partenaires.proconnect.gouv.fr" not in flat2


def test_confirm_ready_gate_aborts_on_decline_or_interrupt(monkeypatch):
    """The pre-questionnaire readiness gate is a yes/no confirm: 'yes' continues,
    while declining (False) or Ctrl+C/EOF (None from .ask()) raises StCliError so
    the whole run aborts instead of half-preparing the questionnaire."""

    class _Q:
        def __init__(self, ans):
            self._ans = ans

        def ask(self):
            return self._ans

    # yes → no raise (continues)
    monkeypatch.setattr(prompts.questionary, "confirm", lambda *a, **k: _Q(True))
    prompts._confirm_ready("ready?")

    # decline (n) → abort
    monkeypatch.setattr(prompts.questionary, "confirm", lambda *a, **k: _Q(False))
    with pytest.raises(StCliError):
        prompts._confirm_ready("ready?")

    # Ctrl+C / EOF → .ask() returns None → abort
    monkeypatch.setattr(prompts.questionary, "confirm", lambda *a, **k: _Q(None))
    with pytest.raises(StCliError):
        prompts._confirm_ready("ready?")


# --------------------------------------------------------------------------- core answers: LOGIN_REDIRECT_URL_FAILURE


def test_ask_core_sets_login_redirect_url_failure_for_non_drive(monkeypatch):
    """`_ask_core` sets LOGIN_REDIRECT_URL_FAILURE so the OIDC login "failure"
    redirect resolves to https://<domain>/ instead of the literal string None
    (browser → /api/v1.0/callback/None → 404). meet now HAS an override, like
    drive: the meet branch reassigns LOGIN_REDIRECT_URL(_FAILURE) (and the
    DJANGO_ALLOWED_HOSTS / CORS / logout redirect) to the st_meet_public_host
    ansible var — single source of truth, mirroring drive's st_drive_public_host
    override. The answer VALUE is the literal `https://{{ st_meet_public_host }}/`
    string; the env template emits it via answers.SOMEKEY so the {{ }} lands
    verbatim in the env blob and ANSIBLE resolves it at deploy. This test covers
    the meet override path; drive is unaffected (its own override uses
    st_drive_public_host), and messages (the actual no-override non-drive case)
    is covered by its own _ask_core tests."""
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

    # meet reassigns the redirect URLs to the st_meet_public_host ansible var
    # (single source of truth); the {{ }} travels verbatim through the answer so
    # ANSIBLE resolves it at deploy from the core vars.yml (which lands the literal
    # DOMAIN). DJANGO_ALLOWED_HOSTS + CSRF/CORS origins get the same treatment.
    assert answers["LOGIN_REDIRECT_URL"] == "https://{{ st_meet_public_host }}/"
    assert answers["LOGIN_REDIRECT_URL_FAILURE"] == "https://{{ st_meet_public_host }}/"
    assert answers["DJANGO_ALLOWED_HOSTS"] == "{{ st_meet_public_host }}"

    # the rendered meet backend env blob emits the verbatim var-ref (the env
    # template prints answers.SOMEKEY, so the {{ }} is not re-evaluated by jinja2)
    body = envrender.render_env("meet", "meet", answers)["st_meet_backend_env"]
    assert "LOGIN_REDIRECT_URL=https://{{ st_meet_public_host }}/" in body
    assert "LOGIN_REDIRECT_URL_FAILURE=https://{{ st_meet_public_host }}/" in body
    # recording is always on now (no confirm) → RECORDING_* lines are always
    # present in the rendered env
    assert "RECORDING_ENABLE=True" in body
