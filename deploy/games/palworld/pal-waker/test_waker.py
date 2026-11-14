"""Tests for the UDP relay, wake trigger and idle sleep.

The relay is the one genuinely new component in the Palworld fork, and it is the
piece with no equivalent to fall back on, so these drive it over real sockets
rather than mocking the transport away.
"""

from __future__ import annotations

import asyncio
import socket
import time

import pytest

import waker as w


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Echo(asyncio.DatagramProtocol):
    """Stands in for a world container: echoes every datagram back."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.received.append(data)
        assert self.transport is not None
        self.transport.sendto(b"pong:" + data, addr)


@pytest.fixture
def calls(monkeypatch):
    """Capture wake/sleep calls instead of hitting the control plane."""
    made: list[tuple[str, str]] = []

    async def fake_wake(_self, subdomain):
        made.append(("wake", subdomain))

    async def fake_sleep(_self, subdomain):
        made.append(("sleep", subdomain))

    monkeypatch.setattr(w.Waker, "call_wake", fake_wake)
    monkeypatch.setattr(w.Waker, "call_sleep", fake_sleep)
    return made


@pytest.fixture
def local_upstream(monkeypatch):
    """Point the relay's upstream at loopback instead of a container name."""
    monkeypatch.setattr(w, "CONTAINER_PREFIX", "")
    return "127.0.0.1"


