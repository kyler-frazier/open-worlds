import socket
import threading

import pytest
import server


def _pair():
    a, b = socket.socketpair()
    a.settimeout(2)
    b.settimeout(2)
    return a, b


def test_a_huge_length_prefix_is_refused_before_it_is_buffered():
    client, srv = _pair()
    # 2^34: the prefix of a 16 GiB packet, followed by nothing.
    client.sendall(server.encode_varint(2**34))
    with pytest.raises(ValueError, match="limit"):
        server.read_packet(srv)
    client.sendall(server.encode_varint(2**34))
    with pytest.raises(ValueError, match="limit"):
        server.read_packet_raw(srv)


def test_a_normal_packet_still_reads():
    client, srv = _pair()
    body = server.encode_varint(0) + server.write_string("x" * 300)
    client.sendall(server.encode_varint(len(body)) + body)
    buf = server.read_packet(srv)
    assert buf.read_varint() == 0 and buf.read_string() == "x" * 300


def test_connections_past_the_limit_are_closed_at_once(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(server, "handle_connection", lambda sock: release.wait(5))
    monkeypatch.setattr(server.Server, "_slots", threading.BoundedSemaphore(1))
    with server.Server(("127.0.0.1", 0), server.Handler) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        addr = srv.server_address
        held = socket.create_connection(addr, timeout=2)
        try:
            # Let the first one take the only slot.
            for _ in range(50):
                if srv._slots._value == 0:
                    break
                threading.Event().wait(0.02)
            extra = socket.create_connection(addr, timeout=2)
            assert extra.recv(1) == b""  # closed without being served
            extra.close()
        finally:
            release.set()
            held.close()
            srv.shutdown()
