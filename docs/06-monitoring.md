# Monitoring

The collection provides two monitoring components:

1. **cAdvisor**, container metrics exporter (Prometheus-compatible)
2. **Grafana Alloy**, telemetry pipeline (logs, metrics, traces)

> [!NOTE]
> The alloy role is not imported by any application role. It is provided as a
> convenience, use it, replace it, or skip it entirely depending on your monitoring strategy.
> The cAdvisor container is deployable on every role if `st_<role>_cadvisor_enabled` is set to `true`.

## cAdvisor

cAdvisor is a container monitoring tool that exposes resource usage and performance metrics for running containers.
It is available as an optional add-on for the `messages`, `drive`, and `keycloak` roles.

### Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_<role>_cadvisor_enabled` | Enable cAdvisor for this role | `false` |
| `st_<role>_cadvisor_port` | Host published port | `127.0.0.1:58080` |

Replace `<role>` with `messages`, `drive`, or `keycloak`.

### Network & Ports

By default, cAdvisor binds to `127.0.0.1:58080` (localhost only). Change `st_<role>_cadvisor_port` to expose it
(e.g. `0.0.0.0:58080`) if your Prometheus or Alloy scraper is on a different host.

### Deployed as a Separate Unit

cAdvisor runs on a rootless podman container and is deployed as its own systemd user unit (`cadvisor.service`)
under the same Unix user as the application.

## Grafana Alloy

Alloy is a telemetry collector from Grafana. It ships logs and metrics from the host and containers to a
Grafana Cloud or self-hosted Grafana LGTM stack.

Unlike the application roles, Alloy runs as a **system systemd service** (not rootless Podman) under a dedicated
`alloy` user. It is a standalone role that does not depend on the podman role.

### Variable Reference

See [roles/alloy/REFERENCE.md](../roles/alloy/REFERENCE.md) for the complete variable reference.

### Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `st_alloy_config_template` | Path to the Alloy config template | **(required)** |
| `st_alloy_user` | Unix user to run Alloy as | `alloy` |
| `st_alloy_config_dir` | Configuration directory | `/etc/alloy` |
| `st_alloy_env_dir` | Environment file directory | `/etc/default` |
| `st_alloy_manage_user` | Whether the role manages the user | `true` |

### Example Configuration Templates

See [examples/monitoring/](00-examples/monitoring/) for Alloy configuration templates
for Messages and Drive.

## Monitoring Stack Integration

A typical monitoring setup for La Suite Territoriale:

1. **cAdvisor** on each application host → scrapes container metrics → Prometheus scrapes cAdvisor
2. **Alloy** on each application host → pushes logs to Loki, metrics to Prometheus/Mimir
3. **Grafana** → queries Loki and Prometheus/Mimir for dashboards and alerts
