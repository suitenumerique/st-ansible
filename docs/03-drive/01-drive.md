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
| **drive** | Core web application (frontend + backend) | This page |
| **workers** | Celery background workers | [02-workers.md](02-workers.md) |
| **collabora** | Collabora Online document editor | [03-collabora.md](03-collabora.md) |

## Container Stack

```text
drive-frontend (docker.io/lasuite/drive-frontend)
  └── nginx reverse proxy → drive-backend (docker.io/lasuite/drive-backend)
```

The frontend runs nginx, serving the SPA and proxying API requests to the backend.
A custom `nginx.conf` is mounted into the frontend container.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 1 GB minimum |
| Disk | 2 GB for images + data |
| Database | External PostgreSQL (configured via env) |
| Redis | External Redis (configured via env) |
| Identity | Keycloak (see [04-keycloak](../04-keycloak/01-keycloak.md)) |
| S3 Storage | S3-compatible object storage for media files |

## Variable Reference

See [roles/drive/REFERENCE.md](../../roles/drive/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_drive_enabled` | Enable the drive app | `false` |
| `st_drive_public_host` | Public hostname for the app | **(required)** |
| `st_drive_tag` | Docker image tag | `main` |
| `st_drive_dir` | Application directory | `/opt/drive/drive` |
| `st_drive_uid` | Unix UID for the drive user | `1100` |
| `st_drive_port` | Host port for frontend | `50080` |
| `st_drive_backend_env` | Backend environment content | _(empty)_ |
| `st_drive_s3_protocol` | S3 storage protocol | `https` |
| `st_drive_s3_host` | S3 storage host | `s3.amazonaws.com` |
| `st_drive_s3_bucket` | S3 storage bucket | `drive-media` |
| `st_drive_backend_run_migrations` | Run Django migrations on deploy | `true` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_drive_port` | `50080` | 3000 |

The backend is not published to the host. It's only reachable from the frontend via the Podman bridge network.

## Data & Volumes

The drive application is stateless: it stores data in an external PostgreSQL database and uses S3-compatible storage
for media files. No persistent bind mounts are needed for the core application.

A custom `nginx.conf` is mounted into the frontend container at `/etc/nginx/conf.d/default.conf`.

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

- Set `st_drive_backend_env` / `st_drive_frontend_env` to provide env content inline
- Set `st_drive_backend_env_template` / `st_drive_frontend_env_template` to use a custom template

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
