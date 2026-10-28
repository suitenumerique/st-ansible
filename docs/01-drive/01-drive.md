# Drive Application

The `drive` role deploys the [Drive](https://github.com/suitenumerique/drive) web application from La Suite Territoriale,
a file storage and collaboration platform.

The role deploys multiple independent sub-applications under the `drive` Unix user, each as a separate systemd
user unit. All sub-apps are disabled by default and must be explicitly enabled.

> [!NOTE]
> This collection does not provision PostgreSQL, Redis, or S3-compatible storage. You
> must provide these externally before deploying Drive. We recommend using managed database
> services from cloud providers such as [Scalingo](https://scalingo.com) or [Scaleway](https://www.scaleway.com).

## Sub-Applications

| Sub-App | Description | Doc |
|---------|-------------|-----|
| **drive** | Core web application (caddy + frontend + backend) | This page |
| **workers** | Celery background workers | [02-workers.md](02-workers.md) |
| **collabora** | Collabora Online document editor | [03-collabora.md](03-collabora.md) |

## Container Stack

```text
drive-caddy (docker.io/caddy), published on the host
  ├── /api/*, /external_api/*, /admin, /admin/*, /static/* → drive-backend (docker.io/lasuite/drive-backend)
  ├── /media/preview/* → S3, forward_auth against drive-backend (inline preview)
  ├── /media/* → S3, forward_auth against drive-backend (download, Content-Disposition attachment)
  └── everything else → drive-frontend (docker.io/lasuite/drive-frontend, stock SPA nginx conf)
```

Caddy owns the host port and dispatches each request to the backend, the frontend, or S3.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 1 GB minimum |
| Disk | 2 GB for images + data |
| Database | External PostgreSQL (configured via env) |
| Redis | External Redis (configured via env) |
| Identity | Keycloak (see [02-keycloak](../02-keycloak/01-keycloak.md)) |
| S3 Storage | S3-compatible object storage for media files |

## Variable Reference

See [roles/drive/REFERENCE.md](../../roles/drive/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_drive_enabled` | Enable the drive app | `false` |
| `st_drive_public_host` | Public hostname for the app | **(required)** |
| `st_drive_tag` | Docker image tag | see REFERENCE.md |
| `st_drive_dir` | Application directory | `/opt/drive/drive` |
| `st_drive_uid` | Unix UID for the drive user | `1101` |
| `st_drive_port` | Host port for the caddy edge | `50100` |
| `st_drive_backend_env` | Backend environment content | _(empty)_ |
| `st_drive_caddy_env` | Caddy env content: `CADDY_S3_PROTOCOL`, `CADDY_S3_HOST`, `CADDY_S3_BUCKET` for the media proxy, plus optional `CADDY_ADMIN_IP_ALLOWLIST` / `CADDY_TRUSTED_PROXIES` | _(empty, required keys)_ |
| `st_drive_backend_run_migrations` | Run Django migrations on deploy | `true` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_drive_port` | `50100` | 50100 (caddy) |

Caddy listens on `50100` inside its own container and is the only container published to the
host. The frontend and backend are only reachable from caddy via the Podman bridge network.

## Django Admin IP Allowlist

The caddy edge adds two optional environment variables. Set them as lines in `st_drive_caddy_env`.

| Variable | Default | Description |
|----------|---------|--------------|
| `CADDY_ADMIN_IP_ALLOWLIST` | `0.0.0.0/0 ::/0` | Space-separated CIDR list of client IPs allowed on `/admin`, `/admin/*`, and `/admin;*`. Caddy answers 403 to a denied request. |
| `CADDY_TRUSTED_PROXIES` | `private_ranges` | Space-separated CIDR list of upstream proxies whose `X-Forwarded-For` sets the client IP. |

Example:

```yaml
st_drive_caddy_env: |
  CADDY_S3_PROTOCOL=https
  CADDY_S3_HOST=s3.example.com
  CADDY_S3_BUCKET=drive-media-storage
  # Load balancer range: trusted proxy only (default: private_ranges).
  CADDY_TRUSTED_PROXIES=203.0.113.0/24
  # Operator network allowed on the Django admin URL (default: allow all).
  CADDY_ADMIN_IP_ALLOWLIST=198.51.100.0/24
```

Follow these rules:

- If you set `CADDY_ADMIN_IP_ALLOWLIST`, add your own administration network. When the list
  does not include it, you lose access to the Django admin.
- Do not set `CADDY_ADMIN_IP_ALLOWLIST` to an empty value. An empty list denies every admin
  request. Remove the line to allow all.
- `CADDY_TRUSTED_PROXIES` must cover the load balancer. When it does not, Caddy uses the load
  balancer IP as the client IP. The allowlist then applies to that IP, not to the client.
- Do not keep the `private_ranges` default when untrusted machines share the private network
  with caddy.
- Caddy walks `X-Forwarded-For` right to left, so a client cannot set its own client IP.
- `st-cli bootstrap` keeps these lines on a Modify or a silent replay. An Override replay
  drops them. Add them back by hand.

## S3 Media Auth

Requests under `/media/*` and `/media/preview/*` are proxied straight to S3
(`CADDY_S3_PROTOCOL` / `CADDY_S3_HOST` / `CADDY_S3_BUCKET`), but caddy first runs a
`forward_auth` subrequest against the backend endpoint `/api/v1.0/items/media-auth/`. Only a
request the backend approves reaches the object storage. `/media/*` adds a
`Content-Disposition: attachment` header so the browser downloads the file; `/media/preview/*`
omits it so the browser renders the file inline.

## Data & Volumes

The drive application is stateless: it stores data in an external PostgreSQL database and uses S3-compatible storage
for media files. No persistent bind mounts are needed for the core application.

The `Caddyfile` is mounted read-only into the caddy container at `/etc/caddy/Caddyfile`.

## Database Migrations

The role runs `python manage.py migrate` via `podman-compose run` after deployment. This task has `run_once: true`,
which limits execution to one host per Ansible batch. When `serial:` is used, each host is its own batch, so
`run_once: true` does **not** prevent migrations from running on every host. Set `st_drive_backend_run_migrations: false`
on all hosts except the first to avoid duplicate migrations. For example :

```yaml
st_drive_backend_run_migrations: "{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}"
```

## Custom Environment

You can either:

- Set `st_drive_backend_env` / `st_drive_frontend_env` / `st_drive_caddy_env` to provide env
  content inline
- Set the matching `_env_template` variable to use a custom template

## Secrets from Files

The drive backend reads some secrets from a file. The backend reads the file when the variable
`<NAME>_FILE` holds the path of the file. The role writes these files and sets these variables for you.

Set `st_drive_backend_secret_files` to a list. Each item has the key `env`, the name of the variable, and one
of `src` (a template) or `content` (an inline value):

```yaml
st_drive_backend_secret_files:
  - env: DB_PASSWORD
    content: "{{ vault_drive_db_password }}"
  - env: AWS_S3_SECRET_ACCESS_KEY
    src: "drive_s3_secret.j2"
```

For each item, the role:

- writes the file `secrets/<env>` in the application directory (mode `0440`), for example
  `{{ st_drive_dir }}/secrets/DB_PASSWORD`;
- mounts the `secrets` directory read-only at `/run/secrets` in the container;
- sets `<ENV>_FILE` in the compose `environment`, for example `DB_PASSWORD_FILE: /run/secrets/DB_PASSWORD`.

The role removes the files of the items that you remove from the list.
It removes the `secrets` directory when the list is empty.

The drive workers use `st_drive_workers_secret_files`. The default value is `st_drive_backend_secret_files`.

The file has priority over the same variable in `st_drive_backend_env`. If both are set, the backend uses the file.

Upstream drive `v0.22.0` supports `_FILE` for these variables (`SecretFileValue` in `src/backend/drive/settings.py`):

- `DJANGO_SECRET_KEY`: Django secret key.
- `DB_PASSWORD`: Database password.
- `AWS_S3_ACCESS_KEY_ID`: S3 access key.
- `AWS_S3_SECRET_ACCESS_KEY`: S3 secret key.
- `DJANGO_EMAIL_HOST_PASSWORD`: SMTP password.
- `OIDC_RP_CLIENT_SECRET`: OIDC client secret.
- `POSTHOG_KEY`: PostHog key.
- `WOPI_ONLYOFFICE_CONVERT_JWT_SECRET`: JWT secret of the OnlyOffice conversion.

Upstream does not support `_FILE` for other variables.
If you add an item for another variable, the backend ignores the file.
Multi-line values, such as a PEM key, work with `content: |` or with a template.
No variable of this list holds a PEM key.

If you use a custom compose template (`st_drive_compose_template` or `st_drive_workers_compose_template`),
the template must declare the `environment:` and `volumes:` entries itself.

If `DATABASE_URL` is set, the DB settings come from the URL. Then `DB_PASSWORD_FILE` has no effect.

## Troubleshooting

```bash
ssh <host>
sudo -iu drive

# Service lifecycle
systemctl --user status drive.service
systemctl --user start drive.service
systemctl --user stop drive.service

# Logs
journalctl --user -u drive.service -f
journalctl --user -u drive.service --since today
journalctl --user -u drive.service --since "3 hours ago"

# Containers
podman-compose -f /opt/drive/drive/compose.yaml ps
```
