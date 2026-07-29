# La Suite Territoriale Ansible Collection

Ansible collection for deploying La Suite Territoriale applications on Debian systems
using rootless Podman containers managed by systemd user units, with the
[st-cli](cli/README.md) wrapper to bootstrap and operate deployments.

## How to use the collection

### st-cli (recommended)

[st-cli](cli/README.md) is a Python wrapper around this collection. It bootstraps a
versionable config tree (with `ansible-vault`-encrypted secrets), generates the
throwaway Ansible scaffolding, and drives deploy (ansible) plus restart / ps / one-off
/ reset / logs (ssh). It ships as a container image and via `pipx`, see the
[getting-started](/docs/00-getting-started/01-st-cli.md) for install and usage.

> [!NOTE]
> `st-cli` runs on your computer or on an "Orchestrator VM", with SSH access to
> the application VMs like ansible would.
> It is not designed to be run on the application VMs directly.

### Installing the Collection

We don't recommend this but if you want to use the collection in your fully
custom ansible repository you can. See the [getting-started](/docs/00-getting-started/02-ansible-galaxy.md) for install and usage.

## Documentation

You can find the documentation of the collection under the [docs/](docs/) directory.

- **[00-getting-started/](docs/00-getting-started/)** st-cli, ansible-galaxy install, architecture, podman base role
- **[01-drive/](docs/01-drive/)** drive app, workers, collabora
- **[02-keycloak/](docs/02-keycloak/)** keycloak identity provider
- **[03-meet/](docs/03-meet/)** meet app, livekit, egress (recording)
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

## Plugins

### compact stdout callback

`suitenumerique.st.compact` replaces the default task banners with one line per
task and host. It honors `display_ok_hosts` and `display_skipped_hosts`.

```text
✔ podman : Install podman packages @ meet1
● meet : Render the env file @ meet1 — changed
--- before: /home/meet/.env
+++ after: /home/meet/.env
✘ meet : Start the systemd unit @ meet1 — failed
fatal: [meet1]: FAILED! => ...
```

`st-cli` selects this callback in the `ansible.cfg` it generates. To use it in
your own setup:

```ini
[defaults]
stdout_callback = suitenumerique.st.compact
result_format = yaml
```

The plugin file is licensed GPL-3.0-or-later (required for code that subclasses
ansible-core), while the rest of the collection is MIT.

## License

MIT, see [LICENSE](LICENSE)
