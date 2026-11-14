#!/usr/bin/env bash
# Host configuration the stack depends on but compose cannot set, kept in host/.
#
#   scripts/host-config.sh                   check only; exits non-zero on drift
#   sudo scripts/host-config.sh --apply      install host/docker/daemon.json
#   scripts/host-config.sh --print-firewall  the DOCKER-USER block for this host
#
# What it covers:
#   * /etc/docker/daemon.json, above all `userns-remap`, a daemon setting no
#     compose key can turn on; without it world containers run as real host
#     uids and nothing looks wrong.
#   * The dockremap range in /etc/subuid and /etc/subgid, which must not overlap
#     another entry (Docker can pick the login user's range, handing that user
#     the uids that own every world's files).
#   * The data root (PLATFORM_DATA_ROOT), owned by the remapped PUID:PGID;
#     --apply creates it. Docker would create a missing bind-mount source owned
#     by container root, which no service writes as.
#   * The DOCKER-USER block in /etc/ufw/after.rules. Published ports and
#     container egress bypass ufw, so this block is what keeps the edge port to
#     the outer proxy and the games network off the LAN. The file in host/ is a
#     template; HOST_IFACE, EDGE_PORT, EDGE_ALLOW_FROM and PLATFORM_GAMES_SUBNET
#     from .env fill it in, so no site's addresses live in the repo. Checked, not
#     applied: editing a live firewall belongs to a person.
#
# --apply on a host that did not have remapping on is a migration, not a
# restart: Docker switches to a new storage root and the data has to move. See
# the RUNBOOK's "Container UID remapping" section before running it.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

DAEMON_SRC=host/docker/daemon.json
DAEMON_DST=/etc/docker/daemon.json
UFW_SRC=host/ufw/after.rules.docker-user
UFW_DST=/etc/ufw/after.rules

# The firewall template's values, from .env. Read by name rather than sourced,
# so nothing else in .env (secrets included) reaches this shell.
env_value() {
  grep -E "^$1=" .env 2>/dev/null | tail -n 1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'
}

render_firewall() {
  local iface port allow games
  iface="$(env_value HOST_IFACE)"
  games="$(env_value PLATFORM_GAMES_SUBNET)"
  port="$(env_value EDGE_PORT)"
  allow="$(env_value EDGE_ALLOW_FROM)"
  if [ -z "$allow" ]; then
    echo "EDGE_ALLOW_FROM is not set in .env (the outer proxy's address)." >&2
    return 1
  fi
  HOST_IFACE="${iface:-eth0}" EDGE_PORT="${port:-18080}" EDGE_ALLOW_FROM="$allow" \
    GAMES_SUBNET="${games:-172.19.0.0/16}" \
    envsubst '${HOST_IFACE} ${EDGE_PORT} ${EDGE_ALLOW_FROM} ${GAMES_SUBNET}' <"$UFW_SRC"
}

remap_base() {
  local b
  b="$(grep -E '^dockremap:' /etc/subuid 2>/dev/null | cut -d: -f2)"
  [ -n "$b" ] && echo "$b"
}
data_root="$(env_value PLATFORM_DATA_ROOT)"
data_root="${data_root:-/srv/games}"
puid="$(env_value PUID)"
puid="${puid:-1000}"
pgid="$(env_value PGID)"
pgid="${pgid:-1000}"

if [ "${1:-}" = "--print-firewall" ]; then
  render_firewall
  exit
fi

drift=0
note() { printf '  %s\n' "$1"; }
bad() {
  printf '  DRIFT: %s\n' "$1" >&2
  drift=1
}

same_json() {
  python3 -I -c 'import json, sys; sys.exit(json.load(open(sys.argv[1])) != json.load(open(sys.argv[2])))' "$1" "$2" 2>/dev/null
}

