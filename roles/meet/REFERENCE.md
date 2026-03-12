<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.meet
Version: 0.0.12

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
| st_meet_tag | Tag of the meet docker image to deploy. | str | no | latest |
| st_meet_backend_env_template | Local path to the custom template to use for meet env file. | str | no | meet/backend_env.j2 |
| st_meet_backend_env | Content of the default backend_env_template, not used if st_meet_backend_env_template is defined. | str | no |  |
| st_meet_backend_run_migrations | Whether to run database migrations on meet backend startup. | bool | no | True |
| st_meet_frontend_env_template | Local path to the custom template to use for meet frontend env file. | str | no | meet/frontend_env.j2 |
| st_meet_frontend_env | Content of the default frontend_env_template, not used if st_meet_frontend_env_template is defined. | str | no |  |
| st_meet_compose_template | Local path to the custom template to use for meet compose file. | str | no | meet/compose.yaml.j2 |
| st_meet_rollback_enabled | Whether or not to trigger the rollback tasks if the meet deployment fails. | bool | no | False |
| st_meet_livekit_enabled | Triggers the installation of meet. | bool | no | False |
| st_meet_livekit_tag | Tag of the meet docker image to deploy. | str | no | latest |
| st_meet_livekit_dir | Remote path to the base directory for livekit app. | str | no | /opt/meet/livekit |
| st_meet_livekit_compose_template | Local path to the custom template to use for meet compose file. | str | no | meet/compose.yaml.j2 |
| st_meet_livekit_rollback_enabled | Whether or not to trigger the rollback tasks if the meet deployment fails. | bool | no | False |
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
