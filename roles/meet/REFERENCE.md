<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.meet
Version: 0.0.16

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
| st_meet_uid | UID of the `meet` user, used for the podman role. | int | no | 1100 |
| st_meet_gid | GID of the `meet` group, used for the podman role. | int | no | {{ st_meet_uid }} |
| st_meet_registries | Optional private container registries to login the `meet` user onto. | list of 'dict' | no |  |
| st_meet_enabled | Triggers the installation of meet. | bool | no | False |
| st_meet_dir | Remote path to the base directory for meet app. | str | no | /opt/meet/meet |
| st_meet_port | The host published port for the meet frontend. | str | no | 50080 |
| st_meet_tag | Tag of the meet docker image to deploy. | str | no | latest |
| st_meet_backend_env_template | Local path to the custom template to use for meet env file. | str | no | meet/backend_env.j2 |
| st_meet_backend_env | Content of the default backend_env_template, not used if st_meet_backend_env_template is defined. | str | no |  |
| st_meet_backend_run_migrations | Whether to run database migrations on meet backend startup. | bool | no | True |
| st_meet_frontend_env_template | Local path to the custom template to use for meet frontend env file. | str | no | meet/frontend_env.j2 |
| st_meet_frontend_env | Content of the default frontend_env_template, not used if st_meet_frontend_env_template is defined. | str | no |  |
| st_meet_compose_template | Local path to the custom template to use for meet compose file. | str | no | meet/compose.yaml.j2 |
| st_meet_rollback_enabled | Whether or not to trigger the rollback tasks if the meet deployment fails. | bool | no | False |
| st_meet_livekit_tag | Tag of the livekit docker image to deploy. | str | no | latest |
| st_meet_livekit_egress_tag | Tag of the livekit egress docker image to deploy. | str | no | latest |
| st_meet_livekit_caddyl4_tag | Tag of the livekit caddyl4 docker image to deploy. | str | no | latest |
| st_meet_livekit_valkey_tag | Tag of the valkey docker image to deploy for livekit when using the full compose template. | str | no | latest |
| st_meet_livekit_enabled | Triggers the installation of livekit. | bool | no | False |
| st_meet_livekit_dir | Remote path to the base directory for livekit app. | str | no | /opt/meet/livekit |
| st_meet_livekit_compose_template | Local path to the custom template to use for the livekit compose file. | str | no | livekit/compose.default.yaml.j2 |
| st_meet_livekit_rollback_enabled | Whether or not to trigger the rollback tasks if the livekit deployment fails. | bool | no | False |
| st_meet_livekit_domain | The domain name for the livekit server. Used in the default caddy configuration. | str | no |  |
| st_meet_livekit_turn_domain | The domain name for the livekit TURN server. Used in the default livekit and caddy configurations. | str | no |  |
| st_meet_livekit_api_key | The API key for the livekit server. Used in the default livekit configuration. | str | no |  |
| st_meet_livekit_api_secret | The API secret for the livekit server. Used in the default livekit configuration. | str | no |  |
| st_meet_livekit_files | List of files to deploy for the livekit application. By default deploys livekit.yaml, caddy.yaml and redis.conf from the default templates. Override this entirely to deploy a custom livekit configuration. | list of 'dict' | no | [{'src': 'livekit/livekit.default.yaml.j2', 'dest': 'livekit.yaml'}, {'src': 'livekit/caddy.default.yaml.j2', 'dest': 'caddy.yaml'}, {'src': 'livekit/valkey.default.conf.j2', 'dest': 'valkey_config/valkey.conf'}, {'src': 'livekit/egress.default.yaml.j2', 'dest': 'egress.yaml'}] |
| st_meet_livekit_directories | List of directories to create for the livekit application. By default creates caddy_data for the caddy file_system storage module. Override this entirely for custom setups. | list of 'dict' | no | [{'name': 'caddy_data'}, {'name': 'valkey_data', 'container_uid': '999'}, {'name': 'valkey_config'}] |
| st_meet_cadvisor_enabled | Triggers the installation of the cadvisor container, used to send metrics to a Prometheus compatible server and logs to a Loki server. | bool | no | False |
| st_meet_cadvisor_port | The host published port of the cadvisor container. | str | no | 127.0.0.1:58080 |



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
