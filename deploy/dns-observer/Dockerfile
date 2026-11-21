# dns-observer: stdlib-only Python, no dependencies to audit or keep current.
#
# Sits on the games network as a resolver for world containers. Unlike the
# egress collector this needs no elevated anything: no host namespace, no root,
# no capabilities. It answers UDP on an unprivileged port and compose maps it.
FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
# Stdlib only: the base image's pip, and what it vendors, is never used.
RUN python -m pip uninstall -y pip

WORKDIR /app
COPY resolver.py /app/resolver.py

ENTRYPOINT ["python", "-u", "/app/resolver.py"]
