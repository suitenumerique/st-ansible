"""Tests for the direct-SSH remote commands (st_cli.cmd.remote)."""

from __future__ import annotations

import shlex
import subprocess
import threading
import types

import pytest
from helpers import seed_creds, seed_drive_unit

from st_cli.cmd import remote
from st_cli.core import generate, manifest, runner, tree
from st_cli.core.errors import StCliError
from st_cli.core.models import StCliManifest, UnitState


def _drive_core(repo, hosts=("10.0.0.1",)):
    seed_creds(repo)
    tree.write_hosts("drive", "prod", "drive", "drive", list(hosts))
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("drive", "prod", "drive", "managed")]
        )
    )


# ------------------------------------------------------------- worker target resolution


def test_remote_worker_resolves_to_core_host_and_worker_unit(repo):
    """`st-cli remote` targets the core's host but the worker's own systemd unit +
    dir."""
    seed_drive_unit(repo, components=("drive", "workers"))

    t = remote.resolve_target("drive", "prod", "workers")
    assert t.host == "10.0.0.1"  # core's host (worker owns no hosts file)
    assert t.app_name == "workers"  # role systemd --user unit name
    assert t.remote_dir == "/opt/drive/workers"  # falls back to /opt/<user>/<app_name>


def test_remote_worker_resolves_to_workers_host_when_group_present(repo):
    """A worker's own `[workers]` inventory group overrides the core-host fallback."""
    seed_drive_unit(
        repo,
        components=("drive", "workers"),
        groups={"drive": ["10.0.0.1"], "workers": ["10.0.0.2"]},
    )

    t = remote.resolve_target("drive", "prod", "workers")
    assert t.host == "10.0.0.2"  # the [workers] host, not the core's
    assert t.app_name == "workers"  # still the worker's own systemd unit
    assert (
        t.remote_dir == "/opt/drive/workers"
    )  # core vars reused, worker dir_var/app_name
    # the core unit itself still resolves to its own host
    assert remote.resolve_target("drive", "prod", "drive").host == "10.0.0.1"


# --------------------------------------------------------------------------- oneoff


def test_oneoff_entrypoint_override(repo, monkeypatch):
    """`oneoff(entrypoint=...)` inserts `--entrypoint <ep>` before the service name."""
    _drive_core(repo)

    captured: dict = {}
    monkeypatch.setattr(
        remote,
        "_ssh",
        lambda host, remote_cmd, **kw: captured.update(cmd=remote_cmd) or 0,
    )

    remote.oneoff("drive", "prod", "drive", service="collabora", entrypoint="sh")
    assert "podman-compose run --rm --entrypoint sh collabora sh" in captured["cmd"]

    remote.oneoff("drive", "prod", "drive", service="backend")
    assert "podman-compose run --rm backend sh" in captured["cmd"]
    assert "--entrypoint" not in captured["cmd"]


# --------------------------------------------------------------------------- ssh user


def test_ssh_bare_host_when_user_unset(repo, monkeypatch):
    """With ST_CLI_SSH_USER unset, `_ssh` targets a bare host so the ssh config chain
    supplies the user."""
    _drive_core(repo)
    monkeypatch.delenv("ST_CLI_SSH_USER", raising=False)

    captured: dict = {}
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda *a, **k: (
            captured.update(argv=a[0]) or types.SimpleNamespace(returncode=0)
        ),
    )

    rc = remote._ssh("10.0.0.9", "echo hi", interactive=False)
    assert rc == 0
    target = captured["argv"][-2]  # host arg (last is the remote cmd)
    assert target == "10.0.0.9"
    assert "@" not in target


def test_ssh_user_at_host_when_user_set(repo, monkeypatch):
    """With ST_CLI_SSH_USER set, _ssh targets 'user@host'."""
    _drive_core(repo)
    monkeypatch.setenv("ST_CLI_SSH_USER", "bob")

    captured: dict = {}
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda *a, **k: (
            captured.update(argv=a[0]) or types.SimpleNamespace(returncode=0)
        ),
    )

    remote._ssh("10.0.0.9", "echo hi", interactive=False)
    target = captured["argv"][-2]
    assert target == "bob@10.0.0.9"


