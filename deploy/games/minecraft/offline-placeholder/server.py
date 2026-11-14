#!/usr/bin/env python3
"""Minecraft placeholder server for worlds that are not running.

mc-router's default backend: any hostname without a route lands here, and the
platform is asked what the subdomain is:

  * no such world (deleted or bogus) -> drop the connection, so the client shows
    the plain "Can't connect to server".
  * a world -> a MOTD for its state. A login asks for a wake (the platform
    decides) and holds the player until the world is up, then relays them;
    otherwise they get the kick message.

If the lookup fails, the OFF message is shown.

Implements only the stable post-1.7 protocol slice: handshake -> status, and
handshake -> login start. The ping is left unanswered on purpose (see
handle_connection). The status reply echoes the client's protocol version, so
every client renders it as compatible.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

MOTD = os.environ.get("MOTD", "This world is currently OFF.")
# A sleeping world has no route either, and calling it off would send players
# away from a world that joining would wake, so the MOTD follows the state.
# MOTD is for OFF, and for when the state cannot be read.
STATE_MOTDS = {
    "SLEEPING": os.environ.get(
        "SLEEPING_MOTD", "This world is asleep. Join to wake it; it takes a minute or so."
    ),
    "STOPPING": os.environ.get(
        "STOPPING_MOTD", "This world is shutting down. Try again in a moment."
    ),
    "ERROR": os.environ.get("ERROR_MOTD", "This world stopped unexpectedly."),
}
KICK_MESSAGE = os.environ.get(
    "KICK_MESSAGE",
    "This world is off.\nStart it from the web UI, then reconnect.",
)
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "25565"))
MAX_PLAYERS = int(os.environ.get("MAX_PLAYERS", "0"))
ONLINE_PLAYERS = int(os.environ.get("ONLINE_PLAYERS", "0"))
VERSION_NAME = os.environ.get("VERSION_NAME", "off")
READ_TIMEOUT = float(os.environ.get("READ_TIMEOUT", "10"))
# Platform API endpoint used to tell OFF worlds apart from deleted/bogus hosts.
# Empty disables the check (every fall-through host gets the OFF message).
ROUTE_STATUS_URL = os.environ.get("ROUTE_STATUS_URL", "").rstrip("/")
# Shared secret sent as X-Internal-Secret so the route-status service can reject
# calls from anything else on the private network. Empty omits the header.
ROUTE_STATUS_SECRET = os.environ.get("ROUTE_STATUS_SECRET", "")
LOOKUP_TIMEOUT = float(os.environ.get("LOOKUP_TIMEOUT", "2"))
# Wake-on-connect. Empty disables it, and a login then gets the kick message --
# the old behaviour, and the fallback if the control plane is unreachable.
WAKE_URL = os.environ.get("WAKE_URL", "").rstrip("/")
# How long to hold a joining player while the world boots. Minecraft servers
# take 30-60s cold; a modded pack can take longer. The client itself gives up
# eventually, and that is the real ceiling.
WAKE_WAIT_SECONDS = float(os.environ.get("WAKE_WAIT_SECONDS", "180"))
# How often to try the backend while waiting.
WAKE_POLL_SECONDS = float(os.environ.get("WAKE_POLL_SECONDS", "2"))
# A wake lost to a restarting control plane is worth retrying; the player is
# already being held, so a second or two costs nothing they can perceive.
WAKE_REQUEST_ATTEMPTS = int(os.environ.get("WAKE_REQUEST_ATTEMPTS", "3"))
WAKE_RETRY_SECONDS = float(os.environ.get("WAKE_RETRY_SECONDS", "1.5"))
# The only packets read here are a handshake and a login start, each a few
# hundred bytes. A larger length is refused before it is buffered: the prefix
# alone could otherwise make this process allocate gigabytes.
MAX_PACKET_BYTES = int(os.environ.get("MAX_PACKET_BYTES", "65536"))
# Connections handled at once, held players included. Past this a new
# connection is closed at once instead of costing a thread.
MAX_CONNECTIONS = int(os.environ.get("MAX_CONNECTIONS", "256"))


def _subdomain_from_address(address: str) -> str:
    """Extract the world subdomain from the handshake's server address.

    Clients send the full host they connected to (e.g.
    ``my-world.servers.example.com``); modded clients append NUL-delimited
    extras (FML marker, proxy real-ip). The subdomain is the first DNS label.
    """
    host = address.split("\x00", 1)[0].strip().rstrip(".").lower()
    return host.split(":", 1)[0].split(".", 1)[0]


def world_state(subdomain: str) -> str | None:
    """The world's state ("OFF", "SLEEPING", ...), "" if there is no such world,
    or None if the lookup failed."""
    if not ROUTE_STATUS_URL or not subdomain:
        return None
    url = f"{ROUTE_STATUS_URL}/{urllib.parse.quote(subdomain)}"
    req = urllib.request.Request(url)
    if ROUTE_STATUS_SECRET:
        req.add_header("X-Internal-Secret", ROUTE_STATUS_SECRET)
    try:
        with urllib.request.urlopen(req, timeout=LOOKUP_TIMEOUT) as resp:
            return str((json.loads(resp.read()) or {}).get("state") or "OFF")
    except urllib.error.HTTPError as exc:
        # 404 is the definitive "no such world" answer; anything else is noise.
        return "" if exc.code == 404 else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def request_wake(subdomain: str) -> str | None:
    """Ask the platform to start the world; return where to relay, or None.

    The platform owns the decision -- it checks the world is wakeable, that the
    game is enabled, and that the host has memory for it -- so a player
    connecting can no more oversubscribe the host than the owner can. It also
    tells us the backend address, so this does not have to know how containers
    are named.
    """
    if not WAKE_URL or not subdomain:
        return None
    req = urllib.request.Request(f"{WAKE_URL}/{urllib.parse.quote(subdomain)}", method="POST")
    if ROUTE_STATUS_SECRET:
        req.add_header("X-Internal-Secret", ROUTE_STATUS_SECRET)
    # Retried, because the failure worth surviving is the control plane
    # restarting -- seconds, while this connection is already being held. A
    # refusal is an answer and is not retried.
    for attempt in range(WAKE_REQUEST_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=LOOKUP_TIMEOUT) as resp:
                return (json.loads(resp.read()) or {}).get("backend")
        except urllib.error.HTTPError as exc:
            # 409 is a deliberate refusal (world OFF, game disabled, host full).
            print(f"wake refused for {subdomain!r}: HTTP {exc.code}", flush=True)
            return None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if attempt + 1 < WAKE_REQUEST_ATTEMPTS:
                time.sleep(WAKE_RETRY_SECONDS)
                continue
            print(f"wake failed for {subdomain!r}: {exc}", flush=True)
    return None


def wait_for_backend(backend: str, deadline: float) -> socket.socket | None:
    """Connect to the world once it is accepting, or give up at the deadline."""
    host, _, port = backend.rpartition(":")
    while time.monotonic() < deadline:
        try:
            return socket.create_connection((host, int(port)), timeout=WAKE_POLL_SECONDS)
        except OSError:
            time.sleep(WAKE_POLL_SECONDS)
    return None


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def relay(client: socket.socket, backend_sock: socket.socket, replay: bytes) -> None:
    """Hand the connection to the world, replaying what we already consumed.

    We had to read the handshake to learn which world was wanted, and the login
    packet to know this was a join rather than a server-list ping. The world
    still needs both, so they go out first, byte for byte, before piping.
    """
    backend_sock.sendall(replay)
    client.settimeout(None)
    backend_sock.settimeout(None)
    t = threading.Thread(target=_pipe, args=(backend_sock, client), daemon=True)
    t.start()
    _pipe(client, backend_sock)
    t.join(timeout=5)


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        temp = value & 0x7F
        value >>= 7
        if value:
            out.append(temp | 0x80)
        else:
            out.append(temp)
            return bytes(out)


def read_varint_stream_raw(sock: socket.socket) -> tuple[int, bytes]:
    """read_varint_stream, keeping the bytes it consumed."""
    raw = bytearray()
    value = 0
    for shift in range(0, 35, 7):
        byte = recv_exact(sock, 1)
        raw += byte
        value |= (byte[0] & 0x7F) << shift
        if not byte[0] & 0x80:
            return value, bytes(raw)
    raise ValueError("varint too long")


def read_varint_stream(sock: socket.socket) -> int:
    """Read a VarInt directly from the socket (used for the packet length)."""
    value = 0
    for i in range(5):
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("socket closed while reading VarInt")
        byte = chunk[0]
        value |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return value
    raise ValueError("VarInt too long")


def _check_length(n: int) -> int:
    if n > MAX_PACKET_BYTES:
        raise ValueError(f"packet of {n} bytes is over the {MAX_PACKET_BYTES} limit")
    return n


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed while reading payload")
        buf.extend(chunk)
    return bytes(buf)


class Buffer:
    """Cursor over an already-read packet body."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read_varint(self) -> int:
        value = 0
        for i in range(5):
            byte = self.data[self.pos]
            self.pos += 1
            value |= (byte & 0x7F) << (7 * i)
            if not byte & 0x80:
                return value
        raise ValueError("VarInt too long")

    def read_ushort(self) -> int:
        val = struct.unpack(">H", self.data[self.pos : self.pos + 2])[0]
        self.pos += 2
        return val

    def read_string(self) -> str:
        length = self.read_varint()
        s = self.data[self.pos : self.pos + length].decode("utf-8", "replace")
        self.pos += length
        return s


