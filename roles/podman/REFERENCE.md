<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.podman
Version: 0.0.8

This role deploys a rootless podman base for La Suite Territoriale applications.

Tags: suiteterritoriale, system, podman, rootless

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments


### Entrypoint: main

Installs and configures the podman rootless base for La Suite Territoriale applications on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_podman_unprivileged_port_start | Port number to pass to sysctl net.ipv4.ip_unprivileged_port. | str | no |  |
| st_podman_user | The unix user that will be configured to manage the podman rootless containers. | str | yes |  |
| st_podman_group | The unix group of st_podman_user. | str | no | {{ st_podman_user }} |
| st_podman_home | The home of st_podman_user. | str | no | /opt/{{ st_podman_user }} |
| st_podman_uid | A custom UID for st_podman_user. | int | no |  |
| st_podman_gid | A custom GID for st_podman_group. | int | no |  |
| st_podman_containers_config_template | Local path to the template for the containers.conf configuration file. This file will be put under st_podman_user homedir. | str | no | containers.conf.j2 |
| st_podman_tz | Sets the default timezone for all containers, in the default containers.conf template. | str | no | UTC |
| st_podman_registries | List of container registries to connect to podman user onto. | list of 'dict' | no |  |
| st_podman_registries_config_template | The ansible template for registries.conf. See https://github.com/containers/image/blob/main/docs/containers-registries.conf.5.md | str | no |  |
| st_podman_application_name | The name of the application to deploy. This is used mainly for the systemd unit filename and the default application_dir. | str | no |  |
| st_podman_application_dir | The base directory to deploy the application compose to. | str | no | {{ st_podman_home }}/{{ st_podman_application_name }} |
| st_podman_application_dir_mode | The permissions to apply to the base directory to deploy the application compose to. | str | no | 0750 |
| st_podman_application_compose_template | Local path to the docker compose template for the application. | str | no |  |
| st_podman_application_sdnotify | Configures podman to send a systemd-notify to the systemd unit either when the container is started or when the container is healthy. Our default is `healthy`, which means we have to configure a healthcheck for every service of every compose file. See https://docs.podman.io/en/stable/markdown/podman-run.1.html#sdnotify-container-conmon-healthy-ignore | str | no | healthy |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.podman
      ansible.builtin.import_role:
        name: suitenumerique.st.podman
      vars:
        st_podman_user: # required, type: str
```

## License

MIT

## Author and Project Information
La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
