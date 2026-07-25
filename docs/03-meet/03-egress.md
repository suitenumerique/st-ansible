# LiveKit Egress (Recording)

LiveKit egress is the recording component for Meet: it joins a room, transcodes the media, and
uploads the finished recording to meet's existing S3 bucket. It is deployed as its own standalone
sub-application (own `egress.service` systemd user unit, own `/opt/meet/egress` directory),
independent of the livekit compose stack, so it can be placed and resourced separately from
livekit-server.

Recording's opt-in status depends on how you deploy: with the **role**, recording is opt-in —
enabling this sub-app (`st_meet_egress_enabled`) only starts the recorder, and you must also
enable recording on the meet backend (see "Meet Backend Configuration" below) and set the
recording webhook prerequisite (see "Recording Webhook" below), or completed recordings will
never be marked ready. With **`st-cli`**, egress is deployed alongside livekit and recording is
enabled automatically — no questions asked, nothing left to wire up.

## Deployment Topologies

Egress and livekit **must share the same redis**. There are two supported topologies:

### Co-located (single node)

Egress runs on the same host as livekit and reuses the local valkey that ships with the
livekit compose stack. This is the default (`st_meet_livekit_valkey_enabled: true`), no redis
vars need to be changed:

```yaml
st_meet_livekit_enabled: true
st_meet_egress_enabled: true
st_meet_livekit_domain: livekit.example.com
st_meet_livekit_turn_domain: turn.example.com
st_meet_livekit_api_key: changeme
st_meet_livekit_api_secret: changeme
st_meet_public_host: meet.example.com # required for the recording webhook, see below
# st_meet_livekit_valkey_enabled: true          (default, local valkey)
# st_meet_livekit_redis_address: 127.0.0.1:6379 (default)
```

### Dedicated node(s)

Egress runs on a different host than livekit (or livekit itself is deployed on multiple hosts).
The local valkey container is not usable in this case (it's bound to `127.0.0.1` on the livekit
host), so you must disable it and point both livekit and egress at an external shared redis:

```yaml
# livekit host(s)
st_meet_livekit_enabled: true
st_meet_livekit_valkey_enabled: false
st_meet_livekit_redis_address: redis.example.com:6379
st_meet_livekit_redis_username: livekit
st_meet_livekit_redis_password: changeme
st_meet_livekit_domain: livekit.example.com
st_meet_livekit_turn_domain: turn.example.com
st_meet_livekit_api_key: changeme
st_meet_livekit_api_secret: changeme
st_meet_public_host: meet.example.com # required for the recording webhook, see below

# egress host(s)
st_meet_egress_enabled: true
st_meet_livekit_redis_address: redis.example.com:6379
st_meet_livekit_redis_username: livekit
st_meet_livekit_redis_password: changeme
st_meet_livekit_domain: livekit.example.com
st_meet_livekit_api_key: changeme
st_meet_livekit_api_secret: changeme
```

> [!NOTE]
> `st-cli` automates this: the livekit bootstrap step asks for the egress hosts right after the
> livekit hosts (blank = co-locate), decides the topology, and mirrors the livekit API
> key/secret (and the redis password, when external) into egress's own vault automatically. It
> also enables recording on the meet backend unconditionally as part of this step — see "Meet
> Backend Configuration" below. See the
> [full-high-availability example](../99-examples/full-high-availability/playbook_egress.yml) for
> a dedicated-node playbook.

## Container Stack

```text
egress (docker.io/livekit/egress)
  ├── network_mode: host, cap_add: CAP_SYS_ADMIN (headless Chromium recorder)
  ├── connects to livekit-server via the shared redis + the LiveKit WebSocket API
  └── uploads finished recordings → meet backend's S3 bucket
```

## Prerequisites

| Requirement | Value |
|-------------|-------|
| Platform | Debian Trixie |
| RAM/CPU | Recording is CPU-heavy transcoding; size the host (or set `st_meet_egress_cpus`/`st_meet_egress_memory`) accordingly |
| Network | Host network (`network_mode: host`) |
| Capabilities | `CAP_SYS_ADMIN` (headless Chromium recorder) |
| Redis | Shared redis/valkey with the livekit unit (same instance, see topologies above) |
| Storage | S3 bucket configured on the meet backend (`AWS_S3_*`) — this collection does not provision it |

## Variable Reference

See [roles/meet/REFERENCE.md](../../roles/meet/REFERENCE.md) for the complete variable reference.

## Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_meet_egress_enabled` | Enable the egress recorder | `false` |
| `st_meet_egress_dir` | Application directory | `/opt/meet/egress` |
| `st_meet_egress_tag` | egress image tag | see REFERENCE.md |
| `st_meet_egress_cpus` | Optional CPU limit (podman-compose `cpus`, e.g. `'2'`) | _(no limit)_ |
| `st_meet_egress_memory` | Optional memory limit (podman-compose `mem_limit`, e.g. `'2g'`) | _(no limit)_ |
| `st_meet_egress_rollback_enabled` | Rollback on failure | `false` |
| `st_meet_egress_compose_template` | Local path to a custom compose template | `egress/compose.yaml.j2` |
| `st_meet_egress_files` | List of config files to deploy | _(defaults)_ |

