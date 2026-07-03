# Messages Workers

Celery background workers for the Messages application. Processes asynchronous tasks (email sending, notifications, etc.)
using the same backend image with a Celery entrypoint.

## Container Stack

```text
messages-worker (ghcr.io/suitenumerique/messages-backend)
  └── celery -A messages.celery_app worker --task-events --beat
```

The worker uses the same Docker image as the messages backend, but runs the Celery worker process instead of the
Django web server.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 512 MB minimum |
| Depends on | Messages application (shared database, Redis, env) |

## Variable Reference

See [roles/messages/REFERENCE.md](../../roles/messages/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_messages_workers_enabled` | Enable the workers | `false` |
| `st_messages_workers_dir` | Application directory | `/opt/messages/workers` |
| `st_messages_workers_env` | Environment content | `{{ st_messages_backend_env }}` |
| `st_messages_workers_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

No ports are published to the host. The worker connects to external services (database, Redis/broker) defined in its environment.

## Data & Volumes

Stateless. No persistent bind mounts.

## Troubleshooting

```bash
ssh <host>
sudo -iu messages

# Service lifecycle
systemctl --user status workers.service
systemctl --user start workers.service
systemctl --user stop workers.service

# Logs
journalctl --user -u workers.service -f
journalctl --user -u workers.service --since today
journalctl --user -u workers.service --since "3 hours ago"

# Containers
podman-compose -f /opt/messages/workers/compose.yaml ps
```
