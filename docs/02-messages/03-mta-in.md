# MTA-In (Mail Transfer Agent Inbound)

An inbound Postfix-based mail transfer agent. Receives incoming email and forwards it to the Messages backend for processing.

## Container Stack

```text
mta-in (ghcr.io/suitenumerique/messages-mta-in)
  └── Postfix on port 25
```

Optionally supports STARTTLS if a certificate is provided.

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 256 MB minimum |
| Network | Port 25 reachable from upstream mail servers |

## Variable Reference

See [roles/messages/REFERENCE.md](../../roles/messages/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_messages_mta_in_enabled` | Enable mta-in | `false` |
| `st_messages_mta_in_tag` | Docker image tag | `main` |
| `st_messages_mta_in_dir` | Application directory | `/opt/messages/mta-in` |
| `st_messages_mta_in_port` | Host port for SMTP | `50025` |
| `st_messages_mta_in_starttls_certificate_path` | Path to STARTTLS cert on host | _(not set)_ |
| `st_messages_mta_in_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_messages_mta_in_port` | `50025` | 25 |

## STARTTLS

To enable STARTTLS, set `st_messages_mta_in_starttls_certificate_path` to the path of a certificate file on the
remote host. The certificate must:

- Be in the `smtpd_tls_chain_files` format (see [Postfix docs](https://www.postfix.org/postconf.5.html#smtpd_tls_chain_files))
- Be readable by the `messages` user

## Data & Volumes

When STARTTLS is enabled, the certificate file is bind-mounted into the container at `/etc/postfix/starttls_certificate.pem`.

## Troubleshooting

```bash
ssh <host>
sudo -iu messages

# Service lifecycle
systemctl --user status mta-in.service
systemctl --user start mta-in.service
systemctl --user stop mta-in.service

# Logs
journalctl --user -u mta-in.service -f
journalctl --user -u mta-in.service --since today
journalctl --user -u mta-in.service --since "3 hours ago"

# Containers
podman-compose -f /opt/messages/mta-in/compose.yaml ps
```
