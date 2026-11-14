#!/usr/bin/env python3
"""pal-waker: per-world UDP relay with wake-on-connect and idle sleep.

Palworld's protocol names no world, so each world owns a host port for life and
this service owns the whole range:

* **SLEEPING**: we listen on its port; a client's datagrams ask the control
  plane to wake it.
* **RUNNING**: traffic is relayed to its container, which publishes nothing to
  the host. Staying in the data path avoids racing Docker for the port.
* **OFF**: nothing is bound, so the connection fails; a stranger connecting
  cannot undo the owner's decision.

Design notes:

* **No Docker access.** The port map and wake/sleep go through the internal
  API with a shared secret.
* **Idle detection lives here**, the only place that sees the traffic: no
  datagrams for ``IDLE_SECONDS`` puts a world to sleep.
* **Sessions are keyed by client address**, each with its own upstream socket,
  so replies find the right player.
* **Stdlib only**: nothing to audit or keep up to date.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Defaults must match the platform's, or every relay dials a container that does
# not exist, silently. Compose sets both; these are for running standalone.
API_BASE = os.environ.get("API_BASE", "http://internal-api:8000/api/internal").rstrip("/")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")
# World containers are this prefix plus the subdomain
# (PLATFORM_CONTAINER_NAME_PREFIX), listening on the stock port.
CONTAINER_PREFIX = os.environ.get("CONTAINER_PREFIX", "game-world-")
GAME_CONTAINER_PORT = int(os.environ.get("GAME_CONTAINER_PORT", "8211"))
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")

POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "5"))
IDLE_SECONDS = float(os.environ.get("IDLE_SECONDS", "600"))
# After a wake, before the idle timer may fire: the server needs ~30-60s to
# accept connections.
WAKE_GRACE_SECONDS = float(os.environ.get("WAKE_GRACE_SECONDS", "180"))
# Idle client sessions are closed after this; well below IDLE_SECONDS.
SESSION_TTL_SECONDS = float(os.environ.get("SESSION_TTL_SECONDS", "120"))
# What it takes to wake a sleeping world. The internet sends lone probes to
# every open UDP port; a real client retries its handshake within seconds. So a
# wake needs WAKE_MIN_DATAGRAMS from one source within WAKE_WINDOW_SECONDS, and a
# port that asked for a wake will not again for WAKE_COOLDOWN_SECONDS, which
# bounds how often one port can start a server.
WAKE_MIN_DATAGRAMS = int(os.environ.get("WAKE_MIN_DATAGRAMS", "3"))
# A wake lost to a restarting control plane is retried briefly while the
# player is still knocking; a refusal is not.
WAKE_REQUEST_ATTEMPTS = int(os.environ.get("WAKE_REQUEST_ATTEMPTS", "3"))
WAKE_RETRY_SECONDS = float(os.environ.get("WAKE_RETRY_SECONDS", "1.5"))
WAKE_WINDOW_SECONDS = float(os.environ.get("WAKE_WINDOW_SECONDS", "10"))
WAKE_COOLDOWN_SECONDS = float(os.environ.get("WAKE_COOLDOWN_SECONDS", "300"))
# Distinct sources a sleeping port remembers between sweeps, bounded.
_WAKE_CANDIDATES_MAX = 256
# /sleep returns once the stop is recorded; /wake waits for the container to
# start, which on a fresh host includes pulling the image. A timeout only costs a
# log line: the world still starts and the next poll sees it.
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "120"))

# States we bind a socket for. Not STOPPING (relaying would feed a dying
# server; the port is rebound at SLEEPING), not OFF (the owner turned it off),
# and not ERROR: waking a world known to be broken would let one player keep it
# in a start-crash loop. Recovery is the owner's "Turn on", as for Minecraft.
_BINDABLE = frozenset({"RUNNING", "SLEEPING"})


def log(message: str, **fields: object) -> None:
    """One-line JSON logs, matching the platform's other services."""
    print(json.dumps({"message": message, **fields}), flush=True)


# --------------------------------------------------------------------------
# Control-plane client
# --------------------------------------------------------------------------


