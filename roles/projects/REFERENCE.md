<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.projects
Version: 0.2.0

This role deploys a Projects instance from La Suite Territoriale on a rootless podman base on Debian systems.

Tags: suiteterritoriale, system

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments


### Entrypoint: main

Installs and configures the projects application from La Suite Territoriale on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_projects_uid | UID of the `projects` user, used for the podman role. | int | no | 1105 |
| st_projects_gid | GID of the `projects` group, used for the podman role. | int | no | {{ st_projects_uid }} |
| st_projects_registries | Optional private container registries to login the `projects` user onto. | list of 'dict' | no |  |
| st_projects_enabled | Triggers the installation of projects. | bool | no | False |
| st_projects_dir | Remote path to the base directory for projects app. | str | no | /opt/projects/projects |
| st_projects_port | The host published port for projects (mapped to the container's 1337). | str | no | 50500 |
| st_projects_image | Image repository for projects. | str | no | docker.io/lasuite/projects |
| st_projects_tag | Tag of the projects docker image to deploy. | str | no | 1.27.10 |
| st_projects_env_template | Local path to the custom template to use for projects env file. | str | no | projects/env.j2 |
| st_projects_env | Content of the default env_template, not used if st_projects_env_template is defined. | str | no |  |
| st_projects_compose_template | Local path to the custom template to use for projects compose file. | str | no | projects/compose.yaml.j2 |
| st_projects_directories | Persistent host directories created for the projects container's user uploads (avatars, project backgrounds, attachments), owned by the container's `node` user (uid 1000) so it can write to the bind-mounted volumes. The container mount targets are fixed by the image (its VOLUME directives) and hardcoded in the compose template — not configurable here. | list of 'dict' | no | [{'name': 'user-avatars', 'container_uid': 1000}, {'name': 'project-background-images', 'container_uid': 1000}, {'name': 'attachments', 'container_uid': 1000}] |
| st_projects_rollback_enabled | Whether to trigger the rollback tasks if the projects deployment fails. Rollback is config-level only (it restores the previous compose/env directory, never the database). Projects runs its DB migrations unconditionally on every container start, so rolling back across a schema-changing upgrade leaves the old image running against an already-migrated database — back up the database before such an upgrade. | bool | no | False |
| st_projects_cadvisor_enabled | Triggers the installation of the cadvisor container, used to send metrics to a Prometheus compatible server and logs to a Loki server. | bool | no | False |
| st_projects_cadvisor_image | Image repository for the cadvisor container. | str | no | ghcr.io/google/cadvisor |
| st_projects_cadvisor_tag | Tag of the cadvisor docker image to deploy. | str | no | v0.60.3 |
| st_projects_cadvisor_port | The host published port of the cadvisor container. | str | no | 127.0.0.1:50599 |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.projects
      ansible.builtin.import_role:
        name: suitenumerique.st.projects
      vars:
```

## License

MIT

## Author and Project Information
La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
