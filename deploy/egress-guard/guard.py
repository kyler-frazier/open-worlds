"""Per-world egress guard: a resolver that only answers approved names, a gate
that judges every outbound TCP connection by the name it asks for, and a
firewall that drops everything else.

One guard runs beside every world, blocking or (blocking off) log-only. The guard
owns the network namespace and the world joins it (``network_mode:
container:<guard>``), so the rules below are the world's rules, and the world
-- which has no CAP_NET_ADMIN -- cannot change them.

The policy is the world's own: trust.py's entries for this game, server
variant and stage (``GUARD_GAME``, ``GUARD_VARIANT``, ``GUARD_STAGE``). A
Minecraft world cannot reach Steam, a Paper server cannot reach Fabric's maven,
and a running server cannot reach the hosts its install downloaded from. A
stage change replaces the guard (the orchestrator stamps the stage into its
template), so one guard only ever enforces one list.

How a connection gets out:

1. The world looks up a name. Docker's embedded resolver hands external names
   to this process on 127.0.0.1:53. A name no entry for this game allows is
   answered NXDOMAIN and never forwarded, so the lookup itself cannot carry data
   out (``secret.evil.example`` never reaches evil.example's nameserver).
2. Every outbound TCP connection, on any port, is redirected to the gate
   (webgate.py parses). The gate reads the first bytes: TLS gives the name in
   the ClientHello, HTTP the Host of every request. An entry must match the
   name, the protocol and the port the world dialled; the gate then resolves
   the name itself and connects to *that* answer on that port, so dialling one
   address while naming another gets the named one.
3. Everything else is dropped by the firewall: UDP (QUIC included), hard-coded
   addresses, other resolvers. Rejected, not just dropped, so software fails
   fast instead of hanging on a timeout.

Defence in depth: the gate's own upstream connections carry a mark the world
cannot set, and the firewall lets marked traffic out only on the ports this
game's entries list. A bug in the gate cannot reach port 8443.

Only this process can reach the upstream resolvers. Its upstream sockets bind a
source port below 1024, the rule accepts DNS only from that range, and the
namespace keeps ``ip_unprivileged_port_start`` at 1024 -- the world has no
CAP_NET_BIND_SERVICE, so nothing in it can bind there. That holds for the
image's own root entrypoint as much as for the server it drops to.

Every refusal is a record (``refuse``): what was refused, by which layer, why,
and a ready-to-paste entry. Printed as a JSON line and reported to the
dns-observer, so adding a missing entry never means reproducing the failure.

Log-only mode (``GUARD_MODE=log``, the per-world "Allow unknown network
connections" override): same namespace, same resolver, same gate, same
records, but nothing is refused. The gate relays to wherever the world was
going and the firewall's last rule accepts, so the records read "would
refuse". For debugging a game behind the guard: switch it to log-only, play,
and the log names exactly what enforcement would break.

Fail closed. The ruleset is applied first and then tested; only if the test
passes is the readiness marker written, and the orchestrator does not start a
world whose guard is not healthy. If this process dies the rules stay in the
kernel: the world loses DNS and its gate, not its fence.

Known limits, recorded rather than solved:

* TLS is judged by its first message. A client can name an approved site in
  SNI and ask for another inside the encrypted stream ("domain fronting"); the
  big CDNs refuse that across customers.
* Per-world, not per-process: anything in the world can use what the game can.
* UDP only for DNS. A reply too large for UDP comes back truncated; nothing the
  worlds resolve has needed TCP so far.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import random
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time

import trust
import webgate
from resolver import _read_name, parse_question

LISTEN = ("127.0.0.1", 53)
UPSTREAMS = [u.strip() for u in os.environ.get("GUARD_UPSTREAMS", "").split(",") if u.strip()]
UPSTREAM_TIMEOUT = float(os.environ.get("GUARD_UPSTREAM_TIMEOUT", "3"))
#: "ip:port" of the dns-observer's refusal report listener. Each refusal is
#: reported the first time it happens, so a game broken by a missing entry
#: reaches the alerting instead of only this container's log. Empty disables.
REPORT_ADDR = os.environ.get("GUARD_REPORT", "").strip()
#: The world this guard serves, and its game, variant and stage: which entries
#: apply.
GAME = os.environ.get("GUARD_GAME", "").strip().upper()
VARIANT = os.environ.get("GUARD_VARIANT", "").strip().upper()
STAGE = os.environ.get("GUARD_STAGE", "").strip().lower()
WORLD = os.environ.get("GUARD_WORLD", "").strip()
#: Floor and ceiling for how long a looked-up address is remembered as that
#: name's (for stream entries and for naming refusals). The floor is generous:
#: software routinely reuses a 20s-TTL answer for minutes.
WINDOW_MIN = int(os.environ.get("GUARD_WINDOW_MIN_SECONDS", "300"))
WINDOW_MAX = int(os.environ.get("GUARD_WINDOW_MAX_SECONDS", "3600"))
#: Source ports for upstream queries. Below 1024 so only a process holding
#: CAP_NET_BIND_SERVICE -- this one -- can use them.
SPORT_LO, SPORT_HI = 600, 999
READY_PATH = "/run/guard-ready"
#: "enforce" (the default; anything unrecognised also enforces) or "log".
LOG_ONLY = os.environ.get("GUARD_MODE", "enforce").strip().lower() == "log"
TABLE = "guard"

NXDOMAIN = 3
NOERROR = 0
#: DNS record types that carry Encrypted Client Hello keys. Answered empty, so
#: an ordinary client never learns a key and keeps its SNI readable.
QTYPE_SVCB, QTYPE_HTTPS = 64, 65

#: Where nftables redirects every outbound TCP connection (the gate).
GATE_PORT = 15000
#: Marks the gate's own upstream connections, so they skip the redirect and
#: pass the filter. Setting a mark needs CAP_NET_ADMIN, which the world lacks.
MARK = 0x6567
SO_MARK = 36
SO_ORIGINAL_DST = 80
#: How long a client may take to send its first message before it is dropped.
FIRST_MESSAGE_TIMEOUT = 10.0
#: Where the self-test aims (TEST-NET-1: routable, never answers), and the name
#: it asks for. Neither is ever reported: they would trip the alert on every
#: guard start.
SELFTEST_ADDR = "192.0.2.1"
SELFTEST_NAME = "guard-selftest.invalid"
#: Never reachable from a world, in either mode: the host's LAN and every
#: other private network behind it. Log-only loosens the fence to the internet,
#: not to the house. The guard's own resolver and report port are accepted
#: before this, and only from its privileged source ports.
PRIVATE4 = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "255.255.255.255",
)
PRIVATE6 = ("fc00::/7", "fe80::/10", "ff00::/8")
PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in PRIVATE4 + PRIVATE6)
#: How often the firewall's drop sets are read for new refusals.
FIREWALL_POLL_SECONDS = float(os.environ.get("GUARD_FIREWALL_POLL_SECONDS", "15"))

#: This world's policy. The union until main() configures it from the
#: environment; main() refuses to start without a game.
POLICY = trust.Policy()


def configure() -> None:
    global POLICY
    if GAME not in trust.GAMES:
        raise SystemExit(f"GUARD_GAME {GAME!r} is not one of {sorted(trust.GAMES)}")
    # No default: a guard that guessed its stage could hand a running server
    # the install list.
    if STAGE not in trust.STAGES:
        raise SystemExit(f"GUARD_STAGE {STAGE!r} is not one of {list(trust.STAGES)}")
    try:
        POLICY = trust.Policy(GAME, VARIANT, STAGE)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def is_approved(qname: str) -> bool:
    """The DNS gate's question: does any entry for this world allow the name?"""
    return POLICY.name_allowed(qname)