class _Refused(Exception):
    """The control plane answered, and the answer was no."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class _Unreachable(Exception):
    """The control plane did not answer. Usually it is restarting."""


def _request(method: str, path: str) -> object | None:
    req = urllib.request.Request(f"{API_BASE}{path}", method=method)
    if INTERNAL_SECRET:
        req.add_header("X-Internal-Secret", INTERNAL_SECRET)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
        return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        # 409 is normal: the world changed state between our poll and this call.
        # A refusal is an answer, so callers should not retry it.
        log("control plane rejected call", path=path, status=exc.code)
        raise _Refused(exc.code) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # No answer at all. Almost always the control plane restarting, which
        # takes seconds -- worth retrying rather than dropping on the floor.
        log("control plane unreachable", path=path, error=str(exc))
        raise _Unreachable(str(exc)) from exc


def fetch_targets() -> dict[int, tuple[str, str, float | None]] | None:
    """Return ``{port: (subdomain, state, players_idle_seconds)}``, or None if
    the API is unreachable."""
    try:
        data = _request("GET", "/wake-targets")
    except (_Refused, _Unreachable):
        # None means "do not reconcile": a control-plane blip must not tear down
        # the ports of worlds people are playing on right now.
        return None
    if not isinstance(data, dict):
        return None
    out: dict[int, tuple[str, str, float | None]] = {}
    for row in data.get("targets") or []:
        try:
            idle = row.get("players_idle_seconds")
            out[int(row["game_port"])] = (
                str(row["subdomain"]),
                str(row["state"]),
                None if idle is None else float(idle),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------
# Per-world relay
# --------------------------------------------------------------------------


@dataclass
class _Session:
    """One client's flow: its upstream socket and when we last saw it."""

    transport: asyncio.DatagramTransport
    last_seen: float


