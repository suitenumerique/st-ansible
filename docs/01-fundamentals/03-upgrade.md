# Upgrade Guide

## Upgrading the Collection

1. Update the collection version in your `galaxy-requirements.yml`:

```yaml
collections:
  - name: https://github.com/suitenumerique/st-ansible.git
    type: git
    version: "2"  # change to target version
```

1. Reinstall the collection:

```bash
ansible-galaxy collection install -r galaxy-requirements.yml --force
```

1. Re-run your playbook. The podman role will pull new images and restart the systemd units.

## Image Tag Strategy

Each application role has a `tag` variable (e.g. `st_messages_tag`, `st_drive_tag`, `st_keycloak_tag`) that controls
which container image version to deploy. The default is `main` (messages, drive) or `latest` (keycloak, collabora).

For production deployments, pin image tags to specific versions:

```yaml
st_messages_tag: "v1.2.3"
st_keycloak_tag: "26.0"
```

## Rollback During Upgrade

Enable rollback to automatically revert if an upgrade fails:

```yaml
st_messages_rollback_enabled: true
st_drive_rollback_enabled: true
st_keycloak_rollback_enabled: true
```

When rollback is enabled, the podman role:

1. Copies the current application directory to `<dir>.rollback` before deploying
2. If the deployment fails, restores the backup and restarts the old version
3. If the deployment succeeds, removes the rollback directory

> [!IMPORTANT]
> We do not suggest to enable rollback for deployments with data directories
> like rspamd or if you add a redis/postgresql to the compose.

## Database Migrations

Both Messages and Drive run Django migrations automatically during deployment
(via `st_messages_backend_run_migrations` / `st_drive_backend_run_migrations`, both default `true`).

**With `serial:` deployments:** Migrations have `run_once: true`, which means they run once per play. If you use
`serial:`, the play is split into batches, and migrations will run on every batch. Set `run_migrations: false`
on all hosts except one to avoid this. For example :

```yaml
st_drive_backend_run_migrations: "{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}"
st_messages_backend_run_migrations: "{{ true if inventory_hostname == ansible_play_hosts_all[0] else false }}"
```

## Checking Running Versions

```bash
sudo -iu <user>
podman ps --format "{{.Names}} {{.Image}}"
```
