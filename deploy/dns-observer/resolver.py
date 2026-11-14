"""A logging DNS forwarder for world containers: names, not just addresses.

conntrack gives addresses only, most CDN addresses have no reverse DNS, and a
certificate probe proves nothing (Cloudflare's edge presents one for any
domain). The container's own query is the evidence that ties a name to its
traffic, and the recorded answers build an address -> name map that explains
unmatched rows in the egress report.

Limits:

* A container dialling a hard-coded address never asks, so never appears.
* DNS-over-HTTPS bypasses this; it shows in the egress report as :443 traffic.
* Counts are queries, not traffic volume.

Each world's egress guard also reports its first refusal of each thing (or,
with blocking off, what it would have refused) to REPORT_PORT: what, by which
layer, why, and a suggested entry. They are kept under ``refused`` for the
exporter, the alert and scripts/egress-refusals.sh.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import threading
import time

UPSTREAM = os.environ.get("DNS_UPSTREAM", "1.1.1.1")
UPSTREAM_PORT = int(os.environ.get("DNS_UPSTREAM_PORT", "53"))
LISTEN_PORT = int(os.environ.get("DNS_LISTEN_PORT", "53"))
OUT_PATH = os.environ.get("OUTPUT_PATH", "/data/dns-observations.json")
WRITE_EVERY = int(os.environ.get("WRITE_SECONDS", "30"))
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "4"))
# Cap both tables so a pathological client cannot exhaust memory.
MAX_NAMES = int(os.environ.get("MAX_NAMES", "5000"))
# Where egress guards report names they refused. Only guards can reach it: a
# guard's firewall lets its own privileged source ports through and nothing in
# a world can bind those.
REPORT_PORT = int(os.environ.get("REPORT_PORT", "5354"))

QTYPES = {1: "A", 2: "NS", 5: "CNAME", 12: "PTR", 15: "MX", 16: "TXT", 28: "AAAA", 65: "HTTPS"}

_lock = threading.Lock()
# qname -> {"count", "clients", "types", "first_seen", "last_seen", "addresses"}
_names: dict[str, dict] = {}
# address -> qname, the map that explains an unmatched egress row
_addresses: dict[str, str] = {}
# qname -> {"count", "clients", "modes", "first_seen", "last_seen"}, as reported
# by egress guards
_refused: dict[str, dict] = {}


def _read_name(buf: bytes, off: int) -> tuple[str, int]:
    """Decode a DNS name at ``off``. Returns (name, offset after it).

    Handles compression pointers, which appear throughout answer sections; the
    offset returned is the position after the pointer, not after the target,
    which is what a caller walking records needs.
    """
    labels: list[str] = []
    jumped = False
    end = off
    steps = 0
    while True:
        if off >= len(buf) or steps > 128:  # malformed or a pointer loop
            break
        length = buf[off]
        if length == 0:
            off += 1
            if not jumped:
                end = off
            break
        if length & 0xC0 == 0xC0:  # pointer
            if off + 1 >= len(buf):
                break
            if not jumped:
                end = off + 2
            off = ((length & 0x3F) << 8) | buf[off + 1]
            jumped = True
            steps += 1
            continue
        off += 1
        labels.append(buf[off : off + length].decode("ascii", "replace"))
        off += length
        if not jumped:
            end = off
    return ".".join(labels), end


def parse_question(buf: bytes) -> tuple[str, int] | None:
    """(qname, qtype) from a query, or None if it has no question section."""
    if len(buf) < 12:
        return None
    qdcount = struct.unpack("!H", buf[4:6])[0]
    if qdcount < 1:
        return None
    name, off = _read_name(buf, 12)
    if off + 4 > len(buf):
        return None
    qtype = struct.unpack("!H", buf[off : off + 2])[0]
    return name, qtype


def parse_answers(buf: bytes) -> list[str]:
    """IPv4/IPv6 addresses from a response's answer section.

    Best-effort: a record we cannot walk ends the scan rather than raising,
    because a malformed upstream reply must not take the resolver down with it.
    """
    out: list[str] = []
    try:
        if len(buf) < 12:
            return out
        qd, an = struct.unpack("!HH", buf[4:8])
        off = 12
        for _ in range(qd):
            _, off = _read_name(buf, off)
            off += 4
        for _ in range(an):
            _, off = _read_name(buf, off)
            if off + 10 > len(buf):
                break
            rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", buf[off : off + 10])
            off += 10
            rdata = buf[off : off + rdlen]
            off += rdlen
            if rtype == 1 and rdlen == 4:
                out.append(socket.inet_ntop(socket.AF_INET, rdata))
            elif rtype == 28 and rdlen == 16:
                out.append(socket.inet_ntop(socket.AF_INET6, rdata))
    except (struct.error, ValueError, OSError):
        pass
    return out


def record(client: str, qname: str, qtype: int, answers: list[str]) -> None:
    if not qname:
        return
    now = int(time.time())
    with _lock:
        row = _names.get(qname)
        if row is None:
            if len(_names) >= MAX_NAMES:
                return
            row = _names[qname] = {
                "name": qname,
                "count": 0,
                "clients": [],
                "types": [],
                "addresses": [],
                "first_seen": now,
                "last_seen": now,
            }
        row["count"] += 1
        row["last_seen"] = now
        if client not in row["clients"]:
            row["clients"].append(client)
        t = QTYPES.get(qtype, str(qtype))
        if t not in row["types"]:
            row["types"].append(t)
        for a in answers:
            if a not in row["addresses"]:
                row["addresses"].append(a)
            # Last writer wins. A shared CDN address genuinely serves many
            # names; the report keeps the full per-name lists, and this map is
            # the "who did this container most recently mean" shortcut.
            if len(_addresses) < MAX_NAMES * 4:
                _addresses[a] = qname


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, sock = self.request
        client = self.client_address[0]
        question = parse_question(data)
        try:
            up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            up.settimeout(UPSTREAM_TIMEOUT)
            up.sendto(data, (UPSTREAM, UPSTREAM_PORT))
            reply, _ = up.recvfrom(65535)
            up.close()
        except OSError:
            # Never leave a client hanging: a resolver that stops answering
            # stops the game server, and observing must not cost availability.
            return
        sock.sendto(reply, self.client_address)
        if question is not None:
            record(client, question[0], question[1], parse_answers(reply))


class Server(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True


#: What a guard's refusal record carries, kept as reported (strings are capped).
_REFUSAL_FIELDS = (
    "game", "variant", "stage", "layer", "reason", "proto", "name", "dst", "port", "qtype",
)
_REFUSAL_LATEST = ("names_for_dst", "alpn", "first_bytes", "suggest", "hint")


def _refusal_key(msg: dict) -> str:
    """One row per distinct refusal: same game, stage, layer, reason, protocol,
    target and port. The worlds and modes it came from are listed on the row."""
    target = msg.get("name") or msg.get("dst") or ""
    parts = [msg.get("game"), msg.get("variant"), msg.get("stage"), msg.get("layer"),
             msg.get("reason"), msg.get("proto"), target, msg.get("port")]
    return "|".join("" if p is None else str(p) for p in parts)


def record_refused(client: str, payload: bytes) -> None:
    """One guard refusal record (see guard.refuse). The old shape -- just
    ``{"name", "mode"}`` -- is read as a refused lookup."""
    try:
        msg = json.loads(payload.decode("utf-8"))
        if not isinstance(msg, dict):
            return
        mode = "log" if msg.get("mode") == "log" else "enforce"
        if msg.get("name"):
            msg["name"] = str(msg["name"]).strip().lower().rstrip(".")[:253]
        msg.setdefault("layer", "dns")
        msg.setdefault("reason", "unlisted_name")
    except (ValueError, TypeError, UnicodeDecodeError):
        return
    if not (msg.get("name") or msg.get("dst")):
        return
    key = _refusal_key(msg)
    world = str(msg.get("world") or "")[:80]
    now = int(time.time())
    with _lock:
        row = _refused.get(key)
        if row is None:
            if len(_refused) >= MAX_NAMES:
                return
            row = _refused[key] = {
                **{f: msg.get(f) for f in _REFUSAL_FIELDS},
                "count": 0,
                "clients": [],
                "worlds": [],
                "modes": [],
                "first_seen": now,
                "last_seen": now,
            }
        row["count"] += 1
        row["last_seen"] = now
        for field, value in (("clients", client), ("worlds", world), ("modes", mode)):
            if value and value not in row[field]:
                row[field].append(value)
        for f in _REFUSAL_LATEST:
            if msg.get(f):
                v = msg[f]
                row[f] = v[:1000] if isinstance(v, str) else v


class ReportHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, _sock = self.request
        record_refused(self.client_address[0], data)


def snapshot() -> dict:
    with _lock:
        names = sorted(_names.values(), key=lambda r: -r["count"])
        return {
            "generated_at": int(time.time()),
            "upstream": UPSTREAM,
            "truncated": len(_names) >= MAX_NAMES,
            "summary": {
                "names": len(names),
                "queries": sum(r["count"] for r in names),
                "clients": len({c for r in names for c in r["clients"]}),
                "addresses_mapped": len(_addresses),
            },
            # The payoff: look an unexplained egress address up here.
            "address_to_name": dict(sorted(_addresses.items())),
            "names": names,
            # What egress guards refused, or would have with blocking off.
            "refused": sorted(
                _refused.values(), key=lambda r: (r.get("name") or "", r.get("dst") or "")
            ),
        }


def write() -> None:
    payload = snapshot()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = f"{OUT_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, OUT_PATH)


def load() -> None:
    """Resume from the last report, so a restart keeps the name history."""
    try:
        with open(OUT_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return
    with _lock:
        for row in saved.get("names", [])[:MAX_NAMES]:
            if isinstance(row, dict) and row.get("name"):
                _names[row["name"]] = row
        _addresses.update(saved.get("address_to_name", {}))
        for row in saved.get("refused", [])[:MAX_NAMES]:
            if isinstance(row, dict) and (row.get("name") or row.get("dst")):
                row.setdefault("layer", "dns")
                row.setdefault("reason", "unlisted_name")
                row.setdefault("worlds", [])
                _refused[_refusal_key(row)] = row
    if _names:
        print(f"resumed {len(_names)} names from {OUT_PATH}", flush=True)


def main() -> None:
    load()
    server = Server(("", LISTEN_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    reports = Server(("", REPORT_PORT), ReportHandler)
    threading.Thread(target=reports.serve_forever, daemon=True).start()
    print(f"dns observer on :{LISTEN_PORT} -> {UPSTREAM}:{UPSTREAM_PORT}, report {OUT_PATH}", flush=True)
    while True:
        time.sleep(WRITE_EVERY)
        write()


if __name__ == "__main__":
    main()