@dataclass
class WorldRelay(asyncio.DatagramProtocol):
    """Listens on one world's host port and relays to that world's container."""

    subdomain: str
    port: int
    state: str
    waker: Waker
    transport: asyncio.DatagramTransport | None = None
    sessions: dict[tuple[str, int], _Session] = field(default_factory=dict)
    last_traffic: float = field(default_factory=time.monotonic)
    # Set when we ask for a wake, so we neither ask twice nor let the idle timer
    # fire while the server is still booting.
    waking_since: float | None = None
    # When this port last asked for a wake, kept across state changes so the
    # cooldown survives the world coming up and going back to sleep.
    last_wake_request: float | None = None
    # Recent datagram times per source address while the world is not running;
    # a source that reaches WAKE_MIN_DATAGRAMS within the window is a client.
    wake_candidates: dict[tuple[str, int], deque[float]] = field(default_factory=dict)
    # How long the server has reported no players, as of the last poll; None
    # when the control plane does not know. Trusted over traffic when known:
    # a spoofed datagram reaches the relay, but never shows up as a player.
    players_idle: float | None = None

    # --- asyncio callbacks ---

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.last_traffic = time.monotonic()
        if self.state != "RUNNING":
            # Wake it if this looks like a client, and drop the packet either
            # way: the client retries into the live server.
            if self._looks_like_a_client(addr):
                self._request_wake()
            return
        session = self.sessions.get(addr)
        if session is None:
            self.waker.spawn(self._open_session(data, addr))
            return
        session.last_seen = self.last_traffic
        try:
            session.transport.sendto(data)
        except OSError as exc:
            log("upstream send failed", subdomain=self.subdomain, error=str(exc))
            self._close_session(addr)

    # --- relay plumbing ---

    async def _open_session(self, first: bytes, addr: tuple[str, int]) -> None:
        """Create the upstream socket for a client we haven't seen before."""
        if addr in self.sessions:  # a second datagram raced us here
            return
        loop = asyncio.get_running_loop()
        upstream_host = f"{CONTAINER_PREFIX}{self.subdomain}"
        try:
            transport, _protocol = await loop.create_datagram_endpoint(
                lambda: _UpstreamProtocol(self, addr),
                remote_addr=(upstream_host, GAME_CONTAINER_PORT),
            )
        except OSError as exc:
            # The container may still be booting; the client will retry.
            log("upstream connect failed", subdomain=self.subdomain, error=str(exc))
            return
        if addr in self.sessions:  # lost the race after the await
            transport.close()
            return
        self.sessions[addr] = _Session(transport=transport, last_seen=time.monotonic())
        transport.sendto(first)

    def deliver_to_client(self, data: bytes, addr: tuple[str, int]) -> None:
        """Called by the upstream protocol with a reply for one client."""
        self.last_traffic = time.monotonic()
        session = self.sessions.get(addr)
        if session is not None:
            session.last_seen = self.last_traffic
        if self.transport is not None:
            self.transport.sendto(data, addr)

    def _close_session(self, addr: tuple[str, int]) -> None:
        session = self.sessions.pop(addr, None)
        if session is not None:
            session.transport.close()

    def close(self) -> None:
        """Drop every client session and give up the host port.

        The transport's socket is closed by the event loop on its next pass, so
        a caller that needs to rebind this port must yield first.
        """
        for addr in list(self.sessions):
            self._close_session(addr)
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    # --- lifecycle decisions ---

    def _looks_like_a_client(self, addr: tuple[str, int]) -> bool:
        """Whether ``addr`` has now sent enough datagrams, fast enough, to be a
        player rather than a scanner. Records this datagram as a side effect."""
        now = time.monotonic()
        hits = self.wake_candidates.get(addr)
        if hits is None:
            if len(self.wake_candidates) >= _WAKE_CANDIDATES_MAX:
                # Under a wide scan; forget everyone rather than grow. A real
                # client's retries will refill its entry within the window.
                self.wake_candidates.clear()
            hits = self.wake_candidates[addr] = deque()
        hits.append(now)
        while hits and now - hits[0] > WAKE_WINDOW_SECONDS:
            hits.popleft()
        return len(hits) >= WAKE_MIN_DATAGRAMS

    def _request_wake(self) -> None:
        now = time.monotonic()
        if self.waking_since is not None and now - self.waking_since < WAKE_GRACE_SECONDS:
            return  # already asked; give the server time to boot
        if (
            self.last_wake_request is not None
            and now - self.last_wake_request < WAKE_COOLDOWN_SECONDS
        ):
            return  # this port started a server recently; not again yet
        self.waking_since = now
        self.last_wake_request = now
        self.wake_candidates.clear()
        log("waking world on connect", subdomain=self.subdomain, port=self.port)
        self.waker.spawn(self._wake_and_settle())

    async def _wake_and_settle(self) -> None:
        """Ask for the wake, and give the state back if it never landed.

        The grace and cooldown are set before the call to stop a second request
        racing it. On failure they are cleared so the next datagram retries; a
        refusal (409) is a real answer and is left alone.
        """
        if await self.waker.call_wake(self.subdomain):
            return
        self.waking_since = None
        self.last_wake_request = None

    def sweep(self, now: float) -> None:
        """Drop stale client sessions and sleep the world if nothing is using it."""
        for addr, session in list(self.sessions.items()):
            if now - session.last_seen > SESSION_TTL_SECONDS:
                self._close_session(addr)
        for addr, hits in list(self.wake_candidates.items()):
            if not hits or now - hits[-1] > WAKE_WINDOW_SECONDS:
                del self.wake_candidates[addr]

        if self.state != "RUNNING":
            return
        if self.waking_since is not None and now - self.waking_since < WAKE_GRACE_SECONDS:
            return
        if self.players_idle is not None:
            if self.players_idle < IDLE_SECONDS:
                return
        elif now - self.last_traffic < IDLE_SECONDS:
            return
        log("sleeping idle world", subdomain=self.subdomain, port=self.port)
        self.last_traffic = now  # don't ask again on the next sweep
        self.players_idle = None
        self.waker.spawn(self.waker.call_sleep(self.subdomain))


class _UpstreamProtocol(asyncio.DatagramProtocol):
    """Receives the container's replies and hands them back to one client."""

    def __init__(self, relay: WorldRelay, client: tuple[str, int]) -> None:
        self._relay = relay
        self._client = client

    def datagram_received(self, data: bytes, _addr: tuple[str, int]) -> None:
        self._relay.deliver_to_client(data, self._client)

    def error_received(self, exc: Exception) -> None:
        # ICMP port-unreachable while the server boots is expected and noisy.
        log("upstream error", subdomain=self._relay.subdomain, error=str(exc))


