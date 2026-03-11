#!/usr/bin/env bash

set -euo pipefail

TIMEOUT="${TIMEOUT:-300}"
INTERVAL="${INTERVAL:-5}"

# --- Phase 1: Startup health wait ---

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

  for c in "${CONTAINERS[@]}"; do
    STATE=$(podman inspect --format "{{.State.Status}}" "$c" 2>/dev/null || true)
    HEALTH=$(podman inspect --format "{{.State.Health.Status}}" "$c" 2>/dev/null || true)

    if [ "$STATE" = "running" ] && { [ -z "$HEALTH" ] || [ "$HEALTH" = "healthy" ]; }; then
      HEALTHY=$((HEALTHY + 1))
    fi
  done

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

# --- Phase 2: Watchdog loop ---

# WATCHDOG_USEC is set automatically by systemd from WatchdogSec= (in microseconds).
# We convert to seconds and halve it so we actually check in well before the deadline.
# We also hardcode on purpose the minimal healcheck interval to 5 seconds.
WATCHDOG_USEC="${WATCHDOG_USEC:-60000000}"
WATCHDOG_SEC=$((WATCHDOG_USEC / 1000000 / 2))
[ "$WATCHDOG_SEC" -lt 5 ] && WATCHDOG_SEC=5

while true; do
  sleep "$WATCHDOG_SEC"

  for c in "${CONTAINERS[@]}"; do
    STATE=$(podman inspect --format "{{.State.Status}}" "$c" 2>/dev/null || true)
    if [ "$STATE" != "running" ]; then
      echo "Container $c is no longer running (state: ${STATE:-gone})"
      exit 1
    fi

    HEALTH=$(podman inspect --format "{{.State.Health.Status}}" "$c" 2>/dev/null || true)
    if [ "$HEALTH" = "unhealthy" ]; then
      echo "Container $c became unhealthy"
      exit 1
    fi
  done

  systemd-notify WATCHDOG=1 --status="Watchdog: ${TOTAL} containers healthy"
done
