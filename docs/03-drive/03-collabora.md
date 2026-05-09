# Collabora Online

Collabora Online is a document editor integrated with Drive, based on LibreOffice technology.
It runs as a separate container alongside the Drive application.

## Container Stack

```text
collabora (docker.io/collabora/code)
  └── LibreOffice Online on port 9980
```

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 2 GB minimum (LibreOffice rendering is memory-intensive) |
| Disk | 1 GB for image |

## Variable Reference

See [roles/drive/REFERENCE.md](../../roles/drive/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_drive_collabora_enabled` | Enable Collabora | `false` |
| `st_drive_collabora_tag` | Docker image tag | `latest` |
| `st_drive_collabora_dir` | Application directory | `/opt/drive/collabora` |
| `st_drive_collabora_port` | Host port (maps to container 9980) | `50080` |
| `st_drive_collabora_env` | Environment content | _(empty)_ |
| `st_drive_collabora_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_drive_collabora_port` | `50080` | 9980 |

## Data & Volumes

Stateless. No persistent bind mounts.

## Troubleshooting

```bash
ssh <host>
sudo -iu drive

# Service lifecycle
systemctl --user status collabora.service
systemctl --user start collabora.service
systemctl --user stop collabora.service

# Logs
journalctl --user -u collabora.service -f
journalctl --user -u collabora.service --since today
journalctl --user -u collabora.service --since "3 hours ago"

# Containers
podman-compose -f /opt/drive/collabora/compose.yaml ps
```
