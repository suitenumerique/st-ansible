# Troubleshooting

## General Debugging Workflow

All application containers run as systemd user units under a dedicated Unix user. The debugging workflow is:

```bash
# 1. SSH into the host
ssh <host>

# 2. Switch to the application user
sudo -iu <user>
# e.g. sudo -iu messages

# 3. Check the systemd unit status
systemctl --user status <app>.service
# e.g. systemctl --user status messages.service

# 4. View logs
journalctl --user -u <app>.service -f
journalctl --user -u <app>.service --since today
journalctl --user -u <app>.service --since '2 days ago'

# 5. Check container status
podman ps

# 6. Get a shell in a container
podman exec -it <container_name> sh
```

## Common Issues

### Service Fails to Start

```bash
# Check the systemd unit logs
journalctl --user -u <app>.service --no-pager -n 50

# Check if the compose file is valid
podman-compose -f /opt/<user>/<app>/compose.yaml config
```

### Container Keeps Restarting

```bash
# Check the restart policy
systemctl --user cat <app>.service

# View container logs
podman logs <container_name> --tail 50
```

### Cannot Pull Images

```bash
# Check registry authentication
podman login <registry> --authfile ~/.config/containers/auth.json

# Try pulling manually
podman pull <image>:<tag>
```

### Port Already in Use

```bash
# Find what's using the port
ss -tlnp | grep <port>

# Check other podman containers
podman ps -a --format "{{.Names}} {{.Ports}}"
```

### Permission Denied on Bind-Mounted Files

Files mounted into containers may need ownership adjusted for the container's sub-UID. The podman role handles this
automatically via `container_uid` in `st_podman_application_files` and `st_podman_application_directories`.
If you see permission errors:

```bash
# Check file ownership in the container's user namespace
podman unshare ls -la /opt/<user>/<app>/
# Fix ownership as root in the user-namespace (host's user == root in the user-namespace)
podman unshare chown root:root /opt/<user>/<app>/<file>
```

## Per-Role Quick Reference

| Role | User | Common units |
|------|------|-------------|
| messages | `messages` | `messages`, `workers`, `mta-in`, `socks-proxy`, `mpa` |
| drive | `drive` | `drive`, `workers`, `collabora` |
| keycloak | `keycloak` | `keycloak` |
| restic | `restic` | `restic-backup`, `restic-forget` (system services, not user) |
| alloy | `alloy` | `alloy` (system service, not user) |

## Restic / Alloy (System Services)

Restic and Alloy run as **system** systemd services (not user units), so the debugging commands differ:

```bash
# Check system service status
sudo systemctl status restic-backup.service
sudo systemctl status alloy.service

# View logs
sudo journalctl -u restic-backup.service -f
sudo journalctl -u alloy.service -f
```

## Podman Socket Issues

If containers fail to start with socket errors:

```bash
# Check the podman socket
systemctl --user status podman.socket

# Restart it
sudo -iu <user>
systemctl --user restart podman.socket
```

## Podman Storage Cleanup

If disk space is low:

```bash
sudo -iu <user>
# Check what uses disk
podman system df -v
# Remove all unused pods, containers, images, networks, and volume data
podman system prune
# Clean only dangling images
podman image prune
# Clean only images dangling or unused
podman image prune -a
```
