<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.podman
Version: 0.6.0

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
| st_podman_compose_version | The Debian package version of podman-compose that the role installs. The role fetches the package from snapshot.debian.org, which keeps every version that entered the Debian archive. Versions 1.5.0 and later exec a `CMD` healthcheck without `/bin/sh`, which images without a shell need. Trixie ships 1.3.0. | str | no | 1.6.0-1 |
| st_podman_compose_deb_url | The URL of a podman-compose Debian package to install instead of the snapshot.debian.org file. Set it with `st_podman_compose_deb_checksum` when the hosts have no access to snapshot.debian.org. The package must have the version `st_podman_compose_version`. | str | no |  |
| st_podman_compose_deb_checksum | The checksum of the package at `st_podman_compose_deb_url`, in the `get_url` format, for example `sha256:<hex>`. Required when `st_podman_compose_deb_url` is set. | str | no |  |
| st_podman_registries | List of container registries to connect to podman user onto. | list of 'dict' | no |  |
| st_podman_registries_config_template | The ansible template for registries.conf. See https://github.com/containers/image/blob/main/docs/containers-registries.conf.5.md | str | no |  |
| st_podman_application_name | The name of the application to deploy. This is used mainly for the systemd unit filename and the default application_dir. | str | no |  |
| st_podman_application_dir | The base directory to deploy the application compose to. | str | no | {{ st_podman_home }}/{{ st_podman_application_name }} |
| st_podman_application_dir_mode | The permissions to apply to the base directory to deploy the application compose to. | str | no | 0750 |
| st_podman_application_directories | Additional directories for files needed for deployment. Will be used to put `st_podman_application_files` inside. | list of 'dict' | no |  |
| st_podman_application_files | Additional files needed for deployment. Could be an env file, a configuration file mounted in the container or anything else. If in a subdirectory, the directory has to be created with `st_podman_application_directories` first. Each item has `dest`, and one of `src` (a template) or `content` (an inline string). Optional item keys are `mode`, `template`, `container_uid`, `container_gid`, `when` and `no_log`. Set `no_log` to true to hide the file value from the task output and the diff. The default is false. | list of 'dict' | no |  |
| st_podman_application_secret_files | Secret files to deploy in the `secrets` directory of the application. Each item has `env` in UPPER_SNAKE_CASE, and one of `src` (a template) or `content` (an inline string). The role writes the item to `secrets/<env>` with mode 0440, and hides its value from the task output. The role deletes the other files in `secrets`. It deletes the directory when the list is empty. The compose template must mount `secrets` in the container and set the `<env>_FILE` variable. The role manages `secrets` only when this variable is defined. An application that does not set it keeps its `secrets` directory. The name `secrets` is reserved. `st_podman_application_directories` and `st_podman_application_files` must not use it. | list of 'dict' | no |  |
| st_podman_application_secret_files_container_uid | The owner uid, inside the container, of the `secrets` directory and of each secret file. Set it to the uid of the container user that reads the secret files. The container gid stays 0 (the application user on the host). When the uid is not 0, the `secrets` directory has mode 2770 and the application user keeps write access through the group. | str | no | 0 |
| st_podman_application_compose_template | Local path to the docker compose template for the application. | str | no |  |
| st_podman_application_restart_policy | The restart policy to apply to the systemd unit of the application. | str | no | on-abnormal |
| st_podman_application_rollback_enabled | Whether or not to trigger the rollback tasks if the deployment fails. | bool | no | False |
| st_podman_application_timeout | The timeout for starting the application compose. | int | no | 300 |

#### Choices for main > st_podman_application_restart_policy

|Choice|
|---|
| no |
| on-success |
| on-failure |
| on-abnormal |
| on-watchdog |
| on-abort |
| always |



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
