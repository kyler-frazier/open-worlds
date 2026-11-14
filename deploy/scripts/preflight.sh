#!/usr/bin/env bash
# Pre-start checks for the secrets file. Called by `make preflight`; runnable on
# its own. Exits non-zero with a plain message on the first problem.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -f .env ]; then
  echo "ERROR: .env is missing (copy .env.example to .env)." >&2
  exit 1
fi

# .env holds every secret, so it must be owner-only.
perms="$(stat -c '%a' .env)"
case "$perms" in
  600 | 400) ;;
  *)
    echo "ERROR: .env has mode $perms; expected 600. Run: chmod 600 .env" >&2
    exit 1
    ;;
esac

# .env.example's CHANGE_ME placeholders let a copy interpolate; refuse to run
# with one. `|| true` because grep exits non-zero on no match, which `set -e`
# would treat as failure.
placeholders="$(grep -E '^[A-Z_]+=CHANGE_ME' .env | cut -d= -f1 | tr '\n' ' ' || true)"
if [ -n "$placeholders" ]; then
  echo "ERROR: .env still has placeholder values for:$placeholders" >&2
  echo "       These are public -- they are in .env.example. Replace them before starting." >&2
  echo "       Generation commands are in the comments above each one." >&2
  exit 1
fi

# The local-only Google bypass (api/src/open_worlds/api/dev_login.py). The
# pass-through check below would catch it too, but this says why.
if grep -qE '^PLATFORM_DEV_LOGIN=' .env; then
  echo "ERROR: .env sets PLATFORM_DEV_LOGIN. That is a local-dev-only login bypass;" >&2
  echo "       it must never be set on a deployment. Remove the line." >&2
  exit 1
fi

