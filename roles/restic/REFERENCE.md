<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.restic
Version: 0.0.1

This role installs and configure restic on Debian systems.

Tags: restic, backup, suiteterritoriale, system, linux

## Requirements

| Platform | Versions |
| -------- | -------- |
| Debian | bookworm, trixie |

## Role Arguments



### Entrypoint: main

Install and configure a restic instance on Debian systems.

|Option|Description|Type|Required|Default|
|---|---|---|---|---|
| restic_repository | The repository's path. Populates the RESTIC_REPOSITORY var value. For now this role only supports s3 repositories. | str | yes |  |
| restic_password | The repository's password. Populates the RESTIC_PASSWORD var. | str | yes |  |
| restic_s3_access_key | The access key for the S3 bucket. | str | yes |  |
| restic_s3_secret_key | The secret key for the S3 bucket. | str | yes |  |
| restic_files | A list containing the files to backup on the system. | list of 'str' | yes |  |
| restic_binary_path | Path to the restic binary. | str | no | /usr/local/bin/restic |
| restic_force_install | Force the installation of restic even if the binary already exists. | bool | no | False |
| restic_version | Version of restic to install. | str | no | 0.18.0 |
| restic_config_dir | Path to the configuration directory of restic. | str | no | /etc/restic |
| restic_cache_dir | Path to the cache directory of restic. | str | no | /var/cache/restic |
| restic_backup_precmd | Command to add to the ExecStartPre field of the restic-backup systemd unit. | str | no |  |
| restic_backup_timer | Systemd OnCalendar timer to trigger the restic-backup systemd unit. | str | no | Mon..Sun 6:00:00 |
| restic_forget_timer | Systemd OnCalendar timer to trigger the restic-forget systemd unit. | str | no | Mon..Sun 18:00:00 |
| restic_forget_keep_daily | Number of daily backups to keep. | int | no | 7 |
| restic_forget_keep_weekly | Number of weekly backups to keep. | int | no | 4 |
| restic_forget_keep_monthly | Number of monthly backups to keep. | int | no | 12 |
| restic_forget_keep_yearly | Number of yearly backups to keep. | int | no | 0 |
| restic_setcap_read_search | Add the CAP_DAC_READ_SEARCH ambient capability to the backup systemd unit. This is used when we need the restic user to read anywhere in the system, to backup files from multiple users. | bool | no | True |
| restic_manage_user | Controls wether the role manages the restic_user and restic_group (true) or if they're created externally (false). | bool | no | True |
| restic_user | The unix user to create and start the backups with. | str | no | restic |
| restic_group | The unix group to create associated to restic_user. | str | no | {{ restic_user }} |
| restic_home | Path to the home directory of the restic_user. | str | no | /var/lib/restic |



## Dependencies
None.

## Example Playbook

```
- hosts: all
  tasks:
    - name: Importing role: suitenumerique.st.restic
      ansible.builtin.import_role:
        name: suitenumerique.st.restic
      vars:
        restic_repository: # required, type: str
        restic_password: # required, type: str
        restic_s3_access_key: # required, type: str
        restic_s3_secret_key: # required, type: str
        restic_files: # required, type: list of 'str'
```

## License

MIT

## Author and Project Information
La Suite Territoriale @ Agence Nationale de la Cohésion des Territoires

Issues: [tracker](https://github.com/suitenumerique/st-ansible/issues)
<!-- END_ANSIBLE_DOCS -->
