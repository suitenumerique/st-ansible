# Architecture

This collection deploys **La Suite Territoriale** applications on Debian systems using **rootless Podman** containers
managed by **systemd user units**.

## The Podman Rootless Model

All containers run under an unprivileged Unix user, never as root. Each application role creates a dedicated system user
(e.g. `messages`, `drive`, `keycloak`) and uses Podman's rootless mode to run containers under that user.

Rootless Podman means:

- Containers cannot escalate to host root
- Each user has their own Podman socket and storage
- Systemd user units manage the container lifecycle (start/stop/restart)
- Lingering is enabled so user services survive logout

## Role Hierarchy

```text
podman (base role)
├── messages
│   ├── messages (web app)
│   ├── workers (Celery workers)
│   ├── mta-in (inbound mail transfer agent)
│   ├── socks-proxy
│   └── mpa (Mail Processing Agent, rspamd stack)
├── drive
│   ├── drive (web app)
│   ├── workers (Celery workers)
│   └── collabora (document editor)
├── keycloak
│   └── keycloak (identity provider)
└── meet
    ├── meet (video conferencing app)
    └── livekit (LiveKit server, host network)

alloy (telemetry, standalone)
restic (backup, standalone)
```

The **podman** role is the foundation. Every application role (`messages`, `drive`, `keycloak`, `meet`) imports it via
`import_role` to set up the Unix user, install Podman, configure registries, and create the systemd unit. You don't
need to call `podman` directly in a playbook; it is invoked implicitly by the application roles.

The **alloy** and **restic** roles are standalone. They do not use the podman role and manage their own users and
systemd system services.

## Role Privilege Model

Each role splits into two phases with different privilege requirements:

- **Base podman installation**: installs packages, creates the Unix user, configures sysctl. Requires root access.
- **Application deployment** (`tasks/deploy.yml`): renders templates, creates directories, starts
  systemd user units. Runs entirely as the application user and does **not** need root.

This means you can optionally split your playbook: run the base installation once from a privileged account,
then delegate `tasks_from: deploy.yml` to an unprivileged user (e.g. a CI deploy user) for ongoing
application updates.

```yaml
# Privileged: one-time base setup
# Nothing is enabled, this only installs the messages user and the podman base
- hosts: all
  become: true
  tasks:
    - ansible.builtin.import_role:
        name: suitenumerique.st.messages

# Unprivileged: deploy/update applications without root
- hosts: all
  become: true
  become_user: messages
  become_method: ansible.builtin.sudo
  become_flags: "-i"
  tasks:
    - ansible.builtin.import_role:
        name: suitenumerique.st.messages
        tasks_from: deploy.yml
      vars:
        st_messages_enabled: true
        st_messages_tag: v1.0.0
```

This is completely optional and you can deploy everything within a single task, but it has to run as root.

```yaml
- hosts: all
  become: true
  tasks:
    - ansible.builtin.import_role:
        name: suitenumerique.st.messages
      vars:
        st_messages_enabled: true
        st_messages_tag: v1.0.0
```

## Container Lifecycle

Each application is deployed as a **systemd user unit** named after the application
(e.g. `messages.service`, `mpa.service`, `keycloak.service`).

```ini
[Unit]
Wants=network-online.target podman.socket
After=network-online.target podman.socket

[Service]
Type=notify
ExecStartPre=-podman-compose pull
ExecStart=<application_name>.sh
ExecStop=podman-compose stop
ExecStopPost=podman-compose down
```

The unit file is generated from `roles/podman/templates/application_systemd_unit.j2` and placed under `~/.config/systemd/user/<application_name>.service`.

## Networking

Most application stacks use **Podman bridge networking**: containers within a compose project communicate via
DNS-resolvable service names. Only the frontend/proxy service publishes ports to the host.

Exceptions:

- **socks-proxy** uses `network_mode: host` (needs full host network access)
- **LiveKit** uses `network_mode: host` (WebRTC requires a wide UDP port range)

## Data Persistence

Application data is stored in **bind-mounted directories** under the application directory
(e.g. `/opt/messages/mpa/rspamd_data`). These directories are pre-created with ownership set to the container UID
(using `podman unshare` when needed), so the container process can read/write its data.

## Rollback

When `rollback_enabled` is set to `true` for an application, the podman role:

1. Backs up the current application directory before deployment
2. If the deployment fails, restores the backup and restarts the service
3. If the deployment succeeds, cleans up the backup

This mimics traditional PaaS deployments and ensures single-host environments aren't bricked after a failed deployment.
For multi-host environments we suggest to use `serial: 1` and to only enable the rollback on the first host :

```plain
# Scenario when 1st host breaks
messages1 (v1→ v2) breaks, rollback to v1,
→ messages2 untouched,
→ both on version v1, no interruption

# Scenario when 2nd host breaks
messages1 (v1→ v2) works,
→ messages2 (v1→ v2) breaks, rollback not configured on messages2
→ messages1 works with version v2, no interruption
→ debug messages2 manually
```

## Variable Naming

All role variables follow the prefix convention `st_<role>_<variable>`
(e.g. `st_messages_tag`, `st_drive_public_host`, `st_keycloak_port`).