def _report_target() -> tuple[str, int] | None:
    host, _, port = REPORT_ADDR.rpartition(":")
    try:
        return (host, int(port)) if host else None
    except ValueError:
        return None


def _port_set(ranges: tuple[tuple[int, int], ...]) -> str:
    return ", ".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in ranges)


def ruleset(
    upstreams: list[str],
    ports: tuple[tuple[int, int], ...] = ((443, 443),),
    log_only: bool = False,
    report: tuple[str, int] | None = None,
) -> str:
    dns = ", ".join(upstreams)
    # Same privileged-source-port rule as DNS: only this process can send it.
    report_rule = (
        f"    ip daddr {report[0]} udp sport {SPORT_LO}-{SPORT_HI} udp dport {report[1]} accept\n"
        if report
        else ""
    )
    if log_only:
        marked = f"    meta mark {MARK:#x} accept\n"
        final = "    accept"
    else:
        # The gate's upstream leg only on this game's ports: a gate bug cannot
        # reach anything the entries do not list. A stage with no entries
        # lists no ports, and nft refuses an empty set.
        marked = (
            f"    meta mark {MARK:#x} tcp dport {{ {_port_set(ports)} }} accept\n" if ports else ""
        )
        final = "    meta l4proto tcp reject with tcp reset\n    reject with icmpx admin-prohibited"
    return f"""
table inet {TABLE} {{
  # What the firewall dropped (or, log-only, would have): read by
  # watch_firewall() and reported, so a refused UDP flow is as visible as a
  # refused connection.
  set refused4 {{ type ipv4_addr . inet_proto . inet_service; flags dynamic, timeout; timeout 1h; size 4096; }}
  set refused6 {{ type ipv6_addr . inet_proto . inet_service; flags dynamic, timeout; timeout 1h; size 4096; }}
  counter blocked {{ }}
  chain gate {{
    type nat hook output priority -100; policy accept;
    meta mark {MARK:#x} return
    oif "lo" return
    # Every TCP port: the gate reads what the connection is before any port
    # decides it, so a refusal names its protocol and site.
    meta l4proto tcp redirect to :{GATE_PORT}
  }}
  # Before the gate's marked leg, so a name that resolves to a LAN address
  # (rebinding) is refused too.
  chain private {{
    meta l4proto {{ tcp, udp }} add @refused4 {{ ip daddr . meta l4proto . th dport }}
    meta l4proto {{ tcp, udp }} add @refused6 {{ ip6 daddr . meta l4proto . th dport }}
    counter name blocked
    meta l4proto tcp reject with tcp reset
    reject with icmpx admin-prohibited
  }}
  chain out {{
    type filter hook output priority 0; policy drop;
    oif "lo" accept
    ct state established,related accept
    # Redirected connections. Not caught by the loopback rule above: within
    # the output hook the packet keeps its original interface after redirect.
    ip daddr 127.0.0.1 tcp dport {GATE_PORT} accept
    ip6 daddr ::1 tcp dport {GATE_PORT} accept
    ip daddr {{ {dns} }} udp sport {SPORT_LO}-{SPORT_HI} udp dport 53 accept
{report_rule}    ip daddr {{ {", ".join(PRIVATE4)} }} goto private
    ip6 daddr {{ {", ".join(PRIVATE6)} }} goto private
{marked}    meta l4proto {{ tcp, udp }} add @refused4 {{ ip daddr . meta l4proto . th dport }}
    meta l4proto {{ tcp, udp }} add @refused6 {{ ip6 daddr . meta l4proto . th dport }}
    counter name blocked
{final}
  }}
}}
"""


