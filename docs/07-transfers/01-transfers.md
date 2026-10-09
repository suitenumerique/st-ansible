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
  └── gunicorn (transfers.wsgi) on port 8000
transfers-worker   (ghcr.io/suitenumerique/transfers-backend)
  └── python worker.py — a Celery worker with the beat scheduler embedded
```

The frontend and backend run in a single compose unit (`/opt/transfers/transfers`); the
worker is a separate, optionally-enabled unit (`/opt/transfers/workers`). The frontend
image bundles Caddy, so — unlike the Drive role — there is **no `nginx.conf`** template:
the reverse-proxy config is baked into the image and only its runtime targets are set
through env (`TRANSFERS_FRONTEND_BACKEND_SERVER`).

> [!NOTE]
> This collection provisions neither PostgreSQL, Redis, nor object storage. You must
> provide them externally before deploying Transfers. We recommend a managed database
> and Redis (e.g. [Scalingo](https://scalingo.com) or [Scaleway](https://www.scaleway.com)).

## Authentication

Login is OIDC (mozilla-django-oidc), wired to your identity provider through
`OIDC_RP_CLIENT_ID` / `OIDC_RP_CLIENT_SECRET` and the `OIDC_OP_*` endpoints. The
`st-cli bootstrap transfers` questionnaire supports Keycloak, ProConnect
(integration / production) and a custom issuer, exactly like Drive and Meet.

## Client IPs behind the balancer

`TRANSFERS_FRONTEND_TRUSTED_PROXIES` names the proxies whose `X-Forwarded-For` Caddy
may believe, and this collection sets it to `private_ranges` in the frontend env.

**Upstream ships no default, deliberately**, and that is right for them: they cannot
assume how the container port is forwarded. With a path that rewrites the source
address (slirp4netns, rootlesskit) every request reaches Caddy from a private address,
so `private_ranges` would trust them all — including one straight off the internet —
and any client could forge the header that `DJANGO_ADMIN_IP_ALLOWLIST` filters on.

This collection can assume it: rootless podman on Debian Trixie forwards through
**pasta** (`podman info` → `rootlessNetworkCmd: pasta`), which **preserves the client
address**. A request arriving directly on the published port therefore keeps its public
source, falls outside `private_ranges` and stays untrusted; only the balancer, on a
private address, is believed. Verify it on your own host rather than trusting this
paragraph, with a request that does **not** go through the balancer — that is the only
one whose source address is in question:

```bash
# from a machine outside the private network, straight at the published port
curl -s -o /dev/null http://<vm-address>:50700/
# then on the VM
sudo -iu transfers podman logs --tail 20 transfers-frontend \
  | grep -oE '"remote_ip":[[:space:]]*"[^"]*"'
```

That request's `remote_ip` must be the public address of the machine you ran `curl`
from. A gateway or any other private address means the source is rewritten, and
`private_ranges` must then be replaced — not kept. Balancer traffic in the log proves
nothing either way: it arrives from a private address whether or not direct requests
keep theirs. If the published port is already firewalled to the balancer, that firewall
is the control and the question is moot — open it to your test machine only.

What it still trusts, by design: **every** machine on that private network, and local
processes on the VM itself (pasta shows host-originated traffic as its gateway). If
those are not trusted, narrow it:

```yaml
st_transfers_frontend_env: |
  TRANSFERS_FRONTEND_BACKEND_SERVER=transfers-backend:8000
  TRANSFERS_FRONTEND_TRUSTED_PROXIES=10.0.0.8/32      # the balancer, and nothing else
```

Restricting the host port to the balancer at the firewall is the stronger control, and
it is the only one available to messages and keycloak, which read `X-Forwarded-For`
with no trusted-proxy notion at all.

### The Django admin allowlist

Transfers 0.4.0 adds `DJANGO_ADMIN_IP_ALLOWLIST`, a space-separated CIDR list of the
client IPs Caddy admits on the admin URL — it answers 403 to the others. This
collection leaves it unset, which keeps the admin open to anyone who reaches the
frontend, like upstream. Narrow it in the same env blob:

```yaml
st_transfers_frontend_env: |
  TRANSFERS_FRONTEND_BACKEND_SERVER=transfers-backend:8000
  TRANSFERS_FRONTEND_TRUSTED_PROXIES=10.0.0.8/32      # the balancer, and nothing else
  DJANGO_ADMIN_IP_ALLOWLIST=192.0.2.0/24              # your VPN or office egress range
