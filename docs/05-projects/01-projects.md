# Projects

The `projects` role deploys a [Projects](https://github.com/suitenumerique/projects)
instance for La Suite Territoriale. Projects is a collaborative project-management
application (a Planka fork): a single Sails.js container serving both its API and its
React UI.

## Container Stack

```text
projects (docker.io/lasuite/projects)
  └── Sails.js app on port 1337 (published on the host as st_projects_port)
```

Unlike the Django apps, Projects is a single self-contained container: no separate
frontend, no Celery, and no Redis unless you [scale out](#horizontal-scaling).
It ships its own `HEALTHCHECK` (`node ./healthcheck.js`
→ `GET :1337`) and runs its database migrations on startup (`node db/init.js`), so
the role neither defines a healthcheck nor runs a separate migration step.

> [!NOTE]
> This collection does not provision PostgreSQL. You must provide it externally
> before deploying Projects (via `DATABASE_URL`). We recommend a managed database
> service such as [Scalingo](https://scalingo.com) or [Scaleway](https://www.scaleway.com).

## Authentication

Login is **OIDC-enforced** (SSO only, no local accounts): `OIDC_ENFORCED=true`.
Wire it to your identity provider (Keycloak, ProConnect, …) via `OIDC_ISSUER`
(the discovery URL, e.g. `https://idp.example.org/realms/<realm>`),
`OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET`.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 1 GB minimum |
| Disk | 1 GB for image + uploads |
| Database | External PostgreSQL (configured via `DATABASE_URL`) |
| Identity provider | OIDC issuer + client credentials |

## Variable Reference

See [roles/projects/REFERENCE.md](../../roles/projects/REFERENCE.md) for the
complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_projects_enabled` | Enable Projects | `false` |
| `st_projects_tag` | Docker image tag | see REFERENCE.md |
| `st_projects_port` | Host port (maps to container 1337) | `50500` |
| `st_projects_uid` | Unix UID for the projects user | `1105` |
| `st_projects_env` | Environment content | _(empty)_ |
| `st_projects_directories` | Persistent upload dirs (avatars, backgrounds, attachments) | see REFERENCE.md |
| `st_projects_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_projects_port` | `50500` | 1337 |

## Data & Volumes

Projects stores uploads (user avatars, project backgrounds, attachments) in one
of two modes, chosen at runtime by whether S3 is configured:

- **Local storage (default).** When neither `S3_ENDPOINT` nor `S3_REGION` is set,
  Projects writes uploads to the local filesystem (its `LocalFileManager`). The role
  bind-mounts three persistent host directories — owned by the container's `node`
  user (uid 1000) — so those uploads survive redeploys:

  ```text
  {{ st_projects_dir }}/user-avatars               → /app/public/user-avatars
  {{ st_projects_dir }}/project-background-images   → /app/public/project-background-images
  {{ st_projects_dir }}/attachments                 → /app/private/attachments
  ```

- **S3 object storage (opt-in, recommended for production).** Set `S3_ENDPOINT`
  (or `S3_REGION`), `S3_BUCKET` and credentials in `st_projects_env` and Projects
  switches to its `S3FileManager` — better durability, and the only option if you
  ever run more than one host. The three local directories are still created but
  go unused.

The `st-cli bootstrap projects` questionnaire asks *"Configure S3 object storage
for uploads (recommended)?"*: answer **no** to run on local storage (nothing else
to provide — st-cli then warns that scaling to several instances later will first
require moving uploads to S3), or **yes** to enter the S3 endpoint, bucket and
credentials. When
[horizontal scaling](#horizontal-scaling) was accepted, this opt-out is skipped
and the S3 questionnaire runs unconditionally.

## Horizontal scaling

By default Projects runs as a **single instance**: it keeps sessions in memory
and broadcasts realtime socket.io events per-process, so a second instance
behind a load balancer would not receive the live updates emitted by the first
(sticky sessions do not fix this — they pin a client to an instance, they do not
broadcast events across instances).

Setting `REDIS_URL` in `st_projects_env` switches upstream to its Redis
adapters (`@sailshq/connect-redis` for sessions, `@sailshq/socket.io-redis` for
socket broadcasts; production only), which makes running several instances
possible. Every replica must then share:

- the same `SECRET_KEY` (session cookies are signed with it), `DATABASE_URL`
  and `REDIS_URL` — automatic here, since every host of the unit renders the
  same `st_projects_env` blob;
- **S3 object storage** — mandatory when scaling out: uploads kept on the
  local bind-mounted dirs are not visible from the other instances;
- an external load balancer in front of the replicas' `st_projects_port` — the
  role does not provide one.

To scale out, list several hosts in the component's `hosts` inventory and put
your load balancer in front of them. The `st-cli bootstrap projects`
questionnaire asks *"Configure Redis for horizontal scaling (multiple
instances)?"* (default no); answering yes prompts the `REDIS_URL` (routed
through the secret backend, as it can embed a password) and makes the S3
questionnaire **mandatory** — its opt-out confirm is skipped, since local
storage cannot work across instances.

> [!IMPORTANT]
> `REDIS_URL` support requires a Projects image that includes upstream
> [PR #85](https://github.com/suitenumerique/projects/pull/85) (horizontal
> scaling via optional Redis adapters). Until it lands in the release pinned by
> `st_projects_tag`, override the tag explicitly. On an older image the
> variable is silently ignored and each instance keeps its in-memory behaviour.
> This collection does not provision Redis: provide it externally, like
> PostgreSQL.

## Upgrades & rollback

Projects runs its database migrations **on every container start**
(`start.sh` → `node db/init.js` → `knex.migrate.latest()`), unconditionally —
there is no gate to skip them and no separate migration step (unlike the Django
apps' `st_<app>_backend_run_migrations`).

`st_projects_rollback_enabled` (default `false`) only rolls back at the
**config level**: on a failed deploy it restores the previous
compose/env directory and restarts the unit. It does **not** roll back the
database. Because an upgrade migrates the schema forward as soon as the new
container starts, a rollback across a schema-changing upgrade leaves the *old*
image running against an *already-migrated* database (knex `down` migrations are
not run automatically) — i.e. a potentially broken state, not the pre-upgrade one.

Rollback is therefore safe for same-tag redeploys and config/env changes, but
**not** across a migration-bearing upgrade. Before upgrading `st_projects_tag`
across a schema change, **back up the database** (managed PITR snapshot or a
`pg_dump`) and restore it manually if you need to revert.

> [!NOTE]
> The deploy considers the unit up once systemd reports it *active* — it does
> not wait for the container's `HEALTHCHECK`. A container that starts but is
> unhealthy (e.g. a bad env value) will **not** trigger the rollback; inspect it
> with `st-cli logs projects <env>`.

## Troubleshooting

```bash
ssh <host>
sudo -iu projects

# Service lifecycle
systemctl --user status projects.service
systemctl --user start projects.service
systemctl --user stop projects.service

# Logs
journalctl --user -u projects.service -f
journalctl --user -u projects.service --since today
journalctl --user -u projects.service --since "3 hours ago"

# Containers
podman-compose -f /opt/projects/projects/compose.yaml ps
```
