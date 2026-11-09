# Calendars Application

The `calendars` role deploys the [Calendars](https://github.com/suitenumerique/calendars)
application from La Suite Territoriale. Users create calendars and events, invite people, and
share calendars. Each invitation, update and cancellation sends an email with an ICS file.
CalDAV clients (Thunderbird, Apple Calendar, DAVx5) can sync the calendars.

The role deploys multiple independent sub-applications under the `calendars` Unix user, each as a separate
systemd user unit. All sub-apps are disabled by default and must be explicitly enabled.

> [!NOTE]
> This collection does not provision PostgreSQL or Redis. You must provide them externally
> before deploying Calendars. Calendars does not use S3 storage.

## Sub-Applications

| Sub-App | Description | Doc |
|---------|-------------|-----|
| **calendars** | Core web application (caddy + frontend + backend + CalDAV server) | This page |
| **workers** | Dramatiq worker and its own CalDAV server | [02-workers.md](02-workers.md) |

## Container Stack

```text
calendars-caddy (docker.io/caddy), published on the host
  ├── /api/*, /external_api/*, /caldav, /caldav/*, /.well-known/caldav, /ical/*, /rsvp/*,
  │   /admin, /admin/*, /static/* → calendars-backend (ghcr.io/suitenumerique/calendars-backend)
  │                                   └── http://caldav:80 → calendars-caldav (ghcr.io/suitenumerique/calendars-caldav)
  └── everything else → calendars-frontend (ghcr.io/suitenumerique/calendars-frontend)
```

Caddy owns the host port and dispatches each request to the backend or the frontend. The
CalDAV server (SabreDAV, PHP and Apache) is not public. The backend authenticates the user and
proxies `/caldav` to it with an API key.

The CalDAV server stores the calendars and the events in the same PostgreSQL database as the
backend, in its own schema (`CALDAV_DB_SCHEMA`, `sabre` in the st-cli scaffold).

## Images

Upstream publishes the images on `ghcr.io` with the mutable `main` tag only. The role pins each
image by digest, for example `main@sha256:...`. Renovate updates the digest when the `main` tag
moves. Each image has its own tag variable: `st_calendars_frontend_tag`,
`st_calendars_backend_tag` and `st_calendars_caldav_tag`. Use the three images of the same
upstream commit.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 2 GB minimum |
| Disk | 2 GB for images |
| Database | External PostgreSQL, shared by the backend and the CalDAV server |
| Redis | External Redis: cache, sessions and Dramatiq broker |
| Identity | An OIDC provider, e.g. Keycloak (see [02-keycloak](../02-keycloak/01-keycloak.md)) |
| SMTP | An SMTP relay for the invitation emails |

The database user must own the database, or have the `CREATE` privilege on it: the CalDAV
schema script creates the `sabre` schema. The script also connects to the `postgres`
maintenance database to check that the target database exists.

## Variable Reference

See [roles/calendars/REFERENCE.md](../../roles/calendars/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_calendars_enabled` | Enable the calendars app | `false` |
| `st_calendars_public_host` | Public hostname for the app | **(required)** |
| `st_calendars_frontend_tag` | Frontend image tag | `main@sha256:...` |
| `st_calendars_backend_tag` | Backend and workers image tag | `main@sha256:...` |
| `st_calendars_caldav_tag` | CalDAV server image tag | `main@sha256:...` |
| `st_calendars_dir` | Application directory | `/opt/calendars/calendars` |
| `st_calendars_uid` | Unix UID for the calendars user | `1110` |
| `st_calendars_port` | Host port for the caddy edge | `51000` |
| `st_calendars_backend_env` | Backend environment content | _(empty)_ |
| `st_calendars_backend_secret_files` | Backend secrets read from files | `[]` |
| `st_calendars_caldav_env` | CalDAV server environment content | _(empty)_ |
| `st_calendars_caddy_env` | Caddy env content: optional `CADDY_ADMIN_IP_ALLOWLIST` / `CADDY_TRUSTED_PROXIES` | _(empty)_ |
| `st_calendars_backend_run_migrations` | Run the Django migrations and the CalDAV schema script on deploy | `true` |
| `st_calendars_messages_enabled` | Turn on the Messages integration | `false` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_calendars_port` | `51000` | 51000 (caddy) |

Caddy listens on `51000` inside its own container and is the only container published to the
host. The frontend, the backend and the CalDAV server are only reachable on the Podman bridge
network.

## Backend Environment

Set these keys in `st_calendars_backend_env`. The keys come from upstream
`src/backend/calendars/settings.py`.

| Variable | Description |
|----------|-------------|
| `DJANGO_SECRET_KEY` | Django secret key |
| `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | The public host, and its `https://` origin |
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_PORT` | PostgreSQL connection. `DATABASE_URL` also works for the backend, but the CalDAV server needs the parts. |
| `REDIS_URL` | Cache and sessions |
| `DRAMATIQ_BROKER_URL` | Dramatiq broker. Use a Redis without eviction. |
| `DRAMATIQ_RESULT_BACKEND_URL` | Dramatiq results. The backend reads the status of the import tasks there. |
| `SALT_KEY` | Salt of the encryption key of the channel tokens. Upstream ships a public default: set your own value. |
| `APP_URL` | Public URL. The emails use it for the RSVP links. |
| `CALDAV_URL` | URL of the CalDAV server, `http://caldav:80` |
| `CALDAV_INBOUND_API_KEY` | Key of the calls from the CalDAV server to the backend (scheduling callback) |
| `CALDAV_OUTBOUND_API_KEY` | Key of the calls from the backend to the CalDAV server (proxy) |
| `CALDAV_INTERNAL_API_KEY` | Key of the internal API of the CalDAV server (calendar creation, ICS import) |
| `OIDC_*`, `LOGIN_REDIRECT_URL*`, `LOGOUT_REDIRECT_URL` | Identity provider |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `EMAIL_USE_SSL`, `DEFAULT_FROM_EMAIL` | SMTP. Calendars uses the plain Django names, not `DJANGO_EMAIL_*`. |

## CalDAV Server Environment

Set these keys in `st_calendars_caldav_env`. The keys come from upstream
`src/caldav/server.php` and `src/caldav/init-database.sh`.

| Variable | Description |
|----------|-------------|
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` | The same database as the backend |
| `CALDAV_DB_SCHEMA` | Schema of the CalDAV tables. Default `public`. Use `sabre` to keep them apart from the Django tables. |
| `CALDAV_BASE_URI` | `/caldav/`, the path that the backend proxies |
| `CALDAV_INBOUND_API_KEY`, `CALDAV_OUTBOUND_API_KEY`, `CALDAV_INTERNAL_API_KEY` | The same values as in the backend env |
| `CALDAV_CALLBACK_BASE_URL` | URL of the backend for the scheduling callback, for example `https://calendars.example.com` |

The CalDAV server sends an invitation to the backend through
`<CALDAV_CALLBACK_BASE_URL>/api/v1.0/caldav-scheduling-callback/`. The backend then sends the
email. Use the public URL: the workers host runs a CalDAV server without a backend next to it.
The host must reach its own public URL.

Optional keys: `SANITIZER_STRIP_BINARY_ATTACHMENTS` (default `true`),
`SANITIZER_MAX_DESCRIPTION_BYTES` (default `102400`), `SANITIZER_MAX_RESOURCE_SIZE` (default
`1048576`) and `PHP_MEMORY_LIMIT` (default `512M`).

## Secrets from Files

The backend reads some secrets from a file. The backend reads the file when the variable
`<NAME>_FILE` holds the path of the file. The role writes these files and sets these variables for you.

Set `st_calendars_backend_secret_files` to a list. Each item has the key `env`, the name of the variable, and one
of `src` (a template) or `content` (an inline value):

```yaml
st_calendars_backend_secret_files:
  - env: DB_PASSWORD
    content: "{{ vault_calendars_db_password }}"
  - env: CALDAV_OUTBOUND_API_KEY
    content: "{{ vault_calendars_caldav_outbound_api_key }}"
```

For each item, the role:

- writes the file `secrets/<env>` in the application directory (mode `0440`), for example
  `{{ st_calendars_dir }}/secrets/DB_PASSWORD`;
- mounts the `secrets` directory read-only at `/run/secrets` in the backend container;
- sets `<ENV>_FILE` in the compose `environment`, for example `DB_PASSWORD_FILE: /run/secrets/DB_PASSWORD`.

The role removes the files of the items that you remove from the list.
It removes the `secrets` directory when the list is empty.

The workers use `st_calendars_workers_secret_files`. The default value is `st_calendars_backend_secret_files`.

The file has priority over the same variable in `st_calendars_backend_env`. If both are set, the backend uses the file.

The upstream backend supports `_FILE` for these variables (`SecretFileValue` in `src/backend/calendars/settings.py`):

- `DJANGO_SECRET_KEY`: Django secret key.
- `DB_PASSWORD`: Database password.
- `OIDC_RP_CLIENT_SECRET`: OIDC client secret.
- `EMAIL_HOST_PASSWORD`: SMTP password.
- `CALDAV_INBOUND_API_KEY`, `CALDAV_OUTBOUND_API_KEY`, `CALDAV_INTERNAL_API_KEY`: CalDAV API keys.
- `MESSAGES_API_KEY`: Messages API key. Do not add it here: set `st_calendars_messages_api_key`, see [Messages Integration](#messages-integration).

Upstream does not support `_FILE` for other variables.
If you add an item for another variable, the backend ignores the file.

If `DATABASE_URL` is set, the DB settings come from the URL. Then `DB_PASSWORD_FILE` has no effect.

The CalDAV server does not read these files. It still reads `PGPASSWORD` and the three API keys
from `st_calendars_caldav_env`.

If you use a custom compose template (`st_calendars_compose_template` or `st_calendars_workers_compose_template`),
the template must declare the `environment:` and `volumes:` entries itself.

## Django Admin IP Allowlist

The caddy edge adds two optional environment variables. Set them as lines in `st_calendars_caddy_env`.

| Variable | Default | Description |
|----------|---------|--------------|
| `CADDY_ADMIN_IP_ALLOWLIST` | `0.0.0.0/0 ::/0` | Space-separated CIDR list of client IPs allowed on `/admin`, `/admin/*`, and `/admin;*`. Caddy answers 403 to a denied request. |
| `CADDY_TRUSTED_PROXIES` | `private_ranges` | Space-separated CIDR list of upstream proxies whose `X-Forwarded-For` sets the client IP. |

Example:

```yaml
st_calendars_caddy_env: |
  # Load balancer range: trusted proxy only (default: private_ranges).
  CADDY_TRUSTED_PROXIES=203.0.113.0/24
  # Operator network allowed on the Django admin URL (default: allow all).
  CADDY_ADMIN_IP_ALLOWLIST=198.51.100.0/24
```

Follow these rules:

- If you set `CADDY_ADMIN_IP_ALLOWLIST`, add your own administration network. When the list
  does not include it, you lose access to the Django admin.
- Do not set `CADDY_ADMIN_IP_ALLOWLIST` to an empty value. An empty list denies every admin
  request. Remove the line to allow all.
- `CADDY_TRUSTED_PROXIES` must cover the load balancer. When it does not, Caddy uses the load
  balancer IP as the client IP. The allowlist then applies to that IP, not to the client.
- Do not keep the `private_ranges` default when untrusted machines share the private network
  with caddy.
- Caddy walks `X-Forwarded-For` right to left, so a client cannot set its own client IP.
- `st-cli bootstrap` does not write `st_calendars_caddy_env`. Add the block to the core
  `vars.yml` by hand. A Modify or a silent replay keeps it. An Override replay drops it.

## Data & Volumes

The calendars application is stateless: it stores data in an external PostgreSQL database. No
persistent bind mounts are needed.

The `Caddyfile` is mounted read-only into the caddy container at `/etc/caddy/Caddyfile`.

## Database Migrations

The role runs two tasks via `podman-compose run` after deployment, in this order:

1. `python manage.py migrate` in the backend image.
2. `/usr/local/bin/init-database.sh` in the CalDAV server image. On the first run, the script
   creates the schema and the CalDAV tables. On the next runs, it applies the SQL files of
   `/var/www/sabredav/sql/migrations`.

Both tasks have `run_once: true`, which limits execution to one host per Ansible batch. When
`serial:` is used, each host is its own batch, so `run_once: true` does **not** prevent the
tasks from running on every host. Set `st_calendars_backend_run_migrations: false` on all
hosts except the first to avoid duplicate runs. For example :

```yaml
st_calendars_backend_run_migrations: "{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}"
```

## Custom Environment

You can either:

- Set `st_calendars_backend_env` / `st_calendars_caldav_env` / `st_calendars_caddy_env` to
  provide env content inline. There is no frontend env: the frontend reads no environment variables.
- Set the matching `_env_template` variable to use a custom template

## Messages Integration

The Messages integration lets users create a calendar for a Messages mailbox. Invitations then
go out through that mailbox. Messages stays the source of truth for the mailbox access. See
the upstream [mailboxes.md](https://github.com/suitenumerique/calendars/blob/main/docs/mailboxes.md).
It is off by default.

| Variable | Backend setting | Description |
|----------|-----------------|-------------|
| `st_calendars_messages_enabled` | `FEATURE_MESSAGES_INTEGRATION` | Turn on the integration |
| `st_calendars_messages_api_url` | `MESSAGES_API_URL` | Base URL of Messages, for example `https://messages.example.org` |
| `st_calendars_messages_channel_id` | `MESSAGES_CHANNEL_ID` | Id of the Messages `api_key` channel, sent as `X-Channel-Id` |
| `st_calendars_messages_api_key` | `MESSAGES_API_KEY` | Key of this channel, sent as `X-API-Key` |

When `st_calendars_messages_enabled` is `true`, the role:

- sets `FEATURE_MESSAGES_INTEGRATION`, `MESSAGES_API_URL` and `MESSAGES_CHANNEL_ID` in the compose
  `environment` of the backend and of the worker;
- writes the API key to `secrets/MESSAGES_API_KEY` and sets `MESSAGES_API_KEY_FILE`;
- runs the mailbox sync on a timer on one workers host. See
  [02-workers.md](02-workers.md#mailbox-sync).

These values have priority over the same variables in `st_calendars_backend_env`.

### On the Messages Side

1. A Messages superadmin creates a channel in the Messages Django admin:
   - type `api_key`;
   - scope level `global`;
   - scopes `mailboxes:read` and `messages:send`.
2. Messages shows the API key once, when it creates the channel. Copy it, and copy the
   channel id.
3. Set the channel id in `st_calendars_messages_channel_id`, and the key in
   `st_calendars_messages_api_key`. Keep the key in the vault.

Calendars reads the organization of a mailbox from the custom attributes of its Messages mail
domain, with the key in `OIDC_USERINFO_ORGANIZATION_CLAIM`. Without this claim, the creation of
a mailbox calendar fails. See [Optional Features](#optional-features).

## Optional Features

`st-cli` does not prompt for these features. Add the env lines by hand to
`st_calendars_backend_env`. Every feature is off by default.

| Feature | Env lines | Upstream doc |
|---------|-----------|--------------|
| Organizations | `OIDC_USERINFO_ORGANIZATION_CLAIM` | [organizations.md](https://github.com/suitenumerique/calendars/blob/main/docs/organizations.md) |
| Entitlements | `ENTITLEMENTS_BACKEND`, `ENTITLEMENTS_BACKEND_PARAMETERS` | [entitlements.md](https://github.com/suitenumerique/calendars/blob/main/docs/entitlements.md) |
| iTIP METHOD in the ICS attachments | `CALENDAR_ITIP_ENABLED=true` | [invitations.md](https://github.com/suitenumerique/calendars/blob/main/docs/invitations.md) |

## First Deploy

Run this one-off command once, after the first deploy, to create a superuser:

```bash
st-cli oneoff calendars <env> -- python manage.py createsuperuser --email admin@example.org --password '<password>'
```

This command is idempotent: run it again to reset the password of an existing superuser.

Without `st-cli`, run the same command as the `calendars` Unix user instead:

```bash
sudo -iu calendars
podman-compose -f /opt/calendars/calendars/compose.yaml run --rm backend python manage.py createsuperuser --email admin@example.org --password '<password>'
```

## Bootstrap with st-cli

`st-cli bootstrap` prompts for the Calendars settings: the domain, the discrete `DB_*`
database values, `REDIS_URL`, the identity provider, optional SMTP (the plain `EMAIL_*` keys),
and cadvisor. It generates `DJANGO_SECRET_KEY`, `SALT_KEY` and the three `CALDAV_*_API_KEY`
secrets. It writes `st_calendars_caldav_env` from the same answers, with
`CALDAV_DB_SCHEMA=sabre` and `CALDAV_CALLBACK_BASE_URL` set to the public URL.

It also asks whether to turn on the Messages integration. If yes, it prompts for the Messages
base URL, the channel id and the API key. Messages generates this key, so st-cli does not
generate it. st-cli writes the `st_calendars_messages_*` variables, with the key in the vault.
It turns on the mailbox sync on the first workers host only. See
[00-getting-started/01-st-cli.md](../00-getting-started/01-st-cli.md) for general st-cli usage.

## Troubleshooting

```bash
ssh <host>
sudo -iu calendars

# Service lifecycle
systemctl --user status calendars.service
systemctl --user start calendars.service
systemctl --user stop calendars.service

# Logs
journalctl --user -u calendars.service -f
journalctl --user -u calendars.service --since today
journalctl --user -u calendars.service --since "3 hours ago"

# Containers
podman-compose -f /opt/calendars/calendars/compose.yaml ps

# CalDAV server health
podman exec calendars-caldav curl -fsS http://127.0.0.1/caldav/healthz
```
