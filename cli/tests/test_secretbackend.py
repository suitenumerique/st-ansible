"""Tests for st_cli.core.secretbackend — the per-(app,env) secret backends.

Two backends: the default AnsibleVaultBackend (plaintext vars.yml with
``{{ vault_* }}`` refs + an encrypted vault.yml) and the opt-in, reference-only
HashiVaultBackend (env blobs carry ``lookup(...)`` refs; no vault.yml, no writes).
"""

from __future__ import annotations

from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import LiteralScalarString

from st_cli.core import envrender, paths, tree
from st_cli.core.models import SecretConfig, StCliManifest, UnitState
from st_cli.core.secretbackend import (
    AnsibleVaultBackend,
    HashiVaultBackend,
    hashi_lookup_ref,
    hashi_render,
    setup_backend,
)


def _raise(*args, **kwargs):
    """A prompt-helper stand-in that fails the test if any prompt fires."""
    raise AssertionError("unexpected prompt on the reuse path")


# --------------------------------------------------------------------------- hashi_lookup_ref (pure builder)


def test_hashi_lookup_ref_plain_term():
    """A term with no quotes/backslashes is embedded verbatim inside the lookup —
    slashes/colons survive; a term with no colon is fine (field split happens later)."""
    ref = hashi_lookup_ref("kv/data/meet/prod:django")
    assert (
        ref
        == "{{ lookup('community.hashi_vault.hashi_vault', 'kv/data/meet/prod:django') }}"
    )
    assert "kv/data/meet/prod:django" in ref  # no path munging
    assert (
        hashi_lookup_ref("nocolon")
        == "{{ lookup('community.hashi_vault.hashi_vault', 'nocolon') }}"
    )


def test_hashi_lookup_ref_single_quote_is_escaped():
    """A single quote in the term is escaped, so it cannot close the literal.

    The injected ``{{``/``}}`` from the payload stay INSIDE the quoted literal
    (inert to Jinja) because the quote before them is now ``\\'`` — the exact
    output below pins that. Every quote from the term appears only in escaped form.
    """
    ref = hashi_lookup_ref("kv/data/x') }}{{ lookup('evil")
    assert (
        ref
        == "{{ lookup('community.hashi_vault.hashi_vault', 'kv/data/x\\') }}{{ lookup(\\'evil') }}"
    )
    # the unescaped break-out sequence must NOT be present…
    assert "x') }}" not in ref
    # …and each of the term's quotes survives only as an escaped \' inside the literal
    assert ref.count("\\'") == 2


def test_hashi_lookup_ref_backslash_is_escaped():
    """A backslash is doubled so it cannot escape the following closing quote."""
    ref = hashi_lookup_ref("kv\\data")
    assert ref == "{{ lookup('community.hashi_vault.hashi_vault', 'kv\\\\data') }}"


# --------------------------------------------------------------------------- hashi_render (inline @openbao()/@vault() interpolation)


def test_hashi_render_no_marker_is_literal():
    """A value with NO marker is kept literal (plain text) — even for a secret
    var like a hashed password. The operator must use @openbao()/@vault() to
    opt a value into a lookup; a bare, marker-less value is never wrapped."""
    assert hashi_render("$2$hashedpw") == "$2$hashedpw"


def test_hashi_render_inline_openbao_marker_mid_string():
    """An inline @openbao(<path>) marker mid-string: only the marked segment
    becomes a lookup; the surrounding literal text is preserved verbatim."""
    assert hashi_render("user1:@openbao(kv/data/x:pw)") == (
        "user1:" + hashi_lookup_ref("kv/data/x:pw")
    )


def test_hashi_render_vault_alias_matches_openbao():
    """The @vault(<path>) alias produces an identical result to @openbao(<path>)
    for the same path — the two markers are interchangeable."""
    assert hashi_render("u:@vault(kv/data/x:pw)") == (
        "u:" + hashi_lookup_ref("kv/data/x:pw")
    )
    # and identical to the openbao form for the same path
    assert hashi_render("u:@vault(kv/data/x:pw)") == hashi_render(
        "u:@openbao(kv/data/x:pw)"
    )


def test_hashi_render_multiple_markers_both_resolve():
    """Multiple markers in one value all resolve; literal text between them is
    kept verbatim. Mixed @openbao/@vault in the same value is allowed."""
    assert hashi_render("a:@openbao(p1) b:@vault(p2)") == (
        "a:" + hashi_lookup_ref("p1") + " b:" + hashi_lookup_ref("p2")
    )


