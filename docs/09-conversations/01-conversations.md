# Conversations Application

The `conversations` role deploys the [Conversations](https://github.com/suitenumerique/conversations)
application from La Suite Territoriale, branded "L'Assistant", an AI chat assistant.

The role deploys multiple independent sub-applications under the `conversations` Unix user, each as a separate
systemd user unit. All sub-apps are disabled by default and must be explicitly enabled.

> [!NOTE]
> This collection does not provision PostgreSQL, Redis, or S3-compatible storage. You
> must provide these externally before deploying Conversations. We recommend using managed database
> services from cloud providers such as [Scalingo](https://scalingo.com) or [Scaleway](https://www.scaleway.com).

## Sub-Applications

| Sub-App | Description | Doc |
|---------|-------------|-----|
| **conversations** | Core web application (caddy + frontend + backend) | This page |
| **workers** | Celery worker with embedded beat | [02-workers.md](02-workers.md) |

## Container Stack

```text
conversations-caddy (docker.io/caddy), published on the host
  ├── /api/*, /admin, /admin/*, /static/* → conversations-backend (docker.io/lasuite/conversations-backend)
  └── everything else → conversations-frontend (docker.io/lasuite/conversations-frontend, stock SPA nginx-unprivileged conf)
```

Caddy owns the host port and dispatches each request to the backend or the frontend. The
browser never fetches an attachment through Caddy: uploads go to S3 with a presigned URL, and
the backend hands files to the LLM.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 2 GB minimum |
| Disk | 2 GB for images + data |
| Database | External PostgreSQL (configured via env) |
| Redis | External Redis (configured via env) |
| Identity | An OIDC provider, e.g. Keycloak (see [02-keycloak](../02-keycloak/01-keycloak.md)) |
| S3 Storage | S3-compatible object storage for chat attachments |
| LLM API | An OpenAI compatible API, e.g. Albert or a self-hosted vLLM endpoint |

## Variable Reference

See [roles/conversations/REFERENCE.md](../../roles/conversations/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_conversations_enabled` | Enable the conversations app | `false` |
| `st_conversations_public_host` | Public hostname for the app | **(required)** |
| `st_conversations_tag` | Docker image tag | `v0.0.24` |
| `st_conversations_dir` | Application directory | `/opt/conversations/conversations` |
| `st_conversations_uid` | Unix UID for the conversations user | `1109` |
| `st_conversations_port` | Host port for the caddy edge | `50900` |
| `st_conversations_backend_env` | Backend environment content | _(empty)_ |
| `st_conversations_caddy_env` | Caddy env content: optional `CADDY_ADMIN_IP_ALLOWLIST` / `CADDY_TRUSTED_PROXIES` | _(empty)_ |
| `st_conversations_backend_run_migrations` | Run Django migrations on deploy | `true` |
| `st_conversations_llm_configuration_src` | Local path to an optional LLM catalog JSON | _(unset)_ |
| `st_conversations_theme_customization_src` | Local path to an optional theme customization JSON | _(unset)_ |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_conversations_port` | `50900` | 50900 (caddy) |

Caddy listens on `50900` inside its own container and is the only container published to the
host. The frontend and backend are only reachable from caddy via the Podman bridge network.

## Django Admin IP Allowlist

The caddy edge adds two optional environment variables. Set them as lines in `st_conversations_caddy_env`.

| Variable | Default | Description |
|----------|---------|--------------|
| `CADDY_ADMIN_IP_ALLOWLIST` | `0.0.0.0/0 ::/0` | Space-separated CIDR list of client IPs allowed on `/admin`, `/admin/*`, and `/admin;*`. Caddy answers 403 to a denied request. |
| `CADDY_TRUSTED_PROXIES` | `private_ranges` | Space-separated CIDR list of upstream proxies whose `X-Forwarded-For` sets the client IP. |

Example:

```yaml
st_conversations_caddy_env: |
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
- `st-cli bootstrap` does not write `st_conversations_caddy_env`. Add the block to the core
  `vars.yml` by hand. A Modify or a silent replay keeps it. An Override replay drops it.

## Data & Volumes

The conversations application is stateless: it stores data in an external PostgreSQL database and uses
S3-compatible storage for chat attachments. No persistent bind mounts are needed for the core
application, other than the optional LLM catalog and theme files below.

The `Caddyfile` is mounted read-only into the caddy container at `/etc/caddy/Caddyfile`.

## Database Migrations

The role runs `python manage.py migrate` via `podman-compose run` after deployment. This task
has `run_once: true`, which limits execution to one host per Ansible batch. When `serial:` is
used, each host is its own batch, so `run_once: true` does **not** prevent migrations from
running on every host. Set `st_conversations_backend_run_migrations: false` on all hosts
except the first to avoid duplicate migrations. For example :

```yaml
st_conversations_backend_run_migrations: "{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}"
```

## Custom Environment

You can either:

- Set `st_conversations_backend_env` / `st_conversations_caddy_env` to provide env content
  inline. There is no frontend env: the frontend reads no environment variables.
- Set the matching `_env_template` variable to use a custom template

## LLM Configuration

Conversations needs an OpenAI compatible LLM provider. Set these keys in
`st_conversations_backend_env`:

| Variable | Description |
|----------|-------------|
| `AI_BASE_URL` | Base URL of the OpenAI compatible API, for example `https://api.openai.com/v1`, or an Albert or vLLM endpoint |
| `AI_MODEL` | Model identifier the provider serves |
| `AI_API_KEY` | API key for the provider |

These three keys define the default catalog: one chat model and one summarization model on
the same provider.

For several models, fallback models, or an OCR model, set `st_conversations_llm_configuration_src`.
Its value is the absolute path of a catalog JSON file on the Ansible controller. The role
copies the file to the host as `llm.json` and mounts it read-only over the backend's
`/app/conversations/configuration/llm/default.json`. Set `LLM_FALLBACK_MODEL_HRID_1` and
`LLM_FALLBACK_MODEL_HRID_2` in the backend env to name the fallback models. See the upstream
[LLM configuration guide](https://github.com/suitenumerique/conversations/blob/main/docs/llm-configuration.md)
for the catalog file format.

> [!NOTE]
> The backend caches the catalog at boot. Restart the `conversations` service after you edit
> `st_conversations_llm_configuration_src` for the change to take effect.

## Optional Features

Conversations ships several optional features behind feature flags. `st-cli` does not prompt
for these: add the env lines by hand to `st_conversations_backend_env`. A feature flag value
must be uppercase: `ENABLED`, `DYNAMIC`, or `DISABLED`.

| Feature | Env lines | Flag |
|---------|-----------|------|
| Document upload / RAG | `ALBERT_API_URL`, `ALBERT_API_KEY` | `FEATURE_FLAG_DOCUMENT_UPLOAD=ENABLED` |
| Web search | `BRAVE_API_KEY` | `FEATURE_FLAG_WEB_SEARCH=ENABLED` |
| Presentation generation | _(none)_ | `FEATURE_FLAG_PRESENTATION_GENERATION=ENABLED` |
| Edit in Docs | `DOCS_BASE_URL=https://docs.example.org`, `OIDC_STORE_ACCESS_TOKEN=true` | _(none)_ |
| data.gouv MCP connector | `DATAGOUV_CONNECTOR_URL` | `FEATURE_FLAG_DATAGOUV_CONNECTOR=ENABLED` |

> [!NOTE]
> "Edit in Docs" needs `OIDC_STORE_ACCESS_TOKEN=true` in addition to `DOCS_BASE_URL`. The
> backend fails to start without it.

Document upload / RAG and web search need an Albert API account. See the upstream
[env reference](https://github.com/suitenumerique/conversations/blob/main/docs/env.md),
[attachments guide](https://github.com/suitenumerique/conversations/blob/main/docs/attachments.md),
and [tools guide](https://github.com/suitenumerique/conversations/blob/main/docs/tools.md) for
the full list of keys.

## Custom Theme

You can override the default Conversations theme without rebuilding the image. Set
`st_conversations_theme_customization_src` to the path of a theme customization JSON file on
the Ansible controller (use an absolute path). The role copies the file to the host as
`theme.json` and mounts it read-only over the backend's default theme file at
`/app/conversations/configuration/theme/default.json`.

> [!NOTE]
> The backend caches the parsed JSON in Redis for `THEME_CUSTOMIZATION_CACHE_TIMEOUT` seconds
> (default: 24 hours), and a backend restart does not clear Redis. Set a lower timeout in the
> backend env while you iterate on the theme, or expect a delay before a change appears.

To apply a theme change immediately, delete the cached key with a one-off command:

```bash
st-cli oneoff conversations <env> -- python manage.py shell -c "from django.core.cache import cache; from django.conf import settings; from django.utils.text import slugify; cache.delete(f'theme_customization_{slugify(settings.THEME_CUSTOMIZATION_FILE_PATH)}')"
```

Do not use `cache.clear()` instead: django-redis implements it as a database flush, and the
sessions live in the same Redis. It logs every user out.

## First Deploy

Run these one-off commands once, after the first deploy.

### Create a Superuser

```bash
st-cli oneoff conversations <env> -- python manage.py createsuperuser --email admin@example.org --password '<password>'
```

This command is idempotent: run it again to reset the password of an existing superuser.

### Configure Bucket CORS

The browser uploads attachments with a presigned PUT request directly to S3, so the bucket
needs a CORS rule for the public origin. The command derives the allowed origins from
`DJANGO_ALLOWED_HOSTS` as `https://<host>` and fails when that key is empty:

```bash
st-cli oneoff conversations <env> -- python manage.py bucket_cors --set
```

Without `st-cli`, run the same commands as the `conversations` Unix user instead:

```bash
sudo -iu conversations
podman-compose -f /opt/conversations/conversations/compose.yaml run --rm backend python manage.py createsuperuser --email admin@example.org --password '<password>'
podman-compose -f /opt/conversations/conversations/compose.yaml run --rm backend python manage.py bucket_cors --set
```

## Periodic Jobs

The `workers` sub-app runs Celery with beat embedded, so the worker container schedules its
own periodic tasks. Two upstream tasks apply only when you use Albert as the LLM provider:

- `fetch_model_health --provider albert` feeds the model outage banner and the fallback routing.
- `deindex_inactive_collections` removes stale RAG collections.

This collection does not schedule these two tasks. Run them by hand as one-off commands until
upstream ships a beat schedule for them:

```bash
st-cli oneoff conversations <env> -- python manage.py fetch_model_health --provider albert
st-cli oneoff conversations <env> -- python manage.py deindex_inactive_collections
```

## Bootstrap with st-cli

`st-cli bootstrap` prompts for the Conversations settings: the domain, the discrete `DB_*`
database values, `REDIS_URL`, S3 storage, the `AI_BASE_URL` / `AI_MODEL` / `AI_API_KEY` LLM
provider, the identity provider, optional SMTP, and cadvisor. See
[00-getting-started/01-st-cli.md](../00-getting-started/01-st-cli.md) for general st-cli usage.

## Troubleshooting

```bash
ssh <host>
sudo -iu conversations

# Service lifecycle
systemctl --user status conversations.service
systemctl --user start conversations.service
systemctl --user stop conversations.service

# Logs
journalctl --user -u conversations.service -f
journalctl --user -u conversations.service --since today
journalctl --user -u conversations.service --since "3 hours ago"

# Containers
podman-compose -f /opt/conversations/conversations/compose.yaml ps
```
