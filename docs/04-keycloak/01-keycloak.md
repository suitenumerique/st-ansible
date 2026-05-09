# Keycloak

The `keycloak` role deploys a Keycloak identity provider instance for La Suite Territoriale applications.
It provides SSO authentication for Messages, Drive, and other platform components.

## Container Stack

```text
keycloak (ghcr.io/suitenumerique/messages-keycloak)
  └── Keycloak on port 8080
```

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
| `st_keycloak_tag` | Docker image tag | `latest` |
| `st_keycloak_port` | Host port (maps to container 8080) | `50080` |
| `st_keycloak_uid` | Unix UID for the keycloak user | `1100` |
| `st_keycloak_env` | Environment content | _(empty)_ |
| `st_keycloak_start_command` | Keycloak start command | `start --optimized` |
| `st_keycloak_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_keycloak_port` | `50080` | 8080 |

## Data & Volumes

Keycloak stores its state in an external PostgreSQL database. No persistent bind mounts are needed.

## Start Command

The default start command is `start --optimized`, which assumes that you're using LST keycloak docker image
(or that your Keycloak image has been pre-built). You can change this via `st_keycloak_start_command`
(e.g. `start-dev` for development, or `start --hostname-strict=false` for custom hostname setups).

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
