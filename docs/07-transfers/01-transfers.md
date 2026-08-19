# Transfers

The `transfers` role deploys a [Transfers](https://github.com/suitenumerique/transfers)
instance for La Suite Territoriale. Transfers is a sovereign file-transfer service (a
companion to Drive): a Django REST backend, a React frontend served by Caddy, and a
Celery worker for background jobs.

## Container Stack

```text
transfers-frontend (ghcr.io/suitenumerique/transfers-frontend)
  └── Caddy on port 8080 (published on the host as st_transfers_port)
      serves the built SPA and reverse-proxies /api, /admin, /static,
      /__heartbeat__ → transfers-backend:8000
transfers-backend  (ghcr.io/suitenumerique/transfers-backend)
  └── gunicorn (transferts.wsgi) on port 8000
transfers-worker   (ghcr.io/suitenumerique/transfers-backend)
  └── python worker.py — a Celery worker with the beat scheduler embedded
```

The frontend and backend run in a single compose unit (`/opt/transfers/transfers`); the
worker is a separate, optionally-enabled unit (`/opt/transfers/workers`). The frontend
image bundles Caddy, so — unlike the Drive role — there is **no `nginx.conf`** template:
the reverse-proxy config is baked into the image and only its runtime targets are set
through env (`TRANSFERTS_FRONTEND_BACKEND_SERVER`).

> [!NOTE]
> The application's Python package is `transferts` (French spelling), so
> `DJANGO_SETTINGS_MODULE=transferts.settings` and the Celery app is
> `transferts.celery_app` — even though the app/role is named `transfers`.

> [!NOTE]
> This collection provisions neither PostgreSQL, Redis, nor object storage. You must
> provide them externally before deploying Transfers. We recommend a managed database
> and Redis (e.g. [Scalingo](https://scalingo.com) or [Scaleway](https://www.scaleway.com)).

## Authentication

Login is OIDC (mozilla-django-oidc), wired to your identity provider through
`OIDC_RP_CLIENT_ID` / `OIDC_RP_CLIENT_SECRET` and the `OIDC_OP_*` endpoints. The
`st-cli bootstrap transfers` questionnaire supports Keycloak, ProConnect
(integration / production) and a custom issuer, exactly like Drive and Meet.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 1 GB minimum |
| Disk | 1 GB for images |
| Database | External PostgreSQL (`DATABASE_URL` or discrete `DB_*`) |
| Cache / broker | External Redis (`REDIS_URL` / `CELERY_BROKER_URL`) |
| Object storage | S3-compatible bucket + credentials (**required**) |
| Identity provider | OIDC issuer + client credentials |

## Variable Reference

See [roles/transfers/REFERENCE.md](../../roles/transfers/REFERENCE.md) for the complete
variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_transfers_enabled` | Enable Transfers (frontend + backend) | `false` |
| `st_transfers_tag` | Docker image tag (backend & frontend) | see REFERENCE.md |
| `st_transfers_port` | Host port (maps to the frontend Caddy 8080) | `50700` |
| `st_transfers_uid` | Unix UID for the transfers user | `1107` |
| `st_transfers_backend_env` | Backend environment content | _(empty)_ |
| `st_transfers_frontend_env` | Frontend (Caddy) environment content | _(empty)_ |
| `st_transfers_backend_run_migrations` | Run DB migrations on deploy | `true` |
| `st_transfers_workers_enabled` | Enable the Celery worker unit | `false` |
| `st_transfers_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_transfers_port` | `50700` | 8080 (frontend Caddy) |

The backend (gunicorn `:8000`) is **not** published on the host — the frontend Caddy is
the only ingress and proxies API/admin/static traffic to it over the compose network.

## Object storage (S3)

Transfers stores every uploaded file in S3-compatible object storage — there is **no
local-filesystem fallback**. Uploads and downloads use presigned URLs straight to the
bucket (the browser talks to S3 directly), so both the backend and the frontend must
know the bucket:

- the backend reads the standard django-lasuite S3 settings — `AWS_S3_ENDPOINT_URL`,
  `AWS_S3_ACCESS_KEY_ID`, `AWS_S3_SECRET_ACCESS_KEY`, `AWS_S3_REGION_NAME`,
  `AWS_S3_SIGNATURE_VERSION` and the bucket `AWS_STORAGE_BUCKET_NAME`;
- the frontend Caddy sets `TRANSFERTS_FRONTEND_S3_ORIGIN` so its Content-Security-Policy
  allows the browser to fetch presigned URLs from your S3 endpoint.

`st-cli bootstrap transfers` derives `TRANSFERTS_FRONTEND_S3_ORIGIN` from the S3
endpoint you enter, so the two stay in sync.

## Background jobs (Celery)

The worker unit (`st_transfers_workers_enabled: true`) runs `python worker.py`, which
launches a single Celery worker with the **beat scheduler embedded** (`--beat`) — there
is no separate beat container. It handles transfer expiry, draft cleanup, S3 orphan
detection and invitation delivery. Enable it on the same hosts as the core or on
dedicated worker hosts (`st-cli` prompts for optional worker IPs at bootstrap).

## Drive integration (optional)

Setting `DRIVE_BASE_URL` (asked optionally at bootstrap) enables the Drive file picker
so users can attach files straight from their Drive. Left unset, the integration is off.

## Upgrades & rollback

Migrations run as a one-shot `podman-compose run --rm backend python manage.py migrate`
on deploy (gated to the first host of a multi-host unit via
`st_transfers_backend_run_migrations`).

`st_transfers_rollback_enabled` (default `false`) rolls back only at the **config
level**: on a failed deploy it restores the previous compose/env directory and restarts
the unit. It does **not** roll back the database. Before upgrading `st_transfers_tag`
across a schema-changing release, **back up the database** and restore it manually if
you need to revert.

## Troubleshooting

```bash
ssh <host>
sudo -iu transfers

# Service lifecycle
systemctl --user status transfers.service
systemctl --user restart transfers.service

# Logs
journalctl --user -u transfers.service -f
journalctl --user -u workers.service -f

# Containers
podman-compose -f /opt/transfers/transfers/compose.yaml ps
```