def nft(script: str) -> None:
    subprocess.run(["nft", "-f", "-"], input=script.encode(), check=True, capture_output=True)


# --- lookups: which names this world resolved to which addresses -------------

# address -> {name: expiry (monotonic)}. Filled from every answer the DNS gate
# forwards. Names the "stream" entries (an address is only allowed if this
# world looked it up as the entry's name) and names a refusal that carries no
# name of its own (a firewall drop, a hard-coded address).
_lookups: dict[str, dict[str, float]] = {}
_lookups_lock = threading.Lock()
MAX_LOOKUP_ADDRESSES = 4096


def note_lookup(name: str, answers: list[tuple[str, int]]) -> None:
    name = name.lower().rstrip(".")
    now = time.monotonic()
    with _lookups_lock:
        if len(_lookups) >= MAX_LOOKUP_ADDRESSES:
            for addr in [a for a, names in _lookups.items() if max(names.values()) < now]:
                del _lookups[addr]
        for addr, ttl in answers:
            if addr not in _lookups and len(_lookups) >= MAX_LOOKUP_ADDRESSES:
                continue
            window = max(WINDOW_MIN, min(WINDOW_MAX, ttl))
            names = _lookups.setdefault(addr, {})
            names[name] = max(names.get(name, 0.0), now + window)


def names_for(addr: str) -> list[str]:
    now = time.monotonic()
    with _lookups_lock:
        return sorted(n for n, exp in _lookups.get(addr, {}).items() if exp > now)


