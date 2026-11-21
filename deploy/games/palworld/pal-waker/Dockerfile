# pal-waker: stdlib-only Python, no dependencies to audit or keep current.
FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
# Stdlib only: the base image's pip, and what it vendors, is never used.
RUN python -m pip uninstall -y pip \
  && adduser -D -u 10001 waker
WORKDIR /app
COPY waker.py /app/waker.py
USER waker

# The port range is published by the compose service; the process binds ports
# from it dynamically as worlds appear and disappear.
ENTRYPOINT ["python", "-u", "/app/waker.py"]
