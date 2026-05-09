# SOCKS Proxy

A SOCKS proxy service for the Messages stack. Runs with `network_mode: host` for full network access.

The SOCKS Proxy in our setup is used by the `MTA_OUT_MODE=direct` Messages environment variable and is only
holding the outgoing public IPs for Messages outgoing SMTP packets.

> [!CAUTION]
> Setting up a public IP for SMTP deliverability is a complex task that requires experience
> with mail infrastructure. This is out of scope for this documentation. If you are not
> familiar with it, we recommend using a managed SMTP gateway with the MTA_OUT_MODE=relay
> Messages environment variable instead.

## Container Stack

```text
socks-proxy (ghcr.io/suitenumerique/messages-socks-proxy)
  └── network_mode: host
```

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 128 MB minimum |
| Network | Host network access required |

## Variable Reference

See [roles/messages/REFERENCE.md](../../roles/messages/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_messages_socks_proxy_enabled` | Enable the proxy | `false` |
| `st_messages_socks_proxy_tag` | Docker image tag | `main` |
| `st_messages_socks_proxy_dir` | Application directory | `/opt/messages/socks-proxy` |
| `st_messages_socks_proxy_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

Uses `network_mode: host` with no port mapping. The proxy listens directly on host network interfaces.
Port configuration is handled within the container's environment.

## Data & Volumes

Stateless. No persistent bind mounts.

## Troubleshooting

```bash
ssh <host>
sudo -iu messages

# Service lifecycle
systemctl --user status socks-proxy.service
systemctl --user start socks-proxy.service
systemctl --user stop socks-proxy.service

# Logs
journalctl --user -u socks-proxy.service -f
journalctl --user -u socks-proxy.service --since today
journalctl --user -u socks-proxy.service --since "3 hours ago"

# Containers
podman-compose -f /opt/messages/socks-proxy/compose.yaml ps
```
