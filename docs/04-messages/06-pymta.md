# pymta (Python Inbound MTA)

A pure-Python inbound mail transfer agent, built on aiosmtpd. It replaces the Postfix-based
mta-in and receives incoming email for the Messages backend to process.

## Container Stack

```text
pymta (ghcr.io/suitenumerique/messages-mta-in-py)
  └── python -m pymta.server, host network, uid 65532
```

The container runs one process, `python -m pymta.server`, as the non-root uid 65532 inside the
rootless `messages` user namespace.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | see the memory formula below |
| Network | SMTP port reachable from upstream mail servers or the load balancer |

pymta buffers each incoming message in memory while it processes it. Peak RSS follows this
formula:

```text
PYMTA_MAX_CONCURRENT_DATA × PYMTA_MAX_INCOMING_EMAIL_SIZE × 2.2 + 64 MiB
```

For example, `PYMTA_MAX_CONCURRENT_DATA=40` and a 35 MiB message size limit give
`40 × 35 MiB × 2.2 + 64 MiB ≈ 3.1 GiB`. Size the host RAM above this peak.

## Variable Reference

See [roles/messages/REFERENCE.md](../../roles/messages/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_messages_pymta_enabled` | Enable pymta | `false` |
| `st_messages_pymta_tag` | Docker image tag | see REFERENCE.md |
| `st_messages_pymta_dir` | Application directory | `/opt/messages/pymta` |
| `st_messages_pymta_port` | Host port for SMTP | `50425` |
| `st_messages_pymta_metrics_port` | Host port for the Prometheus metrics endpoint | `50426` |
| `st_messages_pymta_starttls_certificate_path` | Path to the STARTTLS PEM file on the host | _(not set)_ |
| `st_messages_pymta_rollback_enabled` | Rollback on failure | `false` |
| `st_messages_pymta_env` | Content of the pymta env file | _(empty)_ |

## Environment

Set `st_messages_pymta_env` to the content of the pymta env file. This example shows a
production configuration behind a load balancer, with the domain and secrets replaced by
placeholders:

```yaml
st_messages_pymta_env: |
  MDA_API_SECRET=changeme
  MDA_API_BASE_URL=https://messages.example.com/api/v1.0/
  PYMTA_SMTP_HOSTNAME=mx.example.com
  PYMTA_ENABLE_PROXY_PROTOCOL=haproxy
  PYMTA_TRUSTED_PROXIES=203.0.113.10
  PYMTA_MAX_INCOMING_EMAIL_SIZE=36700160
  PYMTA_MAX_CONCURRENT_DATA=40
```

`PYMTA_MAX_INCOMING_EMAIL_SIZE` must match the backend `MAX_INCOMING_EMAIL_SIZE` setting.

Do not set `PYMTA_SMTP_BIND_PORT`, `PYMTA_METRICS_BIND_HOST`, `PYMTA_METRICS_BIND_PORT`,
`PYMTA_TLS_CERT_FILE` or `PYMTA_TLS_KEY_FILE` in `st_messages_pymta_env`. The compose
`environment:` block owns these keys and overrides the env file.

## Network & Ports

| Variable | Default | Purpose |
|----------|---------|---------|
| `st_messages_pymta_port` | `50425` | SMTP, on the host network stack (`PYMTA_SMTP_BIND_PORT`) |
| `st_messages_pymta_metrics_port` | `50426` | Prometheus metrics, bound to `127.0.0.1` only |

pymta runs with `network_mode: host`, so these are ports on the host network stack, not
published ports. Set `st_messages_pymta_metrics_port` to `0` to disable the metrics endpoint.
Set `PYMTA_METRICS_API_KEY` to require a bearer token on `/metrics`.

pymta listens on `50425`, not on port 25. The load balancer or a DNAT rule on the host must
forward port 25 to `st_messages_pymta_port`.

mta-in and pymta both use port `50425` by default. Two components cannot listen on the same
port on one host at the same time; run them on separate hosts or change one port.

## PROXY protocol

Behind a load balancer, pymta must read the client IP from the PROXY protocol header. pymta does
not support a load balancer that does not send this header: every session then carries the load
balancer's address instead of the real client address.

Set `PYMTA_ENABLE_PROXY_PROTOCOL` to `haproxy` or `true`, and set `PYMTA_TRUSTED_PROXIES` to a
comma-separated list of the load balancer IPs or CIDRs allowed to send the header.

## STARTTLS

To enable STARTTLS, set `st_messages_pymta_starttls_certificate_path` to the path of a PEM file
on the remote host. The file must hold the private key first, then the certificate chain, in
that order.

The role bind-mounts this file at `/starttls_certificate.pem` (read-only) and sets both
`PYMTA_TLS_CERT_FILE` and `PYMTA_TLS_KEY_FILE` to that path.

The container runs as uid 65532 inside the rootless `messages` user namespace, so the host file
must be readable by that mapped uid. Choose one of these two options:

The file must belong to the `messages` user before you run option 1. As root, run
`chown messages <file>` first.

```bash
# Option 1: map uid 65532 to the file owner, inside the messages user namespace
sudo -iu messages podman unshare chown 65532 /path/to/file.pem

# Option 2: make the file world-readable
chmod 0444 /path/to/file.pem
```

Option 2 lets every local account read the private key. Use option 1 on a shared host.

Repeat this step after every certificate renewal, then run:

```bash
systemctl --user restart pymta.service
```

## Data & Volumes

pymta is stateless. The only bind mount is the optional STARTTLS certificate file.

## Graceful stop

pymta waits up to `PYMTA_SHUTDOWN_TIMEOUT` (25 s) for in-flight SMTP sessions to finish before
it exits. The role sets `stop_grace_period` to 30 s in the compose file so Podman waits long
enough for this shutdown to complete.

## Migrating from mta-in

1. mta-in and pymta both use port `50425` by default, so they cannot run on the same host at
   the same time. Stop mta-in first, or deploy pymta on a new host.
2. Rename the mta-in environment variables to their pymta equivalents: `MYHOSTNAME` becomes
   `PYMTA_SMTP_HOSTNAME`. Remove `STARTTLS_CHAIN_FILES` from the env. The role sets
   `PYMTA_TLS_CERT_FILE` and `PYMTA_TLS_KEY_FILE` from `st_messages_pymta_starttls_certificate_path`
   and overrides the env file.
3. Set `st_messages_pymta_enabled: true` and move the environment content from
   `st_messages_mta_in_env` to `st_messages_pymta_env`.
4. Set `st_messages_mta_in_enabled: false` when pymta operates correctly.
5. For st-cli users, run `st-cli bootstrap messages <env> -c pymta` from the root of
   your deployment repo, not on the target host.

## Troubleshooting

```bash
ssh <host>
sudo -iu messages

# Service lifecycle
systemctl --user status pymta.service
systemctl --user start pymta.service
systemctl --user stop pymta.service

# Logs
journalctl --user -u pymta.service -f
journalctl --user -u pymta.service --since today
journalctl --user -u pymta.service --since "3 hours ago"

# Containers
podman-compose -f /opt/messages/pymta/compose.yaml ps

# Metrics
curl -s http://127.0.0.1:50426/metrics | head

# Health status
podman inspect pymta --format '{{.State.Health.Status}}'
```