```

Two traps. **Unset admits everyone, an empty value admits no one**: a bare
`DJANGO_ADMIN_IP_ALLOWLIST=` line locks you out rather than opening up. And the list
is matched against the `{client_ip}` the section above establishes, so narrowing it
while trusting the wrong proxies filters on a proxy's address instead of the user's:
either every request is admitted because the balancer is in your list, or every one is
refused because it is not. The two settings are a single decision — read a Caddy access
log line and compare its `client_ip` with the address you expect before trusting the
filter.

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
- the frontend env sets `TRANSFERS_FRONTEND_TRUSTED_PROXIES=private_ranges`, without
  which the image trusts no proxy and `{client_ip}` — the address the backend logs (the
  Caddyfile forwards `X-Forwarded-For {client_ip}`, and `USE_X_FORWARDED_FOR` is on)
  and the one `DJANGO_ADMIN_IP_ALLOWLIST` filters on — would be the TCP peer instead of
  the user. Read [Client IPs behind the balancer](#client-ips-behind-the-balancer)
  before narrowing or removing it
- the frontend Caddy sets `TRANSFERS_FRONTEND_S3_ORIGIN` so its Content-Security-Policy
  allows the browser to fetch presigned URLs from your S3 endpoint.

`st-cli bootstrap transfers` derives `TRANSFERS_FRONTEND_S3_ORIGIN` from the S3
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

## File scanning / antivirus (optional)

Transfers can submit a completed upload to an external **file-scanner** REST service
for an asynchronous virus scan; the verdict comes back through a webhook and gates
downloads. It is **off by default** (`SCAN_ENABLED=false`). The
`st-cli bootstrap transfers` questionnaire asks *"Configure the file-scanner (antivirus)
integration?"*; answering yes collects:

| Variable | Meaning |
|----------|---------|
| `SCAN_SERVICE_URL` | Base URL of the file-scanner REST service, reachable from the backend **and** worker (e.g. `http://10.0.0.20:50800`). Each submission carries the scan JWT and a presigned S3 URL, so plain HTTP only belongs on a private network — bootstrap warns when the URL is not `https://` |
| `SCAN_WEBHOOK_BASE_URL` | Base URL of this backend **as reachable from the scanner host** (webhook callback). The backend port is not published — use the public transfers URL (the frontend Caddy proxies `/api` to the backend), e.g. `https://transfers.example.org` |
| `SCAN_JWT_PRIVATE_KEY` | EdDSA (Ed25519) private key minting request-bound scan JWTs — **secret** (routed through the vault). Mint the keypair with `st-cli generate-keypairs file-scanner <env>` (or the scanner's `deploy/scripts/new-issuer.py`) and register the public key on the scanner's `JWT_ISSUER_KEYS` |
| `SCAN_JWT_ISSUER` / `SCAN_JWT_AUDIENCE` | JWT `iss` / `aud` claims (defaults `transfers` / `file-scanner`) |
| `SCAN_SCANNERS` | Optional comma-separated engine names sent with every submission (`clamav`, `exav`, …). Empty leaves the choice to the scanner's own `DEFAULT_SCANNERS`. Naming several runs them all, and a file is clean only if every engine cleared it — see [08-file-scanner](../08-file-scanner/01-file-scanner.md#second-engine-exav) |
| `SCAN_API_VERSION` | file-scanner API version the submissions and callbacks use. Defaults to `v2.0`, which this service is the only shape it parses — so **the scanner must serve v2.0 before this app is upgraded**, see the note below |
| `SCAN_JWT_TTL`, `SCAN_MAX_FILE_SIZE`, `SCAN_PRESIGNED_URL_EXPIRY`, `SCAN_PENDING_REAP_MINUTES` | Optional tuning knobs. Upstream defaults them (300s, 2147483645 bytes, 3600s, 15 min), so leaving a prompt blank writes no line and the app keeps its own value — answer only to deviate. Writing them at their default would pin our copy and hide a later change upstream |

> [!WARNING]
> **A confidential transfer is never scanned.** Every transfer is encrypted in the
> browser, so the scanner can only examine one whose key reached the backend — which
> is the ordinary case: the submission carries the key, the chunk size and the AAD
> parts alongside the presigned URL. A **confidential** transfer withholds that key by
> design (the recipient supplies it), so finalize marks each of its files
> `scan_status=skipped` and submits nothing. Those files download with a "not scanned"
> notice instead of being gated. Set `TRANSFER_CONFIDENTIAL_ENABLED=false` in
> `st_transfers_backend_env` — it defaults to true — if the antivirus gate must cover
> every transfer; transfers already created in that mode stay downloadable.

> [!IMPORTANT]
> **Upgrade the scanner first.** Transfers submits to the file-scanner's `v2.0` API
> (`SCAN_API_VERSION`) and no longer parses the `v1.0` shape, so a scanner that only
> serves `v1.0` answers 404 on every submission and downloads stay gated. Deploy a
> file-scanner serving v2.0, then bump transfers — never the other way round.
>
> `SCAN_MAX_FILE_SIZE` counts the same bytes as the scanner's `MAX_URL_SIZE` (the
> plaintext for an encrypted file; the wire adds the chunking overhead): keep the two
> equal. Both default to 2147483645, the most clamav scans in one file, so leaving
> both alone is already consistent — which is why the bootstrap questionnaire only
> writes the key when you deviate.

The keys land in `st_transfers_backend_env`, which the worker unit reuses — so the
scan-submit and stale-scan reap Celery tasks see the same config. The scanner service
itself can be deployed by this collection — see
[08-file-scanner](../08-file-scanner/01-file-scanner.md) and
`st-cli bootstrap file-scanner` — or run externally; either way, point
`SCAN_SERVICE_URL` at it.

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
