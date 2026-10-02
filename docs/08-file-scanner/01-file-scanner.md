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
file-scanner-exav   (ghcr.io/sylvinus/exav)   — optional, off by default
  └── second malware engine, clamd protocol on 3310 (see "Second engine: exav")
```

All three services run in a single compose unit (`/opt/file-scanner/file-scanner`).
Scans stream to clamd over the network (INSTREAM), so no filesystem is shared between
the containers. The dramatiq broker the API and the worker talk through is an
**external Redis** (`WORKER_BROKER_URL`), like every other Redis in this collection —
the upstream `docker-compose.yml` bundles one, this role does not.

> [!NOTE]
> Redis aside, file-scanner needs no external service: no PostgreSQL, no S3, no IdP,
> and its ClamAV daemon is bundled in the stack. The service is stateless — async
> results are pushed to the caller via a signed webhook, nothing is persisted.

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
  `transfers:<transfers-pubkey>`. Onboard a caller with
  `st-cli generate-keypairs file-scanner <env>`: it asks an issuer name per
  keypair and prints the private key to hand to that caller (shown once, never
  stored) plus the ready-to-paste `JWT_ISSUER_KEYS` value — merged with the
  unit's existing keys when it is already bootstrapped. (The upstream
  [`deploy/scripts/new-issuer.py`](https://github.com/suitenumerique/file-scanner/blob/main/deploy/scripts/new-issuer.py)
  does the same, one keypair at a time.) With no issuer keys configured, every
  request is rejected.
- `JWT_SIGNING_KEY` signs the outgoing webhooks; `st-cli bootstrap` **generates** it
  (32 random bytes, base64url — a valid Ed25519 seed) and stores it in the vault.
  Receivers verify webhooks against `/.well-known/jwks.json`, where the key is
  advertised under the `JWT_SIGNING_KID` label (default `v1`; change it when rotating
  the key). To mint one yourself — rotating the key, or running the
  [hashi_vault backend](../../cli/README.md#secret-backends), which generates nothing
  and expects the secret to pre-exist in OpenBao — use
  `st-cli generate-keypairs file-scanner <env> --signing-key`: it prints the seed and
  the public half it will advertise, and (like the caller keypairs) writes nothing
  anywhere.

## Wiring with Transfers

The `st-cli bootstrap transfers` questionnaire's optional file-scanner integration
maps onto this service as follows:

| Transfers side | file-scanner side |
|----------------|-------------------|
| `CLAMAV_SERVICE_URL` = `http://<file-scanner-host>:50800` | the published API port (`st_file_scanner_port`) |
| `SCAN_JWT_PRIVATE_KEY` (private half, in the transfers vault) | public half registered in `JWT_ISSUER_KEYS` |
| `SCAN_JWT_ISSUER` (default `transfers`) | the `iss` prefix of that `JWT_ISSUER_KEYS` entry |
| `SCAN_JWT_AUDIENCE` (default `file-scanner`) | `JWT_AUDIENCE` (upstream default `file-scanner`) |
| `SCAN_API_VERSION` (default `v2.0`) | the API path the submissions use — the scanner must serve that version **before** transfers is upgraded |
| `SCAN_SCANNERS` (optional) | engine names, checked against the scanner's `DEFAULT_SCANNERS` / `EXAV_HOSTS` |
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
| RAM | 3 GB minimum (clamd loads the full signature DB in memory). **exav needs much more**: its own database is ~2.1 GB resident, plus a per-job budget per worker — and compiling that database instead of fetching it spikes ~3.6 GB on every start. Read [Sizing](#sizing-read-the-startup-log) before enabling it: an undersized host does not fail, it scans less |
| Disk | 2 GB for the images + the ClamAV signature volume, **plus** temporary spool: clamd writes each INSTREAM to its temp directory, so up to `CLAMD_CONF_StreamMaxLength` (2200M by default) per scan running in parallel. Size for your peak concurrency, or cap it with `CLAMD_CONF_MaxThreads` in `st_file_scanner_clamav_env` |
| Network | Outbound HTTPS (signature updates + fetching the URLs to scan) |
| Redis | External Redis 7+, the dramatiq broker (`WORKER_BROKER_URL`) |
| Database / S3 / IdP | **None** |

## Variable Reference

See [roles/file_scanner/REFERENCE.md](../../roles/file_scanner/REFERENCE.md) for the
complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_file_scanner_enabled` | Enable file-scanner (API + worker + clamav) | `false` |
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

clamd (3310) stays on the compose network — nothing but the API port is published on
the host. The broker is reached outbound, on whatever `WORKER_BROKER_URL` points at.

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

## Second engine: exav

[exav](https://exav.org) reads ClamAV's signature formats with its own engine. Set
`st_file_scanner_exav_enabled: true` and the role adds a `file-scanner-exav`
container to the same compose unit, on the clamd protocol like clamav.

**Deploying the daemon changes no verdict on its own.** The application only sends
a file to an engine it is told about, so two keys go in `st_file_scanner_env`:

```text
EXAV_HOSTS=exav:3310
DEFAULT_SCANNERS={"malware": ["clamav", "exav"]}
```

`DEFAULT_SCANNERS` maps each category to the engines composing it: the value above
runs both on every scan, `{"malware": ["exav"]}` replaces clamav. A third key,
`ADVISORY_SCANNERS`, names engines whose detections count but whose incomplete
scans do not stop a category from being clean — for an engine under evaluation, or
for clamav next to exav past clamav's 2 GiB ceiling. It is only read by
file-scanner releases that ship it; older ones ignore it (their settings use
`extra="ignore"`), so adding it early breaks nothing.

### Signatures: the one thing to decide

exav ships no database and has **no updater of its own** — ClamAV's CDN only serves
`freshclam`/`cvdupdate`. Two paths:

| | Prebuilt database (default) | Compile locally |
|---|---|---|
| Config | `EXAV_DB_URL=https://…/your.exavdb` (+ the default `EXAV_SIG_DIR=/var/lib/exav`) | `EXAV_SIG_DIR=/var/lib/clamav` |
| Source | a `.exavdb` you build and publish | the files the bundled clamav keeps fresh, mounted read-only |
| Start cost | seconds | a few minutes **and a ~3.6 GB RAM spike, on every start** |
| Resident cost | the loaded database — **~2.1 GB** for the full ClamAV set | the same, after the spike |
| Per-job budget | `EXAV_MAX_PROCESS_BYTES` (2 GiB) × `EXAV_WORKERS` (3), on top of the database | the same |

**Set `EXAV_DB_URL`.** `st_file_scanner_exav_env` is empty by default and the template
then emits `EXAV_SIG_DIR=/var/lib/exav`, `EXAV_UPDATE_INTERVAL_SECS=300`,
`EXAV_MAX_SPILL_BYTES=2200M` and `EXAV_MAX_SCAN_SECS=900`. Setting it replaces those,
so carry them over:

```yaml
st_file_scanner_exav_env: |
  EXAV_DB_URL=https://static.example.org/exav.exavdb
  EXAV_SIG_DIR=/var/lib/exav
  EXAV_UPDATE_INTERVAL_SECS=300
  EXAV_MAX_SPILL_BYTES=2200M
  EXAV_MAX_SCAN_SECS=900
```

Build the file wherever your updater runs
(`exav -d /var/lib/clamav --build-db exav.exavdb`), publish it on HTTPS behind a
stable URL that answers `HEAD` with an `ETag`, and the daemon swaps in a changed
file live (`EXAV_UPDATE_INTERVAL_SECS`). See the upstream
[Running exav](https://github.com/suitenumerique/file-scanner/blob/main/docs/deployment.md#running-exav)
guide for the build step.

Leave it unset and the daemon logs `waiting up to 1800s for signatures in
/var/lib/exav` and never reports healthy. That is deliberate: the API and the worker
are ordered after exav but **not gated on its health**, so the service keeps running
and the misconfiguration shows up as one unhealthy optional container.

The compile path is the one to avoid unless the host is sized for it: its spike is
transient but real, and on a 3 GB host the daemon is OOM-killed (exit 137) on every
start, which systemd retries in a loop.

### Sizing: read the startup log

The database is resident, and each worker wants its own scan budget on top. exav does
not fail when the host cannot back that — it **silently lowers the budget** and says so
once, at startup:

```text
exav: per-job memory 2048 MiB x 3 workers exceeds what this host can back;
      using 256 MiB per job (RAM minus the 2123 MiB shared database)
exav: extraction budget 1024 MiB exceeds the 256 MiB of address space available;
      using 128 MiB so a size limit is reported rather than the scan being killed
```

A degraded budget is not a crash, it is a smaller scan: a file whose extraction
exceeds it comes back with a limit reported — `partial` here, "not scanned" in
Transfers — instead of being examined. Worse, it defeats the usual reason for adding
exav: it cannot scan past clamav's 2 GiB ceiling either, so the deployment advertises
a coverage it does not deliver.

Measured on the full ClamAV set: clamd and the OS hold ~1.5 GB, exav's database 2.1 GB,
and each worker wants 2 GiB on top. **A 4 GB host leaves ~270 MB for all scans** — far
too little; **8 GB fits two workers at the default budget.** So check
`podman logs file-scanner-exav` after the first start, and if you see those lines,
pick one:

- **give the VM more RAM** — the honest fix if you need to scan large files: the
  database plus `EXAV_MAX_PROCESS_BYTES` × `EXAV_WORKERS`, plus clamd's own resident
  set, plus the OS;
- **lower `EXAV_WORKERS`** so each remaining worker gets a real budget, at the cost of
  concurrency (scans are queued asynchronously, so this is often the right trade) —
  but note the ceiling comes from the resident database, not the worker count: on a
  4 GB host even a single worker stays far below the 2 GiB it asks for;
- **set `EXAV_MAX_PROCESS_BYTES`** explicitly to what the host can back, which silences
  the warning without changing what actually happens.

The role mounts `clamav_data` read-only into the daemon either way, and gives it a
writable `exav_data` volume for the downloaded database. Its health probe is the
image's own (`/exav --ping`, which passes only once the database is loaded).

## clamd size limits

clamd enforces its own size caps, independent of the scanner's `MAX_URL_SIZE`
(2 GiB by default), and its stock defaults are far below it (`StreamMaxLength`
100M, `MaxFileSize` 100M, `MaxScanSize` 400M). Left alone, a file above
`StreamMaxLength` dies mid-INSTREAM on every retry, and a file above
`MaxFileSize`/`MaxScanSize` is **skipped and reported clean without being
scanned**. The role therefore ships `st_file_scanner_clamav_env` with all three
raised to `2200M` (matching upstream's compose); the clamav image applies each
`CLAMD_CONF_<Option>=<value>` line to `clamd.conf` at startup (same for
`FRESHCLAM_CONF_<Option>`), so any clamd/freshclam option can be tuned through
this blob in `vars.yml`:

```yaml
st_file_scanner_clamav_env: |
  CLAMD_CONF_StreamMaxLength=2200M
  CLAMD_CONF_MaxFileSize=2200M
  CLAMD_CONF_MaxScanSize=2200M
  CLAMD_CONF_MaxScanTime=900000
  CLAMD_CONF_AlertExceedsMax=yes
  FRESHCLAM_CONF_Checks=24
```

`st_file_scanner_clamav_env` is empty by default, and the template then emits exactly
the five lines above — so an empty value means "the built-in tuning", not "an empty env
file". Setting the variable **replaces** all of it, so carry over what you still want:
`AlertExceedsMax` especially, without which clamd answers OK on a scan it could not
finish and the file is reported clean without having been examined.

Note: clamd spools each INSTREAM to its temporary directory before scanning, so
the clamav container needs free disk to match the limit. If you lower the
scanner's `MAX_URL_SIZE`, the clamd caps only need to clear that value.

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
