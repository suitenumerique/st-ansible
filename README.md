# La Suite Territoriale Ansible Collection

Ansible collection for deploying La Suite Territoriale applications on Debian systems
using rootless Podman containers managed by systemd user units.

## Installing the Collection

Add a `galaxy-requirements.yml` file:

```yaml
collections:
  - name: https://github.com/suitenumerique/st-ansible.git
    type: git
    version: "1"
```

Then install:

```bash
ansible-galaxy collection install -r galaxy-requirements.yml
```

## Documentation

You can find the documentation of the collection under the [docs/](docs/) directory.

- **[00-getting-started/](docs/00-getting-started/)** st-cli, ansible-galaxy install, architecture, podman base role
- **[01-drive/](docs/01-drive/)** drive app, workers, collabora
- **[02-keycloak/](docs/02-keycloak/)** keycloak identity provider
- **[03-meet/](docs/03-meet/)** meet app, livekit
- **[04-messages/](docs/04-messages/)** messages app, workers, mta-in, socks-proxy, mpa
- **[monitoring.md](docs/monitoring.md)** cAdvisor + Grafana Alloy
- **[backup.md](docs/backup.md)** Restic backup
- **[troubleshooting.md](docs/troubleshooting.md)** common issues and debug commands
- **[99-examples/](docs/99-examples/)** playbook examples:
  - [full-high-availability](docs/99-examples/full-high-availability/)
  - [meet](docs/99-examples/meet/)

## Roles

| Role | Description | Reference |
|------|-------------|-----------|
| podman | Rootless Podman base | [REFERENCE.md](roles/podman/REFERENCE.md) |
| messages | Messages application | [REFERENCE.md](roles/messages/REFERENCE.md) |
| drive | Drive application | [REFERENCE.md](roles/drive/REFERENCE.md) |
| keycloak | Keycloak identity provider | [REFERENCE.md](roles/keycloak/REFERENCE.md) |
| meet | Meet video conferencing | [REFERENCE.md](roles/meet/REFERENCE.md) |
| alloy | Grafana Alloy telemetry | [REFERENCE.md](roles/alloy/REFERENCE.md) |
| restic | Restic backup | [REFERENCE.md](roles/restic/REFERENCE.md) |

## License

MIT, see [LICENSE](LICENSE)
