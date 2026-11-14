"""RCON transport failures must surface as RconError, and so as AdminUnavailable.

A refused connection is normal while a server boots and must stay quiet. Real
sockets, since the adapter tests fake AdminUnavailable.
"""

from __future__ import annotations

import socket

import pytest

from open_worlds.common.games import AdminUnavailable, adapter_for
from open_worlds.common.games.minecraft.rcon import RconClient, RconError


def _closed_port() -> int:
    """A local port with nothing listening on it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_connect_refused_is_rcon_error():
    client = RconClient("127.0.0.1", _closed_port(), "pw", timeout=2)
    with pytest.raises(RconError):
        client.connect()
    # Nothing half-open is left behind for close() to trip over.
    assert client._sock is None


def test_unresolvable_host_is_rcon_error():
    client = RconClient("game-world-does-not-exist.invalid", 25575, "pw", timeout=2)
    with pytest.raises(RconError):
        client.connect()


def test_socket_error_mid_command_is_rcon_error():
    class _Reset:
        def sendall(self, _data):
            raise ConnectionResetError("reset by peer")

        def close(self):
            pass

    client = RconClient("127.0.0.1", 25575, "pw")
    client._sock = _Reset()
    with pytest.raises(RconError):
        client.command("list")


def test_minecraft_admin_client_reports_unavailable_while_booting():
    admin = adapter_for("MINECRAFT").admin_client("127.0.0.1", "pw")
    admin._rcon._port = _closed_port()
    admin._rcon._timeout = 2
    try:
        with pytest.raises(AdminUnavailable):
            admin.players()
        with pytest.raises(AdminUnavailable):
            admin.save_flush()
    finally:
        admin.close()


@pytest.mark.parametrize("length", [2**31 - 1, 3, -5])
def test_a_length_the_world_made_up_is_refused_before_reading(length):
    a, b = socket.socketpair()
    try:
        client = RconClient("127.0.0.1", 25575, "pw")
        client._sock = b
        a.sendall(length.to_bytes(4, "little", signed=True))
        with pytest.raises(RconError, match="out of range"):
            client._recv()
    finally:
        a.close()
        b.close()
