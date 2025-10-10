<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.valkey
Version: 0.0.1

This role installs, configure and manage a valkey instance.

Tags: suiteterritoriale, system, podman, rootless

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments


### Entrypoint: main

Installs, configures and manages a Valkey instance. The role also handles valkey-sentinel configurations.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_valkey_instance_name | Name of the instance to deploy. | str | no | stvalkey |
| st_valkey_instance_config_dir | Path to the valkey instance configuration directory. | str | no | /opt/valkey/{{ st_valkey_instance_name }}/config |
| st_valkey_instance_config_template | Local path to the valkey instance configuration file template. | str | no | instance/persistence.j2 |
| st_valkey_instance_users_template | Local path to the valkey instance users (ACL) template. | str | no | instance/users.acl.j2 |
| st_valkey_instance_compose_template | Local path to the valkey docker compose template. | str | no | instance/compose.yml.j2 |
| st_valkey_instance_port | When using the role provided templates, specify the port to bind valkey process to. | str | no | 56379 |
| st_valkey_instance_version | The docker hub tag to use for the valkey container. | str | no | 8 |
| st_valkey_instance_maxmemory | When using the role provided templates, specify the maxmemory parameter. Defaults to 70% of total RAM. | str | no | {{ (ansible_memtotal_mb * 0.7) | int }}mb |
| st_valkey_sentinel_password | When using the role provided templates, specify the 'sentinel' user password. This one is the same for all instances. | str | no |  |
| st_valkey_admin_password | When using the role provided templates, specify the 'admin' user password. This one is the same for all instances. | str | no |  |
| st_valkey_instance_replication_enabled | When using the role provided templates, include the replication configuration in the valkey instance configuration template. | bool | no | False |
| st_valkey_instance_replication_password | When using the role provided templates and when replication is enabled, specify the 'replica' user password. | str | no |  |
| st_valkey_instance_monitoring_password | When using the role provided templates, adds a 'monitoring' user to the ACL file with the provided password. | str | no |  |
| st_valkey_instance_application_users | When using the role provided ACL templates, add additional users with custom permissions. The default permissions allow pretty much everything an application has to do on all keys and removes the dangerous commands. | list of 'str' | no |  |
| st_valkey_sentinel_config_dir | Remote path to the valkey-sentinel configuration directory. | str | no | /etc/valkey |
| st_valkey_sentinel_config_template | Local path to the sentinel configuration template, used after the first run. | str | no | sentinel/sentinel.j2 |
| st_valkey_sentinel_users_template | Local path to the sentinel users (ACL) configuration template. | str | no | sentinel/sentinel-users.acl.j2 |
| st_valkey_sentinel_port | When using role provided config template, specify the port to bind sentinel to. | str | no | 26379 |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.valkey
      ansible.builtin.import_role:
        name: suitenumerique.st.valkey
      vars:
```

## License

MIT

## Author and Project Information
La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