# --------------------------------------------------------------------------
# Supervisor
# --------------------------------------------------------------------------


class Waker:
    """Keeps the set of bound ports reconciled against the control plane."""

    def __init__(self) -> None:
        self.relays: dict[int, WorldRelay] = {}
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> None:
        """Run a coroutine in the background, keeping a reference to it."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def call_wake(self, subdomain: str) -> bool:
        """Ask for a wake. True if the control plane took it.

        Retried for a restarting control plane; a 409 (OFF, or the host is
        full) is not.
        """
        for attempt in range(WAKE_REQUEST_ATTEMPTS):
            try:
                await asyncio.to_thread(_request, "POST", f"/wake/{subdomain}")
                return True
            except _Refused:
                return False
            except _Unreachable:
                if attempt + 1 < WAKE_REQUEST_ATTEMPTS:
                    await asyncio.sleep(WAKE_RETRY_SECONDS)
        log("wake never reached the control plane", subdomain=subdomain)
        return False

    async def call_sleep(self, subdomain: str) -> None:
        try:
            await asyncio.to_thread(_request, "POST", f"/sleep/{subdomain}")
        except (_Refused, _Unreachable):
            # Nothing to undo: the next reconcile sees the real state, and a
            # world left running is corrected by the idle sweep.
            pass

    async def reconcile(self) -> None:
        """Bind/unbind sockets so they match the control plane's port map."""
        targets = await asyncio.to_thread(fetch_targets)
        if targets is None:
            # Leave existing relays alone: a control-plane blip must not tear
            # down the ports of worlds people are playing on right now.
            return

        wanted = {p: v for p, v in targets.items() if v[1] in _BINDABLE}

        # Release first, bind second, and let the loop actually run the close
        # callbacks in between. asyncio closes a datagram transport via
        # call_soon, so a port freed earlier in this pass is still held by the
        # kernel until we yield -- without this, a world whose port was
        # reassigned could not be rebound here and would stay unreachable.
        released = False
        for port, relay in list(self.relays.items()):
            target = wanted.get(port)
            if target is None:
                log("releasing port", port=port, subdomain=relay.subdomain)
            elif target[0] != relay.subdomain:
                # The port was reassigned to a different world; rebuild it so no
                # in-flight session can leak across worlds.
                log("port reassigned", port=port, old=relay.subdomain, new=target[0])
            else:
                state = target[1]
                relay.players_idle = target[2] if len(target) > 2 else None
                if relay.state != state:
                    log("world state changed", subdomain=relay.subdomain, state=state)
                    if state == "RUNNING":
                        # The wake we asked for landed: stop suppressing wakes
                        # and start the idle clock from now.
                        relay.waking_since = None
                        relay.last_traffic = time.monotonic()
                relay.state = state
                continue
            self.relays.pop(port).close()
            released = True

        if released:
            await asyncio.sleep(0)

        loop = asyncio.get_running_loop()
        for port, (subdomain, state, *idle) in sorted(wanted.items()):
            if port in self.relays:
                continue
            relay = WorldRelay(subdomain=subdomain, port=port, state=state, waker=self)
            relay.players_idle = idle[0] if idle else None
            try:
                await loop.create_datagram_endpoint(
                    lambda r=relay: r, local_addr=(LISTEN_HOST, port)
                )
            except OSError as exc:
                # Next poll retries; a port we cannot bind is a port no player
                # can reach, so it is worth saying out loud.
                log("could not bind port", port=port, subdomain=subdomain, error=str(exc))
                continue
            self.relays[port] = relay
            log("listening", port=port, subdomain=subdomain, state=state)

    def sweep(self) -> None:
        now = time.monotonic()
        for relay in list(self.relays.values()):
            relay.sweep(now)

    async def run(self) -> None:
        log(
            "pal-waker starting",
            api=API_BASE,
            idle_seconds=IDLE_SECONDS,
            poll_seconds=POLL_SECONDS,
        )
        while True:
            try:
                await self.reconcile()
                self.sweep()
            except Exception as exc:  # never let one bad pass kill the loop
                log("reconcile failed", error=str(exc))
            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    try:
        asyncio.run(Waker().run())
    except KeyboardInterrupt:
        log("pal-waker shutting down")


if __name__ == "__main__":
    main()
