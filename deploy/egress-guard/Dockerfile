# egress-guard: one per enforced world. Stdlib-only Python plus nftables.
#
# Built from deploy/ so it can take trust.py and each game's policy
# (games/<game>/egress.py) and the DNS parser from the dns observer: one policy
# and one parser, not copies that drift. Dockerfile.dockerignore keeps the rest
# of games/ out. Build with:  docker compose --profile guard build egress-guard
#
# Runs as root holding exactly two capabilities -- NET_ADMIN to program the
# world's firewall and NET_BIND_SERVICE for :53 and the low source ports that
# mark its upstream queries as its own. The orchestrator grants nothing else.
FROM python:3.15-alpine@sha256:a0f2e268f2ce8f3709611a90054ec6a8ae31f6251f5896b8648b9bb99835e30a
# Stdlib only: the base image's pip, and what it vendors, is never used.
RUN python -m pip uninstall -y pip \
  && apk add --no-cache nftables

WORKDIR /app
COPY egress-guard/trust.py dns-observer/resolver.py egress-guard/guard.py egress-guard/webgate.py /app/
COPY games/ /app/games/

# Polled every second while starting, because a world's start waits on it.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --start-interval=1s --retries=3 \
  CMD ["test", "-f", "/run/guard-ready"]

ENTRYPOINT ["python", "-u", "/app/guard.py"]
