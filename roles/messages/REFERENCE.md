<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.messages
Version: 0.0.1

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
| st_messages_uid | UID of the `messages` user, used for the podman role. | int | no | 1100 |
| st_messages_gid | GID of the `messages` group, used for the podman role. | int | no | {{ st_messages_uid }} |
| st_messages_mta_in_enabled | Triggers the installation of the mta-in. | bool | no | False |
| st_messages_mta_in_tag | Tag of the mta-in docker image to deploy. | str | no | main |
| st_messages_mta_in_dir | Remote path to the base directory for mta-in app. | str | no | /opt/messages/mta-in |
| st_messages_mta_in_env_template | Local path to the custom template to use for mta-in env file. | str | no | mta_in/env.j2 |
| st_messages_mta_in_env | Content of the default mta_in_env_template, not used if st_messages_mta_in_env_template is defined. | str | no |  |
| st_messages_mta_out_enabled | Triggers the installation of the mta-out. | bool | no | False |
| st_messages_mta_out_tag | Tag of the mta-out docker image to deploy. | str | no | main |
| st_messages_mta_out_dir | Remote path to the base directory for mta-out app. | str | no | /opt/messages/mta-out |
| st_messages_mta_out_env_template | Local path to the custom template to use for mta-out env file. | str | no | mta_out/env.j2 |
| st_messages_mta_out_env | Content of the default mta_out_env_template, not used if st_messages_mta_out_env_template is defined. | str | no |  |
| st_messages_socks_proxy_enabled | Triggers the installation of the socks-proxy. | bool | no | False |
| st_messages_socks_proxy_tag | Tag of the socks-proxy docker image to deploy. | str | no | main |
| st_messages_socks_proxy_dir | Remote path to the base directory for socks-proxy app. | str | no | /opt/messages/socks-proxy |
| st_messages_socks_proxy_env_template | Local path to the custom template to use for socks-proxy env file. | str | no | socks_proxy/env.j2 |
| st_messages_socks_proxy_env | Content of the default socks_proxy_env_template, not used if st_messages_socks_proxy_env_template is defined. | str | no |  |
| st_messages_mpa_enabled | Triggers the installation of the mpa. | bool | no | False |
| st_messages_mpa_dir | Remote path to the base directory for mpa app. | str | no | /opt/messages/mpa |
| st_messages_mpa_rspamd_tag | The tag of the rspamd docker image to use. See https://hub.docker.com/r/rspamd/rspamd/tags. | str | no | 3 |
| st_messages_mpa_rspamd_controller_password | Password of the rspamd controller webui. | str | no |  |
| st_messages_mpa_rspamd_neighbours | List of URLs to the rspamd neighbours controllers. | list of 'str' | no |  |
| st_messages_mpa_rspamd_config_templates | List of rspamd configs to deploy, merged with the default configuration list. | list of 'dict' | no | [] |
| st_messages_mpa_unbound_config_template | Local path to the unbound.conf template. | str | no | mpa/unbound.conf.j2 |
| st_messages_mpa_clamav_tag | The tag of the clamav docker image to use. See https://hub.docker.com/r/clamav/clamav/tags. | str | no | 1.4 |
| st_messages_mpa_clamav_config_template | Local path to the clamd.conf template. | str | no | mpa/clamd.conf.j2 |
| st_messages_mpa_valkey_enabled | Triggers the installation of a local valkey instance, which also deploys a default redis.conf rspamd config. | bool | no | True |
| st_messages_mpa_valkey_tag | The tag of the valkey docker image to use. See https://hub.docker.com/r/valkey/valkey/tags. | str | no | 8 |
| st_messages_monitoring_enabled | Triggers the installation of the alloy agent, used to send metrics to a Prometheus compatible server and logs to a Loki server. | bool | no | False |
| st_messages_monitoring_prometheus_url | When monitoring deployment is enabled, specifies the Prometheus URL to send metrics to. | str | no |  |
| st_messages_monitoring_loki_url | When monitoring deployment is enabled, specifies the Loki URL to send logs to. | str | no |  |



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
