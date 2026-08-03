"""Shared test helpers: config-tree seeding + a scripted questionary stand-in.

Plain functions (not fixtures) so any test module can import and call them. The
``repo`` fixture (a tmp_path cwd) lives in ``conftest.py``.
"""

from __future__ import annotations

from pathlib import Path

from st_cli.core import manifest, paths, tree, vault
from st_cli.core.models import StCliManifest, UnitState


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


def seed_docs_yprovider_unit(repo: Path) -> None:
    """Seed a bootstrapped docs/prod/yprovider unit (vars/vault/hosts).

    The vault carries distinctive secret values so an adoption test can tell the
    kept unit's values apart from freshly generated ones.
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
        return FakeQuestion(self._consume("text", prompt))

    def password(self, prompt, **kwargs):
        return FakeQuestion(self._consume("password", prompt))

    def confirm(self, prompt, **kwargs):
        # The pre-questionnaire readiness gate auto-passes (yes) without consuming
        # a script, so every full/core/workers-run test needn't script it.
        if "ready" in prompt.lower():
            return FakeQuestion(True)
        return FakeQuestion(self._consume("confirm", prompt))

    def select(self, message, choices, **kwargs):
        self.select_calls.append((message, list(choices)))
        return FakeQuestion(self._consume("select", message))


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