def test_ssh_passes_loglevel_error(repo, monkeypatch):
    """Every `_ssh` call carries `-o LogLevel=ERROR` to trim client host-key
    warnings."""
    _drive_core(repo)
    monkeypatch.delenv("ST_CLI_SSH_USER", raising=False)

    captured: dict = {}
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda *a, **k: (
            captured.update(argv=a[0]) or types.SimpleNamespace(returncode=0)
        ),
    )

    remote._ssh("10.0.0.9", "echo hi", interactive=False)
    argv = captured["argv"]
    assert "-o" in argv and "LogLevel=ERROR" in argv
    assert argv[-2] == "10.0.0.9" and argv[-1] == "echo hi"


def test_ssh_capture_stderr_hidden_on_success_shown_on_failure(repo, monkeypatch):
    """`_ssh(capture_stderr=True)` discards buffered stderr on success and replays it on
    failure."""
    monkeypatch.setattr(remote.sshuser, "ensure_ssh_user", lambda hosts: None)

    warns: list[str] = []
    monkeypatch.setattr(remote.ui, "warn", lambda msg: warns.append(msg))

    captured: dict = {}

    # success: stderr buffered (PIPE) but NOT replayed
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=0, stderr="Authorized users ONLY.\n"
        ),
    )
    remote._ssh("10.0.0.9", "podman ps -a", interactive=False, capture_stderr=True)
    assert warns == []  # banner discarded on success

    # failure: buffered stderr replayed
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda *a, **k: (
            captured.update(k)
            or types.SimpleNamespace(returncode=1, stderr="boom: no such container\n")
        ),
    )
    remote._ssh("10.0.0.9", "podman ps -a", interactive=False, capture_stderr=True)
    assert captured.get("stderr") is subprocess.PIPE  # stderr was captured
    assert "stdout" not in captured  # stdout left live (inherited)
    assert any("boom: no such container" in w for w in warns)


# --------------------------------------------------------------------------- logs


def test_logs_default_and_follow(repo, monkeypatch):
    """`logs()` shells out to `journalctl --user -u <unit>` and appends `-f` only when
    follow=True."""
    _drive_core(repo)

    captured: dict = {}
    monkeypatch.setattr(
        remote,
        "_ssh",
        lambda host, remote_cmd, **kw: captured.update(cmd=remote_cmd) or 0,
    )

    remote.logs("drive", "prod", "drive")
    assert "journalctl --user -u drive --since" in captured["cmd"]
    assert "15 min ago" in captured["cmd"]
    assert not captured["cmd"].rstrip("'").endswith(" -f")

    remote.logs("drive", "prod", "drive", follow=True)
    assert "journalctl --user -u drive --since" in captured["cmd"]
    assert "15 min ago" in captured["cmd"]
    assert captured["cmd"].rstrip("'").endswith(" -f")

    remote.logs("drive", "prod", "drive", since="2 hours ago")
    assert "journalctl --user -u drive --since" in captured["cmd"]
    assert "2 hours ago" in captured["cmd"]


def test_logs_since_is_shell_safe(repo, monkeypatch):
    """An untrusted `--since` value cannot break out of the `bash -lc` script."""
    _drive_core(repo)

    captured: dict = {}
    monkeypatch.setattr(
        remote,
        "_ssh",
        lambda host, remote_cmd, **kw: captured.update(cmd=remote_cmd) or 0,
    )

    payload = "x'; touch pwned; echo '"
    remote.logs("drive", "prod", "drive", since=payload)
    # captured cmd is `sudo -iu <user> bash -lc <inner>`; peel back the two
    # quoting layers and confirm the payload is one journalctl arg, not code.
    inner = shlex.split(shlex.split(captured["cmd"])[-1])
    assert payload in inner  # survived as ONE token
    assert "touch" not in inner  # nothing injected


