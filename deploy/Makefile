COMPOSE = docker compose

.PHONY: help preflight verify host-config up down shutdown logs build migrate ps net scan

help:
	@echo "Targets:"
	@echo "  net          create the external 'games' network (one-time)"
	@echo "  build        build the api/worker, web and egress-guard images"
	@echo "  up           start the core stack"
	@echo "  down         stop the core stack (leaves worlds running)"
	@echo "  shutdown     save and stop every world, then the stack"
	@echo "  verify       check the running stack still matches this repo"
	@echo "  host-config  check the host's Docker daemon and firewall against host/"
	@echo "  migrate      run database migrations (alembic upgrade head)"
	@echo "  logs         tail logs"
	@echo "  ps           list services"
	@echo "  scan         Trivy image + pip-audit dependency vulnerability scan"

# Refuse to start if the secrets file is missing, world/group-readable, contains
# a PLATFORM_* setting compose would not pass through, or hands a service an
# environment its process cannot actually boot with (see scripts/preflight.sh).
preflight:
	@./scripts/preflight.sh

# `docker compose up -d` is not a full reconcile: it will not re-address an
# existing network, and it sees no reason to restart a service whose mounted
# config file changed. Both have bitten us silently. Run this after a deploy.
verify:
	@./scripts/verify-deployed.sh

# Check only. Applying is `sudo scripts/host-config.sh --apply`, deliberately
# not a target: on a host without remapping it moves Docker's storage root.
host-config:
	@./scripts/host-config.sh

# Pinned rather than left to Docker: the egress and DNS observers assume this
# subnet (PLATFORM_GAMES_SUBNET, PLATFORM_DNS_OBSERVER_IP), and a fresh engine
# hands out whatever /16 is free next. Dynamic addresses stay in the lower half
# so they can never take the observer's static 172.19.255.53.
GAMES_SUBNET ?= 172.19.0.0/16
GAMES_IP_RANGE ?= 172.19.0.0/17

net:
	docker network inspect games >/dev/null 2>&1 || docker network create --subnet $(GAMES_SUBNET) --ip-range $(GAMES_IP_RANGE) games

# The egress guard sits behind a profile because it is not a service, so a
# plain `compose build` skips it. Without it no guarded world can start.
build:
	$(COMPOSE) build --pull
	$(COMPOSE) --profile guard build --pull egress-guard

# net first: preflight renders every service's config, and the ones on the
# external games network cannot render until it exists (a fresh host).
up: net preflight
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

# `down` stops the compose services only: world containers are created by the
# orchestrator, not compose, so they keep running. Use this before a deliberate
# reboot -- it gives each world the save window its game declares, which a plain
# `docker stop` (10s) does not.
shutdown:
	@./scripts/graceful-shutdown.sh --all

migrate:
	$(COMPOSE) run --rm migrate

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

scan:
	./scripts/security-scan.sh