if [ "${1:-}" = "--apply" ]; then
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: --apply needs root (sudo $0 --apply)." >&2
    exit 1
  fi
  if [ -f "$DAEMON_DST" ] && same_json "$DAEMON_SRC" "$DAEMON_DST"; then
    echo "$DAEMON_DST already matches $DAEMON_SRC."
  else
    [ -f "$DAEMON_DST" ] && cp "$DAEMON_DST" "$DAEMON_DST.bak-$(date +%Y%m%d-%H%M%S)"
    install -m 644 "$DAEMON_SRC" "$DAEMON_DST"
    systemctl restart docker
    echo "Installed $DAEMON_DST and restarted Docker."
  fi
  if ! base="$(remap_base)"; then
    echo "No dockremap entry in /etc/subuid yet; is userns-remap on? Not creating the data root." >&2
  else
    owner="$((base + puid)):$((base + pgid))"
    for d in worlds backup-staging platform-config content-cache; do
      if [ ! -d "$data_root/$d" ]; then
        install -d -m 755 -o "$((base + puid))" -g "$((base + pgid))" "$data_root/$d"
        echo "Created $data_root/$d ($owner)."
      fi
    done
  fi
  echo
fi

echo "Docker daemon:"
if [ ! -r "$DAEMON_DST" ]; then
  bad "$DAEMON_DST is missing; sudo $0 --apply"
elif ! same_json "$DAEMON_SRC" "$DAEMON_DST"; then
  bad "$DAEMON_DST differs from $DAEMON_SRC; sudo $0 --apply"
else
  note "$DAEMON_DST matches $DAEMON_SRC"
fi
if docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q 'name=userns'; then
  note "userns-remap is active ($(docker info --format '{{.DockerRootDir}}' 2>/dev/null))"
else
  bad "the running daemon has no userns-remap; it was not restarted, or the config is not applied"
fi

echo "Subordinate ids:"
for f in /etc/subuid /etc/subgid; do
  if ! out="$(
    python3 -I - "$f" 2>&1 <<'PY'
import sys
rows = []
for line in open(sys.argv[1]):
    line = line.strip()
    if line and not line.startswith("#"):
        name, start, count = line.split(":")
        rows.append((name, int(start), int(count)))
remap = [r for r in rows if r[0] == "dockremap"]
if not remap:
    sys.exit("no dockremap entry")
_, s, c = remap[0]
for name, s2, c2 in rows:
    if name != "dockremap" and s < s2 + c2 and s2 < s + c:
        sys.exit(f"dockremap {s}:{c} overlaps {name} {s2}:{c2}")
print(f"dockremap {s}:{c}, no overlap")
PY
  )"; then
    bad "$f: $out"
  else
    note "$f: $out"
  fi
done

echo "Data root:"
if ! base="$(remap_base)"; then
  bad "no dockremap entry in /etc/subuid"
elif [ ! -d "$data_root/worlds" ]; then
  bad "$data_root/worlds does not exist; sudo $0 --apply creates it"
elif [ "$(stat -c %u "$data_root/worlds")" != "$((base + puid))" ]; then
  bad "$data_root/worlds is owned by uid $(stat -c %u "$data_root/worlds"), not $((base + puid)) (remapped PUID)"
else
  note "$data_root owned by the remapped PUID ($((base + puid)))"
fi

echo "Firewall:"
# Needed even with the edge on loopback: it also keeps the games network off
# the LAN.
if ! rendered="$(render_firewall 2>&1)"; then
  bad "$rendered"
elif [ ! -r "$UFW_DST" ]; then
  note "$UFW_DST not readable without root; skipped (run with sudo to check it)"
elif ! diff -q <(sed -n '/# BEGIN DOCKER-USER (open-worlds)/,/# END DOCKER-USER (open-worlds)/p' "$UFW_DST") <(printf '%s\n' "$rendered") >/dev/null; then
  bad "the DOCKER-USER block in $UFW_DST differs from $UFW_SRC as rendered; install \`$0 --print-firewall\` and \`sudo ufw reload\`"
else
  note "DOCKER-USER block matches $UFW_SRC"
fi

echo
if [ "$drift" -ne 0 ]; then
  echo "Host configuration does not match host/." >&2
  exit 1
fi
echo "Host configuration matches host/."
