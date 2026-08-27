# File Scanner

The `file_scanner` role deploys a
[file-scanner](https://github.com/suitenumerique/file-scanner) instance for La Suite
Territoriale. file-scanner is a REST antivirus service: callers submit a file (or a URL
to download it from) and get back a per-category verdict (`malware` via ClamAV by
default). Today its only consumer in the suite is
[Transfers](../07-transfers/01-transfers.md), which uses it to scan completed uploads
before allowing downloads.

## Container Stack

```text
file-scanner-app    (ghcr.io/suitenumerique/file-scanner)
  └── uvicorn on port 8090 (published on the host as st_file_scanner_port)
      serves the REST API: /api/v1.0/scan, /api/v1.0/scan-async, /check, /metrics
file-scanner-worker (ghcr.io/suitenumerique/file-scanner)
  └── python -m worker — dramatiq worker running async scans + webhook delivery
file-scanner-clamav (docker.io/clamav/clamav)
  └── clamd on port 3310 (compose-internal only) + freshclam signature updater
file-scanner-redis  (docker.io/library/redis)
  └── dramatiq broker between the API and the worker (compose-internal only)
```

All four services run in a single compose unit (`/opt/file-scanner/file-scanner`),
mirroring the upstream `docker-compose.yml`. Scans stream to clamd over the network
(INSTREAM), so no filesystem is shared between the containers.

> [!NOTE]
> Unlike the Django apps of the suite, file-scanner needs **no external services at
> all**: no PostgreSQL, no S3, and its Redis broker and ClamAV daemon are bundled in
> the stack. The service is stateless — async results are pushed to the caller via a
> signed webhook, nothing is persisted.

> [!NOTE]
> The worker is **not optional**: without it, `scan-async` requests are accepted
> (`202`) but jobs sit in the queue forever and no webhook is ever delivered. That is
> why it rides the same compose unit instead of being a separate, optionally-enabled
> component.

## Authentication

Scan requests carry a short-lived EdDSA (Ed25519) Bearer JWT, verified against the
caller's **public** key looked up by the token's `iss` claim:

- `JWT_ISSUER_KEYS` (prompted at bootstrap) lists the accepted callers as
  comma-separated `iss:base64url-pubkey` pairs, e.g.
  `transferts:<transfers-pubkey>`. Onboard a caller with
  `st-cli generate-keypairs file-scanner <env>`: it asks an issuer name per
  keypair and prints the private key to hand to that caller (shown once, never
  stored) plus the ready-to-paste `JWT_ISSUER_KEYS` value — merged with the
  unit's existing keys when it is already bootstrapped. (The upstream
  [`deploy/scripts/new-issuer.py`](https://github.com/suitenumerique/file-scanner/blob/main/deploy/scripts/new-issuer.py)
  does the same, one keypair at a time.) With no issuer keys configured, every
  request is rejected.
- `JWT_SIGNING_KEY` signs the outgoing webhooks; `st-cli` **generates** it (32 random
  bytes, base64url — a valid Ed25519 seed) and stores it in the vault. Receivers
  verify webhooks against `/.well-known/jwks.json`, where the key is advertised under
  the `JWT_SIGNING_KID` label (default `v1`; change it when rotating the key).

## Wiring with Transfers

The `st-cli bootstrap transfers` questionnaire's optional file-scanner integration
maps onto this service as follows:

| Transfers side | file-scanner side |
|----------------|-------------------|
| `CLAMAV_SERVICE_URL` = `http://<file-scanner-host>:50800` | the published API port (`st_file_scanner_port`) |
| `SCAN_JWT_PRIVATE_KEY` (private half, in the transfers vault) | public half registered in `JWT_ISSUER_KEYS` |
| `SCAN_JWT_ISSUER` (default `transferts`) | the `iss` prefix of that `JWT_ISSUER_KEYS` entry |
| `SCAN_JWT_AUDIENCE` (default `file-scanner`) | `JWT_AUDIENCE` (upstream default `file-scanner`) |
| `SCAN_WEBHOOK_BASE_URL` | must be reachable **from the scanner host** (webhook callback) |

Transfers submits a presigned S3 URL for the scanner to download, so the scanner host
needs outbound access to your S3 endpoint — setting `ALLOWED_URL_HOSTS` to that S3
host (asked optionally at bootstrap) restricts scan submissions to it; left blank,
any host may be submitted. If those hosts resolve to a **private** IP from the
scanner hosts (internal network / VPC gateway), answer yes to the follow-up
question — it sets `SSRF_ALLOWED_HOSTS` to the same list, without which the
worker's SSRF guard refuses to download from private addresses.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 3 GB minimum (clamd loads the full signature DB in memory) |
| Disk | 2 GB for images + the ClamAV signature volume |
| Network | Outbound HTTPS (signature updates + fetching the URLs to scan) |
| Database / S3 / IdP | **None** |

## Variable Reference

See [roles/file_scanner/REFERENCE.md](../../roles/file_scanner/REFERENCE.md) for the
complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_file_scanner_enabled` | Enable file-scanner (API + worker + clamav + redis) | `false` |
| `st_file_scanner_tag` | file-scanner docker image tag | see REFERENCE.md |
| `st_file_scanner_port` | Host port (maps to the API's uvicorn 8090) | `50800` |
| `st_file_scanner_uid` | Unix UID for the file-scanner user | `1108` |
| `st_file_scanner_env` | Environment content (API + worker) | _(empty)_ |
| `st_file_scanner_clamav_tag` | ClamAV image tag | `1.4` |
| `st_file_scanner_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_file_scanner_port` | `50800` | 8090 (API) |
| `st_file_scanner_cadvisor_port` | `127.0.0.1:50899` | 8080 (cadvisor) |

clamd (3310) and the Redis broker stay on the compose network — nothing but the API
port is published on the host.

## Monitoring

`GET /metrics` exposes Prometheus metrics (scan counters, durations, signature
freshness). Because the endpoint rides the published API port and its `api_client`
label names the calling services, the bootstrap questionnaire **generates a
`PROMETHEUS_API_KEY` by default** (kept in the vault — read it with `st-cli secrets`
and set it as the scrape job's bearer token). Decline the confirm only when the port
is isolated at the network layer.

Sync-scan counters are exposed by the API process; async scans are counted in the
worker process, which serves no HTTP — with the default stack, expect the async
counters to be absent from scrapes (see the upstream
[monitoring notes](https://github.com/suitenumerique/file-scanner/blob/main/docs/deployment.md#monitoring)).

The upstream queue dashboard is **disabled by default** (no
`WORKER_DASHBOARD_PASSWORD` set — the route is not even mounted). It exposes
destructive queue operations and scan-job arguments; read the upstream
[dashboard warnings](https://github.com/suitenumerique/file-scanner/blob/main/docs/deployment.md#queue-dashboard)
before enabling it via the env blob.

## First deploy

On its first start the clamav container downloads the full signature database
(~300 MB) before reporting healthy, and the API/worker wait for it — allow several
minutes before the stack settles. The signatures persist in the `clamav_data` volume,
so restarts and redeploys are fast.

## Upgrades & rollback

There is no database and no migration step: upgrading is bumping
`st_file_scanner_tag` and redeploying. `st_file_scanner_rollback_enabled` (default
`false`) restores the previous compose/env directory and restarts the unit on a
failed deploy.

## Troubleshooting

```bash
ssh <host>
sudo -iu file-scanner

# Service lifecycle
systemctl --user status file-scanner.service
systemctl --user restart file-scanner.service

# Logs
journalctl --user -u file-scanner.service -f

# Containers (clamav takes a while to turn healthy on first start)
podman-compose -f /opt/file-scanner/file-scanner/compose.yaml ps

# Liveness (no auth required)
curl http://localhost:50800/check
```
