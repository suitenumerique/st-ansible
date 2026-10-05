# Conversations Workers

Celery worker for the Conversations application. Processes asynchronous tasks using the same
backend image with a Celery entrypoint. The worker runs Celery beat embedded. Beat reads its
schedule from three env vars (upstream release after `v0.0.25`). See
[Periodic Jobs](01-conversations.md#periodic-jobs).

## Features That Run on the Workers

In `Production`, the backend does not run Celery tasks in-process. It queues them in Redis.
Without this unit, the features below do not run: the tasks stay in the Redis queue.

| Feature | Task | When it fires |
|---------|------|---------------|
| Conversation document parse | `chat.tasks.parse_and_store_conversation_document_task` | A user attaches a document to a chat turn. The turn waits for the result through `CELERY_RESULT_BACKEND`, up to `DOCUMENT_PARSE_RESULT_TIMEOUT_SECONDS` (default `360`). |
| Project file indexing | `chat.tasks.index_project_attachment_task` | The malware scan marks a project file safe, or a user starts a re-index of a failed file. The backend does not wait for the result. |
| Conversation summarization | `chat.tasks.summarize_conversation_history` | The conversation history exceeds its token budget. The task retries on model API errors. |
| Model health poll (optional, release after `v0.0.25`) | `chat.tasks.fetch_model_health_task` | Beat fires it every `MODEL_HEALTH_POLL_INTERVAL_SECONDS`. It runs only with `MODEL_HEALTH_POLL_PROVIDER=albert`. |
| RAG collection de-index (optional, release after `v0.0.25`) | `chat.tasks.deindex_inactive_collections_task` | Beat fires it on the `DEINDEX_INACTIVE_COLLECTIONS_CRON` schedule. It runs only when this variable is set. |

The first three features are core Conversations behaviour. Deploy the workers unit on every
production installation.

## Container Stack

```text
conversations-worker (docker.io/lasuite/conversations-backend)
  └── celery -A conversations.celery_app worker --task-events --beat -l INFO -c <nproc> --schedule=/tmp/celerybeat-schedule
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

## Periodic Jobs

Deploy exactly one workers host. Beat is embedded in the worker, so a second host runs every
periodic task twice.

The workers env must carry the same three lines as the backend env. By default, the workers env
inherits them. See [Periodic Jobs](01-conversations.md#periodic-jobs) for the variables and rules.

The beat schedule file lives in `/tmp` inside the container. It stores only the last-run times
and needs no persistence. Interval tasks restart their timer when the container restarts. A cron
run that falls in a downtime does not run on restart.

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