async def _client_exchange(port: int, payload: bytes, timeout: float = 2.0) -> bytes | None:
    """Send one datagram to the relay and wait for a reply, if any."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        await loop.sock_sendto(sock, payload, ("127.0.0.1", port))
        try:
            data = await asyncio.wait_for(loop.sock_recv(sock, 4096), timeout)
            return data
        except asyncio.TimeoutError:
            return None
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_relays_traffic_both_ways_for_a_running_world(monkeypatch, calls, local_upstream):
    loop = asyncio.get_running_loop()
    upstream_port = _free_port()
    _t, echo = await loop.create_datagram_endpoint(
        _Echo, local_addr=("127.0.0.1", upstream_port)
    )
    monkeypatch.setattr(w, "GAME_CONTAINER_PORT", upstream_port)

    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(
        w, "fetch_targets", lambda: {listen_port: (local_upstream, "RUNNING")}
    )
    await waker.reconcile()

    reply = await _client_exchange(listen_port, b"hello")
    assert reply == b"pong:hello"
    assert echo.received == [b"hello"]
    # A running world is never woken -- it is already up.
    assert calls == []
    waker.relays[listen_port].close()


@pytest.mark.asyncio
async def test_a_single_probe_does_not_wake_a_sleeping_world(monkeypatch, calls):
    # The public internet sends one datagram to every open UDP port all day.
    # One packet used to start an 8 GB server; it must not.
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "SLEEPING")})
    await waker.reconcile()

    assert await _client_exchange(listen_port, b"probe", timeout=0.2) is None
    await asyncio.sleep(0.05)
    assert calls == []
    waker.relays[listen_port].close()


@pytest.mark.asyncio
async def test_a_client_retrying_its_handshake_wakes_the_world(monkeypatch, calls):
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "SLEEPING")})
    await waker.reconcile()

    # One socket, several datagrams: what a game client does while connecting.
    # No reply is expected -- the server isn't up yet, so the datagrams are
    # dropped and the client retries into a live server once it is.
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        for _ in range(w.WAKE_MIN_DATAGRAMS):
            await loop.sock_sendto(sock, b"join", ("127.0.0.1", listen_port))
            await asyncio.sleep(0.02)
    finally:
        sock.close()
    await asyncio.sleep(0.05)
    assert calls == [("wake", "demo")]
    waker.relays[listen_port].close()


@pytest.mark.asyncio
async def test_probes_from_many_sources_do_not_add_up_to_a_client(monkeypatch, calls):
    # A scan sweeping many addresses across the port must not look like one
    # player retrying. Each _client_exchange uses a fresh source port.
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "SLEEPING")})
    await waker.reconcile()

    for _ in range(w.WAKE_MIN_DATAGRAMS + 2):
        await _client_exchange(listen_port, b"probe", timeout=0.05)
    await asyncio.sleep(0.05)
    assert calls == []
    waker.relays[listen_port].close()


@pytest.mark.asyncio
async def test_a_port_will_not_wake_again_during_its_cooldown(monkeypatch, calls):
    # Waking is the expensive act; a port that just did it waits before doing
    # it again, whoever is knocking, so a sleep/wake loop cannot be driven from
    # outside faster than the cooldown allows.
    monkeypatch.setattr(w, "WAKE_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(w, "WAKE_COOLDOWN_SECONDS", 60.0)
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "SLEEPING")})
    await waker.reconcile()
    relay = waker.relays[listen_port]

    relay.last_wake_request = time.monotonic() - 1.0  # woke a second ago
    for _ in range(w.WAKE_MIN_DATAGRAMS):
        relay.datagram_received(b"join", ("10.0.0.1", 4444))
    await asyncio.sleep(0.05)
    assert calls == []

    relay.last_wake_request = time.monotonic() - 61.0  # cooldown over
    for _ in range(w.WAKE_MIN_DATAGRAMS):
        relay.datagram_received(b"join", ("10.0.0.1", 4444))
    await asyncio.sleep(0.05)
    assert calls == [("wake", "demo")]
    relay.close()


@pytest.mark.asyncio
async def test_repeated_packets_do_not_stack_wake_requests(monkeypatch, calls):
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "SLEEPING")})
    await waker.reconcile()

    # A game client retries hard while connecting; one wake must be enough.
    relay = waker.relays[listen_port]
    for _ in range(w.WAKE_MIN_DATAGRAMS * 3):
        relay.datagram_received(b"join", ("10.0.0.1", 4444))
    await asyncio.sleep(0.05)
    assert calls == [("wake", "demo")]
    relay.close()


@pytest.mark.asyncio
async def test_off_worlds_are_not_bound_at_all(monkeypatch, calls):
    # Binding an OFF world's port would let a stranger's connection undo the
    # owner's decision to turn it off.
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "OFF")})
    await waker.reconcile()
    assert waker.relays == {}


@pytest.mark.asyncio
async def test_errored_worlds_are_not_bound_at_all(monkeypatch, calls):
    # An ERROR world's container exited non-gracefully, so waking it just retries
    # something already known to be broken. Binding its port meant every join
    # attempt restarted it, letting one player hold a broken world in a
    # start-crash loop. Recovery is the owner's "Turn on" instead.
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "ERROR")})
    await waker.reconcile()
    assert waker.relays == {}
    assert calls == []


@pytest.mark.asyncio
async def test_a_world_that_errors_while_bound_releases_its_port(monkeypatch, calls):
    # The transition matters as much as the initial state: a world that crashes
    # mid-session must stop being relayed rather than keep a dead socket bound.
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "RUNNING")})
    await waker.reconcile()
    assert listen_port in waker.relays

    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "ERROR")})
    await waker.reconcile()
    assert waker.relays == {}


@pytest.mark.asyncio
async def test_idle_world_is_put_to_sleep(monkeypatch, calls):
    monkeypatch.setattr(w, "IDLE_SECONDS", 0.1)
    monkeypatch.setattr(w, "WAKE_GRACE_SECONDS", 0.0)
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "RUNNING")})
    await waker.reconcile()

    waker.sweep()
    assert calls == []  # not idle yet

    waker.relays[listen_port].last_traffic = time.monotonic() - 1.0
    waker.sweep()
    await asyncio.sleep(0.05)
    assert calls == [("sleep", "demo")]
    waker.relays[listen_port].close()


@pytest.mark.asyncio
async def test_a_freshly_woken_world_is_not_slept_during_its_boot_window(monkeypatch, calls):
    # The server takes ~30-60s to accept connections. Without the grace period a
    # woken world would be stopped again before anyone could join it.
    monkeypatch.setattr(w, "IDLE_SECONDS", 0.0)
    monkeypatch.setattr(w, "WAKE_GRACE_SECONDS", 60.0)
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "RUNNING")})
    await waker.reconcile()

    waker.relays[listen_port].waking_since = time.monotonic()
    waker.relays[listen_port].last_traffic = time.monotonic() - 100
    waker.sweep()
    await asyncio.sleep(0.05)
    assert calls == []
    waker.relays[listen_port].close()


@pytest.mark.asyncio
async def test_deleted_world_releases_its_port(monkeypatch, calls):
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "SLEEPING")})
    await waker.reconcile()
    assert listen_port in waker.relays

    monkeypatch.setattr(w, "fetch_targets", dict)
    await waker.reconcile()
    assert waker.relays == {}
    # The port is genuinely free again, not merely forgotten.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", listen_port))


@pytest.mark.asyncio
async def test_reassigned_port_is_rebuilt_not_reused(monkeypatch, calls):
    # A recycled port must not carry a previous world's in-flight sessions.
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("old", "SLEEPING")})
    await waker.reconcile()
    first = waker.relays[listen_port]

    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("new", "SLEEPING")})
    await waker.reconcile()
    assert waker.relays[listen_port] is not first
    assert waker.relays[listen_port].subdomain == "new"
    waker.relays[listen_port].close()


@pytest.mark.asyncio
async def test_control_plane_outage_leaves_live_ports_bound(monkeypatch, calls):
    # A blip in the API must not tear down the ports of worlds people are
    # playing on right now.
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "RUNNING")})
    await waker.reconcile()
    assert listen_port in waker.relays

    monkeypatch.setattr(w, "fetch_targets", lambda: None)
    await waker.reconcile()
    assert listen_port in waker.relays
    waker.relays[listen_port].close()


# --------------------------------------------------------------------------
# A wake lost to a restarting control plane
# --------------------------------------------------------------------------


class _FakeWaker:
    """Stands in for the Waker, recording wake attempts and their outcome."""

    def __init__(self, results):
        self._results = list(results)
        self.attempts = 0
        self.spawned = []

    async def call_wake(self, subdomain):
        self.attempts += 1
        return self._results.pop(0) if self._results else False

    def spawn(self, coro):
        self.spawned.append(asyncio.ensure_future(coro))


def _relay(waker):
    relay = w.WorldRelay.__new__(w.WorldRelay)
    relay.subdomain = "demo"
    relay.port = 8211
    relay.waker = waker
    relay.waking_since = None
    relay.last_wake_request = None
    relay.wake_candidates = {}
    relay.sessions = {}
    return relay


@pytest.mark.asyncio
async def test_a_wake_that_never_landed_does_not_burn_the_cooldown():
    """The bug this exists for.

    Grace and cooldown are set before the request, to stop a second wake racing
    the first. They were kept even when the request failed -- so a wake lost to
    a control plane that was restarting cost the player the full cooldown *and*
    a fresh round of datagrams before anything tried again. The world stayed
    asleep and nothing said why.
    """
    waker = _FakeWaker([False])
    relay = _relay(waker)

    relay._request_wake()
    await asyncio.gather(*waker.spawned)

    assert waker.attempts == 1
    # Released, so the very next datagram can try again.
    assert relay.waking_since is None
    assert relay.last_wake_request is None


@pytest.mark.asyncio
async def test_a_wake_that_landed_keeps_the_cooldown():
    """The other half: a successful wake must still suppress a second one, or
    every datagram during a 60s boot would ask again."""
    waker = _FakeWaker([True])
    relay = _relay(waker)

    relay._request_wake()
    await asyncio.gather(*waker.spawned)

    assert relay.waking_since is not None
    assert relay.last_wake_request is not None


@pytest.mark.asyncio
async def test_an_unreachable_control_plane_is_retried(monkeypatch):
    """A restart lasts seconds while the player is still knocking."""
    calls = []

    def fake_request(method, path):
        calls.append(path)
        if len(calls) < 3:
            raise w._Unreachable("connection refused")
        return {}

    monkeypatch.setattr(w, "_request", fake_request)
    monkeypatch.setattr(w, "WAKE_RETRY_SECONDS", 0)
    waker = w.Waker.__new__(w.Waker)

    assert await waker.call_wake("demo") is True
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_a_refusal_is_not_retried(monkeypatch):
    """409 means the world is OFF or the host is full. Asking four times will
    not change the answer, and it is the control plane that pays for it."""
    calls = []

    def fake_request(method, path):
        calls.append(path)
        raise w._Refused(409)

    monkeypatch.setattr(w, "_request", fake_request)
    waker = w.Waker.__new__(w.Waker)

    assert await waker.call_wake("demo") is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_spoofed_traffic_does_not_keep_an_empty_world_awake(monkeypatch, calls):
    """UDP sources are forgeable; the server's own player count is not. While
    the control plane knows the count, traffic does not reset the idle clock."""
    monkeypatch.setattr(w, "IDLE_SECONDS", 600)
    monkeypatch.setattr(w, "WAKE_GRACE_SECONDS", 0.0)
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "RUNNING", 900.0)})
    await waker.reconcile()

    waker.relays[listen_port].last_traffic = time.monotonic()  # a packet just now
    waker.sweep()
    await asyncio.sleep(0.05)
    assert calls == [("sleep", "demo")]
    waker.relays[listen_port].close()


@pytest.mark.asyncio
async def test_a_world_with_players_is_not_slept_however_quiet(monkeypatch, calls):
    monkeypatch.setattr(w, "IDLE_SECONDS", 0.1)
    monkeypatch.setattr(w, "WAKE_GRACE_SECONDS", 0.0)
    waker = w.Waker()
    listen_port = _free_port()
    monkeypatch.setattr(w, "fetch_targets", lambda: {listen_port: ("demo", "RUNNING", 0.0)})
    await waker.reconcile()

    waker.relays[listen_port].last_traffic = time.monotonic() - 1.0
    waker.sweep()
    await asyncio.sleep(0.05)
    assert calls == []
    waker.relays[listen_port].close()