def test_hashi_render_marker_path_with_quote_is_escaped():
    """A single quote inside a marker path is escaped by hashi_lookup_ref (the
    marked segment goes through it), so the term's quote cannot close the Jinja
    literal early — no unescaped break-out sequence appears. (A path may contain
    ':'/'/' but not ')', so the break-out attempt stops at the first ')' and the
    quote inside the marked path is the only thing that needs escaping.)"""
    raw = "pre:@openbao(kv/data/x'evil)post"
    out = hashi_render(raw)
    # the literal prefix/suffix are preserved
    assert out.startswith("pre:")
    assert out.endswith("post")
    # the marked path's quote survives only as an escaped \' (one quote → one \')
    assert out.count("\\'") == 1
    # the unescaped "x'evil" (quote not escaped) must NOT be present
    assert "x'evil" not in out


def test_hashi_env_secret_interpolates_inline_marker(repo, monkeypatch):
    """HashiVaultBackend.env_secret routes the prompted term through
    hashi_render: an inline @openbao(<path>) marker yields a literal prefix +
    lookup ref in answers, and still no vault.yml is written."""
    backend = HashiVaultBackend("meet")
    monkeypatch.setattr(
        backend, "_prompt_term", lambda label: "user1:@openbao(kv/data/x:pw)"
    )

    answers: dict = {}
    backend.env_secret(answers, "PROXY_AUTH", component="meet", value="ignored")

    assert answers["PROXY_AUTH"] == "user1:" + hashi_lookup_ref("kv/data/x:pw")
    # reference-only: still no vault buffer → no vault.yml
    assert backend.component_secrets("meet") == {}


# --------------------------------------------------------------------------- HashiVaultBackend (reference-only)


def test_hashi_prompt_term_prefills_openbao_default(monkeypatch):
    """HashiVaultBackend._prompt_term prompts with the bare var name and a native
    editable pre-filled @openbao(kv/data/<app>:<VAR>) default (no placeholder) —
    the operator accepts it with Enter or edits inline. When accepted,
    hashi_render turns the @openbao() marker into a lookup ref in answers."""
    from st_cli.core import prompts

    captured: dict = {}

    def _fake_ask(prompt, default="", required=True, placeholder=None):
        captured["prompt"] = prompt
        captured["default"] = default
        captured["placeholder"] = placeholder
        # accepting the pre-filled default: _ask returns the prefilled value.
        return default

    monkeypatch.setattr(prompts, "_ask", _fake_ask)

    backend = HashiVaultBackend("messages")
    answers: dict = {}
    backend.env_secret(answers, "DJANGO_SECRET_KEY", component="messages")

    # the prompt label is the bare var name (no "OpenBao lookup for " prefix)
    assert captured["prompt"] == "DJANGO_SECRET_KEY"
    # native editable pre-fill: default is the @openbao(...) hint, no placeholder
    assert captured["default"] == "@openbao(kv/data/messages:DJANGO_SECRET_KEY)"
    assert captured["placeholder"] is None
    # accepting the hint → hashi_render turns the @openbao() marker into a lookup ref
    assert answers["DJANGO_SECRET_KEY"] == hashi_lookup_ref(
        "kv/data/messages:DJANGO_SECRET_KEY"
    )


def test_hashi_env_secret_embeds_lookup_term_and_no_vault_yml(repo, monkeypatch):
    """HashiVaultBackend.env_secret writes a lookup ref into answers and buffers
    nothing for component_secrets (so _write_vault no-ops → no vault.yml).
    Reference-only: the value (if any) is ignored — nothing is minted or written."""
    backend = HashiVaultBackend("meet")
    # stub the single interactive prompt (TTY-free): the marked lookup term.
    # @openbao() makes the term resolve to a lookup ref (a bare, marker-less
    # value would now be kept literal — see test_hashi_env_secret_no_marker_stays_literal).
    monkeypatch.setattr(
        backend, "_prompt_term", lambda label: "@openbao(kv/data/meet/prod:django)"
    )

    answers: dict = {}
    backend.env_secret(
        answers,
        "DJANGO_SECRET_KEY",
        component="meet",
        value="gen-super-secret",  # ignored by hashi_vault (reference-only)
    )

    # the env answer is a lookup ref embedding the term verbatim
    assert (
        "lookup('community.hashi_vault.hashi_vault', 'kv/data/meet/prod:django')"
        in answers["DJANGO_SECRET_KEY"]
    )
    # no vault buffer → _write_vault would no-op → no vault.yml
    assert backend.component_secrets("meet") == {}

    # render the env blob + save vars.yml — the ignored value must NOT appear
    blobs = envrender.render_env("meet", "meet", answers)
    data = CommentedMap()
    for blob_var, text in blobs.items():
        data[blob_var] = LiteralScalarString(text)
    tree.save_vars("meet", "prod", "meet", data)
    raw = (repo / "meet/prod/meet/vars.yml").read_text()
    assert "gen-super-secret" not in raw  # secret value never committed
    assert "lookup('community.hashi_vault" in raw  # lookup ref is in the env blob
    # and no vault.yml was written
    assert not paths.vault_path("meet", "prod", "meet").exists()


