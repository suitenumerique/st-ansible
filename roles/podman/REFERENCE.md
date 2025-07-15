<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.podman
Version: 0.0.1

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
| st_podman_application_name | The name of the application to deploy. This is used mainly for the systemd unit filename and the default application_dir. | str | no |  |
| st_podman_application_dir | The base directory to deploy the application compose to. | str | no | {{ st_podman_home }}/{{ st_podman_application_name }} |
| st_podman_application_compose_template | Local path to the docker compose template for the application. | str | no |  |



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
