# Podman Base Role

The `podman` role is the foundation of every application deployment in this collection. It has two
entrypoints:

- **`main`** (default): installs Podman, creates a Unix user, configures registries, enables the
  user socket
- **`application`** (`tasks_from: application.yml`): deploys a compose-based application as a
  systemd user unit

Application roles (`messages`, `drive`, `keycloak`) call both entrypoints internally; you do not call
them directly. However, you can use the podman role standalone to deploy custom compose stacks.

## What Each Entrypoint Does

### `main` (base setup)

1. **Installs** `podman` and `docker-cli` via apt, and `podman-compose` from a pinned Debian
   package (`st_podman_compose_version`) downloaded from snapshot.debian.org
2. **Creates** a Unix user with home directory and shell
3. **Configures** `containers.conf` (timezone, storage driver)
4. **Enables** lingering so systemd user services survive logout
5. **Starts** the Podman user socket
6. **Logs in** to private container registries (if configured)

> [!NOTE]
> Trixie ships podman-compose 1.3.0, which runs every compose healthcheck through `/bin/sh -c`.
> Version 1.5.0 and later exec a `CMD` list healthcheck directly, so an image without a shell can
> have a healthcheck. The role resolves `st_podman_compose_version` to a file on
> snapshot.debian.org, so an upgrade changes only that variable; Renovate proposes the change. The
> hosts must have HTTPS access to snapshot.debian.org, or `st_podman_compose_deb_url` and
> `st_podman_compose_deb_checksum` must point to a copy of the package.

### `application` (deploy a compose app)

1. **Backs up** the current application directory (if rollback enabled)
2. **Deploys** a systemd user unit from `application_systemd_unit.j2`
3. **Creates** application directories and files (with container UID ownership)
4. **Deploys** the compose template
5. **Starts** and enables the systemd unit
6. **Rolls back** on failure (if enabled)

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 512 MB minimum |
| Disk | 1 GB for Podman + images |
| Ansible | >= 2.18.0 |

## Variable Reference

See [roles/podman/REFERENCE.md](../../roles/podman/REFERENCE.md) for the complete variable reference.

## Standalone Usage

> [!NOTE]
> The podman role is imported automatically by the messages, drive, keycloak and meet roles.
> You only need to call it standalone to deploy custom applications not covered by this
> collection.

The base setup and application deployment are two or more `import_role` calls: one for the base
setup, then as many `tasks_from: application.yml` calls as you need. You can deploy multiple
applications under the same user this way.

```yaml
- hosts: all
  tasks:
    # 1. Base setup: install podman, create user, configure registries
    - ansible.builtin.import_role:
        name: suitenumerique.st.podman
      vars:
        st_podman_user: myapp
        st_podman_uid: 1200

    # 2. Deploy the application, on the same user
    - ansible.builtin.import_role:
        name: suitenumerique.st.podman
        tasks_from: application.yml
      vars:
        st_podman_user: myapp
        st_podman_application_name: myapp
        st_podman_application_compose_template: myapp/compose.yaml.j2
        st_podman_application_files:
          - src: myapp/env.j2
            dest: env

    # 3. For example deploy cadvisor alongside the application
    - ansible.builtin.import_role:
        name: suitenumerique.st.podman
        tasks_from: application.yml
      vars:
        st_podman_user: myapp
        st_podman_application_name: cadvisor
        st_podman_application_compose_template: monitoring/compose_cadvisor.yaml.j2
```

## Base Setup Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_podman_user` | Unix user for rootless Podman | **(required)** |
| `st_podman_home` | Home directory of the user | `/opt/{{ st_podman_user }}` |
| `st_podman_uid` | Custom UID for the user | _(system-assigned)_ |
| `st_podman_tz` | Default timezone for containers | `UTC` |
| `st_podman_registries` | List of private registries to log into | _(none)_ |
| `st_podman_unprivileged_port_start` | Sysctl `net.ipv4.ip_unprivileged_port_start` | _(not set)_ |

## Application Deployment Variables

Used with `tasks_from: application.yml`:

| Variable | Description | Default |
|----------|-------------|---------|
| `st_podman_application_name` | Application name (systemd unit filename) | _(empty)_ |
| `st_podman_application_dir` | Base directory for compose and files | `{{ st_podman_home }}/{{ st_podman_application_name }}` |
| `st_podman_application_compose_template` | Jinja2 template for `compose.yaml` | _(none)_ |
| `st_podman_application_files` | List of files to deploy. Each item has `dest`, and `src` (template) or `content` (inline string). Optional keys: `mode`, `template`, `container_uid`, `container_gid`, `when`, `no_log` | _(none)_ |
| `st_podman_application_secret_files` | List of `{env, src}` or `{env, content}` secret files to write in `secrets/`. The role manages `secrets/` only when this variable is defined | _(none)_ |
| `st_podman_application_secret_files_container_uid` | Owner uid, inside the container, of `secrets/` and of each secret file | `0` |
| `st_podman_application_directories` | List of `{name, mode, container_uid}` dirs to create | _(none)_ |
| `st_podman_application_restart_policy` | Systemd restart policy | `on-abnormal` |
| `st_podman_application_rollback_enabled` | Enable rollback on deployment failure | `false` |
| `st_podman_application_timeout` | Startup timeout in seconds | `300` |

## File Ownership and Container UIDs

Files that need to be readable by container processes (which run under sub-UIDs) require special
handling. The podman role supports a `container_uid` field in `st_podman_application_directories`
and `st_podman_application_files`. When set, the file/directory ownership is adjusted using
`podman unshare` so the container process can access it.

```yaml
st_podman_application_directories:
  - { name: config, container_uid: "11333" }
  - { name: data, container_uid: "999" }
st_podman_application_files:
  - { src: config.j2, dest: config/app.conf, container_uid: "11333" }
```

## Inline Content and Hidden Values

An item of `st_podman_application_files` can have `content` (an inline string) instead of `src`.
Set `no_log: true` on the item to hide its value from the task output and the diff. The default is `false`.

```yaml
st_podman_application_files:
  - { dest: config/token, content: "{{ vault_token }}", mode: "0640", no_log: true }
```

## Secret Files

`st_podman_application_secret_files` writes each item to `secrets/<env>` with mode `0440`.
The `secrets` directory has mode `2750`, or `2770` when the owner uid is not 0.
The role hides the values from the task output.
The role manages `secrets` only when `st_podman_application_secret_files` is defined.
The name `secrets` is reserved. Do not use it in `st_podman_application_directories` or `st_podman_application_files`.

```yaml
st_podman_application_secret_files:
  - env: DB_PASSWORD
    content: "{{ vault_db_password }}"
  - env: JWT_PRIVATE_KEY
    src: jwt_private_key.pem.j2
```

Each item has `env` in `UPPER_SNAKE_CASE`, and exactly one of `src` or `content`. Each `env` must be unique.
The role deletes the other files in `secrets/`. It deletes the directory when the list is empty.
Set `st_podman_application_secret_files_container_uid` to the uid of the container user that reads the files.
The role then sets this uid as the owner of `secrets/` and of each file. The group stays 0.
When the uid is not `0`, the `secrets` directory has mode `2770`, so that the application user can still write in it.
The compose template must mount `./secrets` in the container and set the `<ENV>_FILE` variable.

```yaml
    environment:
      DB_PASSWORD_FILE: /run/secrets/DB_PASSWORD
    volumes:
      - ./secrets:/run/secrets:ro
```

## Troubleshooting

See [troubleshooting.md](../troubleshooting.md) for general debugging commands.
