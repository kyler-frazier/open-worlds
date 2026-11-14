# pal-waker: stdlib-only Python, no dependencies to audit or keep current.
FROM python:3.15-alpine@sha256:a0f2e268f2ce8f3709611a90054ec6a8ae31f6251f5896b8648b9bb99835e30a
# Stdlib only: the base image's pip, and what it vendors, is never used.
RUN python -m pip uninstall -y pip \
  && adduser -D -u 10001 waker
WORKDIR /app
COPY waker.py /app/waker.py
USER waker

# The port range is published by the compose service; the process binds ports
# from it dynamically as worlds appear and disappear.
ENTRYPOINT ["python", "-u", "/app/waker.py"]
