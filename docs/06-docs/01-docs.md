# Docs Application

The `docs` role deploys the [Docs](https://github.com/suitenumerique/docs) collaborative
document editor from La Suite Territoriale (upstream image name "impress").

The role deploys multiple independent sub-applications under the `docs` Unix user, each as a
separate systemd user unit. All sub-apps are disabled by default and must be explicitly enabled.

> [!NOTE]
> This collection does not provision PostgreSQL, Redis, S3-compatible storage, or an OIDC
> provider. You must provide these externally before deploying Docs. We recommend using
> managed database services from cloud providers such as [Scalingo](https://scalingo.com) or
> [Scaleway](https://www.scaleway.com).

## Sub-Applications

| Sub-App | Description | Doc |
|---------|-------------|-----|
| **docs** | Core web application (caddy + frontend + backend) | This page |
| **workers** | Celery background workers | [02-workers.md](02-workers.md) |
| **yprovider** | Real-time collaboration server | [03-yprovider.md](03-yprovider.md) |

## Container Stack

```text
docs-caddy (docker.io/caddy), published on the host
  ├── /api*, /admin, /admin/*, /external_api/*, /static/* → docs-backend (docker.io/lasuite/impress-backend)
  ├── /media/* → S3, forward_auth against docs-backend
  ├── /collaboration/* → yprovider hosts, lb_policy query room (see 03-yprovider.md)
  └── everything else → docs-frontend (docker.io/lasuite/impress-frontend, stock SPA nginx conf)

docs-docspec (ghcr.io/docspecio/api), no published port
  └── backend-only .docx conversion for the import feature (http://docspec:4000/conversion)
```

Unlike Drive, the edge is a caddy container, not the frontend's own nginx. Caddy owns the host
port and dispatches each request to the backend, the frontend, S3, or the yprovider unit.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 1 GB minimum |
| Disk | 2 GB for images + data |
| Database | External PostgreSQL (configured via env) |
| Redis | External Redis (configured via env) |
| Identity | An OIDC provider, e.g. Keycloak (see [02-keycloak](../02-keycloak/01-keycloak.md)) |
| S3 Storage | S3-compatible object storage for document media |

## Variable Reference

See [roles/docs/REFERENCE.md](../../roles/docs/REFERENCE.md) for the complete variable
reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_docs_enabled` | Enable the docs app | `false` |
| `st_docs_public_host` | Public hostname for the app | **(required)** |
| `st_docs_tag` | Docker image tag (shared by frontend, backend, yprovider) | `v5.4.1` |
| `st_docs_dir` | Application directory | `/opt/docs/docs` |
| `st_docs_uid` | Unix UID for the docs user | `1106` |
| `st_docs_port` | Host port for the caddy edge | `50600` |
| `st_docs_backend_env` | Backend environment content | _(empty)_ |
| `st_docs_caddy_env` | Caddy env content: `CADDY_S3_*` + `CADDY_YPROVIDER_ENDPOINTS` (space-separated `host:port` yprovider upstreams for the `/collaboration/*` route) | _(empty, required keys)_ |
| `st_docs_frontend_logo_src` | Local path to an optional custom logo (svg) | _(unset)_ |
| `st_docs_theme_customization_src` | Local path to an optional theme customization JSON | _(unset)_ |
| `st_docs_backend_run_migrations` | Run Django migrations on deploy | `true` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_docs_port` | `50600` | 50600 (caddy) |

Caddy listens on `50600` inside its own container and is the only container published to the
host. The frontend and backend are only reachable from caddy via the Podman bridge network.

## S3 Media Auth

Requests under `/media/*` are proxied straight to S3 (`CADDY_S3_PROTOCOL` / `CADDY_S3_HOST` /
`CADDY_S3_BUCKET`), but caddy first runs a `forward_auth` subrequest against the backend
endpoint `/api/v1.0/documents/media-auth/`. Only a request the backend approves reaches the
object storage; this mirrors how the upstream nginx ingress protects media downloads.

## S3 Retention

Docs never deletes objects from the bucket. The trash retention (`TRASHBIN_CUTOFF_DAYS`,
default 30 days) only hides expired documents from the trashbin — the database rows, the
document content, the attachments, and all object versions stay in S3 forever. No task or
scheduled job purges them.

What you can do to reclaim the space of one document tree is to run the upstream `clean_document` management
command manually: `st-cli oneoff docs <env> -- python manage.py clean_document --force <uuid>`
— it purges the content and attachments, versions included (`--force` is required when
`DEBUG` is off, so always in production).

## Document Import (docspec)

The compose ships a `docs-docspec` container by default. [DocSpec](https://github.com/docspecio/api)
is a small, stateless conversion service (an Elixir API): it converts `.docx` documents into the
BlockNote JSON format the editor uses. The backend calls it server-side at
`http://docspec:4000/conversion` when a user imports a `.docx` file; `.md` imports go through the
yprovider instead. The container publishes no port, keeps no data, and needs no configuration.
The import feature itself is switched on by `CONVERSION_UPLOAD_ENABLED=true` in the backend env.

## Data & Volumes

The docs application is stateless: it stores data in an external PostgreSQL database and uses
S3-compatible storage for media files. No persistent bind mounts are needed for the core
application, other than the optional theme file below.

## Database Migrations

The role runs `python manage.py migrate` via `podman-compose run` after deployment. This task
has `run_once: true`, so it executes once per play. If your deployment uses `serial:`, set
`st_docs_backend_run_migrations: false` on all hosts except one to avoid running migrations
multiple times.

## Custom Environment

You can either:

- Set `st_docs_backend_env` / `st_docs_frontend_env` / `st_docs_caddy_env` to provide env
  content inline
- Set the matching `_env_template` variable to use a custom template

## Custom Theme

You can override the default Docs theme without rebuilding the image. Set
`st_docs_theme_customization_src` to the path of a theme customization JSON file on the Ansible
controller (use an absolute path). When set, the role copies the file to the host and mounts it
read-only over the backend's default `THEME_CUSTOMIZATION_FILE_PATH`
(`/app/impress/configuration/theme/default.json`). The backend serves the JSON to the browser
through its `/api/v1.0/config/` endpoint — the frontend never reads the file itself.

> [!NOTE]
> The backend caches the parsed JSON in Redis for `THEME_CUSTOMIZATION_CACHE_TIMEOUT` seconds
> (default: 24 hours), and a backend restart does not clear Redis. Set a lower timeout in the
> backend env while you iterate on the theme, or expect a delay before a change appears.

To apply a theme change immediately, delete the cached key with a one-off command:

```bash
st-cli oneoff docs <env> -- python manage.py shell -c "from django.core.cache import cache; from django.conf import settings; from django.utils.text import slugify; cache.delete(f'theme_customization_{slugify(settings.THEME_CUSTOMIZATION_FILE_PATH)}')"
```

Do not use `cache.clear()` instead: django-redis implements it as a database flush, and the
sessions live in the same Redis — it logs every user out.

You can also set the `FRONTEND_THEME` and `FRONTEND_CSS_URL` backend environment keys for
further customization.

## Custom Logo

Set `st_docs_frontend_logo_src` to the path of a logo file (svg) on the Ansible controller
(use an absolute path). The role copies it to the host and mounts it read-only over
`/app/assets/icon-docs.svg` in the frontend container. Mirrors meet's
`st_meet_frontend_logo_src`.

How it works: the theme JSON decides which URL the logo spots load — the built-in default theme
points `header.icon.src` and the footer logo at `/assets/icon-docs.svg` — and the frontend
serves the bytes of that URL. The mount replaces the bytes, so the header, the left panel, and
the footer show your logo without a theme JSON. A custom theme JSON
(`st_docs_theme_customization_src`) can instead point `header.icon.src` at any URL; it then
bypasses the mount.

> [!NOTE]
> The bottom section of the home page embeds the icon at build time, so the mount does not
> change it. All the theme-driven spots (header, left panel, footer) do change.

## Bootstrap with st-cli

`st-cli` generates and mirrors the two secrets the core and the yprovider unit share
(`COLLABORATION_SERVER_SECRET`, `Y_PROVIDER_API_KEY`) into both vaults, and builds
`CADDY_YPROVIDER_ENDPOINTS` from the yprovider hosts you enter during the questionnaire. See
[03-yprovider.md](03-yprovider.md) for the shared-secret and endpoint details, and
[00-getting-started/01-st-cli.md](../00-getting-started/01-st-cli.md) for general st-cli usage.

## Troubleshooting

```bash
ssh <host>
sudo -iu docs

# Service lifecycle
systemctl --user status docs.service
systemctl --user start docs.service
systemctl --user stop docs.service

# Logs
journalctl --user -u docs.service -f
journalctl --user -u docs.service --since today
journalctl --user -u docs.service --since "3 hours ago"

# Containers
podman-compose -f /opt/docs/docs/compose.yaml ps
```