def test_logs_host_selection(repo, monkeypatch):
    """`logs -H <alias>` targets that host; an unknown alias raises."""
    seed_creds(repo)
    tree.write_hosts("drive", "prod", "drive", "drive", ["10.0.0.1", "10.0.0.2"])
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("drive", "prod", "drive", "managed")]
        )
    )

    captured: dict = {}
    monkeypatch.setattr(
        remote,
        "_ssh",
        lambda host, remote_cmd, **kw: captured.update(host=host, cmd=remote_cmd) or 0,
    )

    # a) explicit alias: ssh targets its ip (drive2 = 10.0.0.2)
    remote.logs("drive", "prod", "drive", host="drive2")
    assert captured["host"] == "10.0.0.2"

    # b) a raw ip is NOT an alias: raises (can't reach a host from another env)
    with pytest.raises(StCliError):
        remote.logs("drive", "prod", "drive", host="10.0.0.2")
    # c) unknown alias raises too
    with pytest.raises(StCliError):
        remote.logs("drive", "prod", "drive", host="nope1")

    # d) interactive pick: TTY + questionary.select returns the "alias (ip)" label
    monkeypatch.setattr(remote.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "questionary.select",
        lambda *a, **k: types.SimpleNamespace(ask=lambda: "drive2 (10.0.0.2)"),
    )
    remote.logs("drive", "prod", "drive")
    assert captured["host"] == "10.0.0.2"


# ----------------------------------------------------------------- reset host selection


def test_reset_host_selection(repo, monkeypatch):
    """`reset -H <alias>` targets that host; a multi-host unit needs `--host` off a
    TTY."""
    seed_creds(repo)
    tree.write_hosts("drive", "prod", "drive", "drive", ["10.0.0.1", "10.0.0.2"])
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("drive", "prod", "drive", "managed")]
        )
    )

    captured: dict = {}
    monkeypatch.setattr(
        remote,
        "_ssh",
        lambda host, remote_cmd, **kw: captured.update(host=host, cmd=remote_cmd) or 0,
    )
    # reset redeploys via generate/runner (imported lazily INSIDE reset); patch
    # the module attributes so the late `from ..core import generate` picks up the
    # no-ops (runner is imported at module top).
    monkeypatch.setattr(generate, "generate_all", lambda *a, **k: None)
    monkeypatch.setattr(runner, "galaxy_install", lambda *a, **k: 0)
    monkeypatch.setattr(runner, "play", lambda *a, **k: 0)

    # a) explicit alias: the teardown _ssh call targets its ip
    remote.reset("drive", "prod", "drive", assume_yes=True, host="drive2")
    assert captured["host"] == "10.0.0.2"

    # b) non-TTY + no host on a multi-host unit raises (no silent hosts[0])
    monkeypatch.setattr(remote.sys.stdin, "isatty", lambda: False)
    with pytest.raises(StCliError):
        remote.reset("drive", "prod", "drive", assume_yes=True)


def test_reset_redeploys_only_the_selected_host(repo, monkeypatch):
    """`reset -H <alias>` redeploys with that alias as the ansible `--limit`."""
    seed_creds(repo)
    tree.write_hosts("drive", "prod", "drive", "drive", ["10.0.0.1", "10.0.0.2"])
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("drive", "prod", "drive", "managed")]
        )
    )

    monkeypatch.setattr(remote, "_ssh", lambda *a, **k: 0)
    monkeypatch.setattr(generate, "generate_all", lambda *a, **k: None)
    monkeypatch.setattr(runner, "galaxy_install", lambda *a, **k: 0)
    play_calls: list[dict] = []
    monkeypatch.setattr(runner, "play", lambda *a, **k: play_calls.append(k) or 0)

    remote.reset("drive", "prod", "drive", assume_yes=True, host="drive2")

    assert play_calls[0]["limit"] == "drive2"


# ------------------------------------------------------------------- restart (ssh loop)


def test_restart_loops_all_hosts(repo, monkeypatch):
    """A bare `restart -y` loops `systemctl --user restart <unit>` over every host."""
    _drive_core(repo, hosts=("10.0.0.1", "10.0.0.2"))

    calls: list = []
    monkeypatch.setattr(
        remote,
        "_ssh",
        lambda host, remote_cmd, **kw: calls.append((host, remote_cmd)) or 0,
    )

    remote.restart("drive", "prod", assume_yes=True)
    hosts = [h for h, _ in calls]
    assert hosts == ["10.0.0.1", "10.0.0.2"]  # looped, in inventory order
    assert all("systemctl --user restart drive" in cmd for _, cmd in calls)


def test_restart_host_narrows_to_one(repo, monkeypatch):
    """`restart -c drive -H <alias>` ssh-es only that host; a bad alias raises."""
    _drive_core(repo, hosts=("10.0.0.1", "10.0.0.2"))

    calls: list = []
    monkeypatch.setattr(
        remote,
        "_ssh",
        lambda host, remote_cmd, **kw: calls.append((host, remote_cmd)) or 0,
    )

    remote.restart("drive", "prod", ["drive"], host="drive2")
    assert [h for h, _ in calls] == ["10.0.0.2"]

    with pytest.raises(StCliError):
        remote.restart("drive", "prod", ["drive"], host="nope1")