# A PLATFORM_* line in .env takes effect only if docker-compose.yml (or a
# game's compose.yml) passes it through; refuse one that would be ignored.
missing=""
while IFS= read -r key; do
  if ! grep -qF -- "\${${key}" docker-compose.yml games/*/compose.yml; then
    missing="$missing $key"
  fi
done < <(grep -oE '^PLATFORM_[A-Z0-9_]+' .env | sort -u)

if [ -n "$missing" ]; then
  echo "ERROR: set in .env but not passed to any service by docker-compose.yml:$missing" >&2
  echo "       Add it to x-platform-config (or the service that needs it), or remove it." >&2
  exit 1
fi

# Active profiles, as compose sees them: the environment wins over .env.
profiles="${COMPOSE_PROFILES:-$(grep -E '^COMPOSE_PROFILES=' .env | tail -n 1 | cut -d= -f2- || true)}"
profiles=",${profiles// /},"

# Games are offered from Admin > Settings > Games, and the api refuses to start
# while the old variable is set.
if grep -qE '^PLATFORM_ENABLED_GAMES=' .env; then
  echo "ERROR: .env sets PLATFORM_ENABLED_GAMES, which is retired. Remove the line;" >&2
  echo "       choose the offered games in the web UI under Admin > Settings > Games." >&2
  exit 1
fi

# The tls profile needs both, or Caddy fails ACME and restarts forever, which
# looks like a networking problem. Checked only when the profile is on.
case " ${profiles//,/ } $* " in
  *tls*)
    for var in PUBLIC_DOMAIN ACME_EMAIL; do
      if ! grep -qE "^${var}=.+" .env; then
        echo "ERROR: the tls profile needs $var set in .env." >&2
        echo "       See the TLS section of README.md." >&2
        exit 1
      fi
    done
    ;;
esac

# Grafana starts fine against the placeholder webhook, and every alert then
# posts into nothing. `|| true`: with pipefail a missing line would abort
# silently.
hook="$(grep -E '^ALERT_DISCORD_WEBHOOK_URL=' .env | cut -d= -f2- || true)"
case "$hook" in
  *set-ALERT_DISCORD_WEBHOOK_URL* | *"<id>/<token>"*)
    echo "ERROR: ALERT_DISCORD_WEBHOOK_URL is still the placeholder." >&2
    echo "       Alerts will fire into nothing. See the RUNBOOK's Alerting section." >&2
    exit 1
    ;;
  https://discord.com/api/webhooks/* | https://discordapp.com/api/webhooks/*) ;;
  *)
    echo "ERROR: ALERT_DISCORD_WEBHOOK_URL is not set to a Discord webhook URL." >&2
    echo "       Expected https://discord.com/api/webhooks/<id>/<token> in .env." >&2
    echo "       See the RUNBOOK's Alerting section for how to create one." >&2
    exit 1
    ;;
esac

# World containers run mod code nobody here wrote. userns-remap makes their
# root an unprivileged host uid; it is a daemon setting no compose key can turn
# on, and a host without it looks perfectly healthy.
if ! docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q 'name=userns'; then
  echo "ERROR: the Docker daemon is not running with userns-remap." >&2
  echo "       Run scripts/host-config.sh, and see the RUNBOOK's \"Container UID" >&2
  echo "       remapping\" section before applying it: it moves Docker's storage root." >&2
  exit 1
fi

# With remapping, container uid N is base+N on the host. Restored or copied data
# keeps its old owner and every world fails on its first write.
base="$(grep '^dockremap:' /etc/subuid | cut -d: -f2)"
data_root="$(grep -E '^PLATFORM_DATA_ROOT=' .env | cut -d= -f2- || true)"
data_root="${data_root:-/srv/games}"
puid="$(grep -E '^PUID=' .env | cut -d= -f2- || true)"
puid="${puid:-1000}"
pgid="$(grep -E '^PGID=' .env | cut -d= -f2- || true)"
pgid="${pgid:-1000}"
# Missing is refused too: Docker would create the bind-mount source owned by
# container root, which no service writes as.
if [ ! -d "$data_root/worlds" ]; then
  echo "ERROR: $data_root/worlds does not exist." >&2
  echo "       sudo scripts/host-config.sh --apply creates the data root with the right owner." >&2
  exit 1
fi
if [ -n "$base" ]; then
  owner="$(stat -c %u "$data_root/worlds")"
  if [ "$owner" = "$base" ]; then
    # Container root: Docker created it for a bind mount; nothing was shifted.
    echo "ERROR: $data_root/worlds is owned by container root ($owner), so Docker created it." >&2
    echo "       If it is empty: sudo chown $((base + puid)):$((base + pgid)) $data_root/*" >&2
    echo "       On a new host, sudo scripts/host-config.sh --apply creates the data root." >&2
    exit 1
  elif [ "$owner" != "$((base + puid))" ]; then
    echo "ERROR: $data_root/worlds is owned by uid $owner; containers write as $((base + puid))." >&2
    echo "       sudo scripts/shift-owner.py --to-remapped $data_root" >&2
    exit 1
  fi
fi

# The check above covers .env -> compose; this covers compose -> container,
# feeding each service's actual variables through the app's own startup path
# (an optional arriving as "" is invisible to a .env-only check). Set
# PREFLIGHT_SKIP_CONFIG_CHECK=1 to skip; it needs the image and takes seconds.
if [ "${PREFLIGHT_SKIP_CONFIG_CHECK:-}" = "1" ]; then
  exit 0
fi

check_service_config() {
  service="$1"
  snippet="$2"
  if ! output="$(docker compose run --rm --no-deps "$service" python -c "$snippet" 2>&1)"; then
    echo "ERROR: $service cannot start with the environment docker-compose.yml gives it." >&2
    echo "$output" | tail -n 5 >&2
    echo "       Check that service's environment block, not just .env." >&2
    exit 1
  fi
}

# api and internal-api build their ASGI app; the worker validates its secrets
# inside run_forever, so ask it the same question the way it asks itself.
check_service_config api \
  'from open_worlds.api.main import app'
check_service_config internal-api \
  'from open_worlds.api.main import internal_app'
check_service_config world-manager \
  'from open_worlds.world_manager.main import app'
check_service_config worker \
  'from open_worlds.common.config import get_settings
from open_worlds.worker.main import WORKER_SECRETS
get_settings().require(*WORKER_SECRETS)'
