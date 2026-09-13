# Keycloak

The `keycloak` role deploys a Keycloak identity provider instance for La Suite Territoriale applications.
It provides SSO authentication for Messages, Drive, and other platform components.

## Container Stack

```text
keycloak (ghcr.io/suitenumerique/messages-keycloak)
  ├── Caddy on port 50200 (st_keycloak_port), admin IP allowlist
  │     └── Keycloak on 127.0.0.1:8081
  └── Keycloak management (health, metrics) on port 9000
```

The image sets the Keycloak HTTP and proxy settings. The image binds Keycloak to the
loopback interface, so Caddy is its intended peer. With `network_mode: host`, this
loopback is the host loopback. See the warning in [Network & Ports](#network--ports).
The role requires image tag 0.11.0 or later, which ships the Caddy front (messages PR #793).

The image is a customized Keycloak distribution from the Suite Territoriale project.

> [!NOTE]
> This collection does not provision PostgreSQL. You must provide it externally before
> deploying Keycloak. We recommend using managed database services from cloud providers such as
> [Scalingo](https://scalingo.com) or [Scaleway](https://www.scaleway.com).

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 1 GB minimum |
| Disk | 1 GB for image + data |
| Database | External PostgreSQL (configured via env) |

## Variable Reference

See [roles/keycloak/REFERENCE.md](../../roles/keycloak/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_keycloak_enabled` | Enable Keycloak | `false` |
| `st_keycloak_tag` | Docker image tag | see REFERENCE.md |
| `st_keycloak_port` | Host port Caddy listens on | `50200` |
| `st_keycloak_uid` | Unix UID for the keycloak user | `1102` |
| `st_keycloak_env` | Environment content | _(empty)_ |
| `st_keycloak_start_command` | Keycloak start command | `start --optimized` |
| `st_keycloak_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

The container uses `network_mode: host`. It binds these ports on the host.

| Variable | Default port | Interface | Service |
|----------|---------------|-----------|---------|
| `st_keycloak_port` | `50200` | all | Caddy. The load balancer targets this port. |
| _(image default)_ | `8081` | `127.0.0.1` only | Keycloak |
| _(image default)_ | `9000` | all | Keycloak management: `/health` and `/metrics` |

> [!WARNING]
> The firewall must block port 9000 from the internet. This port has no authentication.

> [!WARNING]
> The container uses `network_mode: host`, so port 8081 is on the host loopback interface.
> A local process can reach Keycloak on port 8081 and bypass the Caddy allowlist.
> Keycloak also trusts the `X-Forwarded-For` header from any local process.
> Do not run untrusted workloads on the Keycloak host.

## Admin Console IP Allowlist

The image adds two optional environment variables. Set them as lines in `st_keycloak_env`.

| Variable | Default | Description |
|----------|---------|--------------|
| `KEYCLOAK_ADMIN_IP_ALLOWLIST` | `0.0.0.0/0 ::/0` | Space-separated CIDR list of client IPs allowed on `/admin`, `/admin/*`, `/realms/master`, and `/realms/master/*`. Caddy answers 403 to denied requests. |
| `KEYCLOAK_TRUSTED_PROXIES` | _(empty)_ | Space-separated CIDR list of upstream proxies whose `X-Forwarded-For` sets the client IP. The keyword `private_ranges` includes all private ranges. Do not use `private_ranges` when untrusted machines share the private network with Caddy. |

Example:

```yaml
st_keycloak_env: |
  # Load balancer range: trusted proxy only.
  KEYCLOAK_TRUSTED_PROXIES=203.0.113.0/24
  # Operator network and Messages backend network.
  KEYCLOAK_ADMIN_IP_ALLOWLIST=198.51.100.0/24 192.0.2.0/24
```

Follow these rules:

- If you set `KEYCLOAK_ADMIN_IP_ALLOWLIST`, add your own administration network. When the
  list does not include it, you lose access to the admin console and to `kcadm`.
- If you set `KEYCLOAK_ADMIN_IP_ALLOWLIST` and Messages runs with `IDENTITY_PROVIDER=keycloak`,
  add the network of the Messages backend. The Messages backend calls the admin REST API
  `/admin/realms/<realm>/*` with its service account. When the list does not include the
  backend network, the identity sync fails with 403.
- Do not set `KEYCLOAK_ADMIN_IP_ALLOWLIST` to an empty value. An empty list denies every admin
  request.
- Set `KEYCLOAK_TRUSTED_PROXIES` to the load balancer ranges. When the list is empty, Caddy
  uses the load balancer IP as the client IP. The allowlist then applies to the load
  balancer IP, not to the client. Keycloak also records the load balancer IP in events and
  in brute force detection.

See the upstream documentation for details:
[Keycloak production image proxy (Caddy)](https://github.com/suitenumerique/messages/blob/main/docs/env.md#keycloak-production-image-proxy-caddy).

## Environment Constraints

The image sets the following values. `st_keycloak_env` must not set `KC_HTTP_HOST`, `KC_HTTP_ENABLED`,
`KC_PROXY_HEADERS`, or `KC_PROXY_TRUSTED_ADDRESSES`. Set `KC_HOSTNAME` to the public URL.

Keycloak listens on `KC_HTTP_PORT` and Caddy connects to it. You can set `KC_HTTP_PORT` in
`st_keycloak_env` when port 8081 collides with another service on the host. You can set
`KC_HTTP_MANAGEMENT_HOST=127.0.0.1` to bind port 9000 to the loopback interface.

A custom `st_keycloak_compose_template` must set `PORT`, not `KC_HTTP_PORT`, for the port Caddy
listens on.

## Data & Volumes

Keycloak stores its state in an external PostgreSQL database. No persistent bind mounts are needed.

## Start Command

The default start command is `start --optimized`, which assumes that you're using LST keycloak docker image
(or that your Keycloak image has been pre-built). You can change this via `st_keycloak_start_command`
(e.g. `start-dev` for development, or `start --hostname-strict=false` for custom hostname setups).
The image starts Caddy only for the `start` and `start-dev` commands.

## Troubleshooting

```bash
ssh <host>
sudo -iu keycloak

# Service lifecycle
systemctl --user status keycloak.service
systemctl --user start keycloak.service
systemctl --user stop keycloak.service

# Logs
journalctl --user -u keycloak.service -f
journalctl --user -u keycloak.service --since today
journalctl --user -u keycloak.service --since "3 hours ago"

# Containers
podman-compose -f /opt/keycloak/keycloak/compose.yaml ps
```

> [!NOTE]
> You can ignore the log line "Keycloak is running inside a container, but is not PID 1".
> The image entrypoint runs Caddy and Keycloak together, so Keycloak is not PID 1.
