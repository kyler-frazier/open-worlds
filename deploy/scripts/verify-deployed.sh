#!/usr/bin/env bash
# Compare what Docker is actually running against what this repo declares.
#
# `docker compose up -d` is not a full reconcile, and the gaps are quiet:
#
#   * A network keeps the IPAM it was created with, so a subnet declared later
#     is never applied.
#   * A bind-mounted config file can change without the service definition
#     changing, so compose sees nothing to do and the process keeps the old one.
#   * A container keeps the service definition it was created from.
#
# Exits non-zero if anything has drifted, so it can gate a deploy.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# From compose, not the directory name (`deploy`), which is not the project
# name; guessing wrong would make every check below pass on nothing.
PROJECT="$(docker compose config --format json 2>/dev/null | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])' 2>/dev/null)"
if [ -z "$PROJECT" ]; then
  echo "Could not read the compose project name; is docker-compose.yml valid?" >&2
  exit 1
fi
REPO="$PWD"
drift=0

note() { printf '  %s\n' "$1"; }
bad() {
  printf '  DRIFT: %s\n' "$1" >&2
  drift=1
}

# --- 1. Service definitions -------------------------------------------------
# Compose stamps each container with a hash of its service definition;
# a mismatch is a container still running an older compose file.
echo "Service definitions:"
# Enumerated from what is running, not `compose config --services`, which
# hides profiled services (tls).
running_services() {
  docker ps --filter "label=com.docker.compose.project=${PROJECT}" \
    --format '{{.Label "com.docker.compose.service"}}' 2>/dev/null | sort -u
}
services="$(running_services)"
for svc in $services; do
  cid="$(docker ps -q --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.service=${svc}" 2>/dev/null | head -1)"
  [ -z "$cid" ] && continue
  running="$(docker inspect "$cid" --format '{{index .Config.Labels "com.docker.compose.config-hash"}}' 2>/dev/null)"
  declared="$(docker compose config --hash "$svc" 2>/dev/null | awk '{print $2}')"
  if [ -n "$declared" ] && [ -n "$running" ] && [ "$declared" != "$running" ]; then
    bad "$svc is running an older definition; \`docker compose up -d $svc\` to apply"
  fi
done
[ "$drift" -eq 0 ] && note "all running services match the compose file"

# A core service that is simply gone passes the loop above. `compose config
# --services` without a profile is the core stack, which must all run.
# One-shot services (restart: "no") run and exit on every `up`; they must
# have exited 0 instead.
oneshots="db-init migrate"
for svc in $(docker compose config --services 2>/dev/null); do
  # Whole names: `grep -w db` would match db-init, since - ends a word.
  if grep -qxF "$svc" <<<"${oneshots// /$'\n'}"; then
    code="$(docker ps -a --filter "label=com.docker.compose.project=${PROJECT}" \
      --filter "label=com.docker.compose.service=${svc}" --format '{{.Status}}' 2>/dev/null | head -1)"
    case "$code" in
      "Exited (0)"*) ;;
      *) bad "$svc did not finish cleanly (${code:-never ran}); \`docker compose logs $svc\`" ;;
    esac
    continue
  fi
  if ! grep -qx "$svc" <<<"$services"; then
    bad "$svc is not running; \`docker compose up -d $svc\` to start it"
  fi
done

# --- 2. Network IPAM --------------------------------------------------------
# Docker will not re-address an existing network, so a declared subnet added
# after the network was created is silently ignored.
echo "Networks:"
net_drift=0
while IFS=$'\t' read -r name subnet range; do
  [ -z "$name" ] && continue
  actual_subnet="$(docker network inspect "${PROJECT}_${name}" --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null)"
  actual_range="$(docker network inspect "${PROJECT}_${name}" --format '{{range .IPAM.Config}}{{.IPRange}}{{end}}' 2>/dev/null)"
  [ -z "$actual_subnet" ] && continue
  if [ "$subnet" != "$actual_subnet" ] || { [ -n "$range" ] && [ "$range" != "$actual_range" ]; }; then
    bad "network $name declares ${subnet}${range:+ (range $range)} but holds ${actual_subnet}${actual_range:+ (range $actual_range)}"
    bad "  recreating it means stopping everything attached: docker compose down && docker compose up -d"
    net_drift=1
  fi
done < <(
  docker compose config --format json 2>/dev/null | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for name, net in (doc.get("networks") or {}).items():
    for cfg in ((net.get("ipam") or {}).get("config") or []):
        print("\t".join([name, cfg.get("subnet", ""), cfg.get("ip_range", "")]))
'
)
[ "$net_drift" -eq 0 ] && note "declared subnets match what Docker holds"

# --- 3. Bind-mounted config ------------------------------------------------
# A bind mount is live, so contents always match; what goes stale is the
# process, which read the file at startup. So: was it edited after the
# container started?
echo "Mounted config files:"
file_drift=0
for svc in $services; do
  cid="$(docker ps -q --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.service=${svc}" 2>/dev/null | head -1)"
  [ -z "$cid" ] && continue
  started="$(docker inspect "$cid" --format '{{.State.StartedAt}}' 2>/dev/null)"
  started_epoch="$(date -d "$started" +%s 2>/dev/null)"
  [ -z "$started_epoch" ] && continue
  while IFS=$'\t' read -r src dst; do
    [ -z "$src" ] && continue
    case "$src" in "$REPO"/*) ;; *) continue ;; esac # only this repo's config
    [ -f "$src" ] || continue
    mtime="$(stat -c %Y "$src" 2>/dev/null)"
    [ -z "$mtime" ] && continue
    if [ "$mtime" -gt "$started_epoch" ]; then
      bad "$svc started before ${src#"$REPO"/} was last edited; it is running the older copy"
      bad "  docker compose up -d --force-recreate $svc"
      file_drift=1
    fi
  done < <(docker inspect "$cid" --format '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}{{"\t"}}{{.Destination}}{{"\n"}}{{end}}{{end}}' 2>/dev/null)
done
[ "$file_drift" -eq 0 ] && note "mounted config matches the files on disk"

# --- 4. Host configuration --------------------------------------------------
# What compose cannot express: the daemon's userns-remap, the remap id range,
# and the firewall block for the edge port. See scripts/host-config.sh.
echo
scripts/host-config.sh || drift=1

echo
if [ "$drift" -ne 0 ]; then
  echo "Deployed state does not match this repo." >&2
  exit 1
fi
echo "Deployed state matches this repo."