def test_hashi_env_secret_no_marker_stays_literal(repo, monkeypatch):
    """A no-marker term routed through HashiVaultBackend.env_secret lands LITERALLY
    in answers (no lookup wrapping) — e.g. an operator types a hashed password
    plainly, refusing the @openbao() default, and it is kept verbatim. The
    operator must use @openbao()/@vault() to opt a value into a lookup."""
    backend = HashiVaultBackend("meet")
    # stub the prompt to a bare, marker-less value (a hashed password)
    monkeypatch.setattr(backend, "_prompt_term", lambda label: "$2$sphk3ry85")

    answers: dict = {}
    backend.env_secret(
        answers,
        "DJANGO_SECRET_KEY",
        component="meet",
        value="ignored",  # reference-only: the prompt term wins, not this
    )

    # the env answer is the literal value — no lookup ref, no wrapping
    assert answers["DJANGO_SECRET_KEY"] == "$2$sphk3ry85"
    assert "lookup(" not in answers["DJANGO_SECRET_KEY"]


def test_hashi_var_secret_sets_pvars_lookup_ref(repo, monkeypatch):
    """HashiVaultBackend.var_secret puts a lookup ref into the provider's plaintext
    pvars (not a vault.yml); the value is ignored (reference-only)."""
    backend = HashiVaultBackend("meet")
    monkeypatch.setattr(
        backend, "_prompt_term", lambda label: "@openbao(kv/data/livekit:api_key)"
    )

    pvars = CommentedMap()
    backend.var_secret(
        pvars, "st_meet_livekit_api_key", "minted-token", component="livekit"
    )
    assert "lookup('community.hashi_vault" in pvars["st_meet_livekit_api_key"]
    assert (
        "minted-token" not in pvars["st_meet_livekit_api_key"]
    )  # value ignored, not in plaintext
    assert backend.component_secrets("livekit") == {}  # no vault.yml


def test_hashi_shared_provider_secret_single_source(repo, monkeypatch):
    """A secret that is both a provider var and a consumer env ref is prompted
    ONCE and both sides point at the same OpenBao lookup ref (single source of
    truth) — one lookup term, no writes (reference-only)."""
    backend = HashiVaultBackend("meet")
    calls = {"term": 0}

    def _term(label):
        calls["term"] += 1
        return "@openbao(kv/data/livekit:api_key)"

    monkeypatch.setattr(backend, "_prompt_term", _term)

    pvars = CommentedMap()
    answers: dict = {}
    backend.shared_provider_secret(
        pvars,
        answers,
        "st_meet_livekit_api_key",
        "LIVEKIT_API_KEY",
        "minted-token",
        provider="livekit",
        consumer="meet",
    )
    # prompted exactly ONCE (one lookup term; reference-only — nothing derived)
    assert calls == {"term": 1}
    # both sides carry the SAME lookup ref
    assert pvars["st_meet_livekit_api_key"] == answers["LIVEKIT_API_KEY"]
    assert "lookup('community.hashi_vault" in answers["LIVEKIT_API_KEY"]
    # value ignored — never in plaintext, never written (reference-only)
    assert "minted-token" not in pvars["st_meet_livekit_api_key"]


# --------------------------------------------------------------------------- AnsibleVaultBackend (default)


def test_ansible_vault_shared_provider_secret_keeps_two_stores():
    """The default (ansible-vault) shared_provider_secret keeps two stores: raw
    value in the provider buffer, vault_<key> copy in the consumer buffer, and a
    {{ vault_<key> }} ref in the consumer answers."""
    b = AnsibleVaultBackend()
    pvars = CommentedMap()
    answers: dict = {}
    b.shared_provider_secret(
        pvars,
        answers,
        "st_meet_livekit_api_key",
        "LIVEKIT_API_KEY",
        "tok",
        provider="livekit",
        consumer="meet",
    )
    assert b.component_secrets("livekit") == {"st_meet_livekit_api_key": "tok"}
    assert b.component_secrets("meet") == {"vault_livekit_api_key": "tok"}
    assert answers["LIVEKIT_API_KEY"] == "{{ vault_livekit_api_key }}"
    assert "st_meet_livekit_api_key" not in pvars  # provider var stays out of plaintext