def parse_answers_ttl(buf: bytes) -> list[tuple[str, int]]:
    """(address, ttl) for every A/AAAA in the answer section, CNAME targets
    included -- the query name was approved, so its whole chain is."""
    out: list[tuple[str, int]] = []
    try:
        qd, an = struct.unpack("!HH", buf[4:8])
        off = 12
        for _ in range(qd):
            _, off = _read_name(buf, off)
            off += 4
        for _ in range(an):
            _, off = _read_name(buf, off)
            rtype, _rclass, ttl, rdlen = struct.unpack("!HHIH", buf[off : off + 10])
            off += 10
            rdata = buf[off : off + rdlen]
            off += rdlen
            if rtype == 1 and rdlen == 4:
                out.append((socket.inet_ntop(socket.AF_INET, rdata), ttl))
            elif rtype == 28 and rdlen == 16:
                out.append((socket.inet_ntop(socket.AF_INET6, rdata), ttl))
    except (struct.error, ValueError, OSError):
        pass
    return out


# --- refusals: one record per thing refused, enough to write its entry --------

#: Why something was refused. Each record carries exactly one.
REASONS = (
    "unlisted_name",  # no entry for this game names it
    "denied",  # on DENY: refused on purpose
    "port_not_allowed",  # an entry names it, but not on this port
    "proto_not_allowed",  # an entry names it, but not for this protocol
    "no_name",  # TLS/HTTP naming no site, or a bare address
    "ech",  # TLS hiding its name (Encrypted Client Hello)
    "not_tcp",  # UDP or another protocol: only the guard's DNS may use UDP
    "no_client_bytes",  # silent client: a server-speaks-first protocol?
    "stream_addr_not_resolved",  # a stream entry's port, to an address not looked up as its name
)

_refusals: dict[tuple, int] = {}
_refusals_lock = threading.Lock()


def _send_report(payload: bytes) -> None:
    """Best effort: a lost report costs an alert, never a connection."""
    target = _report_target()
    if target is None:
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(20):
            try:
                sock.bind(("0.0.0.0", random.randint(SPORT_LO, SPORT_HI)))
                break
            except OSError:
                continue
        sock.sendto(payload, target)
    except OSError:
        pass
    finally:
        sock.close()


def _suggest(
    name: str | None, proto: str | None, port: int | None, looked_up: list[str]
) -> tuple[str | None, str]:
    """(entry, hint): a ready-to-paste entry, and what it can't know."""
    if proto not in (None, *trust.PROTOCOLS):
        return None, (
            f"{proto} cannot be an entry: only the guard's own DNS may use UDP, "
            "so allowing this needs a change to the guard, not a list entry"
        )
    if not webgate.is_name(name):
        if not looked_up:
            return None, "no name, and this world looked up no name for the address"
        name = looked_up[0]
        hint = f"named by this world's lookup of the address ({', '.join(looked_up)})"
        return trust.suggest(GAME, VARIANT, name, proto or "tls", port or 443), hint
    if proto in trust.PROTOCOLS and port:
        return trust.suggest(GAME, VARIANT, name or "", proto, port), ""
    return (
        trust.suggest(GAME, VARIANT, name or "", "tls", 443),
        "guessed tls/443: a refused lookup never reaches a connection. Set this "
        "world to allow unknown connections (log-only) to see its real protocol and port.",
    )


