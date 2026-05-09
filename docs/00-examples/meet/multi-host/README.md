# Multi-Host Meet + LiveKit

> [!WARNING]
> This setup is provided as an example to illustrate how the collection works.
> It is not meant to be used as-is in production. Adjust images, passwords, TLS settings,
> and backup strategies to your needs.

> [!IMPORTANT]
> This setup requires external Redis, external PostgreSQL, external S3 and an external OIDC provider make sure they are
> provisioned and reachable before running the playbooks. The setup also requires an external reverse proxy,
> out of scope of this collection. If that seems too complicated for your needs, use PaaS platforms such as [Scalingo](https://github.com/suitenumerique/meet/blob/main/docs/installation/scalingo.md)
> for the Meet application and use this collection for the LiveKit part.

> [!IMPORTANT]
> LiveKit uses `network_mode: host` because of the wide range of UDP ports required.
> You should always deploy LiveKit on a **dedicated host**. As DNAT is unreliable with
> WebRTC, LiveKit must also be deployed **with a dedicated public IP** bound to the host.

This example deploys the **Meet** application and its **LiveKit** video conferencing
infrastructure across multiple hosts:

- **Meet** — frontend + backend, behind a reverse proxy (not included).
- **LiveKit** — video server with caddy layer4 proxy, Scaleway DNS ACME, per-host RTC interface binding.
- **Egress** — standalone egress worker for recording and streaming, connects to LiveKit via WebSocket.

LiveKit needs its own public IP for WebRTC traffic (UDP ports 50000-60000). Egress
requires `CAP_SYS_ADMIN` on the host for screen recording.

## External Services

| Service   | PostgreSQL | Redis | S3 |
|-----------|:----------:|:-----:|:--:|
| meet      | x          | x     | x  |
| livekit   |            | x     |    |
| egress    |            | x     | x  |

## Playbooks

| Playbook                     | Hosts | Description                           |
|------------------------------|:-----:|---------------------------------------|
| `playbook_meet.yml`          | 2     | Meet app (frontend + backend)         |
| `playbook_livekit_multi.yml` | 2     | LiveKit with custom caddy-l4 proxy    |
| `playbook_livekit_egress.yml`| 1     | Standalone egress worker              |

### playbook_meet.yml

Standard Meet deployment. Requires PostgreSQL, Redis, and S3. The `NEXT_PUBLIC_LIVEKIT_URL`
variable must point to your LiveKit domain so the frontend can connect to the video server.

### playbook_livekit_multi.yml

Overrides the compose and config files with custom templates from `templates/livekit-multi/`:

- **caddy** — layer4 proxy that terminates TLS and routes by SNI (LiveKit API on `:7880`,
  TURN on `:5349`). ACME certificates via Scaleway DNS.
- **livekit.yaml** — binds RTC traffic to a specific network interface (`public_interface`).
- **External Redis** — shared across LiveKit nodes for state.

### playbook_livekit_egress.yml

Deploys a standalone egress worker on a dedicated host. Egress connects to LiveKit via
WebSocket (`ws_url`) and uses the same external Redis as the LiveKit servers.

> [!NOTE]
> Egress requires `CAP_SYS_ADMIN` for screen recording. Make sure the host allows this
> capability (e.g. via `podman --cap-add SYS_ADMIN`).

## Key Variables

### Meet

| Variable | Description |
|----------|-------------|
| `st_meet_tag` | Meet Docker image tag |
| `st_meet_frontend_env` | Frontend environment variables (newline-separated) |
| `st_meet_backend_env` | Backend environment variables (newline-separated) |

### LiveKit

| Variable | Description |
|----------|-------------|
| `st_meet_livekit_domain` | LiveKit server domain (used for TLS/SNI) |
| `st_meet_livekit_turn_domain` | TURN server domain |
| `st_meet_livekit_api_key` | LiveKit API key |
| `st_meet_livekit_api_secret` | LiveKit API secret |
| `st_meet_livekit_tag` | LiveKit server image tag |
| `st_meet_livekit_egress_tag` | LiveKit egress image tag |
| `st_meet_livekit_caddyl4_tag` | caddy-l4 image tag |
| `public_interface` | Network interface for RTC traffic (livekit hosts) |

### Egress

| Variable | Description |
|----------|-------------|
| `st_meet_livekit_egress_tag` | LiveKit egress image tag |
| `st_meet_livekit_domain` | LiveKit server domain (for `ws_url`) |
| `st_meet_livekit_api_key` | LiveKit API key |
| `st_meet_livekit_api_secret` | LiveKit API secret |
| `redis_host` | External Redis address (`host:port`) |
| `redis_username` | External Redis username |
| `redis_password` | External Redis password |

## Running

```bash
# Install the collection
ansible-galaxy collection install -r galaxy-requirements.yml --force

# Deploy the meet application
ansible-playbook -i hosts playbook_meet.yml

# Deploy livekit (multi-host mode)
ansible-playbook -i hosts playbook_livekit_multi.yml

# Deploy livekit egress (standalone)
ansible-playbook -i hosts playbook_livekit_egress.yml
```