def test_ansible_vault_backend_env_and_var_secret():
    """AnsibleVaultBackend.env_secret buffers vault_<key> and puts a
    {{ vault_<key> }} ref in answers; var_secret buffers the raw provider scalar
    and does NOT touch pvars."""
    b = AnsibleVaultBackend()
    answers: dict = {}
    b.env_secret(answers, "DJANGO_SECRET_KEY", component="meet", value="sek")
    assert answers["DJANGO_SECRET_KEY"] == "{{ vault_django_secret_key }}"
    assert b.component_secrets("meet") == {"vault_django_secret_key": "sek"}

    # var_secret: provider standalone scalar goes raw into the component buffer,
    # pvars is untouched.
    pvars: dict = {}
    b.var_secret(pvars, "st_meet_livekit_api_secret", "raw", component="livekit")
    assert pvars == {}
    assert b.component_secrets("livekit") == {"st_meet_livekit_api_secret": "raw"}


# --------------------------------------------------------------------------- backend selection + common.yml


def test_write_common_connection_preserves_existing_keys(repo):
    """write_common_connection merges ansible_hashi_vault_* into common.yml,
    preserving any pre-existing keys AND the header comment block."""
    from st_cli.core import secretbackend

    tree.ensure_common("meet", "prod")
    # hand-edit: append keys after the header comment (simulating a user edit
    # that preserves the # preamble). Using raw text because load_common/save_common
    # themselves don't round-trip a comment before '---'.
    p = paths.common_path("meet", "prod")
    p.write_text(
        p.read_text() + "st_meet_uid: 1234\nansible_hashi_vault_url: https://old:8200\n"
    )

    secretbackend.write_common_connection(
        "meet", "prod", "https://vault.example:8200", True, "token"
    )

    raw = (repo / "meet/prod/common.yml").read_text()
    loaded = tree.load_common("meet", "prod")
    assert loaded["st_meet_uid"] == 1234  # pre-existing key kept
    assert (
        str(loaded["ansible_hashi_vault_url"]) == "https://vault.example:8200"
    )  # overwritten
    assert loaded["ansible_hashi_vault_validate_certs"] is True
    assert str(loaded["ansible_hashi_vault_auth_method"]) == "token"
    # the original header comment is preserved (re-applied from the raw text)
    assert "st-cli app/env-wide vars" in raw


# --------------------------------------------------------------------------- setup_backend reuse


def test_setup_backend_reuses_ansible_vault_without_prompt(repo, monkeypatch, capfd):
    """On a re-bootstrap of an already-configured (app, env), setup_backend reuses
    the persisted backend choice silently — no "Secret backend:" select prompt.

    The ansible-vault backend writes NO ``secrets:`` entry (per the "omit empty
    secrets block" convention), so a prior unit is the signal it was bootstrapped.
    The default backend name resolves to ``ansible-vault``.
    """
    from st_cli.core import prompts

    m = StCliManifest(
        "0.0.20", "0.0.20", [UnitState("meet", "prod", "meet", "managed")]
    )
    monkeypatch.setattr(prompts, "_ask_select", _raise)

    backend = setup_backend(m, "meet", "prod")

    assert isinstance(backend, AnsibleVaultBackend)
    assert backend.kind == "ansible-vault"
    # the one-line reuse notice is printed (no prompt was issued)
    assert "Reusing the 'ansible-vault' secret backend" in capfd.readouterr().out


def test_setup_backend_reuses_hashi_vault_without_prompt(repo, monkeypatch, capfd):
    """A hashi_vault (app, env) — recorded via a ``secrets:`` entry — is reused
    silently on re-bootstrap: no backend select, no OpenBao URL/TLS prompt, and no
    common.yml write (connection vars are already in common.yml from the first
    bootstrap)."""
    from st_cli.core import prompts

    m = StCliManifest("0.0.20", "0.0.20", [])
    m.secrets = [SecretConfig(app="meet", env="prod", backend="hashi_vault")]
    monkeypatch.setattr(prompts, "_ask_select", _raise)
    monkeypatch.setattr(prompts, "_ask", _raise)
    monkeypatch.setattr(prompts, "_confirm", _raise)

    backend = setup_backend(m, "meet", "prod")

    assert isinstance(backend, HashiVaultBackend)
    assert backend.kind == "hashi_vault"
    assert "Reusing the 'hashi_vault' secret backend" in capfd.readouterr().out
