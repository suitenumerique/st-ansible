# Messages Workers

Celery background workers for the Messages application. Processes asynchronous tasks (email sending, notifications, etc.)
using the same backend image with a Celery entrypoint.

## Container Stack

```text
messages-worker (ghcr.io/suitenumerique/messages-backend)
  └── python worker.py --concurrency=<nproc> [--disable-scheduler]
```

`worker.py` starts a Celery worker with beat embedded. The role adds `--disable-scheduler` when
`st_messages_workers_beat_enabled` is `false`. `worker.py` accepts this flag from upstream `v0.10.0`.

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
| `st_messages_workers_beat_enabled` | Run Celery beat inside the worker | `true` |

## Network & Ports

No ports are published to the host. The worker connects to external services (database, Redis/broker) defined in its environment.

## Data & Volumes

Stateless. No persistent bind mounts.

## Periodic Jobs

Run beat on exactly one workers host. Each beat scheduler enqueues every periodic task, so
two hosts with beat run every task twice. Set `st_messages_workers_beat_enabled: false` on
every other workers host. The hosts without beat still consume the tasks from the queue.

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
