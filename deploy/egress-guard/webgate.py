"""The gate's parsers: what protocol a connection speaks, and what name it asks for.

Every outbound TCP connection a world makes is redirected to the guard's gate
(nftables, in the guard's namespace) and judged by its first bytes, whatever
the port. An address cannot tell apart two sites behind it -- a Cloudflare
address answering an approved name answers thousands of others -- and a program
can skip the lookup and dial that address itself; the name in the connection
can.

* TLS: the SNI in the ClientHello, read without decrypting anything. Steam's
  connection servers on 27018-27038 are TLS too (websockets), so this covers
  them as well as 443.
* HTTP: the Host header -- of *every* request on the connection, since
  keep-alive lets a second request name a different site.
* Anything else is a "stream": no name to read, refused unless an entry
  allows that port as a stream (see trust.py).

An approved name is resolved here and connected to *that* answer; the address
the program dialled is ignored, so naming an approved site while dialling
another gets the approved site. Refused, and reported like a refused lookup:
no name (a bare address), a name on no list, and TLS that hides its name with
Encrypted Client Hello. QUIC is rejected in nftables, so clients fall back to
TLS over TCP where the name is readable; and the DNS gate refuses the HTTPS
records that carry ECH keys, so ordinary clients never try ECH at all.

Limits, stated so they are not over-read:

* Only the first message of TLS is read. A client can name an approved site in
  SNI and ask for another inside the encrypted stream ("domain fronting"); the
  big CDNs refuse that across customers, which is the remaining defence.
* The HTTP check scans the client's stream for Host header lines. A request
  body containing a line that looks like one can trip it: that fails closed.
* With blocking off (log-only) everything is relayed to where it was going, and
  what would have been refused is logged and reported.
"""

from __future__ import annotations

import struct

#: TLS record and handshake bounds. A ClientHello carrying post-quantum key
#: shares is ~1.5-2 KB; anything far past this is not a hello worth waiting for.
MAX_HELLO_BYTES = 64 * 1024
MAX_HTTP_HEADER_BYTES = 16 * 1024

EXT_SERVER_NAME = 0x0000
EXT_ALPN = 0x0010
EXT_ENCRYPTED_CLIENT_HELLO = 0xFE0D

NEED_MORE = "need_more"  # not enough bytes yet
NOT_TLS = "not_tls"  # first byte is not a TLS handshake record
MALFORMED = "malformed"
OK = "ok"

#: Request methods that start an HTTP/1.x request. A first line beginning with
#: one of these and a space is read as HTTP.
HTTP_METHODS = (b"GET", b"HEAD", b"POST", b"PUT", b"DELETE", b"OPTIONS", b"PATCH", b"CONNECT", b"TRACE")


def classify(first: bytes) -> str | None:
    """``tls``, ``http`` or ``stream`` from a connection's first bytes; None
    while too few have arrived to tell."""
    if not first:
        return None
    if first[0] == 0x16:
        return "tls"
    word = first.split(b" ", 1)
    if len(word) == 2:
        return "http" if word[0] in HTTP_METHODS else "stream"
    # No space yet: still a possible method prefix?
    if len(first) < 8 and any(m.startswith(first) for m in HTTP_METHODS):
        return None
    return "stream"


Hello = tuple[str, "str | None", bool, tuple[str, ...]]


