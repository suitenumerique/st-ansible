# Deployment

This guide covers deploying multiple La Suite Territoriale roles together on one or more hosts.

## Installing the Collection

Add a `galaxy-requirements.yml` file:

```yaml
collections:
  - name: https://github.com/suitenumerique/st-ansible.git
    type: git
    version: "1"
```

Then install:

```bash
ansible-galaxy collection install -r galaxy-requirements.yml
```

## Multi-Host Deployment

Split roles across hosts by targeting different inventory groups:

```yaml
- hosts: keycloak_servers
  become: true
  tasks:
    - ansible.builtin.import_role:
        name: suitenumerique.st.keycloak
      vars:
        st_keycloak_enabled: true

- hosts: messages_servers
  become: true
  tasks:
    - ansible.builtin.import_role:
        name: suitenumerique.st.messages
      vars:
        st_messages_enabled: true
        st_messages_mpa_enabled: true
        st_messages_mta_in_enabled: true

- hosts: drive_servers
  become: true
  tasks:
    - ansible.builtin.import_role:
        name: suitenumerique.st.drive
      vars:
        st_drive_enabled: true
        st_drive_collabora_enabled: true
```

See [docs/00-examples/full-high-availability](docs/00-examples/full-high-availability) for a full example.

## Single-Host Deployment

> [!CAUTION]
> We do not recommend using a single-host deployment for production.
> See Multi-Host Deployment instead or take a look at PaaS providers such as [Scalingo](https://scalingo.com).

```yaml
- hosts: suite_territoriale
  become: true
  tasks:
    - ansible.builtin.import_role:
        name: suitenumerique.st.keycloak
      vars:
        st_keycloak_enabled: true
        st_keycloak_env: |
          KC_DB=postgres
          KC_DB_URL=jdbc:postgresql://db.example.com/keycloak
          # ... more Keycloak env vars

    - ansible.builtin.import_role:
        name: suitenumerique.st.messages
      vars:
        st_messages_enabled: true
        st_messages_backend_env: |
          DATABASE_URL=postgres://user:pass@db.example.com/messages
          # ... more messages env vars

    - ansible.builtin.import_role:
        name: suitenumerique.st.drive
      vars:
        st_drive_enabled: true
        st_drive_public_host: drive.example.com
        st_drive_backend_env: |
          DATABASE_URL=postgres://user:pass@db.example.com/drive
          # ... more drive env vars
```

### Ports and UID Conflicts

Each role creates its Unix user and binds its host ports on a **distinct default** so that
multiple roles can run **on the same host** without conflicting. The defaults are:

| Role | UID / GID | Frontend port |
|------|-----------|---------------|
| `drive` | `1101` | `50100` |
| `keycloak` | `1102` | `50200` |
| `meet` | `1103` | `50300` |
| `messages` | `1104` | `50400` |

Each role owns a `50<n>00`–`50<n>99` port block (where `<n>` is the role's index — drive
`1`, keycloak `2`, meet `3`, messages `4`). The frontend sits at `50<n>00`, auxiliary
services increment from there (e.g. `messages` mpa/rspamd on `50402`–`50404`, mta-in on
`50425`), and cAdvisor is
pinned at `50<n>99`. This layout leaves room to grow: an 11th–20th role would carry into
`51<n>00` (and UIDs into `111<n>`). You can still override any `st_<role>_uid` or
`st_<role>_port` to fit your own numbering scheme:

```yaml
st_keycloak_uid: 1102
st_keycloak_port: 50200
```

> [!NOTE]
> **Exception — LiveKit:** the `livekit` sub-app of `meet` does **not** follow this scheme.
> It runs with `network_mode: host` and binds fixed ports (7880/7881/5349/3478 and UDP
> 50000-60000) directly on the host, so it must run on a dedicated host with a dedicated
> public IP. See [../05-meet/02-livekit.md](../05-meet/02-livekit.md).

## Customizing Deployments

Each application role exposes variables to override the default compose template and inject
additional files or directories. This is useful when you need to add sidecar containers, mount
extra config files, or create data directories with specific ownership.

### Compose Template

Every sub-app has a `st_<role>_<subapp>_compose_template` variable that points to a Jinja2
template for the `compose.yaml` file. The default templates are in the role's `templates/`
directory. To use a custom template, set the variable to the path of your template relative to
your project's `templates/` directory (or an absolute path):

```yaml
st_messages_compose_template: my-custom/compose.yaml.j2
st_drive_compose_template: my-custom/drive-compose.yaml.j2
st_keycloak_compose_template: my-custom/keycloak-compose.yaml.j2
```

### Application Files

The `st_podman_application_files` variable is a list of `{src, dest}` entries that get rendered
as Jinja2 templates and placed in the application directory. Each application role already sets
this internally (for env files, config files, etc.). If you need to add extra files, you can
extend the list in your playbook:

```yaml
- ansible.builtin.import_role:
    name: suitenumerique.st.messages
  vars:
    st_messages_enabled: true
    st_messages_compose_template: my-custom/compose.yaml.j2
    st_podman_application_files:
      - src: my-custom/backend_env.j2
        dest: backend_env
      - src: my-custom/frontend_env.j2
        dest: frontend_env
      - src: my-custom/extra-config.conf.j2
        dest: extra-config.conf
```

> [!WARNING]
> When you set `st_podman_application_files` in your playbook, it **replaces** the default
> file list set by the role. Make sure to include all the files the role expects (env files,
> config files) alongside your additions.

### Application Directories

The `st_podman_application_directories` variable creates additional directories in the
application directory. Each entry can specify a `name`, `mode`, and `container_uid`:

```yaml
st_podman_application_directories:
  - name: config
    mode: "0750"
  - name: data
    container_uid: "999"
```

The `container_uid` field uses `podman unshare` to set ownership so the container process
(running under a sub-UID) can read and write to the directory.

### Putting It Together

A full example with a custom compose template, extra files, and directories:

```yaml
- ansible.builtin.import_role:
    name: suitenumerique.st.messages
  vars:
    st_messages_enabled: true
    st_messages_compose_template: my-custom/compose.yaml.j2
    st_podman_application_files:
      - src: my-custom/backend_env.j2
        dest: backend_env
      - src: my-custom/frontend_env.j2
        dest: frontend_env
      - src: my-custom/extra-config.conf.j2
        dest: extra-config.conf
        container_uid: "1100"
    st_podman_application_directories:
      - name: config
      - name: data
        container_uid: "1100"
```

## Adding Monitoring

Deploy Alloy and cAdvisor alongside your applications:

```yaml
- ansible.builtin.import_role:
    name: suitenumerique.st.messages
  vars:
    st_messages_enabled: true
    st_messages_cadvisor_enabled: true

- ansible.builtin.import_role:
    name: suitenumerique.st.alloy
  vars:
    st_alloy_config_template: alloy/config.alloy.j2
```

See [docs/monitoring](docs/monitoring) for a examples of alloy configurations.

## Adding Backups

```yaml
- ansible.builtin.import_role:
    name: suitenumerique.st.messages
  vars:
    st_messages_mpa_enabled: true
    # ...

- ansible.builtin.import_role:
    name: suitenumerique.st.restic
  vars:
    restic_repository: s3:https://s3.example.com/backups
    restic_password: "{{ vault_restic_password }}"
    restic_s3_access_key: "{{ vault_s3_access_key }}"
    restic_s3_secret_key: "{{ vault_s3_secret_key }}"
    restic_files:
      - /opt/messages
```
