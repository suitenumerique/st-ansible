"""Shared test helpers: config-tree seeding + a scripted questionary stand-in.

Plain functions (not fixtures) so any test module can import and call them. The
``repo`` fixture (a tmp_path cwd) lives in ``conftest.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from st_cli.cmd import bootstrap
from st_cli.core import appmeta, manifest, paths, tree, vault, writer
from st_cli.core.models import SecretConfig, StCliManifest, UnitState


def seed_creds(repo: Path) -> None:
    """Write a .vault-pass so the vault-password prompt is skipped."""
    (repo / ".vault-pass").write_text("testpass\n")


def seed_meet_unit(repo: Path) -> None:
    """Seed a managed meet/prod/meet unit with vars + hosts (ansible-vault backend)."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("meet", "prod", "meet", "managed")]
        )
    )
    data = tree.load_vars("meet", "prod", "meet")
    # enabled flag is NOT stored in vars.yml — the generated playbook injects it
    data["st_meet_backend_env"] = "DJANGO_CONFIGURATION=Production\n"
    tree.save_vars("meet", "prod", "meet", data)
    tree.write_hosts("meet", "prod", "meet", "meet", ["10.0.0.5"])


def seed_scaffolding_artifacts() -> None:
    """Pre-create the 4 trashable .st-cli/ artifacts so a clean/no-clean assertion is meaningful."""
    paths.st_cli_dir().mkdir(parents=True, exist_ok=True)
    (paths.st_cli_dir() / "ansible.cfg").write_text("[defaults]\n")
    (paths.st_cli_dir() / "galaxy-requirements.yml").write_text("collections: []\n")
    paths.playbooks_dir().mkdir(parents=True, exist_ok=True)
    (paths.playbooks_dir() / "meet-prod-meet.yml").write_text("[]\n")
    paths.collections_dir().mkdir(parents=True, exist_ok=True)


def seed_livekit_provider(repo: Path) -> None:
    """Seed a bootstrapped meet/prod/livekit provider unit (vars/vault/hosts).

    The vars reflect what a fresh ``-c livekit`` bootstrap now writes (including the
    egress-bundled valkey/redis topology decision), so a standalone ``-c egress`` run
    that ADOPTS them can be asserted on. The seeded redis address is a distinctive
    value (NOT the ``127.0.0.1:6379`` co-located default) so adoption tests can tell a
    real adoption apart from the fallback default. The vault also seeds the
    external-redis password (valkey is disabled here, so a real bootstrap always
    mirrors one).
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


def seed_meet_egress_unit(repo: Path, hosts=("10.0.0.2",)) -> None:
    """Seed a bootstrapped meet/prod/egress unit standalone on ``hosts``.

    Compose with ``seed_livekit_provider`` (call it first): the domain and redis
    address match its values, so the egress unit reads as already bundled with
    that livekit unit. Models ``seed_livekit_provider``'s vault shape — the
    mirrored api key/secret + the redis password ``real-redis-pass`` — so a
    livekit replay that mirrors them again is a byte no-op.
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
    """Seed a bootstrapped meet/prod/livekit provider unit under the hashi_vault
    backend: lookup-ref secrets in vars.yml, no ``vault.yml``, no ``.vault-pass``.

    Co-located (valkey enabled, default redis address) so a standalone
    ``-c livekit`` replay never touches the egress/redis prompts — this seeds
    for a test about the shared-secret refs, not the redis topology.
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
    """Seed a meet/prod/livekit unit recorded ``external`` in the manifest, with
    its local tree (vars/vault/hosts) still on disk from before it was hand-
    flipped external — the "recorded mode wins over tree presence" scenario.

    Runs a real co-located meet+livekit bootstrap first (so the core is a
    genuine, fully-recoverable unit — DB/S3/OIDC included, not just the
    livekit-related keys), then flips the livekit unit's manifest mode and
    re-points the core's committed ``LIVEKIT_API_URL`` at a distinct external
    host — the leftover livekit tree keeps its OWN (now-ignored) domain, so a
    test can tell the two apart.
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


class _AcceptDefault:
    """Sentinel script answer: "press Enter" on whatever ``default=`` the
    prompt call was given (a native editable pre-fill, per
    ``core/prompts.py``'s ``_text_question``/``_ask_select`` docstrings). Used
    by rebootstrap tests to script an Enter-through run without hardcoding the
    recovered value at every single prompt."""

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

    Each script is a ``(kind, substring, answer)`` tuple; the first script whose
    ``kind`` matches and whose ``substring`` is found in the prompt is consumed.
    ``select`` records the ``(message, choices)`` it was offered so tests can
    assert on the available options (e.g. no "Yes — bootstrap now" in wire-only mode).
    """

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.select_calls: list[tuple[str, list[str]]] = []

    def _consume(self, kind, prompt):
        for i, (k, sub, ans) in enumerate(self._scripts):
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


def script_questionary(monkeypatch, scripts) -> ScriptedQuestionary:
    """Patch ``st_cli.core.prompts.questionary`` with scripted responses.

    ``setup_backend`` and the bootstrap helpers all reach questionary through the
    shared primitives in ``core/prompts.py`` (re-exported by ``cmd/bootstrap.py``),
    so patching that module's ``questionary`` surface covers every interactive
    call (vault-password prompts are skipped because the tests pre-seed
    ``.vault-pass``).
    """
    from st_cli.core import prompts

    sq = ScriptedQuestionary(scripts)
    monkeypatch.setattr(prompts.questionary, "text", sq.text)
    monkeypatch.setattr(prompts.questionary, "password", sq.password)
    monkeypatch.setattr(prompts.questionary, "confirm", sq.confirm)
    monkeypatch.setattr(prompts.questionary, "select", sq.select)
    return sq
