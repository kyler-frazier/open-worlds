"""Minimal Source RCON client for talking to Minecraft servers.

We implement the tiny binary protocol directly rather than pull in a dependency:
it is a few dozen lines and keeps the supply chain small. Minecraft's admin
protocol; reached through the game adapter, which wraps it in the platform's
shared ``AdminClient`` interface alongside Palworld's REST equivalent.
"""

from __future__ import annotations

import socket
import struct

_SERVERDATA_AUTH = 3
_SERVERDATA_AUTH_RESPONSE = 2
_SERVERDATA_EXECCOMMAND = 2
_SERVERDATA_RESPONSE_VALUE = 0
# Request id, type and the two trailing NULs.
_MIN_PACKET = 10
_MAX_PACKET = 64 * 1024


class RconError(Exception):
    pass


class RconAuthError(RconError):
    pass


class RconClient:
    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._req_id = 0

    def __enter__(self) -> RconClient:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        # Socket failures are RconError like the protocol's own, so callers
        # need one except clause. Refused is the normal answer while a server
        # boots, and escaping as OSError it was logged as an error every poll.
        try:
            self._sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
            self._sock.settimeout(self._timeout)
            self._authenticate()
        except OSError as exc:
            self.close()
            raise RconError(f"Could not reach RCON at {self._host}:{self._port}: {exc}") from exc

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _send(self, packet_type: int, body: str) -> int:
        assert self._sock is not None
        req_id = self._next_id()
        payload = struct.pack("<ii", req_id, packet_type) + body.encode("utf-8") + b"\x00\x00"
        self._sock.sendall(struct.pack("<i", len(payload)) + payload)
        return req_id

    def _recv(self) -> tuple[int, int, str]:
        assert self._sock is not None
        raw_len = self._read_exact(4)
        (length,) = struct.unpack("<i", raw_len)
        # The length comes from the world, which is untrusted: bound it before
        # reading. A server splits long output into packets of 4096 or less.
        if not _MIN_PACKET <= length <= _MAX_PACKET:
            raise RconError(f"RCON packet length {length} is out of range.")
        data = self._read_exact(length)
        req_id, packet_type = struct.unpack("<ii", data[:8])
        body = data[8:-2].decode("utf-8", errors="replace")
        return req_id, packet_type, body

    def _read_exact(self, n: int) -> bytes:
        assert self._sock is not None
        chunks = bytearray()
        while len(chunks) < n:
            chunk = self._sock.recv(n - len(chunks))
            if not chunk:
                raise RconError("Connection closed by server.")
            chunks.extend(chunk)
        return bytes(chunks)

    def _authenticate(self) -> None:
        sent_id = self._send(_SERVERDATA_AUTH, self._password)
        req_id, _type, _body = self._recv()
        # Some servers emit an empty RESPONSE_VALUE before the AUTH_RESPONSE.
        if req_id == -1:
            raise RconAuthError("RCON authentication failed.")
        if req_id != sent_id:
            req_id, _type, _body = self._recv()
            if req_id == -1:
                raise RconAuthError("RCON authentication failed.")

    def command(self, cmd: str) -> str:
        try:
            self._send(_SERVERDATA_EXECCOMMAND, cmd)
            _req_id, _type, body = self._recv()
        except OSError as exc:
            raise RconError(f"RCON connection failed: {exc}") from exc
        return body
