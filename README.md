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

- **[01-fundamentals/](docs/01-fundamentals/)** architecture, podman base role, deployment, upgrade guide
- **[02-messages/](docs/02-messages/)** messages app, workers, mta-in, socks-proxy, mpa
- **[03-drive/](docs/03-drive/)**  drive app, workers, collabora
- **[04-keycloak/](docs/04-keycloak/)** keycloak identity provider
- **[05-meet/](docs/05-meet/)** meet app, livekit
- **[06-monitoring.md](docs/06-monitoring.md)** cAdvisor + Grafana Alloy
- **[07-backup.md](docs/07-backup.md)** Restic backup
- **[08-troubleshooting.md](docs/08-troubleshooting.md)** common issues and debug commands
- **[00-examples/](docs/00-examples/)** playbook examples:
  - [full-high-availability](docs/00-examples/full-high-availability/)
  - [meet](docs/00-examples/meet/)

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