def read_packet(sock: socket.socket) -> Buffer:
    length = _check_length(read_varint_stream(sock))
    return Buffer(recv_exact(sock, length))


def read_packet_raw(sock: socket.socket) -> tuple[Buffer, bytes]:
    """A packet, plus the exact bytes it arrived as.

    Relaying a held connection means replaying what we consumed to learn where
    it was going. Re-encoding it would risk not being byte-identical, so keep
    the original: the length prefix as it was written, then the body.
    """
    length, prefix = read_varint_stream_raw(sock)
    body = recv_exact(sock, _check_length(length))
    return Buffer(body), prefix + body


def write_packet(sock: socket.socket, packet_id: int, payload: bytes = b"") -> None:
    body = encode_varint(packet_id) + payload
    sock.sendall(encode_varint(len(body)) + body)


def write_string(s: str) -> bytes:
    raw = s.encode("utf-8")
    return encode_varint(len(raw)) + raw


def _status_json(protocol: int, state: str | None) -> str:
    return json.dumps(
        {
            "version": {"name": VERSION_NAME, "protocol": protocol},
            "players": {"max": MAX_PLAYERS, "online": ONLINE_PLAYERS, "sample": []},
            "description": {"text": STATE_MOTDS.get(state or "", MOTD)},
        }
    )


