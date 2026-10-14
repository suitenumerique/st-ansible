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
          ├── clamav (docker.io/clamav/clamav) or exav (ghcr.io/sylvinus/exav), antivirus
          ├── unbound (docker.io/alpinelinux/unbound), DNS resolver
          └── valkey (docker.io/valkey/valkey), rspamd state (bayes, history)
```

All services communicate via the Podman bridge network. Only Caddy publishes ports to the host.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 2 GB minimum (clamav is memory-hungry; exav loads the full prebuilt database in about 1 GB) |
| Disk | 4 GB minimum (clamav signature database ~1.5 GB; the exav prebuilt database ~350 MB) |
| Network | Caddy port reachable from pymta or upstream |

## Variable Reference

See [roles/messages/REFERENCE.md](../../roles/messages/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_messages_mpa_enabled` | Enable the MPA | `false` |
| `st_messages_mpa_dir` | Application directory | `/opt/messages/mpa` |
| `st_messages_mpa_antivirus_engine` | Antivirus engine behind rspamd: `clamav` or `exav` | `clamav` |
| `st_messages_mpa_exav_db_url` | URL of the prebuilt `.exavdb` file. Required with `exav` | _(empty)_ |
| `st_messages_mpa_auth_bearer` | Bearer token for Caddy auth | _(required)_ |
| `st_messages_mpa_rspamd_controller_password` | Rspamd webui password | _(required)_ |
| `st_messages_mpa_caddy_port` | Host port for `/checkv2` | `50402` |
| `st_messages_mpa_caddy_healthcheck_port` | Host port for `/healthcheck` | `50403` |
| `st_messages_mpa_rspamd_controller_port` | Host port for rspamd controller | `50404` |
| `st_messages_mpa_blacklist_domains` | Domains to blacklist | `[]` |
| `st_messages_mpa_blacklist_ips` | IPs/CIDRs to blacklist | `[]` |
| `st_messages_mpa_whitelist_domains` | Domains to whitelist | `[]` |
| `st_messages_mpa_whitelist_ips` | IPs/CIDRs to whitelist | `[]` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_messages_mpa_caddy_port` | `50402` | 80 |
| `st_messages_mpa_caddy_healthcheck_port` | `50403` | 90 |
| `st_messages_mpa_rspamd_controller_port` | `50404` | 11334 |

Rspamd's worker port (11333) is **internal-only** and not published to the host. All inter-service
communication uses DNS-resolvable container names via the Podman bridge network.

## Data & Volumes

```text
/opt/messages/mpa/
├── rspamd_config/
│   ├── local.d/          # rspamd config overrides (Ansible-managed)
│   └── override.d/       # rspamd hard overrides (Ansible-managed)
├── rspamd_data/          # rspamd runtime data + .map files
├── clamav_config/        # clamd.conf (engine clamav)
├── exav_data/            # exav database (engine exav)
├── unbound_config/       # unbound.conf
├── caddy_config/         # Caddyfile
└── valkey_data/          # valkey persistence
```

Ownership is set to container UIDs:

- `rspamd_config/` and `rspamd_data/` → UID 11333 (rspamd container user)
- `valkey_data/` → UID 999 (valkey container user)
- `exav_data/` → UID 65532 (exav container user)

## Rspamd Configuration

The role deploys a set of default rspamd config templates. You can override or extend them via
`st_messages_mpa_rspamd_config_templates`: custom entries are merged with the defaults. If a custom entry
targets the same `dest` path as a default, the custom entry replaces the default entirely.

Default configs include:

- **antivirus.conf**, antivirus over the clamd protocol, `clamav:3310` or `exav:3310`
- **phishing.conf**, OpenPhish + phishcatch enabled
- **multimap.conf**, Blacklist/whitelist domain and IP maps
- **mime_types.conf**, Extended dangerous extension list
- **classifier-bayes.conf**, Bayes autolearn enabled, 100 learns per class before classification
- **redis.conf** / **history_redis.conf**, Valkey at `valkey:6379`
- **worker-controller.inc** / **worker-proxy.inc**, Controller and proxy settings
- **options.inc** / **logging.inc**, General options

## Antivirus engine: exav

`exav` is an alternative antivirus daemon for the mpa app. It reads ClamAV signature databases and
answers the clamd protocol on port 3310, so rspamd's `antivirus.conf` module type does not change.

clamav stays the default engine. Set `st_messages_mpa_antivirus_engine` to `exav` to use exav instead:

```yaml
st_messages_mpa_antivirus_engine: exav
st_messages_mpa_exav_db_url: https://files.example.org/signatures/clamav.exavdb
```

With the exav engine, the role:

- deploys an `exav` container (`st_messages_mpa_exav_image`, `st_messages_mpa_exav_tag`) instead of clamav
- creates an `exav_data/` directory for the database, owned by UID 65532
- does not create the clamav container or the `clamav_config/` directory

exav ships no signature database and has no updater of its own. You must build a `.exavdb` file with
`exav --build-db` from the ClamAV signature files, then publish it on any HTTP(S) URL. Point
`st_messages_mpa_exav_db_url` at that URL. The URL can carry basic auth as `user:pass@`, over `https://`
only. exav downloads the file to `exav_data/remote.exavdb` before its first load, then checks the URL
again every 300 seconds with a conditional HEAD request and hot-reloads the file when it changes. The
deploy task asserts that the URL is not empty when the engine is `exav`, so a missing URL fails the
deploy early. See the [prebuilt database guide](https://exav.org/scanner/guides/prebuilt-database/) for
how to build and size the file.

Loading the full main+daily+bytecode prebuilt database (about 352 MB) takes about 1.3 seconds and uses
about 1 GB of RAM. The first start downloads the file before the daemon reports healthy. The systemd
unit waits `st_messages_mpa_timeout` seconds (300 by default) for the stack: raise it on a slow link.
Use `st_messages_mpa_exav_env` for extra `EXAV_*` lines, for example `EXAV_WORKERS` or
`EXAV_MAX_PROCESS_BYTES`. The role renders them into the exav env file from
`st_messages_mpa_exav_env_template`. The compose file sets `EXAV_DB_URL` itself.

The compose unit does not remove the container of the previous engine when you switch. Remove the old
container once, as the `messages` user, before the deploy that switches the engine:

```sh
podman rm -f clamav   # or: podman rm -f exav
```

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
