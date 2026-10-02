<!-- BEGIN_ANSIBLE_DOCS -->
# Ansible Role: suitenumerique.st.file_scanner
Version: 0.4.0

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
| st_file_scanner_tag | Tag of the file-scanner docker image to deploy. | str | no | 0.2.0 |
| st_file_scanner_clamav_image | Image repository for the bundled ClamAV daemon. | str | no | docker.io/clamav/clamav |
| st_file_scanner_clamav_tag | Tag of the ClamAV docker image to deploy. | str | no | 1.4 |
| st_file_scanner_dir | Remote path to the base directory for the file-scanner app. | str | no | /opt/file-scanner/file-scanner |
| st_file_scanner_port | The host published port for the file-scanner API (maps to the container's uvicorn port 8090). | str | no | 50800 |
| st_file_scanner_rollback_enabled | Whether or not to trigger the rollback tasks if the file-scanner deployment fails. | bool | no | False |
| st_file_scanner_compose_template | Local path to the custom template to use for file-scanner compose file. | str | no | file_scanner/compose.yaml.j2 |
| st_file_scanner_env_template | Local path to the custom template to use for file-scanner env file. | str | no | file_scanner/env.j2 |
| st_file_scanner_env | Content of the default env_template, not used if st_file_scanner_env_template is defined. | str | no |  |
| st_file_scanner_clamav_env_template | Local path to the custom template to use for the clamav env file. | str | no | file_scanner/clamav_env.j2 |
| st_file_scanner_clamav_env | Lines of the default clamav_env_template, one `KEY=value` per element, not used if st_file_scanner_clamav_env_template is defined. The clamav image applies `CLAMD_CONF_<Option>=<value>` and `FRESHCLAM_CONF_<Option>=<value>` lines to clamd.conf / freshclam.conf at startup. The default raises clamd's size caps above the scanner's 2 GiB MAX_URL_SIZE: below StreamMaxLength a too-big file dies mid-INSTREAM, and below MaxFileSize/MaxScanSize it is skipped and reported CLEAN unscanned. clamd spools each INSTREAM to its temporary directory, so the clamav container needs that much free disk per concurrent scan (cap the concurrency with CLAMD_CONF_MaxThreads). | list of 'str' | no | ['CLAMD_CONF_StreamMaxLength=2200M', 'CLAMD_CONF_MaxFileSize=2200M', 'CLAMD_CONF_MaxScanSize=2200M', 'CLAMD_CONF_MaxScanTime=900000', 'CLAMD_CONF_AlertExceedsMax=yes'] |
| st_file_scanner_exav_enabled | Triggers the installation of exav, a second malware engine alongside clamav. The daemon alone changes nothing: name it in the application's `DEFAULT_SCANNERS` (e.g. `{"malware": ["clamav", "exav"]}`) and point `EXAV_HOSTS` at `exav:3310` in `st_file_scanner_env` for scans to reach it. | bool | no | False |
| st_file_scanner_exav_image | Image repository for the exav daemon (distroless, runs as uid 65532, speaks the clamd protocol on 3310). | str | no | ghcr.io/sylvinus/exav |
| st_file_scanner_exav_tag | Tag of the exav docker image to deploy. | str | no | 0.0.1 |
| st_file_scanner_exav_env_template | Local path to the custom template to use for the exav env file. | str | no | file_scanner/exav_env.j2 |
| st_file_scanner_exav_env | Lines of the default exav_env_template, one `KEY=value` per element. exav ships no signature database and has no updater of its own, so the default compiles the files the bundled clamav keeps fresh, read-only from its volume — a few minutes and a ~3.6 GB RAM spike on every start, which usually means raising st_file_scanner_timeout. The cheaper production path is a prebuilt database served over HTTPS: set `EXAV_DB_URL=https://…/your.exavdb` plus `EXAV_SIG_DIR=/var/lib/exav` (the daemon's own writable volume) and it loads in seconds. The spill and scan limits sit above the application's MAX_URL_SIZE, like clamd's StreamMaxLength. | list of 'str' | no | ['EXAV_SIG_DIR=/var/lib/clamav', 'EXAV_MAX_SPILL_BYTES=2200M', 'EXAV_MAX_SCAN_SECS=900'] |
| st_file_scanner_timeout | Seconds the systemd unit waits for every container of the stack to report healthy. The first clamav start downloads the signature database; an exav that compiles its own needs several minutes more. | int | no | 300 |
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
