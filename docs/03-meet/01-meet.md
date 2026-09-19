# Meet Application

The `meet` role deploys the Meet video conferencing application from La Suite Territoriale,
powered by [LiveKit](https://livekit.com/).

The role deploys multiple independent sub-applications under the `meet` Unix user, each as a
separate systemd user unit. All sub-apps are disabled by default and must be explicitly enabled.

> [!NOTE]
> This collection does not provision PostgreSQL or S3-compatible storage. You must provide them externally before
> deploying Meet. We recommend using managed database services from cloud providers such as
> [Scalingo](https://scalingo.com) or [Scaleway](https://www.scaleway.com).

## Sub-Applications

| Sub-App | Description | Doc |
|---------|-------------|-----|
| **meet** | Core web application (caddy + frontend + backend) | This page |
| **livekit** | LiveKit server for video/audio | [02-livekit.md](02-livekit.md) |
| **egress** | LiveKit egress recorder (meeting recordings) | [03-egress.md](03-egress.md) |

Meeting recording requires the **egress** sub-app plus the `RECORDING_*` backend environment
variables — see [03-egress.md](03-egress.md) for the full setup.

## Container Stack

```text
meet-caddy (docker.io/caddy), published on the host
  ├── /api/*, /external-api/*, /admin, /admin/*, /static/* → meet-backend (docker.io/lasuite/meet-backend)
  ├── /media/files/* → S3, forward_auth against meet-backend (file share, Content-Disposition attachment)
  ├── /media/* → S3, forward_auth against meet-backend (recordings)
  └── everything else → meet-frontend (docker.io/lasuite/meet-frontend, stock SPA nginx conf)
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

## Variable Reference

See [roles/meet/REFERENCE.md](../../roles/meet/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_meet_enabled` | Enable the meet app | `false` |
| `st_meet_tag` | Docker image tag | see REFERENCE.md |
| `st_meet_dir` | Application directory | `/opt/meet/meet` |
| `st_meet_uid` | Unix UID for the meet user | `1103` |
| `st_meet_port` | Host port for the caddy edge | `50300` |
| `st_meet_backend_env` | Backend environment content | _(empty)_ |
| `st_meet_frontend_env` | Frontend environment content | _(empty)_ |
| `st_meet_caddy_env` | Caddy env content: `CADDY_S3_PROTOCOL`, `CADDY_S3_HOST`, `CADDY_S3_BUCKET` for the media proxy, plus optional `CADDY_ADMIN_IP_ALLOWLIST` / `CADDY_TRUSTED_PROXIES` | _(empty, required keys)_ |
| `st_meet_backend_run_migrations` | Run Django migrations on deploy | `true` |
| `st_meet_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_meet_port` | `50300` | 50300 (caddy) |

Caddy listens on `50300` inside its own container and is the only container published to the
host. The frontend and backend are only reachable from caddy via the Podman bridge network.

## Django Admin IP Allowlist

The caddy edge adds two optional environment variables. Set them as lines in `st_meet_caddy_env`.

| Variable | Default | Description |
|----------|---------|--------------|
| `CADDY_ADMIN_IP_ALLOWLIST` | `0.0.0.0/0 ::/0` | Space-separated CIDR list of client IPs allowed on `/admin`, `/admin/*`, and `/admin;*`. Caddy answers 403 to a denied request. |
| `CADDY_TRUSTED_PROXIES` | `private_ranges` | Space-separated CIDR list of upstream proxies whose `X-Forwarded-For` sets the client IP. |

Example:

```yaml
st_meet_caddy_env: |
  CADDY_S3_PROTOCOL=https
  CADDY_S3_HOST=s3.example.com
  CADDY_S3_BUCKET=meet-media-storage
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

Requests under `/media/files/*` and `/media/*` are proxied straight to S3 (`CADDY_S3_PROTOCOL` /
`CADDY_S3_HOST` / `CADDY_S3_BUCKET`), but caddy first runs a `forward_auth` subrequest against the
backend. Only a request the backend approves reaches the object storage. `/media/files/*` calls
the backend endpoint `/api/v1.0/files/media-auth/` and adds a `Content-Disposition: attachment`
header, so the browser downloads the file. `/media/*` calls the backend endpoint
`/api/v1.0/recordings/media-auth/` and passes the object's own `Content-Disposition` header
through unchanged; the frontend forces the download with its own download attribute.

## Data & Volumes

The meet application is stateless: it stores data in an external PostgreSQL database. No
persistent bind mounts are needed for the core application.

The `Caddyfile` is mounted read-only into the caddy container at `/etc/caddy/Caddyfile`.

## Database Migrations

The role runs `python manage.py migrate` via `podman-compose run` after deployment. This task
has `run_once: true`, so it executes once per play. If your deployment uses `serial:`, set
`st_meet_backend_run_migrations: false` on all hosts except one to avoid running migrations
multiple times.

## Custom Environment

You can either:

- Set `st_meet_backend_env` / `st_meet_frontend_env` / `st_meet_caddy_env` to provide env
  content inline
- Set the matching `_env_template` variable to use a custom template

## Custom Logo

You can override the meet frontend logo without rebuilding the image. Set
`st_meet_frontend_logo_src` to the local path of a logo file (e.g. an SVG) on the Ansible
controller. When set, the file is copied to the host and mounted read-only over
`/usr/share/nginx/html/assets/logo.svg` in the frontend container.

```yaml
# with st-cli put my-logo.svg in
#   /repo/meet/prod/meet/my-logo.svg
st_meet_frontend_logo_src: "{{ inventory_dir }}/my-logo.svg"
```

For further custom theming, see the
[meet theming documentation](https://github.com/suitenumerique/meet/blob/main/docs/theming.md).

## Troubleshooting

```bash
ssh <host>
sudo -iu meet

# Service lifecycle
systemctl --user status meet.service
systemctl --user start meet.service
systemctl --user stop meet.service

# Logs
journalctl --user -u meet.service -f
journalctl --user -u meet.service --since today
journalctl --user -u meet.service --since "3 hours ago"

# Containers
podman-compose -f /opt/meet/meet/compose.yaml ps
```
