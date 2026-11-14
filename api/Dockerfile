# Single image, two run-modes (api / worker) selected via the compose command.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# uv provides fast, reproducible installs from the committed lockfile. Pinned,
# like everything else here: a floating :latest is the one unpinned input in
# an otherwise digest-pinned build.
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /uvx /bin/

# Debian fixes land here before a rebuilt base image does.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# Layer 1: dependencies only (cached until the lockfile changes).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Layer 2: application source (README is required by the build backend).
COPY README.md ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

# Non-root runtime user.
# The app runs from the venv; the base image's own pip (and the libraries it
# vendors) is unused, and only adds scanner findings.
RUN /usr/local/bin/python -m pip uninstall -y pip \
    && useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
# Default to the API; the worker service overrides this with `platform-worker`.
CMD ["platform-api"]
