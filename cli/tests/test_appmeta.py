"""Tests for st_cli.core.appmeta — app/component metadata loaded from resources/apps."""

from __future__ import annotations

import pytest

from st_cli.core import appmeta
from st_cli.core.errors import StCliError
from st_cli.core.models import Component


def test_all_apps_load_with_core_and_components():
    for app in ["meet", "drive", "messages", "keycloak", "transfers"]:
        a = appmeta.load_app(app)
        assert a.components
        assert a.core().is_core


def test_keycloak_is_a_standalone_single_component_app():
    """keycloak is a non-Django app: one core component, no deps, no worker, and a
    single ``st_keycloak_env`` blob rendered from keycloak.env.j2."""
    assert "keycloak" in appmeta.list_apps()
    meta = appmeta.load_app("keycloak")
    core = meta.core()
    assert core.key == "keycloak"
    assert core.role == "suitenumerique.st.keycloak"
    assert core.enabled_var == "st_keycloak_enabled"
    assert meta.dependencies == []
    assert meta.worker() is None
    spec = meta.env_render_spec("keycloak")
    assert spec["backend"]["blob_var"] == "st_keycloak_env"
    assert spec["backend"]["templates"] == ["keycloak.env.j2"]


def test_dependency_graph():
    assert ("meet", "livekit") in [
        (d.of, d.on) for d in appmeta.load_app("meet").dependencies
    ]
    assert ("drive", "collabora") in [
        (d.of, d.on) for d in appmeta.load_app("drive").dependencies
    ]
    assert {"mta-in", "mpa", "socks-proxy"} <= {
        c.key for c in appmeta.load_app("messages").components
    }
    # egress is a meet dependency (the livekit dep stays dependencies[0]).
    meet_deps = appmeta.load_app("meet").dependencies
    assert ("meet", "egress") in [(d.of, d.on) for d in meet_deps]
    assert meet_deps[0].on == "livekit"
    assert {d.on for d in meet_deps} == {"livekit", "egress"}


def test_meet_livekit_shared_rules():
    dep = appmeta.load_app("meet").dependencies[0]
    vars_ = {r["var"] for r in dep.shared}
    assert "st_meet_livekit_turn_domain" in vars_  # provider-only var present
    url_rule = next(
        r for r in dep.shared if r.get("consumer_env_key") == "LIVEKIT_API_URL"
    )
    assert url_rule["consumer_format"] == "wss://{value}"


def test_meet_egress_component_metadata():
    """egress is a full, independently-deployable meet component: deploy_order 15
    (BETWEEN livekit=10 and meet=20), no ``vars`` block, no ``env_render`` (egress
    reads its ansible vars straight from vars.yml/vault.yml). It is NOT core (the meet
    Django backend stays the core). The livekit dependency stays ``dependencies[0]``;
    egress is a SECOND meet dependency appended after it (bundled into the livekit
    bootstrap step on the CLI side, also targetable standalone via ``-c egress``)."""
    meta = appmeta.load_app("meet")
    egress = meta.component("egress")
    assert egress.key == "egress"
    assert egress.role == "suitenumerique.st.meet"
    assert egress.user == "meet"
    assert egress.app_name == "egress"
    assert egress.dir_var == "st_meet_egress_dir"
    assert egress.enabled_var == "st_meet_egress_enabled"
    assert egress.deploy_order == 15
    assert egress.is_core is False
    assert egress.is_worker is False
    # no vars / no env_render: egress is a bare unit (vars written by bootstrap helpers)
    assert meta.component_vars("egress") == {}
    assert meta.env_render_spec("egress") == {}
    # deploy_order sits between livekit (10) and meet (20) so it rolls out after
    # livekit and before the meet core on a `st-cli deploy`.
    orders = {c.key: c.deploy_order for c in meta.components}
    assert orders["livekit"] < orders["egress"] < orders["meet"]
    # the egress dep is shared: [] — egress is fully standalone (no env blob wiring).
    egress_dep = next(d for d in meta.dependencies if d.on == "egress")
    assert egress_dep.shared == []


