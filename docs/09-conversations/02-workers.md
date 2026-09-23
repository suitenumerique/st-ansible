# Conversations Workers

Celery worker for the Conversations application. Processes asynchronous tasks using the same
backend image with a Celery entrypoint. The worker runs Celery beat embedded. Upstream
declares no beat schedule today, so beat starts no task yet.

## Container Stack

```text
conversations-worker (docker.io/lasuite/conversations-backend)
  └── celery worker, with beat embedded
```

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 512 MB minimum |
| Depends on | Conversations application (shared database, Redis, S3, env) |

## Variable Reference

See [roles/conversations/REFERENCE.md](../../roles/conversations/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_conversations_workers_enabled` | Enable the workers | `false` |
| `st_conversations_workers_dir` | Application directory | `/opt/conversations/workers` |
| `st_conversations_workers_env` | Environment content | `{{ st_conversations_backend_env }}` |
| `st_conversations_workers_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

No ports are published to the host.

## Data & Volumes

Stateless. No persistent bind mounts.

## Troubleshooting

```bash
ssh <host>
sudo -iu conversations

# Service lifecycle
systemctl --user status workers.service
systemctl --user start workers.service
systemctl --user stop workers.service

# Logs
journalctl --user -u workers.service -f
journalctl --user -u workers.service --since today
journalctl --user -u workers.service --since "3 hours ago"

# Containers
podman-compose -f /opt/conversations/workers/compose.yaml ps
```