Egress reuses these livekit-side variables — they must be set on the egress hosts too (`st-cli`
mirrors them automatically, see above):

| Variable | Description | Default |
|----------|-------------|---------|
| `st_meet_livekit_redis_address` | Shared redis/valkey address (`host:port`) | `127.0.0.1:6379` |
| `st_meet_livekit_redis_username` | Shared redis username (external redis only) | _(empty)_ |
| `st_meet_livekit_redis_password` | Shared redis password (external redis only) | _(empty)_ |
| `st_meet_livekit_api_key` | LiveKit API key | **(required)** |
| `st_meet_livekit_api_secret` | LiveKit API secret | **(required)** |
| `st_meet_livekit_domain` | LiveKit domain; egress connects to `wss://<domain>` | **(required)** |

## Recording Webhook

> [!IMPORTANT]
> Recording completion is signalled to the meet backend by a LiveKit webhook, configured on the
> **livekit** hosts (not egress). `livekit.default.yaml.j2` only emits the `webhook:` block —
> pointing at `https://{{ st_meet_public_host }}/api/v1.0/rooms/webhooks-livekit/` — when
> `st_meet_public_host` is set. If it's left unset, recordings still run and upload to S3,
> but the meet backend is never notified: they will never be marked complete/ready. Set
> `st_meet_public_host` alongside the other livekit variables.

## Meet Backend Configuration

Enabling the egress sub-app only starts the recorder — recording must also be turned on in the
meet backend. These are Django environment variables, not role variables. `st-cli` writes the
whole `RECORDING_*` block below into the meet core's backend env automatically, no prompts
involved; role users deliver it themselves via `st_meet_backend_env`:

| Variable | Description | Value |
|----------|-------------|-------|
| `RECORDING_ENABLE` | Enables recording in the meet backend | `True` |
| `RECORDING_STORAGE_EVENT_ENABLE` | Always disabled by this collection | `False` |
| `RECORDING_OUTPUT_FOLDER` | S3 key prefix recordings are uploaded under | `recordings` |
| `RECORDING_DOWNLOAD_BASE_URL` | Base URL for the emailed recording-ready link | `https://{{ st_meet_public_host }}/recording` |

`st-cli` fixes `RECORDING_OUTPUT_FOLDER` at `recordings`; it's no longer promptable. To use a
different S3 folder prefix, edit `RECORDING_OUTPUT_FOLDER` directly in the core's `vars.yml`
after bootstrap.

> [!NOTE]
> Note the deliberate asymmetry between the last two variables: `RECORDING_DOWNLOAD_BASE_URL` uses
> the SINGULAR `/recording` — it must match the meet frontend SPA route, and pluralizing it 404s
> the emailed recording-ready link — while `RECORDING_OUTPUT_FOLDER` is legitimately PLURAL
> `recordings`, since it's just an S3 key prefix, unrelated to the URL path. Don't "fix" one to
> match the other.

Recordings are uploaded to meet's existing S3 bucket (the backend's `AWS_S3_*` configuration);
this collection does not provision S3.

## Resource Limits

`st_meet_egress_cpus` and `st_meet_egress_memory` set optional podman-compose `cpus` / `mem_limit`
limits on the egress container. They are recommended on single-node (co-located) setups: starting
a recording is CPU-heavy transcoding and, left unbounded, can starve or OOM the livekit server
running on the same host.

```yaml
st_meet_egress_cpus: "2"
st_meet_egress_memory: "2g"
```

## Data & Volumes

```text
/opt/meet/egress/
├── compose.yaml    # podman-compose file (generated)
└── egress.yaml     # egress config
```

## Custom Configuration

The `st_meet_egress_files` variable controls which config files are deployed. The default deploys
`egress.yaml` from the role's template. Override this entirely for a custom setup:

```yaml
st_meet_egress_files:
  - src: my-custom/egress.yaml.j2
    dest: egress.yaml
```

`st_meet_egress_compose_template` can similarly be pointed at a custom compose template if the
default container definition (network mode, capabilities, resource limits) doesn't fit your setup.

## Troubleshooting

```bash
ssh <host>
sudo -iu meet

# Service lifecycle
systemctl --user status egress.service
systemctl --user start egress.service
systemctl --user stop egress.service

# Logs
journalctl --user -u egress.service -f
journalctl --user -u egress.service --since today
journalctl --user -u egress.service --since "3 hours ago"

# Containers
podman-compose -f /opt/meet/egress/compose.yaml ps
```
