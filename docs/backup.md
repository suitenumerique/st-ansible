# Backup

> [!NOTE]
> The restic role is not imported by any application role. It is provided as a
> convenience, use it, replace it, or skip it entirely depending on your backup strategy.

The `restic` role provides automated backups for La Suite Territoriale deployments. It runs as a **system systemd
service** (not rootless Podman) under a dedicated `restic` user.

## How It Works

Restic creates encrypted, deduplicated backups to an S3-compatible repository. Two systemd timers drive the backup lifecycle:

1. **restic-backup.timer**, triggers `restic backup` (default: daily at 06:00)
2. **restic-forget.timer**, triggers `restic forget --prune` (default: daily at 18:00)

The forget policy retains:

- 7 daily backups
- 4 weekly backups
- 12 monthly backups

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 128 MB minimum |
| Disk | Varies with backup size + cache (`/var/cache/restic`) |
| S3 Bucket | S3-compatible storage for the restic repository |

## Variable Reference

See [roles/restic/REFERENCE.md](../roles/restic/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `restic_repository` | S3 repository path | **(required)** |
| `restic_password` | Repository encryption password | **(required)** |
| `restic_s3_access_key` | S3 access key | **(required)** |
| `restic_s3_secret_key` | S3 secret key | **(required)** |
| `restic_files` | List of paths to back up | **(required)** |
| `restic_backup_timer` | Systemd OnCalendar schedule | `Mon..Sun 6:00:00` |
| `restic_forget_timer` | Systemd OnCalendar schedule | `Mon..Sun 18:00:00` |
| `restic_forget_keep_daily` | Daily retention count | `7` |
| `restic_forget_keep_weekly` | Weekly retention count | `4` |
| `restic_forget_keep_monthly` | Monthly retention count | `12` |
| `restic_backup_precmd` | Pre-backup command (ExecStartPre) | _(none)_ |
| `restic_setcap_read_search` | Grant CAP_DAC_READ_SEARCH | `true` |

## CAP_DAC_READ_SEARCH

By default, the role grants the `CAP_DAC_READ_SEARCH` ambient capability to the backup service. This allows the
`restic` user to read any file on the system, which is needed when backing up files from multiple users
(e.g. Podman container data owned by different UIDs). Set `restic_setcap_read_search: false` if you prefer
to manage file permissions manually.

## Alerting

The role can send Mattermost alerts when the backup service fails:

```yaml
restic_alert_enabled: true
restic_alert_mattermost_webhook: "https://mattermost.example.com/hooks/xxx"
```

## What to Back Up

LaSuite applications are actually mostly stateless - you only need to backup
your PostgreSQL databases and S3 volumes on most of them. You can use the
`restic` role of this collection to backup them if you need, but we recommend
using managed databases and S3 volumes most of the time.

The only application that can benefit backups in this collection is `messages/mpa`
because it uses a local cache for redis history. For this case you can
use the following configuration :

```yaml
restic_repository: s3:https://s3.example.com/backups
restic_password: "{{ vault_restic_password }}"
restic_s3_access_key: "{{ vault_s3_access_key }}"
restic_s3_secret_key: "{{ vault_s3_secret_key }}"
restic_files:
  - /opt/messages
```

## Pre-Backup Commands

The `restic_backup_precmd` variable sets the `ExecStartPre` directive of the restic-backup
systemd unit. This runs a command before the actual backup starts, which is useful for dumping
databases that live in containers or on remote hosts.

For example, to dump a PostgreSQL database before the backup:

```yaml
restic_backup_precmd: "pg_dump -h db.example.com -U messages messages > /opt/messages/messages.sql"
```

Or to dump multiple databases:

```yaml
restic_backup_precmd: >-
  pg_dump -h db.example.com -U messages messages > /opt/messages/messages.sql &&
  pg_dump -h db.example.com -U drive drive > /opt/drive/drive.sql
```

The dump files are written to the application directories, which are then picked up by restic
as part of the regular file backup. If the pre-command fails, the systemd unit fails and no
backup is taken, use this with the alerting feature to get notified.

## Troubleshooting

```bash
ssh <host>
sudo -i

# Service lifecycle (system services, not user units)
systemctl status restic-backup.service
systemctl start restic-backup.service
systemctl stop restic-backup.service
systemctl status restic-backup.timer

# Logs
journalctl -u restic-backup.service -f
journalctl -u restic-backup.service --since today
journalctl -u restic-backup.service --since "3 hours ago"

# Snapshots
set -a; source /etc/restic/env
restic snapshots

# Restore a specific path from the latest snapshot
restic restore latest --target /tmp/restore --path /opt/messages

# Restore a specific snapshot
restic restore <snapshot-id> --target /tmp/restore
```
