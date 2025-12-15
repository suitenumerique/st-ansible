<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.alloy
Version: 0.0.5

This role installs, manages and configures a `alloy` instance on Debian systems.

Tags: suiteterritoriale, system

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | trixie |

## Role Arguments


### Entrypoint: main

Install and configure a alloy instance on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| st_alloy_manage_user | Controls wether the role manages the alloy user of if it's externally managed. | bool | no | True |
| st_alloy_user | The unix user to run alloy as. | str | no | alloy |
| st_alloy_group | The primary group of the alloy user. | str | no | {{ st_alloy_user }} |
| st_alloy_config_template | Local path to the alloy configuration template. | str | yes |  |
| st_alloy_config_dir | Absolute path to the alloy configurations directory. | str | no | /etc/alloy |
| st_alloy_env_template | Local path to the alloy environment config file. | str | no | default_env.j2 |
| st_alloy_env_dir | Absolute path to the environment configurations directory. | str | no | /etc/default |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.alloy
      ansible.builtin.import_role:
        name: suitenumerique.st.alloy
      vars:
        st_alloy_config_template: # required, type: str
```

## License

MIT

## Author and Project Information
La Suite territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
