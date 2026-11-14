"""World names and states in /metrics stay off the games network."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

import pytest

from open_worlds.common.metrics import scrape_allowed, start_metrics_server

GAMES = "172.19.0.0/16"


@pytest.mark.parametrize(
    ("peer", "allowed"),
    [("172.19.0.7", False), ("172.19.255.53", False), ("172.20.0.4", True), (None, False)],
)
def test_who_may_scrape(peer, allowed):
    assert scrape_allowed(peer, GAMES) is allowed


@pytest.mark.parametrize(("peer", "code"), [("172.19.0.7", 403), ("172.20.0.4", 200)])
def test_internal_api_metrics(peer, code):
    from fastapi.testclient import TestClient

    from open_worlds.api.main import create_internal_app

    client = TestClient(create_internal_app(), client=(peer, 40000))
    assert client.get("/metrics").status_code == code
    # Only /metrics: the internal routes the routers call are untouched.
    assert client.get("/api/healthz").status_code == 200


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize(("subnet", "code"), [("127.0.0.0/8", 403), (GAMES, 200)])
def test_worker_metrics_server(subnet, code):
    port = _free_port()
    start_metrics_server(port, subnet)
    try:
        status = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == code
