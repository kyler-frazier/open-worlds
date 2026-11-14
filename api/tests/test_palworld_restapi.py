"""The world answers the admin API, so its replies are untrusted: a reply too
large to be real is an error, not something to buffer."""

from __future__ import annotations

import httpx2
import pytest

from open_worlds.common.games.palworld import restapi
from open_worlds.common.games.palworld.restapi import PalApiClient, PalApiError


def _client(body: bytes) -> PalApiClient:
    transport = httpx2.MockTransport(lambda _req: httpx2.Response(200, content=body))
    return PalApiClient("world", 8212, "pw", timeout=2, transport=transport)


def test_a_normal_reply_is_read():
    assert _client(b'{"version": "v1"}').info() == {"version": "v1"}


def test_an_oversized_reply_is_refused(monkeypatch):
    monkeypatch.setattr(restapi, "MAX_RESPONSE_BYTES", 1024)
    with pytest.raises(PalApiError):
        _client(b'{"x": "' + b"a" * 4096 + b'"}').info()
