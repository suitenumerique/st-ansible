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
| **meet** | Core web application (frontend + backend) | This page |
| **livekit** | LiveKit server for video/audio | [02-livekit.md](02-livekit.md) |

## Container Stack

```text
meet-frontend (docker.io/lasuite/meet-frontend)
  └── depends on → meet-backend (docker.io/lasuite/meet-backend)
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

## Variable Reference

See [roles/meet/REFERENCE.md](../../roles/meet/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_meet_enabled` | Enable the meet app | `false` |
| `st_meet_tag` | Docker image tag | `latest` |
| `st_meet_dir` | Application directory | `/opt/meet/meet` |
| `st_meet_uid` | Unix UID for the meet user | `1100` |
| `st_meet_backend_env` | Backend environment content | _(empty)_ |
| `st_meet_frontend_env` | Frontend environment content | _(empty)_ |
| `st_meet_backend_run_migrations` | Run Django migrations on deploy | `true` |
| `st_meet_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_meet_port` | `50080` | 8080 |

The backend is not published to the host. It's only reachable from the frontend via the Podman
bridge network.

## Data & Volumes

The meet application is stateless: it stores data in an external PostgreSQL database. No
persistent bind mounts are needed for the core application.

## Database Migrations

The role runs `python manage.py migrate` via `podman-compose run` after deployment. This task
has `run_once: true`, so it executes once per play. If your deployment uses `serial:`, set
`st_meet_backend_run_migrations: false` on all hosts except one to avoid running migrations
multiple times.

## Custom Environment

You can either:

- Set `st_meet_backend_env` / `st_meet_frontend_env` to provide env content inline
- Set `st_meet_backend_env_template` / `st_meet_frontend_env_template` to use a custom template

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
