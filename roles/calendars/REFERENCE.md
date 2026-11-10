<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.calendars
Version: 0.7.0
This role deploys the Calendars application from La Suite Territoriale on a rootless podman base on Debian systems.

Tags: suiteterritoriale, system

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments

### Entrypoint: main

Installs and configures the Calendars application from La Suite Territoriale on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_calendars_public_host | The public hostname used to access the calendars application. | str | no |  |
| st_calendars_uid | UID of the `calendars` user, used for the podman role. | int | no | `1110` |
| st_calendars_gid | GID of the `calendars` group, used for the podman role. | int | no | `{{ st_calendars_uid }}` |
| st_calendars_registries | Optional private container registries to login the `calendars` user onto. | list of `dict` | no |  |
| st_calendars_frontend_image | Image repository for the calendars frontend. | str | no | `ghcr.io/suitenumerique/calendars-frontend` |
| st_calendars_frontend_tag | Tag of the calendars frontend image to deploy. Upstream publishes only the mutable `main` tag, so the default pins it by digest. | str | no | `main@sha256:216c097eea804322df2b60247d00a7deba5e01c5a001b7809118ab4c52e82aab` |
| st_calendars_backend_image | Image repository for the calendars backend and workers. | str | no | `ghcr.io/suitenumerique/calendars-backend` |
| st_calendars_backend_tag | Tag of the calendars backend image to deploy. Upstream publishes only the mutable `main` tag, so the default pins it by digest. | str | no | `main@sha256:bb5411a05a02c7ac3ea96d8ec9971fef57be026534333c134d9978bf19182cd2` |
| st_calendars_caldav_image | Image repository for the calendars CalDAV server (SabreDAV). | str | no | `ghcr.io/suitenumerique/calendars-caldav` |
| st_calendars_caldav_tag | Tag of the calendars CalDAV server image to deploy. Upstream publishes only the mutable `main` tag, so the default pins it by digest. | str | no | `main@sha256:074da1f141d697d8ce0667c6fd6df3208ec70ce6af5b53537ac5dd5f2c814a57` |
| st_calendars_enabled | Triggers the installation of the calendars application. | bool | no | `False` |
| st_calendars_dir | Remote path to the base directory for calendars app. | str | no | `/opt/calendars/calendars` |
| st_calendars_port | The host published port for the calendars caddy edge. | str | no | `51000` |
| st_calendars_rollback_enabled | Whether or not to trigger the rollback tasks if the calendars deployment fails. | bool | no | `False` |
| st_calendars_compose_template | Local path to the custom template to use for calendars compose file. | str | no | `calendars/compose.yaml.j2` |
| st_calendars_backend_env_template | Local path to the custom template to use for calendars backend env file. | str | no | `calendars/backend_env.j2` |
| st_calendars_backend_env | Content of the default backend_env_template, not used if st_calendars_backend_env_template is defined. | str | no |  |
| st_calendars_backend_secret_files | Secrets that the container reads from files. Each item has the key "env" (the variable name in upper case) and one of the keys "src" (local template) or "content" (inline value). The role writes the file "secrets/<env>" in the application directory. The role mounts the "secrets" directory read-only at /run/secrets. The role sets "<ENV>_FILE" in the compose environment. The "<ENV>_FILE" variable has priority over the same variable in the env content. The CalDAV server does not read these files. | list of `dict` | no | `[]` |
| st_calendars_backend_run_migrations | Whether to run the database migrations on deploy. The role runs the Django migrations, then the CalDAV server schema script (init-database.sh). | bool | no | `True` |
| st_calendars_caldav_env_template | Local path to the custom template to use for calendars CalDAV server env file. | str | no | `calendars/caldav_env.j2` |
| st_calendars_caldav_env | Content of the default caldav_env_template, not used if st_calendars_caldav_env_template is defined. The CalDAV server requires PGHOST, PGDATABASE, PGUSER, PGPASSWORD, CALDAV_INBOUND_API_KEY, CALDAV_OUTBOUND_API_KEY, CALDAV_INTERNAL_API_KEY and CALDAV_CALLBACK_BASE_URL. The three API keys must have the same values as in the backend env. | str | no |  |
| st_calendars_messages_enabled | Turns on the Messages integration (mailbox calendars). The role sets FEATURE_MESSAGES_INTEGRATION, MESSAGES_API_URL and MESSAGES_CHANNEL_ID in the compose environment of the backend and of the worker, and gives them MESSAGES_API_KEY as a secret file. These values have priority over the same variables in the env content. | bool | no | `False` |
| st_calendars_messages_api_url | Base URL of the Messages application, for example https://messages.example.org. Required when st_calendars_messages_enabled is true. | str | no |  |
| st_calendars_messages_channel_id | Id of the global api_key channel that a Messages superadmin creates for Calendars. Required when st_calendars_messages_enabled is true. | str | no |  |
| st_calendars_messages_api_key | API key of this Messages channel. Messages shows it once, at channel creation. Required when st_calendars_messages_enabled is true. | str | no |  |
| st_calendars_caddy_env_template | Local path to the custom template to use for calendars caddy env file. | str | no | `calendars/caddy_env.j2` |
| st_calendars_caddy_env | Content of the default caddy_env_template, not used if st_calendars_caddy_env_template is defined. Every key is optional: CADDY_ADMIN_IP_ALLOWLIST (space-separated CIDR list of client IPs allowed on the Django admin URL, default allows all) and CADDY_TRUSTED_PROXIES (space-separated CIDR list of proxies whose X-Forwarded-For sets the client IP, default private_ranges). | str | no |  |
| st_calendars_caddy_image | Image repository for the calendars caddy reverse-proxy. | str | no | `docker.io/caddy` |
| st_calendars_caddy_tag | The tag of the caddy docker image to use. See https://hub.docker.com/_/caddy/tags. | str | no | `2.11.7-alpine` |
| st_calendars_workers_enabled | Triggers the installation of the calendars workers. | bool | no | `False` |
| st_calendars_workers_dir | Remote path to the base directory for calendars workers. | str | no | `/opt/calendars/workers` |
| st_calendars_workers_env_template | Local path to the custom template to use for calendars workers env file. | str | no | `workers/env.j2` |
| st_calendars_workers_env | Content of the default workers_env_template, not used if st_calendars_workers_env_template is defined. | str | no | `{{ st_calendars_backend_env }}` |
| st_calendars_workers_secret_files | Secrets that the container reads from files. Each item has the key "env" (the variable name in upper case) and one of the keys "src" (local template) or "content" (inline value). The role writes the file "secrets/<env>" in the application directory. The role mounts the "secrets" directory read-only at /run/secrets. The role sets "<ENV>_FILE" in the compose environment. The "<ENV>_FILE" variable has priority over the same variable in the env content. The default value is the list of the backend. | list of `dict` | no | `{{ st_calendars_backend_secret_files }}` |
| st_calendars_workers_caldav_env_template | Local path to the custom template to use for the env file of the workers CalDAV server. | str | no | `workers/caldav_env.j2` |
| st_calendars_workers_caldav_env | Content of the default workers_caldav_env_template, not used if st_calendars_workers_caldav_env_template is defined. | str | no | `{{ st_calendars_caldav_env }}` |
| st_calendars_workers_mailbox_sync_enabled | Runs the Messages mailbox sync (manage.py sync_mailbox_acls) on a systemd user timer on this workers host. The timer exists only when st_calendars_messages_enabled is true. Set it to false on every workers host except one, because one run syncs every user. | bool | no | `True` |
| st_calendars_workers_mailbox_sync_timer | Systemd OnCalendar value of the mailbox sync timer. | str | no | `hourly` |
| st_calendars_workers_rollback_enabled | Whether or not to trigger the rollback tasks if the workers deployment fails. | bool | no | `False` |
| st_calendars_workers_compose_template | Local path to the custom template to use for workers compose file. | str | no | `workers/compose.yaml.j2` |
| st_calendars_cadvisor_enabled | Triggers the installation of the cadvisor container, a Prometheus-compliant containers monitoring tool. | bool | no | `False` |
| st_calendars_cadvisor_image | Image repository for the cadvisor container. | str | no | `ghcr.io/google/cadvisor` |
| st_calendars_cadvisor_tag | Tag of the cadvisor docker image to deploy. | str | no | `v0.60.6` |
| st_calendars_cadvisor_port | The host published port of the cadvisor container. | str | no | `127.0.0.1:51099` |

## Dependencies

None.

## Example Playbook

```yaml
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.calendars
      ansible.builtin.import_role:
        name: suitenumerique.st.calendars
      vars:
```

## License

MIT

## Author and Project Information

La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
