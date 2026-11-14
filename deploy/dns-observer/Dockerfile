# dns-observer: stdlib-only Python, no dependencies to audit or keep current.
#
# Sits on the games network as a resolver for world containers. Unlike the
# egress collector this needs no elevated anything: no host namespace, no root,
# no capabilities. It answers UDP on an unprivileged port and compose maps it.
FROM python:3.15-alpine@sha256:a0f2e268f2ce8f3709611a90054ec6a8ae31f6251f5896b8648b9bb99835e30a
# Stdlib only: the base image's pip, and what it vendors, is never used.
RUN python -m pip uninstall -y pip

WORKDIR /app
COPY resolver.py /app/resolver.py

ENTRYPOINT ["python", "-u", "/app/resolver.py"]