def handle_connection(sock: socket.socket) -> None:
    # Only for the handshake; relay() clears it before holding a player.
    sock.settimeout(READ_TIMEOUT)

    handshake, handshake_raw = read_packet_raw(sock)
    if handshake.read_varint() != 0x00:  # handshake packet id
        return
    protocol = handshake.read_varint()
    address = handshake.read_string()  # server address the client connected to
    handshake.read_ushort()  # server port (ignored)
    next_state = handshake.read_varint()

    # Deleted/bogus hosts (world row gone) get no reply at all, so the client
    # shows "Can't connect to server" instead of a misleading "start me" MOTD.
    subdomain = _subdomain_from_address(address)
    state = world_state(subdomain)
    if state == "":
        print(f"unknown host {subdomain!r}: dropping", flush=True)
        return

    if next_state == 1:  # status
        read_packet(sock)  # status request (empty body)
        write_packet(sock, 0x00, write_string(_status_json(protocol, state)))
        # Deliberately do NOT answer the ping/pong. Echoing it back makes the
        # client draw a healthy green latency bar (e.g. "16ms"), which
        # misleadingly implies an OFF world is online. Closing without a pong
        # leaves the connection indicator blank/unreachable while the MOTD
        # (already parsed from the status response) still explains it's off.
    elif next_state == 2:  # login: a real join attempt, so wake and hold
        try:
            _login, login_raw = read_packet_raw(sock)  # login start (name/uuid)
        except (ConnectionError, socket.timeout, IndexError, ValueError):
            _login, login_raw = None, b""

        # A status ping must never wake anything -- server lists and the
        # multiplayer screen send those constantly, and waking an 8 GB world
        # because someone opened a menu is exactly the abuse pal-waker's
        # datagram heuristic exists to prevent. Only a join gets here.
        backend = request_wake(subdomain)
        if backend:
            print(f"waking {subdomain!r}, holding the player", flush=True)
            deadline = time.monotonic() + WAKE_WAIT_SECONDS
            backend_sock = wait_for_backend(backend, deadline)
            if backend_sock is not None:
                print(f"relaying {subdomain!r} to {backend}", flush=True)
                relay(sock, backend_sock, handshake_raw + login_raw)
                return
            print(f"{subdomain!r} did not come up within the hold window", flush=True)

        # No wake configured, refused, or too slow: say so rather than hang.
        reason = json.dumps({"text": KICK_MESSAGE, "color": "red"})
        write_packet(sock, 0x00, write_string(reason))  # login disconnect


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            handle_connection(self.request)
        except (ConnectionError, socket.timeout, OSError, ValueError, IndexError):
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    _slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):  # type: ignore[no-untyped-def]
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):  # type: ignore[no-untyped-def]
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def main() -> None:
    with Server((LISTEN_HOST, LISTEN_PORT), Handler) as srv:
        print(f"offline-placeholder listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
