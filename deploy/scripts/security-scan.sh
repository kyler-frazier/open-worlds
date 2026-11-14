#!/usr/bin/env bash
# Dependency + image vulnerability scan for the platform.
#
# Runs on demand (or from cron); CI does not run it. It scans every
# image in docker-compose.yml with Trivy and the API's Python dependencies with
# pip-audit. Build the local images first (`make build`) or their scan fails.
# Exits non-zero if any HIGH/CRITICAL, fixable issue is found so it can gate a
# scheduled job. Requires Docker and uv on the host.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$(cd "${REPO_ROOT}/../api" && pwd)"

# Scanner is intentionally :latest so it uses the freshest vulnerability DB.
TRIVY_IMAGE="${TRIVY_IMAGE:-aquasec/trivy:latest}"
# Every image the stack runs, across all profiles: the ones we build and the
# third-party ones pinned by digest. Read from compose rather than listed here,
# so a new service is scanned without anyone remembering to add it. Pinned
# images never pick up fixes on their own, so they need watching the most.
mapfile -t IMAGES < <(
  cd "${REPO_ROOT}"
  {
    docker compose --profile '*' config --images
    # The game servers, which the API pins rather than compose.
    scripts/game-images.sh
  } | sort -u
)

rc=0

echo "== Trivy image scan (HIGH,CRITICAL; fixable only) =="
for img in "${IMAGES[@]}"; do
  echo "--- ${img} ---"
  # Host userns: a remapped root cannot open the host's Docker socket, and
  # Trivy then falls back to pulling each image from Docker Hub, which fails
  # for the ones built here. Holding the socket is host root either way.
  docker run --rm --userns host \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v trivy-cache:/root/.cache/ \
    "${TRIVY_IMAGE}" image \
    --scanners vuln \
    --severity HIGH,CRITICAL \
    --ignore-unfixed \
    --exit-code 1 \
    "${img}" || rc=1
done

echo "== pip-audit (API Python dependencies) =="
# Audit the locked dependency set without installing anything.
# --no-emit-project drops the first-party `open-worlds` package (a local path,
# which pip-audit can't install/resolve) so only third-party deps are audited.
# The export is complete, pinned and hashed, so --disable-pip skips pip-audit's
# own resolve -- which builds a venv and fails on a host Python without ensurepip.
reqs="$(mktemp)"
trap 'rm -f "${reqs}"' EXIT
(cd "${API_DIR}" && uv export --frozen --no-emit-project --format requirements-txt 2>/dev/null) >"${reqs}"
uvx pip-audit --disable-pip --requirement "${reqs}" || rc=1

if [ "${rc}" -ne 0 ]; then
  echo "!! Vulnerabilities found (see above)." >&2
else
  echo "OK: no fixable HIGH/CRITICAL issues found."
fi
exit "${rc}"