def parse_client_hello(data: bytes) -> Hello:
    """(status, sni, has_ech, alpn) from the start of a TLS connection.

    Reassembles the handshake across records when a large hello spans more
    than one. ``status`` is NEED_MORE until the whole hello has arrived.
    """
    if not data:
        return NEED_MORE, None, False, ()
    if data[0] != 0x16:
        return NOT_TLS, None, False, ()
    # Concatenate handshake-record payloads until the hello is complete.
    payload = b""
    off = 0
    while off + 5 <= len(data):
        rtype, _ver, rlen = data[off], data[off + 1 : off + 3], struct.unpack("!H", data[off + 3 : off + 5])[0]
        if rtype != 0x16:
            return MALFORMED, None, False, ()
        if off + 5 + rlen > len(data):
            payload += data[off + 5 :]
            break
        payload += data[off + 5 : off + 5 + rlen]
        off += 5 + rlen
        if len(payload) >= 4:
            need = 4 + int.from_bytes(payload[1:4], "big")
            if len(payload) >= need:
                break
    if len(payload) < 4:
        return (MALFORMED if len(data) > MAX_HELLO_BYTES else NEED_MORE), None, False, ()
    if payload[0] != 0x01:  # handshake type: client_hello
        return MALFORMED, None, False, ()
    need = 4 + int.from_bytes(payload[1:4], "big")
    if need > MAX_HELLO_BYTES:
        return MALFORMED, None, False, ()
    if len(payload) < need:
        return NEED_MORE, None, False, ()
    return _parse_hello_body(payload[4:need])


def _parse_hello_body(b: bytes) -> Hello:
    try:
        off = 2 + 32  # legacy_version, random
        off += 1 + b[off]  # session id
        off += 2 + struct.unpack("!H", b[off : off + 2])[0]  # cipher suites
        off += 1 + b[off]  # compression methods
        if off == len(b):
            return OK, None, False, ()  # no extensions at all
        end = off + 2 + struct.unpack("!H", b[off : off + 2])[0]
        off += 2
        sni: str | None = None
        ech = False
        alpn: list[str] = []
        while off + 4 <= end:
            etype, elen = struct.unpack("!HH", b[off : off + 4])
            body = b[off + 4 : off + 4 + elen]
            off += 4 + elen
            if etype == EXT_ENCRYPTED_CLIENT_HELLO:
                ech = True
            elif etype == EXT_ALPN and len(body) >= 2:
                # protocol_name_list: u16 len, then (u8 len, name) entries
                i = 2
                while i < len(body):
                    n = body[i]
                    alpn.append(body[i + 1 : i + 1 + n].decode("ascii", "replace"))
                    i += 1 + n
            elif etype == EXT_SERVER_NAME and len(body) >= 5:
                # server_name_list: u16 len, then entries of (u8 type, u16 len, name)
                i = 2
                while i + 3 <= len(body):
                    ntype, nlen = body[i], struct.unpack("!H", body[i + 1 : i + 3])[0]
                    name = body[i + 3 : i + 3 + nlen]
                    i += 3 + nlen
                    if ntype == 0:  # host_name
                        sni = name.decode("ascii", "strict").lower().rstrip(".")
                        break
        return OK, sni, ech, tuple(alpn)
    except (IndexError, struct.error, UnicodeDecodeError):
        return MALFORMED, None, False, ()


class HostScanner:
    """Every Host header in a client's HTTP stream, as the bytes arrive.

    Keep-alive lets one connection carry many requests, each with its own Host,
    so checking only the first would let a second request reach any site behind
    the same address. This scans for header lines by their shape rather than
    parsing request framing; a body that happens to contain one fails closed.
    """

    def __init__(self) -> None:
        self._tail = b""

    def feed(self, chunk: bytes) -> list[str]:
        data = self._tail + chunk
        hosts: list[str] = []
        lines = data.split(b"\n")
        # The last piece may be a partial line; keep it for the next chunk.
        self._tail = lines.pop()[-MAX_HTTP_HEADER_BYTES:]
        for raw in lines:
            line = raw.rstrip(b"\r")
            if line[:5].lower() == b"host:":
                hosts.append(_host_value(line[5:]))
        return hosts


def _host_value(raw: bytes) -> str:
    host = raw.strip().decode("ascii", "replace").lower()
    if host.startswith("["):  # [v6]:port
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_name(host: str | None) -> bool:
    """Whether ``host`` is a DNS name rather than missing or a bare address."""
    if not host:
        return False
    h = host.strip("[]")
    if ":" in h:
        return False  # IPv6 literal
    return not all(p.isdigit() for p in h.split("."))
