#!/usr/bin/env bash
# What egress guards refused (or, with blocking off, would have), with a
# ready-to-paste policy entry for each one worth a look.
#
#   scripts/egress-refusals.sh            # refusals no DENY entry explains
#   scripts/egress-refusals.sh --all      # including DENY hits
#
# Reads the dns-observer's report, where every guard sends its refusals. The
# same records are in each guard's log: docker logs game-world-<sub>--egress.
# Adding an entry: paste the suggestion into the list for its STAGE (INSTALL
# or PLAY) in games/<game>/egress.py,
# replace 'TODO' with why the game needs it (the guard will not build with a
# TODO), rebuild the guard, and sleep/wake the world.
set -euo pipefail

cd "$(dirname "$0")/.."
DATA_ROOT="${PLATFORM_DATA_ROOT:-$(grep -E '^PLATFORM_DATA_ROOT=' .env 2>/dev/null | cut -d= -f2- || true)}"
REPORT="${1:-}"
ALL=0
if [[ "$REPORT" == "--all" ]]; then
  ALL=1
  REPORT="${2:-}"
fi
REPORT="${REPORT:-${DATA_ROOT:-/srv/games}/observations/dns-observations.json}"

if [[ ! -r "$REPORT" ]]; then
  echo "no report at $REPORT (pass its path as an argument)" >&2
  exit 1
fi

ALL="$ALL" PYTHONPATH=egress-guard python3 - "$REPORT" <<'EOF'
import json, os, sys, time
import trust

rows = json.load(open(sys.argv[1])).get("refused", [])
show_all = os.environ.get("ALL") == "1"


rows = [r for r in rows if show_all or not trust.explained(r)]
if not rows:
    print("nothing refused that no DENY entry explains")
    sys.exit(0)

rows.sort(key=lambda r: -r.get("last_seen", 0))
fmt = "{:<9} {:<8} {:<7} {:<8} {:<17} {:<6} {:<38} {:>6} {:>5}  {}"
print(fmt.format("GAME", "VARIANT", "STAGE", "LAYER", "REASON", "PROTO", "NAME / ADDRESS", "PORT", "COUNT", "WORLDS"))
for r in rows:
    target = r.get("name") or r.get("dst") or "?"
    print(fmt.format(
        (r.get("game") or "?")[:9], (r.get("variant") or "-")[:8], (r.get("stage") or "-")[:7],
        (r.get("layer") or "?")[:8],
        (r.get("reason") or "?")[:17], (r.get("proto") or "-")[:6], target[:38],
        r.get("port") or "-", r.get("count", 0),
        ",".join(r.get("worlds") or r.get("clients") or []),
    ))

print()
for r in rows:
    target = r.get("name") or r.get("dst")
    seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("last_seen", 0)))
    stage = (r.get("stage") or "").upper() or "the right stage's list"
    print(f"# {target}:{r.get('port') or '-'} -- {r.get('reason')} ({r.get('layer')}), last {seen}; for {stage}")
    if r.get("names_for_dst"):
        print(f"#   the world looked this address up as: {', '.join(r['names_for_dst'])}")
    if r.get("alpn"):
        print(f"#   TLS offered: {', '.join(r['alpn'])}")
    if r.get("first_bytes"):
        print(f"#   first bytes: {r['first_bytes']}")
    if r.get("hint"):
        print(f"#   {r['hint']}")
    if r.get("suggest"):
        print(f"    {r['suggest']},")
    print()
EOF
