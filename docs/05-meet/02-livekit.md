# LiveKit Server

LiveKit is the video/audio backend for Meet. It handles WebRTC media routing, TURN relay, and
recording (egress). The default deployment uses a standalone compose with caddy-l4 for TLS
termination, livekit-server, egress, and valkey.

> [!IMPORTANT]
> LiveKit uses `network_mode: host` because of the wide range of UDP ports required.
> You should always deploy LiveKit on a **dedicated host**. As DNAT is unreliable with
> WebRTC, LiveKit must also be deployed **with a dedicated public IP** bound to the host.

## Container Stack

```text
caddy-l4 (docker.io/livekit/caddyl4)
  └── layer4 TLS termination → livekit-server (:7880)
                                  ├── egress (recording)
                                  └── valkey (redis-compatible, state)
```

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 2 GB minimum |
| Network | Host network (ports 7880, 7881, 5349, 3478, UDP 50000-60000) |
| DNS | Two domains: one for livekit, one for TURN |

## Variable Reference

See [roles/meet/REFERENCE.md](../../roles/meet/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_meet_livekit_enabled` | Enable livekit | `false` |
| `st_meet_livekit_domain` | Domain for the livekit server | **(required)** |
| `st_meet_livekit_turn_domain` | Domain for the TURN server | **(required)** |
| `st_meet_livekit_api_key` | LiveKit API key | **(required)** |
| `st_meet_livekit_api_secret` | LiveKit API secret | **(required)** |
| `st_meet_livekit_dir` | Application directory | `/opt/meet/livekit` |
| `st_meet_livekit_tag` | livekit-server image tag | `latest` |
| `st_meet_livekit_egress_tag` | egress image tag | `latest` |
| `st_meet_livekit_caddyl4_tag` | caddy-l4 image tag | `latest` |
| `st_meet_livekit_valkey_tag` | valkey image tag | `latest` |
| `st_meet_livekit_rollback_enabled` | Rollback on failure | `false` |
| `st_meet_livekit_files` | List of config files to deploy | _(defaults)_ |
| `st_meet_livekit_directories` | List of directories to create | _(defaults)_ |

## Network & Ports

All services use `network_mode: host`. No port mapping; they bind directly to host interfaces.

| Port | Protocol | Service |
|------|----------|---------|
| 7880 | TCP | LiveKit HTTP (WebSocket, API) |
| 7881 | TCP | LiveKit RTC over TCP |
| 5349 | TCP | TURN over TLS |
| 3478 | UDP | TURN |
| 50000-60000 | UDP | WebRTC media (RTP/RTCP) |

The caddy-l4 container handles TLS termination on port 443 and routes traffic based on SNI:
requests to `st_meet_livekit_turn_domain` go to the TURN server (port 5349), requests to
`st_meet_livekit_domain` go to livekit-server (port 7880).

> [!NOTE]
> Since all services use `network_mode: host`, livekit must run on dedicated hosts. It cannot
> share a host with other applications that bind the same ports.

## Data & Volumes

```text
/opt/meet/livekit/
├── livekit.yaml        # livekit-server config
├── caddy.yaml          # caddy-l4 config
├── egress.yaml         # egress config
├── valkey_config/      # valkey config
├── caddy_data/         # caddy TLS certificates
└── valkey_data/        # valkey persistence
```

## Custom Configuration

The `st_meet_livekit_files` variable controls which config files are deployed. The defaults
deploy `livekit.yaml`, `caddy.yaml`, `valkey.conf`, and `egress.yaml` from the role's templates.
Override this entirely for custom setups:

```yaml
st_meet_livekit_files:
  - src: my-custom/livekit.yaml.j2
    dest: livekit.yaml
  - src: my-custom/caddy.yaml.j2
    dest: caddy.yaml
```

## Troubleshooting

```bash
ssh <host>
sudo -iu meet

# Service lifecycle
systemctl --user status livekit.service
systemctl --user start livekit.service
systemctl --user stop livekit.service

# Logs
journalctl --user -u livekit.service -f
journalctl --user -u livekit.service --since today
journalctl --user -u livekit.service --since "3 hours ago"

# Containers
podman-compose -f /opt/meet/livekit/compose.yaml ps

# Check livekit health
curl -s http://localhost:7880
```