def test_meet_and_livekit_component_vars_carry_public_host():
    """Both the meet core and the livekit component expose the same
    st_meet_public_host var (=="{DOMAIN}") so writer.apply_component_vars writes
    a single source of truth for the public meet domain into BOTH units' vars.yml
    — the role then derives every public-facing URL (DJANGO_ALLOWED_HOSTS, the
    login/logout redirects, the LiveKit recording webhook, the recordings download
    base) from it. The {DOMAIN} placeholder is re-rendered via str.format —
    single-brace, intentional (unlike the meet core's quadrupled braces which emit
    literal ``{{ }}`` for an Ansible expression)."""
    meta = appmeta.load_app("meet")
    assert meta.component_vars("meet")["st_meet_public_host"] == "{DOMAIN}"
    assert meta.component_vars("livekit")["st_meet_public_host"] == "{DOMAIN}"


def test_transfers_component_metadata():
    """transfers is a django-lasuite app (like drive): a backend core rendering
    both a backend and a frontend env blob, plus a workers component (Celery worker
    with embedded beat). No collabora-style dependencies."""
    meta = appmeta.load_app("transfers")
    core = meta.core()
    assert core.key == "transfers"
    assert core.role == "suitenumerique.st.transfers"
    assert core.user == "transfers"
    assert core.enabled_var == "st_transfers_enabled"
    assert meta.dependencies == []
    # the core renders two env layers (backend + frontend Caddy runtime env)
    spec = meta.env_render_spec("transfers")
    assert spec["backend"]["blob_var"] == "st_transfers_backend_env"
    assert spec["backend"]["templates"] == ["transfers.backend.env.j2"]
    assert spec["frontend"]["blob_var"] == "st_transfers_frontend_env"
    assert spec["frontend"]["templates"] == ["transfers.frontend.env.j2"]
    # public-host var (healthcheck Host header) + run-once migration gate
    cvars = meta.component_vars("transfers")
    assert cvars["st_transfers_public_host"] == "{DOMAIN}"
    assert "ansible_play_hosts_all[0]" in cvars["st_transfers_backend_run_migrations"]
    # workers reuse the core's files/role
    assert meta.files_component("workers").key == "transfers"
    assert meta.worker().is_worker is True


# --------------------------------------------------------------------------- workers component


def test_worker_component_metadata():
    """Each app exposes a first-class workers component (is_worker, app_name, enabled_var)."""
    for app in ("drive", "messages", "meet", "transfers"):
        w = appmeta.load_app(app).worker()
        assert w is not None, f"{app} has no workers component"
        assert w.is_worker is True
        assert w.key == "workers"
        assert (
            w.app_name == "workers"
        )  # matches the role's st_podman_application_name (systemd unit)
        assert w.enabled_var == f"st_{app}_workers_enabled"
        assert w.is_core is False
        # workers reuse the same role + user as the core, with no env_render of their own
        core = appmeta.load_app(app).core()
        assert w.role == core.role
        assert w.user == core.user
        assert appmeta.load_app(app).env_render_spec(w.key) == {}


def test_files_component_worker_resolves_to_core():
    """A worker resolves to the core unit's files; every other component to itself."""
    meta = appmeta.load_app("drive")
    assert meta.files_component("workers").key == "drive"  # worker → core
    assert meta.files_component("drive").key == "drive"  # core → itself
    assert meta.files_component("collabora").key == "collabora"


def test_component_implemented_flag():
    """Component.implemented defaults True; meet's workers is False (no role impl),
    so it is metadata-only — never prompted, never registered as a unit."""
    # dataclass default
    c = Component(
        key="x",
        role="r",
        user="u",
        app_name="x",
        dir_var="d",
        enabled_var="e",
        deploy_order=1,
        is_core=True,
        is_worker=False,
    )
    assert c.implemented is True
    # drive/messages workers omit the key → default True (deployable)
    assert appmeta.load_app("drive").worker().implemented is True
    assert appmeta.load_app("messages").worker().implemented is True
    # meet workers is explicitly flagged False
    assert appmeta.load_app("meet").worker().implemented is False
    # core components are always implemented
    assert appmeta.load_app("meet").core().implemented is True


# --------------------------------------------------------------------------- error surface


def test_load_app_unknown_raises_stclierror():
    """load_app of an unknown app raises StCliError (not FileNotFoundError) so
    main._run surfaces a clean message instead of a traceback."""
    with pytest.raises(StCliError, match=r"unknown app 'bogus'"):
        appmeta.load_app("bogus")


def test_component_unknown_raises_stclierror():
    """AppMeta.component of an unknown key raises StCliError with a
    stale-manifest-friendly message (not a bare KeyError)."""
    meta = appmeta.load_app("meet")
    with pytest.raises(StCliError, match=r"unknown component 'bogus' for app 'meet'"):
        meta.component("bogus")
