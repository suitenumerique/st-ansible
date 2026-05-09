# MPA (Mail Processing Agent)

A standalone rspamd-based mail processing stack. Scans incoming email for spam, phishing, and malware.
Deployed as a multi-container compose application behind a Caddy reverse proxy with bearer authentication.
The default MPA deployment is entirely standalone and does not need any high-availability, because Messages
will automatically bypass the MPA if it's not available. This allows to setup multiple standalone MPA instances
with different configurations or blacklists/whitelists for different domains for examples.

## Container Stack

```text
caddy (docker.io/caddy)
  └── reverse proxy with bearer auth → rspamd /checkv2
      └── rspamd (docker.io/rspamd/rspamd)
          ├── clamav (docker.io/clamav/clamav), antivirus
          ├── unbound (docker.io/alpinelinux/unbound), DNS resolver
          └── valkey (docker.io/valkey/valkey), rspamd state (bayes, history)
```

All services communicate via the Podman bridge network. Only Caddy publishes ports to the host.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 2 GB minimum (clamav is memory-hungry) |
| Disk | 4 GB minimum (clamav signature database ~1.5 GB) |
| Network | Caddy port reachable from mta-in or upstream |

## Variable Reference

See [roles/messages/REFERENCE.md](../../roles/messages/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_messages_mpa_enabled` | Enable the MPA | `false` |
| `st_messages_mpa_dir` | Application directory | `/opt/messages/mpa` |
| `st_messages_mpa_auth_bearer` | Bearer token for Caddy auth | _(required)_ |
| `st_messages_mpa_rspamd_controller_password` | Rspamd webui password | _(required)_ |
| `st_messages_mpa_caddy_port` | Host port for `/checkv2` | `50080` |
| `st_messages_mpa_caddy_healthcheck_port` | Host port for `/healthcheck` | `50090` |
| `st_messages_mpa_rspamd_controller_port` | Host port for rspamd controller | `11334` |
| `st_messages_mpa_blacklist_domains` | Domains to blacklist | `[]` |
| `st_messages_mpa_blacklist_ips` | IPs/CIDRs to blacklist | `[]` |
| `st_messages_mpa_whitelist_domains` | Domains to whitelist | `[]` |
| `st_messages_mpa_whitelist_ips` | IPs/CIDRs to whitelist | `[]` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_messages_mpa_caddy_port` | `50080` | 80 |
| `st_messages_mpa_caddy_healthcheck_port` | `50090` | 90 |
| `st_messages_mpa_rspamd_controller_port` | `11334` | 11334 |

Rspamd's worker port (11333) is **internal-only** and not published to the host. All inter-service
communication uses DNS-resolvable container names via the Podman bridge network.

## Data & Volumes

```text
/opt/messages/mpa/
├── rspamd_config/
│   ├── local.d/          # rspamd config overrides (Ansible-managed)
│   └── override.d/       # rspamd hard overrides (Ansible-managed)
├── rspamd_data/          # rspamd runtime data + .map files
├── clamav_config/        # clamd.conf
├── unbound_config/       # unbound.conf
├── caddy_config/         # Caddyfile
└── valkey_data/          # valkey persistence
```

Ownership is set to container UIDs:

- `rspamd_config/` and `rspamd_data/` → UID 11333 (rspamd container user)
- `valkey_data/` → UID 999 (valkey container user)

## Rspamd Configuration

The role deploys a set of default rspamd config templates. You can override or extend them via
`st_messages_mpa_rspamd_config_templates`: custom entries are merged with the defaults. If a custom entry
targets the same `dest` path as a default, the custom entry replaces the default entirely.

Default configs include:

- **antivirus.conf**, ClamAV integration via `clamav:3310`
- **phishing.conf**, OpenPhish + phishcatch enabled
- **multimap.conf**, Blacklist/whitelist domain and IP maps
- **mime_types.conf**, Extended dangerous extension list
- **redis.conf** / **history_redis.conf**, Valkey at `valkey:6379`
- **worker-controller.inc** / **worker-proxy.inc**, Controller and proxy settings
- **options.inc** / **logging.inc**, General options

## Blacklist/Whitelist

Lists are managed via Ansible variables and rendered as `.map` files in `rspamd_data/`:

```yaml
st_messages_mpa_blacklist_domains:
  - spam-domain.example.com
st_messages_mpa_blacklist_ips:
  - "192.0.2.0/24"
st_messages_mpa_whitelist_domains:
  - trusted-partner.gouv.fr
```

## Troubleshooting

```bash
ssh <host>
sudo -iu messages

# Service lifecycle
systemctl --user status mpa.service
systemctl --user start mpa.service
systemctl --user stop mpa.service

# Logs
journalctl --user -u mpa.service -f
journalctl --user -u mpa.service --since today
journalctl --user -u mpa.service --since "3 hours ago"

# Containers
podman-compose -f /opt/messages/mpa/compose.yaml ps

# Check rspamd directly
podman exec rspamd rspamc ping
podman exec rspamd rspamc stat
```
