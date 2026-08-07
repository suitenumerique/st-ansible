<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.docs
Version: 0.2.0

This role deploys a Docs instance from La Suite Territoriale on a rootless podman base on Debian systems.

Tags: suiteterritoriale, system

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments


### Entrypoint: main

Installs and configures the docs application from La Suite Territoriale on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_docs_uid | UID of the `docs` user, used for the podman role. | int | no | 1106 |
| st_docs_gid | GID of the `docs` group, used for the podman role. | int | no | {{ st_docs_uid }} |
| st_docs_registries | Optional private container registries to login the `docs` user onto. | list of 'dict' | no |  |
| st_docs_public_host | The public hostname used to access the docs application. | str | no |  |
| st_docs_enabled | Triggers the installation of docs. | bool | no | False |
| st_docs_dir | Remote path to the base directory for docs app. | str | no | /opt/docs/docs |
| st_docs_port | The host published port for the docs frontend. | str | no | 50600 |
| st_docs_frontend_image | Image repository for the docs frontend. | str | no | docker.io/lasuite/impress-frontend |
| st_docs_backend_image | Image repository for the docs backend. | str | no | docker.io/lasuite/impress-backend |
| st_docs_yprovider_image | Image repository for the docs y-provider collaboration server. | str | no | docker.io/lasuite/impress-y-provider |
| st_docs_tag | Tag of the docs docker images to deploy. | str | no | v5.4.1 |
| st_docs_backend_env_template | Local path to the custom template to use for docs backend env file. | str | no | docs/backend_env.j2 |
| st_docs_backend_env | Content of the default backend_env_template, not used if st_docs_backend_env_template is defined. | str | no |  |
| st_docs_backend_run_migrations | Whether to run database migrations on docs backend startup. | bool | no | True |
| st_docs_frontend_env_template | Local path to the custom template to use for docs frontend env file. | str | no | docs/frontend_env.j2 |
| st_docs_frontend_env | Content of the default frontend_env_template, not used if st_docs_frontend_env_template is defined. | str | no |  |
| st_docs_caddy_env_template | Local path to the custom template to use for docs caddy env file. | str | no | docs/caddy_env.j2 |
| st_docs_caddy_env | Content of the default caddy_env_template, not used if st_docs_caddy_env_template is defined. The Caddyfile requires CADDY_S3_PROTOCOL, CADDY_S3_HOST, CADDY_S3_BUCKET and CADDY_YPROVIDER_ENDPOINTS (a space-separated host:port list of y-provider upstreams, load-balanced by document room). | str | no |  |
| st_docs_compose_template | Local path to the custom template to use for docs compose file. | str | no | docs/compose.yaml.j2 |
| st_docs_caddy_image | Image repository for the docs caddy reverse-proxy. | str | no | docker.io/caddy |
| st_docs_caddy_tag | The tag of the caddy docker image to use. See https://hub.docker.com/_/caddy/tags. | str | no | 2.11.4-alpine |
| st_docs_docspec_image | Image repository for the docspec conversion service. | str | no | ghcr.io/docspecio/api |
| st_docs_docspec_tag | Tag of the docspec docker image to deploy. | str | no | 3.0.1 |
| st_docs_rollback_enabled | Whether or not to trigger the rollback tasks if the docs deployment fails. | bool | no | False |
| st_docs_frontend_logo_src | Local path to a custom logo file (svg) to mount over the docs frontend logo (/assets/icon-docs.svg). No validation is performed. Empty means use the image default. | str | no |  |
| st_docs_theme_customization_src | Local path to a custom theme customization JSON file to mount over the docs backend default theme. No validation is performed. Empty means use the image default. | str | no |  |
| st_docs_workers_enabled | Triggers the installation of the docs workers. | bool | no | False |
| st_docs_workers_dir | Remote path to the base directory for docs workers. | str | no | /opt/docs/workers |
| st_docs_workers_env_template | Local path to the custom template to use for docs workers env file. | str | no | workers/env.j2 |
| st_docs_workers_env | Content of the default workers_env_template, not used if st_docs_workers_env_template is defined. | str | no | {{ st_docs_backend_env }} |
| st_docs_workers_rollback_enabled | Whether or not to trigger the rollback tasks if the workers deployment fails. | bool | no | False |
| st_docs_workers_compose_template | Local path to the custom template to use for workers compose file. | str | no | workers/compose.yaml.j2 |
| st_docs_yprovider_enabled | Triggers the installation of the docs y-provider collaboration server. | bool | no | False |
| st_docs_yprovider_dir | Remote path to the base directory for the y-provider app. | str | no | /opt/docs/yprovider |
| st_docs_yprovider_env_template | Local path to the custom template to use for y-provider env file. | str | no | yprovider/env.j2 |
| st_docs_yprovider_env | Content of the default yprovider_env_template, not used if st_docs_yprovider_env_template is defined. | str | no |  |
| st_docs_yprovider_rollback_enabled | Whether or not to trigger the rollback tasks if the y-provider deployment fails. | bool | no | False |
| st_docs_yprovider_compose_template | Local path to the custom template to use for y-provider compose file. | str | no | yprovider/compose.yaml.j2 |
| st_docs_yprovider_port | The host published port for the y-provider collaboration server. | str | no | 50601 |
| st_docs_cadvisor_image | Image repository for the cadvisor container. | str | no | ghcr.io/google/cadvisor |
| st_docs_cadvisor_tag | Tag of the cadvisor docker image to deploy. | str | no | v0.60.3 |
| st_docs_cadvisor_enabled | Triggers the installation of the cadvisor container, used to send metrics to a Prometheus compatible server and logs to a Loki server. | bool | no | False |
| st_docs_cadvisor_port | The host published port of the cadvisor container. | str | no | 127.0.0.1:50699 |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.docs
      ansible.builtin.import_role:
        name: suitenumerique.st.docs
      vars:
```

## License

MIT

## Author and Project Information
La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
