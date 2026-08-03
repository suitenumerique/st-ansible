# Docs Workers

Celery background workers for the Docs application. Processes asynchronous tasks using the same
backend image with a Celery entrypoint. Upstream Docs has no beat schedule, so this unit runs a
plain worker, no `--beat`.

## Features That Run on the Workers

In `Production`, the backend never executes Celery tasks in-process — it queues them in Redis.
Without this unit, the features below stall silently: the tasks queue up and never run.

| Feature | Task | When it fires |
|---------|------|---------------|
| Collaboration permission resets | `core.tasks.access.reset_service_connections_in_cascade` | A document's link reach/role or a user access changes. The backend tells the y-provider to reset the websocket connections of the document and its descendants, so connected editors get the new permissions. |
| Ask-for-access emails | `core.tasks.mail.send_ask_for_access_mail` | A user requests access to a document. The owners and admins receive a notification email (needs the SMTP settings). |
| User reconciliation imports | `core.tasks.user_reconciliation.user_reconciliation_csv_import_job` | An operator launches a CSV import job from the Django admin. |
| Search indexing (optional) | `core.tasks.search.document_indexer_task` / `batch_document_indexer_task` | A document or access is saved, debounced. Only active when `SEARCH_INDEXER_CLASS` points at a deployed `find` app (off by default, not covered by this role). |
| Marketing contact sync (optional) | `lasuite.marketing.tasks.create_or_update_contact` | A new user signs in for the first time. Only active with `SIGNUP_NEW_USER_TO_MARKETING_EMAIL` set (Brevo backend). |
| Malware scanning of uploads (optional) | `lasuite.malware_detection.tasks.jcop` | A user uploads a document attachment. Only active with the JCOP malware-detection backend configured (the default dummy backend uses no tasks). |

The first three features are core Docs behaviour. Deploy the workers unit on every production
installation.

## Container Stack

```text
docs-worker (docker.io/lasuite/impress-backend)
  └── celery -A impress.celery_app worker
```

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 512 MB minimum |
| Depends on | Docs application (shared database, Redis, env) |

## Variable Reference

See [roles/docs/REFERENCE.md](../../roles/docs/REFERENCE.md) for the complete variable
reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_docs_workers_enabled` | Enable the workers | `false` |
| `st_docs_workers_dir` | Application directory | `/opt/docs/workers` |
| `st_docs_workers_env` | Environment content | `{{ st_docs_backend_env }}` |
| `st_docs_workers_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

No ports are published to the host.

## Data & Volumes

Stateless. No persistent bind mounts.

## Troubleshooting

```bash
ssh <host>
sudo -iu docs

# Service lifecycle
systemctl --user status workers.service
systemctl --user start workers.service
systemctl --user stop workers.service

# Logs
journalctl --user -u workers.service -f
journalctl --user -u workers.service --since today
journalctl --user -u workers.service --since "3 hours ago"

# Containers
podman-compose -f /opt/docs/workers/compose.yaml ps
```
