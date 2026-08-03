# Y-Provider (Collaboration Server)

The y-provider is the real-time collaboration server for Docs. It holds the live editing state
for each document ("room") in memory, so it runs as its own standalone sub-application (own
`yprovider.service` systemd user unit, own `/opt/docs/yprovider` directory, own published port),
independent of the core caddy + frontend + backend compose.

## Why a Separate Unit

A collaboration session lives in the y-provider process memory, scoped to one document room. If
two yprovider replicas served the same room, each would hold a different in-memory state and
edits would diverge. The upstream Helm chart prevents this with the ingress annotation
`upstream-hash-by: $arg_room`, which pins every request for a given room to the same pod. The
core caddy reproduces the same pinning on the `/collaboration/*` route with `lb_policy query
room` (see [Room Pinning](#room-pinning) below), so this collection needs the yprovider hosts to
be dispatched to from a single place — hence a dedicated unit with its own compose, hosts, and
port, listed independently in the `CADDY_YPROVIDER_ENDPOINTS` env value of `st_docs_caddy_env`.

## Container Stack

```text
y-provider (docker.io/lasuite/impress-y-provider), published on the host
  └── healthcheck: GET /ping
```

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 512 MB minimum |
| Depends on | Docs core application (shared secrets, public domain) |

## Variable Reference

See [roles/docs/REFERENCE.md](../../roles/docs/REFERENCE.md) for the complete variable
reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_docs_yprovider_enabled` | Enable the yprovider unit | `false` |
| `st_docs_yprovider_dir` | Application directory | `/opt/docs/yprovider` |
| `st_docs_yprovider_port` | Host port for the yprovider container | `50601` |
| `st_docs_tag` | Docker image tag, shared with the core images | `v5.4.1` |
| `st_docs_yprovider_env` | Environment content | _(empty)_ |
| `st_docs_yprovider_rollback_enabled` | Rollback on failure | `false` |
| `CADDY_YPROVIDER_ENDPOINTS` (in `st_docs_caddy_env`) | Space-separated `host:port` yprovider upstreams, set on the **core** side, consumed by the caddy `/collaboration/*` route | **(required)** |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_docs_yprovider_port` | `50601` | 4444 |

## Room Pinning

The core caddy's `/collaboration/*` route uses `reverse_proxy` with `lb_policy query room` over
every host in `CADDY_YPROVIDER_ENDPOINTS` (caddy substitutes the env value at parse time, so
the space-separated list expands into upstreams). Caddy hashes the `room` query argument to pick an
upstream, so every core caddy instance sends a given room to the same yprovider host, whichever
core node the browser first reached. This covers both the `/collaboration/ws/` WebSocket path
and the `/collaboration/api/` collaboration API path.

> [!IMPORTANT]
> `CADDY_YPROVIDER_ENDPOINTS` must list the same hosts, in the same order, on every core host.
> A mismatched list breaks the hash and can route one room to two different yprovider nodes.

## Scaling Notes

To add yprovider capacity, deploy the role on more hosts and append them to
`CADDY_YPROVIDER_ENDPOINTS` on every core host, then redeploy the core unit so all caddy
instances agree on the new list. Existing rooms keep the connection they already hold; only new
rooms hash across the updated set. `st-cli` builds this list from the yprovider hosts you enter
during the questionnaire and keeps it in sync across core hosts.

## Secrets

The core backend and the yprovider unit share two secrets:

| Variable | Purpose |
|----------|---------|
| `COLLABORATION_SERVER_SECRET` | Authenticates the backend against the yprovider WebSocket server |
| `Y_PROVIDER_API_KEY` | Authenticates the backend against the yprovider conversion API |

`st-cli` generates both secrets at bootstrap and mirrors them into the backend vault and the
yprovider vault. If you use the role without st-cli, set matching values on both sides.

## Conversion API Routing

The yprovider exposes two APIs, routed differently:

| API | Env key | Routing |
|-----|---------|---------|
| Room-stateful collaboration (WebSocket + room state) | `COLLABORATION_API_URL`, `COLLABORATION_WS_URL` | `/collaboration/*` on the public domain; the core caddy pins the room to one yprovider host |
| Stateless document conversion | `Y_PROVIDER_API_BASE_URL` | Internal, backend-only: `http://<first yprovider host>:50601/api/` — the browser never calls it |

`Y_PROVIDER_API_BASE_URL` is a server-to-server URL: the backend uses it to convert uploaded
`.md`/`.docx` files on import, to export document content as markdown/html/json, and for
server-to-server document creation. `st-cli` points it at the first yprovider endpoint. If you
have an internal load balancer, put its URL there to spread the calls over all the yprovider
hosts.

## Co-location With the Core

When the core and the yprovider run on the same single host, `st-cli` uses
`host.containers.internal:50601` (the podman host alias) for both `CADDY_YPROVIDER_ENDPOINTS`
and `Y_PROVIDER_API_BASE_URL`: the containers cannot always hairpin the host's public IP. With
more than one host, the real `host:port` list stays in place — every caddy must share one
identical list so the room hash stays consistent.

## Troubleshooting

```bash
ssh <host>
sudo -iu docs

# Service lifecycle
systemctl --user status yprovider.service
systemctl --user start yprovider.service
systemctl --user stop yprovider.service

# Logs
journalctl --user -u yprovider.service -f
journalctl --user -u yprovider.service --since today
journalctl --user -u yprovider.service --since "3 hours ago"

# Containers
podman-compose -f /opt/docs/yprovider/compose.yaml ps

# Check yprovider health
curl -s http://localhost:50601/ping
```