def refuse(
    layer: str,
    reason: str,
    *,
    proto: str | None = None,
    name: str | None = None,
    dst: str | None = None,
    port: int | None = None,
    alpn: tuple[str, ...] = (),
    first_bytes: bytes = b"",
    qtype: int | None = None,
) -> dict | None:
    """Record a refusal (or, log-only, a would-be refusal). Printed on the first
    occurrence and every 100th; reported on the first. Returns the record."""
    if dst == SELFTEST_ADDR or name == SELFTEST_NAME:
        return None
    name = name.lower().rstrip(".") if name else None
    key = (layer, reason, proto, name or dst, port)
    with _refusals_lock:
        n = _refusals[key] = _refusals.get(key, 0) + 1
    if n != 1 and n % 100:
        return None
    looked_up = names_for(dst)[:5] if dst else []
    if reason == "denied":
        entry, hint = None, "on DENY: refused on purpose"
    elif reason == "private_address":
        entry, hint = None, "a private/LAN address: no entry can allow it"
    else:
        entry, hint = _suggest(name, proto, port, looked_up)
    record = {
        "world": WORLD,
        "game": GAME,
        "variant": VARIANT,
        # Which of the game's lists a suggested entry belongs in.
        "stage": STAGE,
        "mode": "log" if LOG_ONLY else "enforce",
        "layer": layer,
        "reason": reason,
        "proto": proto,
        "name": name,
        "dst": dst,
        "port": port,
        "names_for_dst": looked_up,
        "count": n,
    }
    if qtype is not None:
        record["qtype"] = qtype
    if alpn:
        record["alpn"] = list(alpn)
    if first_bytes:
        record["first_bytes"] = first_bytes[:16].hex()
    if entry:
        record["suggest"] = entry
    if hint:
        record["hint"] = hint
    verb = "would refuse" if LOG_ONLY else "refused"
    print(f"{verb} {json.dumps(record, separators=(',', ':'))}", flush=True)
    if n == 1:
        _send_report(json.dumps(record).encode())
    return record


# --- DNS gate -------------------------------------------------------------------


def refusal(query: bytes, rcode: int = NXDOMAIN) -> bytes:
    """The query echoed back as a response with ``rcode`` and no answers."""
    flags = struct.unpack("!H", query[2:4])[0]
    # QR=1, keep opcode and RD, RA=1, set rcode.
    flags = 0x8000 | (flags & 0x7900) | 0x0080 | rcode
    return query[:2] + struct.pack("!HHHHH", flags, 1, 0, 0, 0) + _question_bytes(query)


def _question_bytes(query: bytes) -> bytes:
    _, off = _read_name(query, 12)
    return query[12 : off + 4]


def forward(query: bytes) -> bytes | None:
    for upstream in UPSTREAMS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _ in range(20):
                try:
                    sock.bind(("0.0.0.0", random.randint(SPORT_LO, SPORT_HI)))
                    break
                except OSError:
                    continue
            sock.settimeout(UPSTREAM_TIMEOUT)
            sock.sendto(query, (upstream, 53))
            reply, _ = sock.recvfrom(65535)
            return reply
        except OSError:
            continue
        finally:
            sock.close()
    return None


def dns_reason(qname: str) -> str | None:
    """Why the DNS gate refuses a name, or None if it forwards it."""
    if POLICY.denied(qname):
        return "denied"
    if not is_approved(qname):
        return "unlisted_name"
    return None


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, sock = self.request
        question = parse_question(data)
        if question is None:
            return
        qname, qtype = question
        reason = dns_reason(qname)
        if reason:
            refuse("dns", reason, name=qname, qtype=qtype)
            if not LOG_ONLY:
                sock.sendto(refusal(data), self.client_address)
                return
        elif qtype in (QTYPE_HTTPS, QTYPE_SVCB) and not LOG_ONLY:
            # The name exists, so not NXDOMAIN (some resolvers would cache that
            # for every type); just no record of this type, and no ECH key.
            sock.sendto(refusal(data, NOERROR), self.client_address)
            return
        reply = forward(data)
        if reply is None:
            return  # the client retries; never answer with a guess
        note_lookup(qname, parse_answers_ttl(reply))
        sock.sendto(reply, self.client_address)


class Server(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True


# --- the gate: every outbound TCP connection (see webgate.py) -----------------


def client_hello(server_name: str) -> bytes:
    """A real TLS ClientHello naming ``server_name``, for the self-test."""
    import ssl

    ctx = ssl.create_default_context()
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ctx.wrap_bio(incoming, outgoing, server_hostname=server_name)
    try:
        tls.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()


def build_query(name: str, qtype: int) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".") if p)
    head = struct.pack("!HHHHHH", random.randint(0, 0xFFFF), 0x0100, 1, 0, 0, 0)
    return head + labels + b"\0" + struct.pack("!HH", qtype, 1)


def resolve(name: str) -> list[str]:
    """Approved name -> addresses, through the same upstreams as the DNS gate."""
    out: list[str] = []
    for qtype in (1, 28):
        answers = parse_answers_ttl(forward(build_query(name, qtype)) or b"")
        note_lookup(name, answers)
        out += [addr for addr, _ttl in answers]
        if out:
            break
    return out


