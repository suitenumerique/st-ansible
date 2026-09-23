<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.conversations
Version: 0.4.0

This role deploys the Conversations application from La Suite Territoriale on a rootless podman base on Debian systems.

Tags: suiteterritoriale, system

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments


### Entrypoint: main

Installs and configures the Conversations application from La Suite Territoriale on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_conversations_public_host | The public hostname used to access the conversations application. | str | no |  |
| st_conversations_uid | UID of the `conversations` user, used for the podman role. | int | no | 1109 |
| st_conversations_gid | GID of the `conversations` group, used for the podman role. | int | no | {{ st_conversations_uid }} |
| st_conversations_registries | Optional private container registries to login the `conversations` user onto. | list of 'dict' | no |  |
| st_conversations_frontend_image | Image repository for the conversations frontend. | str | no | docker.io/lasuite/conversations-frontend |
| st_conversations_backend_image | Image repository for the conversations backend. | str | no | docker.io/lasuite/conversations-backend |
| st_conversations_tag | Tag of the conversations docker images to deploy. | str | no | v0.0.24 |
| st_conversations_enabled | Triggers the installation of the conversations application. | bool | no | False |
| st_conversations_dir | Remote path to the base directory for conversations app. | str | no | /opt/conversations/conversations |
| st_conversations_port | The host published port for the conversations caddy edge. | str | no | 50900 |
| st_conversations_rollback_enabled | Whether or not to trigger the rollback tasks if the conversations deployment fails. | bool | no | False |
| st_conversations_compose_template | Local path to the custom template to use for conversations compose file. | str | no | conversations/compose.yaml.j2 |
| st_conversations_backend_env_template | Local path to the custom template to use for conversations backend env file. | str | no | conversations/backend_env.j2 |
| st_conversations_backend_env | Content of the default backend_env_template, not used if st_conversations_backend_env_template is defined. | str | no |  |
| st_conversations_backend_run_migrations | Whether to run database migrations on conversations backend startup. | bool | no | True |
| st_conversations_caddy_env_template | Local path to the custom template to use for conversations caddy env file. | str | no | conversations/caddy_env.j2 |
| st_conversations_caddy_env | Content of the default caddy_env_template, not used if st_conversations_caddy_env_template is defined. Every key is optional: CADDY_ADMIN_IP_ALLOWLIST (space-separated CIDR list of client IPs allowed on the Django admin URL, default allows all) and CADDY_TRUSTED_PROXIES (space-separated CIDR list of proxies whose X-Forwarded-For sets the client IP, default private_ranges). | str | no |  |
| st_conversations_caddy_image | Image repository for the conversations caddy reverse-proxy. | str | no | docker.io/caddy |
| st_conversations_caddy_tag | The tag of the caddy docker image to use. See https://hub.docker.com/_/caddy/tags. | str | no | 2.11.4-alpine |
| st_conversations_llm_configuration_src | Local path of an LLM model catalog JSON. When set, the role mounts it read-only over `/app/conversations/configuration/llm/default.json` in the backend container. The backend caches the catalog at boot. | str | no |  |
| st_conversations_theme_customization_src | Local path of a theme customization JSON. When set, the role mounts it read-only over `/app/conversations/configuration/theme/default.json` in the backend container. | str | no |  |
| st_conversations_workers_enabled | Triggers the installation of the conversations workers | bool | no | False |
| st_conversations_workers_dir | Remote path to the base directory for conversations workers. | str | no | /opt/conversations/workers |
| st_conversations_workers_env_template | Local path to the custom template to use for conversations workers env file. | str | no | workers/env.j2 |
| st_conversations_workers_env | Content of the default workers_env_template, not used if st_conversations_workers_env_template is defined. | str | no | {{ st_conversations_backend_env }} |
| st_conversations_workers_rollback_enabled | Whether or not to trigger the rollback tasks if the workers deployment fails. | bool | no | False |
| st_conversations_workers_compose_template | Local path to the custom template to use for workers compose file. | str | no | workers/compose.yaml.j2 |
| st_conversations_cadvisor_enabled | Triggers the installation of the cadvisor container, a Prometheus-compliant containers monitoring tool. | bool | no | False |
| st_conversations_cadvisor_image | Image repository for the cadvisor container. | str | no | ghcr.io/google/cadvisor |
| st_conversations_cadvisor_tag | Tag of the cadvisor docker image to deploy. | str | no | v0.60.3 |
| st_conversations_cadvisor_port | The host published port of the cadvisor container. | str | no | 127.0.0.1:50999 |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.conversations
      ansible.builtin.import_role:
        name: suitenumerique.st.conversations
      vars:
```

## License

MIT

## Author and Project Information
La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
