<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.drive
Version: 0.0.1

This role deploys the Drive applications from La Suite Territoriale on a rootless podman base on Debian systems.

Tags: suiteterritoriale, system

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments


### Entrypoint: main

Installs and configures the Drive application from La Suite Territoriale on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_drive_uid | UID of the `drive` user, used for the podman role. | int | no | 1100 |
| st_drive_gid | GID of the `drive` group, used for the podman role. | int | no | {{ st_drive_uid }} |
| st_drive_collabora_enabled | Triggers the installation of collabora. | bool | no | False |
| st_drive_collabora_tag | Tag of the collabora docker image to deploy. | str | no | latest |
| st_drive_collabora_dir | Remote path to the base directory for collabora app. | str | no | /opt/drive/collabora |
| st_drive_collabora_env_template | Local path to the custom template to use for collabora env file. | str | no | collabora/env.j2 |
| st_drive_collabora_env | Content of the default collabora_env_template, not used if st_drive_collabora_env_template is defined. | str | no |  |
| st_drive_collabora_port | The port to open on the host, redirecting to port 9980 in the container. It can also specify the ip address, something like 127.0.0.1:9980. | str | no | 9980 |
| st_drive_monitoring_enabled | Triggers the installation of the alloy agent, used to send metrics to a Prometheus compatible server and logs to a Loki server. | bool | no | False |
| st_drive_monitoring_prometheus_url | When monitoring deployment is enabled, specifies the Prometheus URL to send metrics to. | str | no |  |
| st_drive_monitoring_loki_url | When monitoring deployment is enabled, specifies the Loki URL to send logs to. | str | no |  |
| st_drive_monitoring_cadvisor_port | The host published port of the cadvisor container. | str | no | 127.0.0.1:58080 |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.drive
      ansible.builtin.import_role:
        name: suitenumerique.st.drive
      vars:
```

## License

MIT

## Author and Project Information
La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
