<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.drive
Version: 0.0.5

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
| st_drive_tag | Tag of the drive docker images to deploy. | str | no | main |
| st_drive_enabled | Triggers the installation of the drive application. | bool | no | False |
| st_drive_dir | Remote path to the base directory for drive app. | str | no | /opt/drive/drive |
| st_drive_nginx_port | The port to expose the drive application on the host. | int | no | 8080 |
| st_drive_s3_protocol | The S3 compatible storage protocol used for media storage. | str | no | https |
| st_drive_s3_host | The S3 compatible storage host used for media storage. | str | no | s3.amazonaws.com |
| st_drive_s3_bucket | The S3 compatible storage bucket used for media storage. | str | no | drive-media |
| st_drive_compose_template | Local path to the custom template to use for drive compose file. | str | no | drive/compose.yaml.j2 |
| st_drive_backend_env_template | Local path to the custom template to use for drive backend env file. | str | no | drive/backend_env.j2 |
| st_drive_backend_env | Content of the default backend_env_template, not used if st_drive_backend_env_template is defined. | str | no |  |
| st_drive_frontend_env_template | Local path to the custom template to use for drive frontend env file. | str | no | drive/frontend_env.j2 |
| st_drive_workers_enabled | Triggers the installation of the drive workers | bool | no | False |
| st_drive_workers_dir | Remote path to the base directory for drive workers. | str | no | /opt/drive/workers |
| st_drive_workers_env_template | Local path to the custom template to use for drive workers env file. | str | no | workers/env.j2 |
| st_drive_workers_env | Content of the default workers_env_template, not used if st_drive_workers_env template is defined. | str | no | {{ st_drive_backend_env }} |
| st_drive_collabora_enabled | Triggers the installation of collabora. | bool | no | False |
| st_drive_collabora_tag | Tag of the collabora docker image to deploy. | str | no | latest |
| st_drive_collabora_dir | Remote path to the base directory for collabora app. | str | no | /opt/drive/collabora |
| st_drive_collabora_env_template | Local path to the custom template to use for collabora env file. | str | no | collabora/env.j2 |
| st_drive_collabora_env | Content of the default collabora_env_template, not used if st_drive_collabora_env_template is defined. | str | no |  |
| st_drive_collabora_compose_template | Local path to the custom template to use for collabora compose file. | str | no | collabora/compose.yaml.j2 |
| st_drive_collabora_port | The port to open on the host, redirecting to port 9980 in the container. It can also specify the ip address, something like 127.0.0.1:9980. | str | no | 9980 |
| st_drive_cadvisor_enabled | Triggers the installation of the alloy agent, used to send metrics to a Prometheus compatible server and logs to a Loki server. | bool | no | False |
| st_drive_cadvisor_port | The host published port of the cadvisor container. | str | no | 127.0.0.1:58080 |



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
