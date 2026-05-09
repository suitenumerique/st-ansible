# Drive Workers

Celery background workers for the Drive application. Processes asynchronous tasks using the same backend image
with a Celery entrypoint.

## Container Stack

```text
drive-worker (docker.io/lasuite/drive-backend)
  └── celery worker
```

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 512 MB minimum |
| Depends on | Drive application (shared database, Redis, env) |

## Variable Reference

See [roles/drive/REFERENCE.md](../../roles/drive/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_drive_workers_enabled` | Enable the workers | `false` |
| `st_drive_workers_dir` | Application directory | `/opt/drive/workers` |
| `st_drive_workers_env` | Environment content | `{{ st_drive_backend_env }}` |
| `st_drive_workers_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

No ports are published to the host.

## Data & Volumes

Stateless. No persistent bind mounts.

## Troubleshooting

```bash
ssh <host>
sudo -iu drive

# Service lifecycle
systemctl --user status workers.service
systemctl --user start workers.service
systemctl --user stop workers.service

# Logs
journalctl --user -u workers.service -f
journalctl --user -u workers.service --since today
journalctl --user -u workers.service --since "3 hours ago"

# Containers
podman-compose -f /opt/drive/workers/compose.yaml ps
```
