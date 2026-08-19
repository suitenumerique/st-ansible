<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.transfers
Version: 0.2.1

This role deploys the Transfers application from La Suite Territoriale on a rootless podman base on Debian systems.

Tags: suiteterritoriale, system

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments


### Entrypoint: main

Installs and configures the Transfers application from La Suite Territoriale on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_transfers_public_host | The public hostname used to access the transfers application. | str | no |  |
| st_transfers_uid | UID of the `transfers` user, used for the podman role. | int | no | 1107 |
| st_transfers_gid | GID of the `transfers` group, used for the podman role. | int | no | {{ st_transfers_uid }} |
| st_transfers_registries | Optional private container registries to login the `transfers` user onto. | list of 'dict' | no |  |
| st_transfers_frontend_image | Image repository for the transfers frontend. | str | no | ghcr.io/suitenumerique/transfers-frontend |
| st_transfers_backend_image | Image repository for the transfers backend. | str | no | ghcr.io/suitenumerique/transfers-backend |
| st_transfers_tag | Tag of the transfers docker images to deploy. | str | no | 0.1.0 |
| st_transfers_enabled | Triggers the installation of the transfers application. | bool | no | False |
| st_transfers_dir | Remote path to the base directory for transfers app. | str | no | /opt/transfers/transfers |
| st_transfers_port | The host published port for the transfers frontend (maps to the container's Caddy port 8080). | str | no | 50700 |
| st_transfers_rollback_enabled | Whether or not to trigger the rollback tasks if the transfers deployment fails. | bool | no | False |
| st_transfers_compose_template | Local path to the custom template to use for transfers compose file. | str | no | transfers/compose.yaml.j2 |
| st_transfers_backend_env_template | Local path to the custom template to use for transfers backend env file. | str | no | transfers/backend_env.j2 |
| st_transfers_backend_env | Content of the default backend_env_template, not used if st_transfers_backend_env_template is defined. | str | no |  |
| st_transfers_backend_run_migrations | Whether to run database migrations on transfers backend startup. | bool | no | True |
| st_transfers_frontend_env_template | Local path to the custom template to use for transfers frontend env file. | str | no | transfers/frontend_env.j2 |
| st_transfers_frontend_env | Content of the default frontend_env_template, not used if st_transfers_frontend_env_template is defined. | str | no |  |
| st_transfers_workers_enabled | Triggers the installation of the transfers workers (Celery worker with embedded beat scheduler). | bool | no | False |
| st_transfers_workers_dir | Remote path to the base directory for transfers workers. | str | no | /opt/transfers/workers |
| st_transfers_workers_env_template | Local path to the custom template to use for transfers workers env file. | str | no | workers/env.j2 |
| st_transfers_workers_env | Content of the default workers_env_template, not used if st_transfers_workers_env_template is defined. | str | no | {{ st_transfers_backend_env }} |
| st_transfers_workers_rollback_enabled | Whether or not to trigger the rollback tasks if the workers deployment fails. | bool | no | False |
| st_transfers_workers_compose_template | Local path to the custom template to use for workers compose file. | str | no | workers/compose.yaml.j2 |
| st_transfers_cadvisor_enabled | Triggers the installation of the cadvisor container, a Prometheus-compliant containers monitoring tool. | bool | no | False |
| st_transfers_cadvisor_image | Image repository for the cadvisor container. | str | no | ghcr.io/google/cadvisor |
| st_transfers_cadvisor_tag | Tag of the cadvisor docker image to deploy. | str | no | v0.60.3 |
| st_transfers_cadvisor_port | The host published port of the cadvisor container. | str | no | 127.0.0.1:50799 |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.transfers
      ansible.builtin.import_role:
        name: suitenumerique.st.transfers
      vars:
```

## License

MIT

## Author and Project Information
La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