def _original_dst(sock: socket.socket) -> tuple[str, int] | None:
    """Where the world was dialling before the redirect."""
    try:
        raw = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
        return socket.inet_ntoa(raw[4:8]), struct.unpack("!H", raw[2:4])[0]
    except OSError:
        pass
    try:
        raw = sock.getsockopt(socket.IPPROTO_IPV6, SO_ORIGINAL_DST, 28)
        return socket.inet_ntop(socket.AF_INET6, raw[8:24]), struct.unpack("!H", raw[2:4])[0]
    except OSError:
        return None


async def _open_marked(host: str, port: int):  # type: ignore[no-untyped-def]
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, SO_MARK, MARK)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().sock_connect(sock, (host, port)), UPSTREAM_TIMEOUT * 3
        )
    except BaseException:
        sock.close()
        raise
    return await asyncio.open_connection(sock=sock)


async def _pump(reader, writer, check=None) -> None:  # type: ignore[no-untyped-def]
    try:
        while data := await reader.read(65536):
            if check is not None and not check(data):
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


def decide(name: str | None, proto: str, port: int, ech: bool = False) -> str | None:
    """Why a named connection is refused, or None if an entry allows it."""
    if ech:
        return "ech"
    if not webgate.is_name(name):
        return "no_name"
    name = name or ""
    if POLICY.denied(name):
        return "denied"
    if POLICY.match(name, proto, port):
        return None
    same_name = [e for e in POLICY.allow if e.name_matches(name)]
    if not same_name:
        return "unlisted_name"
    if any(e.proto == proto for e in same_name):
        return "port_not_allowed"
    return "proto_not_allowed"


def stream_allowed(addr: str, port: int) -> bool:
    """A stream entry for this port whose name this world looked ``addr`` up as."""
    looked_up = names_for(addr)
    return any(
        e.name_matches(n) for e in POLICY.stream_entries(port) for n in looked_up
    )


async def _read_first(reader) -> tuple[bytes, str | None]:  # type: ignore[no-untyped-def]
    """The connection's opening bytes and its protocol, read until the name (if
    any) is complete. Protocol None: the client sent nothing in time."""
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(16384), FIRST_MESSAGE_TIMEOUT)
        except TimeoutError:
            return buf, webgate.classify(buf) if buf else None
        if not chunk:
            return buf, webgate.classify(buf) if buf else None
        buf += chunk
        proto = webgate.classify(buf)
        if proto is None:
            continue
        if proto == "tls":
            if (
                webgate.parse_client_hello(buf)[0] == webgate.NEED_MORE
                and len(buf) < webgate.MAX_HELLO_BYTES
            ):
                continue
        elif proto == "http":
            if b"\r\n\r\n" not in buf and b"\n\n" not in buf and len(buf) < webgate.MAX_HTTP_HEADER_BYTES:
                continue
        return buf, proto


def is_private(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in PRIVATE_NETS)


async def _relay(reader, writer, host: str, port: int, buf: bytes, check=None) -> None:  # type: ignore[no-untyped-def]
    # The firewall refuses these too; refusing here names the reason.
    if is_private(host):
        refuse("gate", "private_address", proto="stream", dst=host, port=port)
        writer.close()
        return
    try:
        up_reader, up_writer = await _open_marked(host, port)
    except (TimeoutError, ConnectionError, OSError):
        writer.close()
        return
    if buf:
        up_writer.write(buf)
        await up_writer.drain()
    await asyncio.gather(_pump(reader, up_writer, check), _pump(up_reader, writer))


