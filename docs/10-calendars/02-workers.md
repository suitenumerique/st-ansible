# Calendars Workers

Dramatiq worker for the Calendars application. It processes asynchronous tasks with the same
backend image and the upstream `worker.py` entrypoint. Dramatiq has no scheduler, so you can
run several workers hosts.

The tasks call the CalDAV server. The workers host can be another host than the core, so the
workers stack runs its own CalDAV server on the same database. This is the upstream Scalingo
layout: the worker process runs its own SabreDAV too.

## Features That Run on the Workers

In `Production`, the backend does not run the tasks in-process. It queues them in Redis.
Without this unit, the features below do not run: the tasks stay in the Redis queue.

| Feature | Task | Queue | When it fires |
|---------|------|-------|---------------|
| ICS import | `core.tasks.import_events_task` | `import` | A user imports an ICS file. The frontend polls the task status. |
| Mailbox ACL sync (optional) | `core.tasks.sync_all_mailbox_acls` | `sync` | Upstream does not schedule it. It runs only with `FEATURE_MESSAGES_INTEGRATION=true`. |

## Container Stack

```text
calendars-worker (ghcr.io/suitenumerique/calendars-backend)
  └── python worker.py --concurrency=<nproc>
        └── http://caldav:80 → calendars-workers-caldav (ghcr.io/suitenumerique/calendars-caldav)
```

The worker has no healthcheck: `worker.py` has no probe command.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 512 MB minimum |
| Depends on | Calendars application (shared database, Redis, env) |

## Variable Reference

See [roles/calendars/REFERENCE.md](../../roles/calendars/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_calendars_workers_enabled` | Enable the workers | `false` |
| `st_calendars_workers_dir` | Application directory | `/opt/calendars/workers` |
| `st_calendars_workers_env` | Worker environment content | `{{ st_calendars_backend_env }}` |
| `st_calendars_workers_secret_files` | Worker secrets read from files | `{{ st_calendars_backend_secret_files }}` |
| `st_calendars_workers_caldav_env` | Environment content of the workers CalDAV server | `{{ st_calendars_caldav_env }}` |
| `st_calendars_workers_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

No ports are published to the host.

## Data & Volumes

Stateless. No persistent bind mounts.

## Troubleshooting

```bash
ssh <host>
sudo -iu calendars

# Service lifecycle
systemctl --user status workers.service
systemctl --user start workers.service
systemctl --user stop workers.service

# Logs
journalctl --user -u workers.service -f
journalctl --user -u workers.service --since today
journalctl --user -u workers.service --since "3 hours ago"

# Containers
podman-compose -f /opt/calendars/workers/compose.yaml ps
```