def test_restart_all_confirmation(repo, monkeypatch):
    """Bare restart warns + confirms; declining aborts, -y skips, non-TTY raises."""
    _drive_core(repo, hosts=("10.0.0.1",))
    calls: list = []
    monkeypatch.setattr(
        remote, "_ssh", lambda host, remote_cmd, **kw: calls.append(host) or 0
    )
    monkeypatch.setattr(remote.sys.stdin, "isatty", lambda: True)

    # decline: nothing restarts
    monkeypatch.setattr(
        "questionary.confirm", lambda *a, **k: types.SimpleNamespace(ask=lambda: False)
    )
    remote.restart("drive", "prod")
    assert calls == []

    # accept: restarts
    monkeypatch.setattr(
        "questionary.confirm", lambda *a, **k: types.SimpleNamespace(ask=lambda: True)
    )
    remote.restart("drive", "prod")
    assert calls == ["10.0.0.1"]

    # non-TTY without -y raises rather than prompting
    monkeypatch.setattr(remote.sys.stdin, "isatty", lambda: False)
    with pytest.raises(StCliError):
        remote.restart("drive", "prod")


def test_restart_parallel_fans_out_components_host_serial(repo, monkeypatch):
    """`-p` restarts components concurrently while each component's hosts stay
    serial."""
    seed_drive_unit(
        repo,
        components=("drive", "collabora"),
        component_hosts={
            "drive": ["10.0.0.1", "10.0.0.2"],
            "collabora": ["10.0.0.3", "10.0.0.4"],
        },
    )
    monkeypatch.setenv("ST_CLI_SSH_USER", "deployer")  # ensure_ssh_user is a fast no-op

    calls: list[tuple[str, str]] = []
    lock = threading.Lock()

    def _record(host, remote_cmd, **kw):
        with lock:
            calls.append((host, remote_cmd))
        return 0

    monkeypatch.setattr(remote, "_ssh", _record)

    remote.restart("drive", "prod", assume_yes=True, parallel=True)

    # every host was hit exactly once
    assert {h for h, _ in calls} == {"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"}
    # within each component, hosts are restarted in inventory order (serial)
    drive_hosts = [h for h, c in calls if "systemctl --user restart drive" in c]
    collabora_hosts = [h for h, c in calls if "systemctl --user restart collabora" in c]
    assert drive_hosts == ["10.0.0.1", "10.0.0.2"]
    assert collabora_hosts == ["10.0.0.3", "10.0.0.4"]


def test_restart_parallel_aggregates_failures(repo, monkeypatch):
    """`-p` does not fail fast: a failing host is recorded and the rest still run."""
    seed_drive_unit(
        repo,
        components=("drive", "collabora"),
        component_hosts={
            "drive": ["10.0.0.1", "10.0.0.2"],
            "collabora": ["10.0.0.3", "10.0.0.4"],
        },
    )
    monkeypatch.setenv("ST_CLI_SSH_USER", "deployer")

    calls: list[str] = []
    lock = threading.Lock()

    def _flaky(host, remote_cmd, **kw):
        with lock:
            calls.append(host)
        return 1 if host == "10.0.0.3" else 0  # collabora1 fails

    monkeypatch.setattr(remote, "_ssh", _flaky)

    with pytest.raises(StCliError) as exc:
        remote.restart("drive", "prod", assume_yes=True, parallel=True)

    assert "collabora@collabora1" in str(exc.value)
    # aggregate, not fail-fast: every host was still attempted (both drive hosts
    # AND the failing component's second host 10.0.0.4)
    assert set(calls) == {"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"}


def test_restart_parallel_prewarms_ssh_user(repo, monkeypatch):
    """`-p` pre-warms the ssh-user guard on the main thread before fanning out."""
    _drive_core(repo)

    guard_calls: list[list[str]] = []
    monkeypatch.setattr(
        remote.sshuser, "ensure_ssh_user", lambda hosts: guard_calls.append(list(hosts))
    )
    monkeypatch.setattr(remote, "_ssh", lambda *a, **k: 0)

    remote.restart("drive", "prod", assume_yes=True, parallel=True)

    assert guard_calls  # called at least once
    assert guard_calls[0] == ["10.0.0.1"]  # first host ip, on the main thread


