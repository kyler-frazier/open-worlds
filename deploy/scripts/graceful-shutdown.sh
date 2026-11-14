#!/usr/bin/env bash
# Stop every running world with the save window its game needs, then (optionally)
# the stack. Run this before a deliberate reboot or host maintenance.
#
# World containers are not compose's, so `compose down` leaves them, and a plain
# `docker stop` gives Docker's 10s default, which SIGKILLs Palworld mid-save.
# This waits each container's game's declared window instead. Database state is
# left for the maintenance sweep to reconcile on the next boot.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

LABEL_NS="open-worlds"
# Must match GameAdapter.stop_timeout_seconds for each game.
timeout_for() {
  case "$1" in
    PALWORLD) echo 300 ;;
    MINECRAFT) echo 60 ;;
    *) echo 60 ;;
  esac
}

mapfile -t running < <(
  docker ps --filter "label=${LABEL_NS}=true" --format '{{.Names}}' 2>/dev/null || true
)

if [ ${#running[@]} -eq 0 ]; then
  echo "No worlds running."
else
  for name in "${running[@]}"; do
    game="$(docker inspect "$name" --format "{{index .Config.Labels \"${LABEL_NS}.game\"}}" 2>/dev/null || echo "")"
    t="$(timeout_for "$game")"
    echo "Stopping $name (${game:-unknown}); allowing ${t}s to save…"
    # SIGTERM, then up to t seconds before Docker escalates. The image saves on
    # SIGTERM, so a clean exit well inside the window is the expected result.
    docker stop --time "$t" "$name" >/dev/null
    echo "  stopped."
  done
fi

if [ "${1:-}" = "--all" ]; then
  echo "Stopping the platform stack…"
  docker compose down
fi
