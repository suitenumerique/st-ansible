# Collabora Online

Collabora Online is a document editor integrated with Drive, based on LibreOffice technology.
It runs as a separate container alongside the Drive application.

## Container Stack

```text
collabora (docker.io/collabora/code)
  └── LibreOffice Online on port 9980
```

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM | 2 GB minimum (LibreOffice rendering is memory-intensive) |
| Disk | 1 GB for image |

## Variable Reference

See [roles/drive/REFERENCE.md](../../roles/drive/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_drive_collabora_enabled` | Enable Collabora | `false` |
| `st_drive_collabora_tag` | Docker image tag | see REFERENCE.md |
| `st_drive_collabora_dir` | Application directory | `/opt/drive/collabora` |
| `st_drive_collabora_port` | Host port (maps to container 9980) | `50101` |
| `st_drive_collabora_env` | Environment content | _(empty)_ |
| `st_drive_collabora_rollback_enabled` | Rollback on failure | `false` |

## Network & Ports

| Variable | Default | Container port |
|----------|---------|---------------|
| `st_drive_collabora_port` | `50101` | 9980 |

## Data & Volumes

Stateless. No persistent bind mounts.

## Single-node setup (Drive + Collabora on one host)

When Drive and Collabora run on the **same host** behind one reverse proxy (e.g. Caddy),
two server-to-server calls have to reach the host's public IP:

| Hop | From → To | URL it uses |
|-----|-----------|-------------|
| Browser → Collabora | editor iframe (`urlsrc`) | public Collabora URL |
| Browser → Drive | SPA / API | public Drive URL |
| **Drive worker → Collabora** | `GET /hosting/discovery` | `WOPI_COLLABORA_DISCOVERY_URL` |
| **Collabora → Drive** | WOPI `CheckFileInfo`/`GetFile`/`PutFile` | `WOPI_SRC_BASE_URL` |

### Why it doesn't work out of the box (pasta)

Rootless Podman defaults to **pasta** networking. To make outbound traffic look like it
comes from the host, pasta assigns the **host's own IP addresses** to the container's
network namespace. A side effect (and a form of host isolation): from inside a container,
the host's **public IP is a local address** — a packet sent to it is delivered back into the
namespace instead of going out to the reverse proxy, so you get an immediate
`Connection refused`. The browser is unaffected (it's outside the namespace); only the
container-to-host hops break. The result is Collabora documents reported as
**"Unsupported format"**, because the worker never fetched Collabora's discovery XML.

`host.containers.internal` (and the `host-gateway` token) is the address pasta *does* route
to the host — that's why it works when the plain public IP doesn't.

### Fix: pin the public hostnames to `host-gateway` in the containers that call out

Point Collabora's public hostname (in the **worker**) and Drive's public hostname (in
**Collabora**) at `host-gateway` via `extra_hosts`. Then both server-to-server hops use the
same public HTTPS URLs the browser uses, and TLS validates normally.

1. Copy the two compose templates and add an `extra_hosts` entry to each service.

   `workers/compose.yaml.j2` (worker fetches Collabora discovery):

   ```yaml
   name: drive_workers
   services:
     worker:
       container_name: drive-worker
       image: "{{ st_drive_backend_image }}:{{ st_drive_tag }}"
       env_file: ./env
       command: celery -A drive.celery_app worker --task-events --beat -l INFO -c {{ ansible_processor_nproc }} -Q celery,default --schedule=/tmp/celerybeat-schedule
       extra_hosts:
         - "collabora.example.org:host-gateway"    # <-- your public Collabora domain
       healthcheck:
         test: ["CMD-SHELL", "celery -A drive.celery_app inspect ping"]
         interval: 1m
         timeout: 10s
         start_interval: 5s
   ```

   `collabora/compose.yaml.j2` (Collabora calls back to Drive):

   ```yaml
   name: drive_collabora
   services:
     collabora:
       container_name: collabora
       image: "{{ st_drive_collabora_image }}:{{ st_drive_collabora_tag }}"
       env_file: ./env
       ports:
         - "{{ st_drive_collabora_port }}:9980"
       extra_hosts:
         - "{{ st_drive_public_host }}:host-gateway"   # <-- Drive's public domain
       healthcheck:
         test: bash -c 'exec 3<>/dev/tcp/localhost/9980'
         interval: 1m
         timeout: 3s
         start_period: 10s
   ```

2. Point the role at your overrides and keep the WOPI URLs public:

   ```yaml
   st_drive_workers_compose_template: path/to/compose.yaml.j2
   st_drive_collabora_compose_template: path/to/compose.yaml.j2

   st_drive_backend_env: |
     ...
     WOPI_COLLABORA_DISCOVERY_URL=https://collabora.example.org/hosting/discovery
     WOPI_SRC_BASE_URL=https://drive.example.org
   ```


### Verify

```bash
sudo -iu drive
# repopulate the supported-formats cache, then reload Drive in the browser:
podman exec drive-backend python manage.py trigger_wopi_configuration
```

A document should now open in Collabora instead of showing "Unsupported format".

## Troubleshooting

```bash
ssh <host>
sudo -iu drive

# Service lifecycle
systemctl --user status collabora.service
systemctl --user start collabora.service
systemctl --user stop collabora.service

# Logs
journalctl --user -u collabora.service -f
journalctl --user -u collabora.service --since today
journalctl --user -u collabora.service --since "3 hours ago"

# Containers
podman-compose -f /opt/drive/collabora/compose.yaml ps
```
