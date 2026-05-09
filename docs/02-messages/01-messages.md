# Messages Application

The `messages` role deploys the [Messages](https://github.com/suitenumerique/messages) web application from
La Suite Territoriale, a messaging platform for French territorial collectivities.

The role deploys multiple independent sub-applications under the `messages` Unix user, each as a separate systemd
user unit. All sub-apps are disabled by default and must be explicitly enabled.

> [!NOTE]
> This collection does not provision PostgreSQL, Redis, S3 or OpenSearch. You must provide
> these externally before deploying Messages. We recommend using managed database services from
> cloud providers such as [Scalingo](https://scalingo.com) or [Scaleway](https://www.scaleway.com).

## Sub-Applications

| Sub-App | Description | Doc |
|---------|-------------|-----|
| **messages** | Core web application (frontend + backend) | This page |
| **workers** | Celery background workers | [02-workers.md](02-workers.md) |
| **mta-in** | Inbound mail transfer agent (Postfix) | [03-mta-in.md](03-mta-in.md) |
| **socks-proxy** | SOCKS proxy | [04-socks-proxy.md](04-socks-proxy.md) |
| **mpa** | Mail Processing Agent (rspamd + clamav + valkey) | [05-mpa.md](05-mpa.md) |

## Container Stack

```text
messages-frontend (ghcr.io/suitenumerique/messages-frontend)
  └── depends on → messages-backend (ghcr.io/suitenumerique/messages-backend)
```

The frontend serves the web UI on port 8080, proxying API calls to the backend.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 1 GB minimum |
| Disk | 2 GB for images + data |
| Database | External PostgreSQL (configured via env) |
| Redis | External Redis (configured via env) |
| Identity | Keycloak (see [04-keycloak](../04-keycloak/01-keycloak.md)) |
| S3 Storage | S3-compatible object storage for mailbox imports |

## Variable Reference

See [roles/messages/REFERENCE.md](../../roles/messages/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_messages_enabled` | Enable the messages app | `false` |
| `st_messages_tag` | Docker image tag | `main` |
| `st_messages_dir` | Application directory | `/opt/messages/messages` |
| `st_messages_uid` | Unix UID for the messages user | `1100` |
| `st_messages_port` | Host port for frontend | `50080` |
| `st_messages_backend_env` | Backend environment content | _(empty)_ |
| `st_messages_frontend_env` | Frontend environment content | _(empty)_ |
| `st_messages_backend_run_migrations` | Run Django migrations on deploy | `true` |
| `st_messages_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_messages_port` | `50080` | 8080 |

The backend is not published to the host. It's only reachable from the frontend via the Podman
bridge network.

## Data & Volumes

The messages application is stateless: it stores data in an external PostgreSQL database and uses S3-compatible storage
for media files. No persistent bind mounts are needed for the core application.

## Database Migrations

The role runs `python manage.py migrate` via `podman-compose run` after deployment. This task has `run_once: true`,
which limits execution to one host per Ansible batch. When `serial:` is used, each host is its own batch, so
`run_once: true` does **not** prevent migrations from running on every host. Set `st_messages_backend_run_migrations: false`
on all hosts except the first to avoid duplicate migrations. For example :

```yaml
st_messages_backend_run_migrations: "{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}"
```

## Custom Environment

The backend and frontend environment files are generated from Jinja2 templates. You can either:

- Set `st_messages_backend_env` / `st_messages_frontend_env` to provide the entire env content inline
- Set `st_messages_backend_env_template` / `st_messages_frontend_env_template` to use a custom template file

## Troubleshooting

```bash
ssh <host>
sudo -iu messages

# Service lifecycle
systemctl --user status messages.service
systemctl --user start messages.service
systemctl --user stop messages.service

# Logs
journalctl --user -u messages.service -f
journalctl --user -u messages.service --since today
journalctl --user -u messages.service --since "3 hours ago"

# Containers
podman-compose -f /opt/messages/messages/compose.yaml ps
```