async def _gate(reader, writer) -> None:  # type: ignore[no-untyped-def]
    dst = _original_dst(writer.get_extra_info("socket"))
    if dst is None:
        writer.close()
        return
    addr, port = dst

    # A stream entry's connection may be one where the server speaks first, so
    # it is decided on its address before waiting for any bytes.
    if stream_allowed(addr, port):
        await _relay(reader, writer, addr, port, b"")
        return

    try:
        buf, proto = await _read_first(reader)
    except (ConnectionError, OSError):
        writer.close()
        return
    if proto is None:
        if buf == b"" and reader.at_eof():
            writer.close()  # opened and closed without a word: nothing to judge
            return
        refuse("gate", "no_client_bytes", proto="stream", dst=addr, port=port)
        if not LOG_ONLY:
            writer.close()
            return
        await _relay(reader, writer, addr, port, buf)
        return

    name: str | None = None
    ech = False
    alpn: tuple[str, ...] = ()
    hosts: list[str] = []
    if proto == "tls":
        _status, name, ech, alpn = webgate.parse_client_hello(buf)
        reason = decide(name, proto, port, ech)
    elif proto == "http":
        hosts = webgate.HostScanner().feed(buf)
        name = hosts[0] if hosts else None
        reason = decide(name, proto, port)
    else:
        reason = (
            "stream_addr_not_resolved" if POLICY.stream_entries(port) else "proto_not_allowed"
        )

    refused = False
    if reason:
        refuse(
            "gate", reason, proto=proto, name=name, dst=addr, port=port, alpn=alpn,
            first_bytes=buf if proto == "stream" else b"",
        )
        refused = True
    # Pipelined requests already in the first chunk name sites too.
    for host in hosts[1:]:
        why = decide(host, proto, port)
        if why:
            refuse("gate", why, proto=proto, name=host, dst=addr, port=port)
            refused = True
    if refused and not LOG_ONLY:
        writer.close()
        return

    check = None
    if proto == "http":
        # Every later request on a kept-alive connection names a site too.
        later = webgate.HostScanner()
        later.feed(buf)  # already checked; this only carries its partial line

        def check(data: bytes) -> bool:
            for host in later.feed(data):
                why = decide(host, "http", port)
                if why:
                    refuse("gate", why, proto="http", name=host, dst=addr, port=port)
                    if not LOG_ONLY:
                        return False
            return True

    if LOG_ONLY:
        # Relayed to where it was going, refused or not: the point of this
        # mode is seeing what enforcement would break without breaking it.
        await _relay(reader, writer, addr, port, buf, check)
        return
    addrs = await asyncio.to_thread(resolve, name or "")
    if not addrs:
        writer.close()
        return
    await _relay(reader, writer, addrs[0], port, buf, check)


def start_gate() -> None:
    """Listen behind the redirect, in a thread of its own."""
    ready = threading.Event()
    failed: list[BaseException] = []

    async def serve() -> None:
        servers = []
        for host in ("127.0.0.1", "::1"):
            try:
                servers.append(await asyncio.start_server(_gate, host, GATE_PORT))
            except OSError as exc:
                if host == "127.0.0.1":  # IPv6 may simply be absent
                    failed.append(exc)
        ready.set()
        await asyncio.gather(*(srv.serve_forever() for srv in servers))

    threading.Thread(target=lambda: asyncio.run(serve()), name="gate", daemon=True).start()
    ready.wait(10)
    if failed or not ready.is_set():
        raise SystemExit(f"gate could not listen: {failed}")


# --- firewall drops ------------------------------------------------------------

_PROTO_NAMES = {6: "tcp", 17: "udp"}


def firewall_elements(listing: dict) -> list[tuple[str, str, int]]:
    """(address, protocol, port) from ``nft -j list set`` output."""
    out: list[tuple[str, str, int]] = []
    for item in listing.get("nftables", []):
        for elem in (item.get("set") or {}).get("elem", []) or []:
            val = elem.get("elem", {}).get("val", elem) if isinstance(elem, dict) else elem
            parts = val.get("concat") if isinstance(val, dict) else None
            if not parts or len(parts) != 3:
                continue
            addr, proto, port = parts
            proto = _PROTO_NAMES.get(proto, proto) if isinstance(proto, int) else str(proto)
            try:
                out.append((str(addr), proto, int(port)))
            except (TypeError, ValueError):
                continue
    return out


def firewall_reason(proto: str) -> str:
    # TCP reaches the firewall only as the gate's own marked leg to a port no
    # entry lists -- the defence-in-depth rule catching a gate bug.
    return "port_not_allowed" if proto == "tcp" else "not_tcp"


def read_firewall() -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for setname in ("refused4", "refused6"):
        proc = subprocess.run(
            ["nft", "-j", "list", "set", "inet", TABLE, setname], capture_output=True, check=False
        )
        if proc.returncode == 0:
            try:
                out += firewall_elements(json.loads(proc.stdout))
            except ValueError:
                continue
    return out


