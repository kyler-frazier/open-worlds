# Tiny stdlib-only Minecraft SLP responder used as mc-router's DEFAULT backend
# so OFF worlds show a friendly "server is off" status instead of a connect
# error. No third-party deps; python:slim is multi-arch (works on arm64).
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
# Stdlib only: the base image's pip, and what it vendors, is never used.
RUN python -m pip uninstall -y pip
# Debian fixes land here before a rebuilt base image does.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py .

USER nobody
EXPOSE 25565

CMD ["python", "-u", "server.py"]
