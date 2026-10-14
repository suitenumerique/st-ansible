# Drive Workers

Celery background workers for the Drive application. Processes asynchronous tasks using the same backend image
with a Celery entrypoint. The worker runs Celery beat embedded. Set `st_drive_workers_beat_enabled`
to `false` to stop beat on a host.

## Container Stack

```text
drive-worker (docker.io/lasuite/drive-backend)
  └── celery -A drive.celery_app worker --task-events -l INFO -c <nproc> -Q celery,default [--beat --schedule=/tmp/celerybeat-schedule]
```

The bracketed part is present when `st_drive_workers_beat_enabled` is `true` (the default).

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
| `st_drive_workers_beat_enabled` | Run Celery beat inside the worker | `true` |

## Network & Ports

No ports are published to the host.

## Data & Volumes

Stateless. No persistent bind mounts.

## Periodic Jobs

Run beat on exactly one workers host. Each beat scheduler enqueues every periodic task, so
two hosts with beat run every task twice. Set `st_drive_workers_beat_enabled: false` on
every other workers host. The hosts without beat still consume the tasks from the queue.

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
