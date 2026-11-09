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

## Mailbox Sync

With the Messages integration, Calendars copies the mailbox access from Messages into CalDAV
shares. It does this for a user each time that user opens the app. The mailbox sync does it for
every user, so that a change in Messages also reaches the users who do not open the app.

When `st_calendars_messages_enabled` and `st_calendars_workers_mailbox_sync_enabled` are both
`true`, the role installs two systemd user units for the `calendars` user:

- `mailbox-sync.service` runs `python manage.py sync_mailbox_acls` in a one-off worker
  container (`podman-compose run --rm`);
- `mailbox-sync.timer` starts it on `st_calendars_workers_mailbox_sync_timer` (`hourly` by
  default, a systemd `OnCalendar` value).

One run syncs every user. Set `st_calendars_workers_mailbox_sync_enabled: false` on every
workers host except one:

```yaml
st_calendars_workers_mailbox_sync_enabled: "{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}"
```

The st-cli scaffold sets this expression by itself. When one of the two variables is `false`,
the role stops the timer and removes the units.

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
| `st_calendars_workers_mailbox_sync_enabled` | Run the mailbox sync timer on this host | `true` |
| `st_calendars_workers_mailbox_sync_timer` | `OnCalendar` value of the mailbox sync timer | `hourly` |
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

# Mailbox sync
systemctl --user list-timers mailbox-sync.timer
systemctl --user start mailbox-sync.service
journalctl --user -u mailbox-sync.service
```
