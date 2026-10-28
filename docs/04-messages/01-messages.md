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
| **mta-in** | Inbound mail transfer agent (Postfix, DEPRECATED) | [03-mta-in.md](03-mta-in.md) |
| **socks-proxy** | SOCKS proxy | [04-socks-proxy.md](04-socks-proxy.md) |
| **mpa** | Mail Processing Agent (rspamd + clamav or exav + valkey) | [05-mpa.md](05-mpa.md) |
| **pymta** | Inbound mail transfer agent (Python) | [06-pymta.md](06-pymta.md) |

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
| Identity | Keycloak (see [02-keycloak](../02-keycloak/01-keycloak.md)) |
| S3 Storage | S3-compatible object storage for mailbox imports |

## Variable Reference

See [roles/messages/REFERENCE.md](../../roles/messages/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_messages_enabled` | Enable the messages app | `false` |
| `st_messages_tag` | Docker image tag | see REFERENCE.md |
| `st_messages_dir` | Application directory | `/opt/messages/messages` |
| `st_messages_uid` | Unix UID for the messages user | `1104` |
| `st_messages_port` | Host port for frontend | `50400` |
| `st_messages_backend_env` | Backend environment content | _(empty)_ |
| `st_messages_frontend_env` | Frontend environment content | _(empty)_ |
| `st_messages_backend_run_migrations` | Run Django migrations on deploy | `true` |
| `st_messages_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_messages_port` | `50400` | 8080 |

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

## Secrets from Files

> [!WARNING]
> No messages release has this feature yet. The latest release is `0.10.0`, the default of `st_messages_tag`.
> Until a release contains the upstream commit `07ed2bc9`, use a newer image tag.
> Otherwise the backend ignores the `_FILE` variables.

The backend can read a secret from a file. For a variable `NAME`, the container reads the file that `NAME_FILE` names.
The role writes the files and sets the `NAME_FILE` variables.

Set `st_messages_backend_secret_files` to a list of items.
Each item has the key `env` and one of the keys `src` or `content`.

```yaml
st_messages_backend_secret_files:
  - env: DB_PASSWORD
    content: "{{ vault_db_password }}"
  - env: SALT_KEY
    content: "{{ vault_salt_key }}"
```

- `env` is the variable name in upper case.
- `src` is a local template. `content` is an inline value. Both keys can hold a multi-line value, such as a PEM key.
- The role writes `{{ st_messages_dir }}/secrets/<env>` with mode `0440`.
- The role mounts the `secrets` directory read-only at `/run/secrets`.
- The role sets `<ENV>_FILE` in the `environment` of the compose service.
- The role deletes the files that are no longer in the list. It deletes `secrets` when the list is empty.
- The `<ENV>_FILE` variable has priority over the same variable in `st_messages_backend_env`.
  The exception is `CELERY_BROKER_URL` (see the caveats).
- A custom compose template must declare the `environment` and `volumes` entries itself.

The backend accepts the `_FILE` suffix for these variables:

| Area | Variables |
|------|-----------|
| Django | `DJANGO_SECRET_KEY`, `DJANGO_SERVER_TO_SERVER_API_TOKENS` |
| Database, cache, broker and search | `DB_PASSWORD`, `REDIS_URL`, `CELERY_BROKER_URL`, `OPENSEARCH_URL` |
| Object storage | `AWS_S3_ACCESS_KEY_ID`, `AWS_S3_SECRET_ACCESS_KEY`, `STORAGE_MESSAGE_IMPORTS_ACCESS_KEY`, `STORAGE_MESSAGE_IMPORTS_SECRET_KEY`, `STORAGE_MESSAGE_BLOBS_ACCESS_KEY`, `STORAGE_MESSAGE_BLOBS_SECRET_KEY` |
| Encryption keys | `SALT_KEY`, `MESSAGES_BLOBS_ENCRYPT_KEYS`, `OIDC_STORE_REFRESH_TOKEN_KEY` |
| Authentication | `OIDC_RP_CLIENT_SECRET`, `KEYCLOAK_CLIENT_SECRET` |
| Mail | `MTA_OUT_RELAY_PASSWORD`, `MTA_OUT_DIRECT_PROXIES`, `SPAM_CONFIG`, `MESSAGES_SELFCHECK_SECRET`, `MESSAGES_SELFCHECK_WEBHOOK_URL` |
| Third-party services | `AI_API_KEY`, `CALDAV_DEFAULT_PASSWORD`, `DNS_SCALEWAY_API_TOKEN` |
| Monitoring and API keys | `PROMETHEUS_API_KEY`, `METRICS_API_KEY` (deprecated), `PROVISIONING_API_KEY` (deprecated) |
| Push notifications | `PUSH_APNS_KEY`, `PUSH_FCM_CREDENTIALS`, `PUSH_VAPID_PRIVATE_KEY`, `PUSH_VAPID_PUBLIC_KEY` |

The workers use `st_messages_workers_secret_files`. It defaults to `st_messages_backend_secret_files`.

Caveats:

- If `DATABASE_URL` is set, the DB settings come from the URL. Then `DB_PASSWORD_FILE` has no effect.
- Celery reads `CELERY_BROKER_URL` from the environment before the Django settings.
  When you use `CELERY_BROKER_URL_FILE`, do not set `CELERY_BROKER_URL` in the env content.
  Otherwise the workers ignore the file.

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
