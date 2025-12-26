<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.keycloak
Version: 0.0.9

This role deploys the Drive applications from La Suite Territoriale on a rootless podman base on Debian systems.

Tags: suiteterritoriale, system

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments


### Entrypoint: main

Installs and configures the keycloak application from La Suite Territoriale on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_keycloak_uid | UID of the `keycloak` user, used for the podman role. | int | no | 1100 |
| st_keycloak_gid | GID of the `keycloak` group, used for the podman role. | int | no | {{ st_keycloak_uid }} |
| st_keycloak_enabled | Triggers the installation of keycloak. | bool | no | False |
| st_keycloak_tag | Tag of the keycloak docker image to deploy. | str | no | latest |
| st_keycloak_dir | Remote path to the base directory for keycloak app. | str | no | /opt/keycloak/keycloak |
| st_keycloak_env_template | Local path to the custom template to use for keycloak env file. | str | no | keycloak/env.j2 |
| st_keycloak_env | Content of the default keycloak_env_template, not used if st_keycloak_env_template is defined. | str | no |  |
| st_keycloak_compose_template | Local path to the custom template to use for keycloak compose file. | str | no | keycloak/compose.yaml.j2 |
| st_keycloak_port | The port to open on the host, redirecting to port 8080 in the container. It can also specify the ip address, something like 127.0.0.1:8080. | str | no | 8080 |
| st_keycloak_cadvisor_enabled | Triggers the installation of the alloy agent, used to send metrics to a Prometheus compatible server and logs to a Loki server. | bool | no | False |
| st_keycloak_cadvisor_port | The host published port of the cadvisor container. | str | no | 127.0.0.1:58080 |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.keycloak
      ansible.builtin.import_role:
        name: suitenumerique.st.keycloak
      vars:
```

## License

MIT

## Author and Project Information
La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