def watch_firewall() -> None:
    """Report what the firewall drops. An element is new until it times out of
    its set (1h), so a flow still being dropped is seen again, and counted."""
    while True:
        time.sleep(FIREWALL_POLL_SECONDS)
        for addr, proto, port in read_firewall():
            refuse(
                "firewall", firewall_reason(proto), proto=proto,
                name=(names_for(addr) or [None])[0], dst=addr, port=port,
            )


# --- self-test -------------------------------------------------------------------


def _expect_refused(port: int, mark: bool = False) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if mark:
        sock.setsockopt(socket.SOL_SOCKET, SO_MARK, MARK)
    sock.settimeout(3)
    try:
        sock.connect((SELFTEST_ADDR, port))
    except ConnectionRefusedError:
        return  # reset by the firewall: the pass condition
    except OSError as exc:
        raise SystemExit(f"probe to :{port} was not rejected ({exc}): the ruleset is not in effect")
    finally:
        sock.close()
    raise SystemExit(f"probe to :{port} connected: the ruleset is not in effect")


def _expect_gate_refuses(port: int, name: str) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect((SELFTEST_ADDR, port))
        sock.sendall(client_hello(name))
        if sock.recv(1):
            raise SystemExit(f"the gate relayed TLS for {name} on :{port}")
    except SystemExit:
        raise
    except ConnectionRefusedError as exc:
        raise SystemExit("the gate is not listening; every TCP connection would fail") from exc
    except TimeoutError as exc:
        raise SystemExit(f"the gate neither relayed nor refused {name} on :{port}") from exc
    except OSError:
        pass  # reset after the hello: refused, as it should be
    finally:
        sock.close()


def self_test() -> None:
    """Prove the fence holds before declaring the guard ready."""
    with open("/proc/sys/net/ipv4/ip_unprivileged_port_start", encoding="ascii") as fh:
        start = int(fh.read().strip())
    if start < 1024:
        raise SystemExit(
            f"ip_unprivileged_port_start is {start}: the world could bind the "
            "resolver's source ports and reach upstream DNS directly"
        )
    if LOG_ONLY:
        return  # letting the probes through is the point of this mode
    # The gate: an unapproved name on a listed port, and an approved name on a
    # port no entry lists, must both be taken and then dropped.
    _expect_gate_refuses(443, SELFTEST_NAME)
    approved = next((e.name for e in POLICY.allow if e.proto == "tls" and "*" not in e.name), None)
    if approved:
        _expect_gate_refuses(8443, approved)
    # Defence in depth: the gate's own marked leg must not reach an unlisted port.
    _expect_refused(8443 if not POLICY.port_listed(8443) else 1, mark=True)
    # UDP is dropped, and the drop is recorded for reporting.
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp.sendto(b"guard self-test", (SELFTEST_ADDR, 9))
    except OSError:
        pass  # rejected at once: also a pass, if recorded below
    finally:
        udp.close()
    if (SELFTEST_ADDR, "udp", 9) not in read_firewall():
        raise SystemExit("a dropped UDP packet was not recorded: firewall refusals would go unseen")


def main() -> None:
    # PID 1 ignores SIGTERM unless it asks for it, which turned every world sleep
    # into a five-second wait and a SIGKILL. Nothing to flush: the rules live in
    # the kernel and go with the namespace.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if not UPSTREAMS:
        raise SystemExit("GUARD_UPSTREAMS is empty: nothing to forward approved names to")
    configure()
    nft(ruleset(UPSTREAMS, POLICY.ports, LOG_ONLY, _report_target()))
    server = Server(LISTEN, Handler)
    start_gate()
    self_test()
    threading.Thread(target=watch_firewall, name="firewall", daemon=True).start()
    with open(READY_PATH, "w", encoding="ascii") as fh:
        fh.write("ok\n")
    mode = "LOG-ONLY (nothing blocked)" if LOG_ONLY else "enforcing"
    print(
        f"egress guard ready, {mode}: {WORLD or '?'} {GAME} {VARIANT or '(any variant)'} "
        f"{STAGE}, {len(POLICY.allow)} entries, ports {_port_set(POLICY.ports) or 'none'}, "
        f"upstreams {UPSTREAMS}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
