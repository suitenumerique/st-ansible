#!/usr/bin/env bash

set -euo pipefail

TIMEOUT="${TIMEOUT:-300}"
INTERVAL="${INTERVAL:-5}"

# Start the compose in detached mode
# then let some time to podman to register containers
podman-compose --podman-run-args="--sdnotify ignore" --no-ansi up -d
sleep 2

# Read container IDs into array, one per line, then put it into the CONTAINERS var
mapfile -t CONTAINERS < <(podman-compose ps -q)
TOTAL=${#CONTAINERS[@]}

if [ "$TOTAL" -eq 0 ]; then
  echo "No containers found after compose up, aborting."
  exit 1
fi

ELAPSED=0

while true; do
  HEALTHY=0
  FAILED=""

  for c in "${CONTAINERS[@]}"; do
    STATE=$(podman inspect --format "{{.State.Status}}" "$c" 2>/dev/null || true)
    HEALTH=$(podman inspect --format "{{.State.Health.Status}}" "$c" 2>/dev/null || true)

    if [ "$STATE" = "exited" ]; then
      EXIT_CODE=$(podman inspect --format "{{.State.ExitCode}}" "$c" 2>/dev/null || echo "1")
      if [ "$EXIT_CODE" != "0" ]; then
        FAILED="$c"
        break
      fi
      HEALTHY=$((HEALTHY + 1))
    elif [ "$STATE" = "running" ] && { [ -z "$HEALTH" ] || [ "$HEALTH" = "healthy" ]; }; then
      HEALTHY=$((HEALTHY + 1))
    fi
  done

  if [ -n "$FAILED" ]; then
    NAME=$(podman inspect --format "{{.Name}}" "$FAILED" 2>/dev/null || echo "$FAILED")
    systemd-notify --status="Error: container ${NAME} exited with non-zero code"
    exit 1
  fi

  systemd-notify --status="Starting: ${HEALTHY}/${TOTAL} healthy (${ELAPSED}s)"

  if [ "$HEALTHY" -ge "$TOTAL" ]; then
    break
  fi

  ELAPSED=$((ELAPSED + INTERVAL))
  if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
    echo "Timeout after ${TIMEOUT}s waiting for healthy containers."
    exit 1
  fi

  sleep "$INTERVAL"
done

systemd-notify --ready --status="All ${TOTAL} containers healthy"

# Wait for any still-running container to stop, then exit with failure
# so systemd knows the service is no longer healthy.
RUNNING=()
for c in "${CONTAINERS[@]}"; do
  STATE=$(podman inspect --format "{{.State.Status}}" "$c" 2>/dev/null || true)
  [ "$STATE" = "running" ] && RUNNING+=("$c")
done
[ ${#RUNNING[@]} -gt 0 ] && podman wait --condition=stopped "${RUNNING[@]}"
exit 1
