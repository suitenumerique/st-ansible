<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.file_scanner
Version: 0.2.2

This role deploys the file-scanner antivirus service for La Suite Territoriale applications on a rootless podman base on Debian systems.

Tags: suiteterritoriale, system

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments


### Entrypoint: main

Installs and configures the file-scanner service from La Suite Territoriale on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_file_scanner_uid | UID of the `file-scanner` user, used for the podman role. | int | no | 1108 |
| st_file_scanner_gid | GID of the `file-scanner` group, used for the podman role. | int | no | {{ st_file_scanner_uid }} |
| st_file_scanner_registries | Optional private container registries to login the `file-scanner` user onto. | list of 'dict' | no |  |
| st_file_scanner_enabled | Triggers the installation of the file-scanner application. | bool | no | False |
| st_file_scanner_image | Image repository for file-scanner (one image runs both the web API and the worker). | str | no | ghcr.io/suitenumerique/file-scanner |
| st_file_scanner_tag | Tag of the file-scanner docker image to deploy. | str | no | 0.1.1 |
| st_file_scanner_clamav_image | Image repository for the bundled ClamAV daemon. | str | no | docker.io/clamav/clamav |
| st_file_scanner_clamav_tag | Tag of the ClamAV docker image to deploy. | str | no | 1.4 |
| st_file_scanner_redis_image | Image repository for the bundled Redis broker (dramatiq queue between the API and the worker). | str | no | docker.io/library/redis |
| st_file_scanner_redis_tag | Tag of the Redis docker image to deploy. | str | no | 7-alpine |
| st_file_scanner_dir | Remote path to the base directory for the file-scanner app. | str | no | /opt/file-scanner/file-scanner |
| st_file_scanner_port | The host published port for the file-scanner API (maps to the container's uvicorn port 8090). | str | no | 50800 |
| st_file_scanner_rollback_enabled | Whether or not to trigger the rollback tasks if the file-scanner deployment fails. | bool | no | False |
| st_file_scanner_compose_template | Local path to the custom template to use for file-scanner compose file. | str | no | file_scanner/compose.yaml.j2 |
| st_file_scanner_env_template | Local path to the custom template to use for file-scanner env file. | str | no | file_scanner/env.j2 |
| st_file_scanner_env | Content of the default env_template, not used if st_file_scanner_env_template is defined. | str | no |  |
| st_file_scanner_cadvisor_enabled | Triggers the installation of the cadvisor container, a Prometheus-compliant containers monitoring tool. | bool | no | False |
| st_file_scanner_cadvisor_image | Image repository for the cadvisor container. | str | no | ghcr.io/google/cadvisor |
| st_file_scanner_cadvisor_tag | Tag of the cadvisor docker image to deploy. | str | no | v0.60.3 |
| st_file_scanner_cadvisor_port | The host published port of the cadvisor container. | str | no | 127.0.0.1:50899 |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.file_scanner
      ansible.builtin.import_role:
        name: suitenumerique.st.file_scanner
      vars:
```

## License

MIT

## Author and Project Information
La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
