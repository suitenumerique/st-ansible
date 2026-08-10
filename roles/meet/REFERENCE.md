<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.meet
Version: 0.2.1

This role deploys a Meet instance from La Suite Territoriale on a rootless podman base on Debian systems.

Tags: suiteterritoriale, system

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments


### Entrypoint: main

Installs and configures the meet application from La Suite Territoriale on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_meet_uid | UID of the `meet` user, used for the podman role. | int | no | 1103 |
| st_meet_gid | GID of the `meet` group, used for the podman role. | int | no | {{ st_meet_uid }} |
| st_meet_registries | Optional private container registries to login the `meet` user onto. | list of 'dict' | no |  |
| st_meet_public_host | The public hostname used to access the meet application. | str | no |  |
| st_meet_enabled | Triggers the installation of meet. | bool | no | False |
| st_meet_dir | Remote path to the base directory for meet app. | str | no | /opt/meet/meet |
| st_meet_port | The host published port for the meet frontend. | str | no | 50300 |
| st_meet_frontend_image | Image repository for the meet frontend. | str | no | docker.io/lasuite/meet-frontend |
| st_meet_backend_image | Image repository for the meet backend. | str | no | docker.io/lasuite/meet-backend |
| st_meet_tag | Tag of the meet docker image to deploy. | str | no | v1.23.0 |
| st_meet_backend_env_template | Local path to the custom template to use for meet env file. | str | no | meet/backend_env.j2 |
| st_meet_backend_env | Content of the default backend_env_template, not used if st_meet_backend_env_template is defined. | str | no |  |
| st_meet_backend_run_migrations | Whether to run database migrations on meet backend startup. | bool | no | True |
| st_meet_frontend_env_template | Local path to the custom template to use for meet frontend env file. | str | no | meet/frontend_env.j2 |
| st_meet_frontend_env | Content of the default frontend_env_template, not used if st_meet_frontend_env_template is defined. | str | no |  |
| st_meet_frontend_logo_src | Local path to a custom logo file (e.g. svg) to mount over the meet frontend logo. No validation is performed. Empty means use the image default. | str | no |  |
| st_meet_caddy_env_template | Local path to the custom template to use for meet caddy env file. | str | no | meet/caddy_env.j2 |
| st_meet_caddy_env | Content of the default caddy_env_template, not used if st_meet_caddy_env_template is defined. | str | no |  |
| st_meet_compose_template | Local path to the custom template to use for meet compose file. | str | no | meet/compose.yaml.j2 |
| st_meet_caddy_image | Image repository for the meet caddy reverse-proxy. | str | no | docker.io/caddy |
| st_meet_caddy_tag | The tag of the caddy docker image to use. See https://hub.docker.com/_/caddy/tags. | str | no | 2.11.4-alpine |
| st_meet_rollback_enabled | Whether or not to trigger the rollback tasks if the meet deployment fails. | bool | no | False |
| st_meet_livekit_caddyl4_image | Image repository for livekit caddyl4. | str | no | docker.io/livekit/caddyl4 |
| st_meet_livekit_image | Image repository for the livekit server. | str | no | docker.io/livekit/livekit-server |
| st_meet_livekit_valkey_image | Image repository for the livekit valkey. | str | no | docker.io/valkey/valkey |
| st_meet_livekit_tag | Tag of the livekit docker image to deploy. | str | no | v1.13.4 |
| st_meet_livekit_caddyl4_tag | Tag of the livekit caddyl4 docker image to deploy. | str | no | v2.11.3 |
| st_meet_livekit_valkey_tag | Tag of the valkey docker image to deploy for livekit when using the full compose template. | str | no | 9.1.1 |
| st_meet_livekit_valkey_enabled | Deploy a local valkey in the livekit compose (single-node co-located egress). Set false when using an external shared redis. | bool | no | True |
| st_meet_livekit_redis_address | Redis/valkey address (host:port) the livekit server connects to. Defaults to the local valkey; set to a shared external redis when egress runs on a different node. | str | no | 127.0.0.1:6379 |
| st_meet_livekit_redis_username | Username for the external shared redis used by livekit and egress. Not used by the co-located local valkey, which has no auth. | str | no |  |
| st_meet_livekit_redis_password | Password for the external shared redis used by livekit and egress, provided via vault. Not used by the co-located local valkey, which has no auth. | str | no |  |
| st_meet_livekit_enabled | Triggers the installation of livekit. | bool | no | False |
| st_meet_livekit_dir | Remote path to the base directory for livekit app. | str | no | /opt/meet/livekit |
| st_meet_livekit_compose_template | Local path to the custom template to use for the livekit compose file. | str | no | livekit/compose.default.yaml.j2 |
| st_meet_livekit_rollback_enabled | Whether or not to trigger the rollback tasks if the livekit deployment fails. | bool | no | False |
| st_meet_livekit_domain | The domain name for the livekit server. Used in the default caddy configuration. | str | no |  |
| st_meet_livekit_turn_domain | The domain name for the livekit TURN server. Used in the default livekit and caddy configurations. | str | no |  |
| st_meet_livekit_api_key | The API key for the livekit server. Used in the default livekit configuration. | str | no |  |
| st_meet_livekit_api_secret | The API secret for the livekit server. Used in the default livekit configuration. | str | no |  |
| st_meet_livekit_files | List of files to deploy for the livekit application. By default deploys livekit.yaml, caddy.yaml and valkey_config/valkey.conf from the default templates. Override this entirely to deploy a custom livekit configuration. | list of 'dict' | no | [{'src': 'livekit/livekit.default.yaml.j2', 'dest': 'livekit.yaml'}, {'src': 'livekit/caddy.default.yaml.j2', 'dest': 'caddy.yaml'}, {'src': 'livekit/valkey.default.conf.j2', 'dest': 'valkey_config/valkey.conf', 'when': '{{ st_meet_livekit_valkey_enabled }}'}] |
| st_meet_livekit_directories | List of directories to create for the livekit application. By default creates caddy_data for the caddy file_system storage module. Override this entirely for custom setups. | list of 'dict' | no | [{'name': 'caddy_data'}, {'name': 'valkey_data', 'container_uid': '999', 'when': '{{ st_meet_livekit_valkey_enabled }}'}, {'name': 'valkey_config', 'when': '{{ st_meet_livekit_valkey_enabled }}'}] |
| st_meet_egress_image | Image repository for livekit egress. | str | no | docker.io/livekit/egress |
| st_meet_egress_tag | Tag of the livekit egress docker image to deploy. | str | no | v1.13.0 |
| st_meet_egress_cpus | Optional CPU limit for the livekit egress container (podman-compose `cpus`, e.g. '2' or '1.5'). Recommended on single-node setups so starting a recording (CPU-heavy transcoding) cannot starve the livekit server. Empty means no limit. | str | no |  |
| st_meet_egress_memory | Optional memory limit for the livekit egress container (podman-compose `mem_limit`, e.g. '2g'). Recommended on single-node setups so a recording cannot OOM the node and take down the livekit server. Empty means no limit. | str | no |  |
| st_meet_egress_enabled | Triggers the installation of the livekit egress recorder as a separate component. | bool | no | False |
| st_meet_egress_dir | Remote path to the base directory for the egress app. | str | no | /opt/meet/egress |
| st_meet_egress_compose_template | Local path to the custom template to use for the egress compose file. | str | no | egress/compose.yaml.j2 |
| st_meet_egress_rollback_enabled | Whether or not to trigger the rollback tasks if the egress deployment fails. | bool | no | False |
| st_meet_egress_files | List of files to deploy for the egress application. By default deploys egress.yaml from the default template. Override this entirely to deploy a custom egress configuration. | list of 'dict' | no | [{'src': 'egress/egress.yaml.j2', 'dest': 'egress.yaml'}] |
| st_meet_cadvisor_image | Image repository for the cadvisor container. | str | no | ghcr.io/google/cadvisor |
| st_meet_cadvisor_tag | Tag of the cadvisor docker image to deploy. | str | no | v0.60.3 |
| st_meet_cadvisor_enabled | Triggers the installation of the cadvisor container, used to send metrics to a Prometheus compatible server and logs to a Loki server. | bool | no | False |
| st_meet_cadvisor_port | The host published port of the cadvisor container. | str | no | 127.0.0.1:50399 |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.meet
      ansible.builtin.import_role:
        name: suitenumerique.st.meet
      vars:
```

## License

MIT

## Author and Project Information
La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
