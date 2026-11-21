# egress-exporter: stdlib-only Python, no dependencies to audit or keep current.
# Serves the guards' refusals from the dns-observer's report as /metrics.
# Needs the egress policy (egress-guard/trust.py and each game's egress.py) to
# tell refusals we meant from new ones. Built from deploy/;
# Dockerfile.dockerignore keeps the context to those files.
FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
# Stdlib only: the base image's pip, and what it vendors, is never used.
RUN python -m pip uninstall -y pip

WORKDIR /app
COPY egress-exporter/exporter.py egress-guard/trust.py /app/
COPY games/ /app/games/

# Compose runs it as an unprivileged user.
ENTRYPOINT ["python", "-u", "/app/exporter.py"]
