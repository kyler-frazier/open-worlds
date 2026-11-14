#!/usr/bin/env bash
# The game server images, which the API pins by digest in each game's adapter
# (api/src/open_worlds/common/games/*/adapter.py), where Dependabot cannot see
# them. These containers take player traffic, so they need watching as much
# as anything in docker-compose.yml.
#
#   scripts/game-images.sh            print each pinned image (for security-scan.sh)
#   scripts/game-images.sh --check    exit 1 if a newer release is out, naming it
#
# "Newer" means the most recently pushed version-numbered tag on Docker Hub
# (2026.10.1, v2.8.1) that is at least MIN_AGE_DAYS (14) old: not :latest,
# which can be a build from minutes ago, and not a release that has not yet
# had time to show its problems.
# Bumping is by hand: read the image's release notes, then try an install
# locally (see deploy/RUNBOOK.md).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

pinned() {
  python3 - "${REPO_ROOT}"/api/src/open_worlds/common/games/*/adapter.py <<'EOF'
import re
import sys

for path in sys.argv[1:]:
    text = open(path, encoding="utf-8").read()
    # image = ("repo@sha256:" "digest"): two adjacent string literals.
    m = re.search(r'image = \(\s*"([^"@]+)@sha256:"\s*"([0-9a-f]{64})"\s*\)', text)
    if m:
        print(f"{m[1]}@sha256:{m[2]}")
EOF
}

if [ "${1:-}" != "--check" ]; then
  pinned
  exit 0
fi

MIN_AGE_DAYS="${MIN_AGE_DAYS:-14}"
rc=0
while IFS= read -r ref; do
  repo="${ref%@*}"
  digest="${ref#*@}"
  # Tags are returned newest push first.
  newest="$(curl -fsS "https://hub.docker.com/v2/repositories/${repo}/tags?page_size=100&ordering=last_updated" |
    python3 -c '
import datetime, json, re, sys
cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=int(sys.argv[1]))
for t in json.load(sys.stdin)["results"]:
    pushed = datetime.datetime.fromisoformat(t["last_updated"].replace("Z", "+00:00"))
    if not (re.fullmatch(r"v?\d+(\.\d+)+", t["name"]) and t.get("digest")):
        continue
    # Reached the pin before any old-enough release: it is the newer one.
    if t["digest"] == sys.argv[2] or pushed <= cutoff:
        print(t["name"], t["digest"], t["last_updated"][:10])
        break
' "${MIN_AGE_DAYS}" "${digest}")"
  if [ -z "${newest}" ]; then
    echo "${repo}: no version tag older than ${MIN_AGE_DAYS} days found" >&2
    rc=1
    continue
  fi
  read -r tag tag_digest pushed <<<"${newest}"
  if [ "${tag_digest}" = "${digest}" ]; then
    echo "${repo}: up to date (${tag})"
  else
    echo "${repo}: ${tag} (pushed ${pushed}) is newer than the pin; ${repo}@${tag_digest}"
    rc=1
  fi
done < <(pinned)
exit "${rc}"