def test_restart_ssh_quiet_discards_output(repo, monkeypatch):
    """`_ssh(quiet=True)` discards both ssh stdout and stderr."""
    monkeypatch.setattr(remote.sshuser, "ensure_ssh_user", lambda hosts: None)

    captured: dict = {}

    def _fake(*a, **k):
        captured.clear()
        captured.update(k)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(remote.subprocess, "run", _fake)

    remote._ssh("10.0.0.9", "echo hi", interactive=False, quiet=True)
    assert captured.get("stdout") is subprocess.DEVNULL
    assert captured.get("stderr") is subprocess.DEVNULL

    remote._ssh("10.0.0.9", "echo hi", interactive=False, quiet=False)
    assert "stdout" not in captured
    assert "stderr" not in captured


def test_restart_reports_success_per_component(repo, monkeypatch, capfd):
    """Off a TTY, restart's per-component reporter prints plain success lines."""
    seed_drive_unit(
        repo,
        components=("drive", "collabora"),
        component_hosts={
            "drive": ["10.0.0.1", "10.0.0.2"],
            "collabora": ["10.0.0.3", "10.0.0.4"],
        },
    )
    monkeypatch.setenv("ST_CLI_SSH_USER", "deployer")  # ensure_ssh_user is a fast no-op
    monkeypatch.setattr(remote, "_ssh", lambda *a, **k: 0)

    remote.restart("drive", "prod", assume_yes=True, parallel=True)

    out = capfd.readouterr().out
    assert "Successfully restarted drive" in out
    assert "Successfully restarted collabora" in out


def test_restart_failure_hint_mentions_logs(repo, monkeypatch):
    """A failed host's aggregated error names it and points at `st-cli logs`."""
    seed_drive_unit(
        repo,
        components=("drive", "collabora"),
        component_hosts={
            "drive": ["10.0.0.1", "10.0.0.2"],
            "collabora": ["10.0.0.3", "10.0.0.4"],
        },
    )
    monkeypatch.setenv("ST_CLI_SSH_USER", "deployer")
    monkeypatch.setattr(
        remote, "_ssh", lambda host, *a, **k: 1 if host == "10.0.0.3" else 0
    )

    with pytest.raises(StCliError) as exc:
        remote.restart("drive", "prod", assume_yes=True, parallel=True)

    msg = str(exc.value)
    assert "collabora@collabora1" in msg
    assert "st-cli logs" in msg


# ------------------------------------------------------------------------ ps (ssh loop)


def test_ps_loops_hosts_and_skips_workers(repo, monkeypatch):
    """`ps` runs `podman ps -a` per host and skips `is_worker` components."""
    seed_drive_unit(
        repo, hosts=["10.0.0.1", "10.0.0.2"], components=("drive", "workers")
    )

    calls: list = []
    monkeypatch.setattr(
        remote,
        "_ssh",
        lambda host, remote_cmd, **kw: calls.append((host, remote_cmd, kw)) or 0,
    )

    remote.ps("drive", "prod")
    # only the core's two hosts (workers share them and are skipped)
    assert [h for h, _, _ in calls] == ["10.0.0.1", "10.0.0.2"]
    assert all("podman ps -a" in cmd for _, cmd, _ in calls)
    # ps captures ssh stderr so the banner/motd is hidden on success
    assert all(kw.get("capture_stderr") for _, _, kw in calls)


def test_ps_header_is_compact(repo, monkeypatch, capfd):
    """`ps` prints a compact `<unit> on <host>` header."""
    seed_creds(repo)
    manifest.save_manifest(
        StCliManifest(
            "0.0.19", "0.0.19", [UnitState("drive", "prod", "drive", "managed")]
        )
    )
    tree.write_hosts("drive", "prod", "drive", "drive", ["10.0.0.1"])
    monkeypatch.setattr(remote, "_ssh", lambda *a, **k: 0)

    remote.ps("drive", "prod")
    out = capfd.readouterr().out
    assert "drive on 10.0.0.1" in out  # compact, host = ansible_host ip
    assert "podman ps -a for" not in out  # the old verbose header is gone
    assert "(user drive)" not in out
