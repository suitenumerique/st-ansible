<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.messages
Version: 0.2.1

This role deploys the Messages applications from La Suite Territoriale on a rootless podman base on Debian systems.

Tags: suiteterritoriale, system

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments


### Entrypoint: main

Installs and configures the Messages application from La Suite Territoriale on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_messages_uid | UID of the `messages` user, used for the podman role. | int | no | 1104 |
| st_messages_gid | GID of the `messages` group, used for the podman role. | int | no | {{ st_messages_uid }} |
| st_messages_registries | Optional private container registries to login the `messages` user onto. | list of 'dict' | no |  |
| st_messages_frontend_image | Image repository for the messages frontend. | str | no | ghcr.io/suitenumerique/messages-frontend |
| st_messages_backend_image | Image repository for the messages backend. | str | no | ghcr.io/suitenumerique/messages-backend |
| st_messages_tag | Tag of the messages docker images to deploy. | str | no | 0.5.0 |
| st_messages_enabled | Triggers the installation of the messages application. | bool | no | False |
| st_messages_dir | Remote path to the base directory for messages app. | str | no | /opt/messages/messages |
| st_messages_port | The host published port for the messages frontend. | str | no | 50400 |
| st_messages_rollback_enabled | Whether or not to trigger the rollback tasks if the messages deployment fails. | bool | no | False |
| st_messages_compose_template | Local path to the custom template to use for messages compose file. | str | no | messages/compose.yaml.j2 |
| st_messages_backend_env_template | Local path to the custom template to use for messages env file. | str | no | messages/backend_env.j2 |
| st_messages_backend_env | Content of the default backend_env_template, not used if st_messages_backend_env_template is defined. | str | no |  |
| st_messages_backend_run_migrations | Triggers the migrations task on the host. By default this is true, but the task has `run_once:` set on it which will trigger the migrations once per play. This var is useful if your deployment workflow uses `serial:`, in this case the play is separated in X plays of 1 host each, and `run_once` will effectively run once per play, resulting running the migrations on all hosts. If you use `serial:`, set this var to false on every host except one. | bool | no | True |
| st_messages_frontend_env_template | Local path to the custom template to use for messages env file. | str | no | messages/frontend_env.j2 |
| st_messages_frontend_env | Content of the default frontend_env_template, not used if st_messages_frontend_env_template is defined. | str | no |  |
| st_messages_workers_enabled | Triggers the installation of the messages workers | bool | no | False |
| st_messages_workers_dir | Remote path to the base directory for messages workers. | str | no | /opt/messages/workers |
| st_messages_workers_env_template | Local path to the custom template to use for messages workers env file. | str | no | workers/env.j2 |
| st_messages_workers_env | Content of the default workers_env_template, not used if st_messages_workers_env_template is defined. | str | no | {{ st_messages_backend_env }} |
| st_messages_workers_rollback_enabled | Whether or not to trigger the rollback tasks if the workers deployment fails. | bool | no | False |
| st_messages_workers_compose_template | Local path to the custom template to use for messages workers compose file. | str | no | workers/compose.yaml.j2 |
| st_messages_mta_in_enabled | Triggers the installation of the mta-in. | bool | no | False |
| st_messages_mta_in_image | Image repository for mta-in. | str | no | ghcr.io/suitenumerique/messages-mta-in |
| st_messages_mta_in_tag | Tag of the mta-in docker image to deploy. | str | no | 0.5.0 |
| st_messages_mta_in_dir | Remote path to the base directory for mta-in app. | str | no | /opt/messages/mta-in |
| st_messages_mta_in_port | The host published port for the mta-in SMTP endpoint. | str | no | 50425 |
| st_messages_mta_in_env_template | Local path to the custom template to use for mta-in env file. | str | no | mta_in/env.j2 |
| st_messages_mta_in_env | Content of the default mta_in_env_template, not used if st_messages_mta_in_env_template is defined. | str | no |  |
| st_messages_mta_in_starttls_certificate_path | Path of the starttls certificate on the remote host. The certificate must be in the smtpd_tls_chain_files format, see https://www.postfix.org/postconf.5.html#smtpd_tls_chain_files. The file must be accessible by the `messages` user. | str | no |  |
| st_messages_mta_in_compose_template | Local path to the custom template to use for mta-in compose file. | str | no | mta_in/compose.yaml.j2 |
| st_messages_mta_in_rollback_enabled | Whether or not to trigger the rollback tasks if the mta-in deployment fails. | bool | no | False |
| st_messages_socks_proxy_enabled | Triggers the installation of the socks-proxy. | bool | no | False |
| st_messages_socks_proxy_image | Image repository for socks-proxy. | str | no | ghcr.io/suitenumerique/messages-socks-proxy |
| st_messages_socks_proxy_tag | Tag of the socks-proxy docker image to deploy. | str | no | 0.5.0 |
| st_messages_socks_proxy_dir | Remote path to the base directory for socks-proxy app. | str | no | /opt/messages/socks-proxy |
| st_messages_socks_proxy_env_template | Local path to the custom template to use for socks-proxy env file. | str | no | socks_proxy/env.j2 |
| st_messages_socks_proxy_env | Content of the default socks_proxy_env_template, not used if st_messages_socks_proxy_env_template is defined. | str | no |  |
| st_messages_socks_proxy_compose_template | Local path to the custom template to use for socks-proxy compose file. | str | no | socks_proxy/compose.yaml.j2 |
| st_messages_socks_proxy_rollback_enabled | Whether or not to trigger the rollback tasks if the socks-proxy deployment fails. | bool | no | False |
| st_messages_mpa_enabled | Triggers the installation of the mpa. | bool | no | False |
| st_messages_mpa_dir | Remote path to the base directory for mpa app. | str | no | /opt/messages/mpa |
| st_messages_mpa_auth_bearer | Add a caddy container in front of the rspamd worker with a simple authorization check. The value of this variable should then be used as a Bearer token when calling the /checkv2 rspamd endpoint. | str | no |  |
| st_messages_mpa_caddy_image | Image repository for the mpa caddy. | str | no | docker.io/caddy |
| st_messages_mpa_caddy_tag | The tag of the caddy docker image to use. See https://hub.docker.com/_/caddy/tags. | str | no | 2.11.4-alpine |
| st_messages_mpa_caddy_port | The host published port for the caddy /checkv2 endpoint. | str | no | 50402 |
| st_messages_mpa_caddy_healthcheck_port | The host published port for the caddy /healthcheck endpoint. | str | no | 50403 |
| st_messages_mpa_rspamd_image | Image repository for the mpa rspamd. | str | no | docker.io/rspamd/rspamd |
| st_messages_mpa_rspamd_tag | The tag of the rspamd docker image to use. See https://hub.docker.com/r/rspamd/rspamd/tags. | str | no | 4.1.4 |
| st_messages_mpa_rspamd_controller_password | Password of the rspamd controller webui. | str | no |  |
| st_messages_mpa_rspamd_controller_port | The host published port for the rspamd controller/webui. | str | no | 50404 |
| st_messages_mpa_rspamd_add_header_score | The score triggering the add_header action on Messages. | str | no | 4 |
| st_messages_mpa_rspamd_rewrite_subject_score | The score triggering the rewrite_subject action on Messages. | str | no | 6 |
| st_messages_mpa_rspamd_reject_score | The score triggering the reject action on Messages. | str | no | 9 |
| st_messages_mpa_rspamd_redirectors | The list of domains that should be checked by URL redirector in addition to the default ones. | list of 'str' | no | [] |
| st_messages_mpa_rspamd_history_nrows | The maximum rows of the redis history index, before old history lines get removed. Good idea to try to keep this between 2 weeks and 1 month. | str | no | 100000 |
| st_messages_mpa_blacklist_domains | Domains to blacklist via rspamd multimap. | list of 'str' | no | [] |
| st_messages_mpa_blacklist_ips | IPs or CIDRs to blacklist via rspamd multimap. | list of 'str' | no | [] |
| st_messages_mpa_whitelist_domains | Domains to whitelist via rspamd multimap. | list of 'str' | no | [] |
| st_messages_mpa_whitelist_ips | IPs or CIDRs to whitelist via rspamd multimap. | list of 'str' | no | [] |
| st_messages_mpa_rspamd_config_templates | List of rspamd configs to deploy, merged with the default configuration list. | list of 'dict' | no | [] |
| st_messages_mpa_unbound_image | Image repository for the mpa unbound. | str | no | docker.io/alpinelinux/unbound |
| st_messages_mpa_unbound_tag | Tag of the unbound docker image to use. alpinelinux/unbound publishes no versioned tags, only latest. | str | no | latest |
| st_messages_mpa_unbound_config_template | Local path to the unbound.conf template. | str | no | mpa/unbound.conf.j2 |
| st_messages_mpa_clamav_image | Image repository for the mpa clamav. | str | no | docker.io/clamav/clamav |
| st_messages_mpa_clamav_tag | The tag of the clamav docker image to use. See https://hub.docker.com/r/clamav/clamav/tags. | str | no | 1.5.3 |
| st_messages_mpa_clamav_config_template | Local path to the clamd.conf template. | str | no | mpa/clamd.conf.j2 |
| st_messages_mpa_valkey_image | Image repository for the mpa valkey. | str | no | docker.io/valkey/valkey |
| st_messages_mpa_valkey_tag | The tag of the valkey docker image to use. See https://hub.docker.com/r/valkey/valkey/tags. | str | no | 9.1.1 |
| st_messages_mpa_rollback_enabled | Whether or not to trigger the rollback tasks if the mpa deployment fails. | bool | no | False |
| st_messages_mpa_compose_template | Local path to the custom template to use for mpa compose file. | str | no | mpa/compose.yaml.j2 |
| st_messages_cadvisor_enabled | Triggers the installation of the cadvisor container, a Prometheus-compliant containers monitoring tool. | bool | no | False |
| st_messages_cadvisor_image | Image repository for the cadvisor container. | str | no | ghcr.io/google/cadvisor |
| st_messages_cadvisor_tag | Tag of the cadvisor docker image to deploy. | str | no | v0.60.3 |
| st_messages_cadvisor_port | The host published port of the cadvisor container. | str | no | 127.0.0.1:50499 |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.messages
      ansible.builtin.import_role:
        name: suitenumerique.st.messages
      vars:
```

## License

MIT

## Author and Project Information
La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
