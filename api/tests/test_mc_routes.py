"""Publishing Minecraft routes, so mc-router needs no Docker credential.

mc-router can find worlds itself by watching container labels, but that costs it
the ability to create containers -- which is the ability to be root on the host.
The platform already knows every world, so it tells mc-router instead.
"""

from __future__ import annotations

import http.client

import pytest

from open_worlds.common.games.minecraft import routing as routes


class _Db:
    """Stands in for a Session; desired_routes only ever executes one select."""

    def __init__(self, worlds):
        self._worlds = worlds

    def execute(self, _stmt):
        class _R:
            def __init__(self, w):
                self._w = w

            def scalars(self):
                return iter(self._w)

        return _R(self._worlds)


class _World:
    def __init__(self, subdomain, game, state):
        self.subdomain = subdomain
        self.game = game
        self.state = state


def test_a_change_mc_router_does_not_answer_is_not_a_failure(monkeypatch):
    """mc-router applies a route write and closes without responding.

    Reading that as an error logged a warning on every publish while the route
    sat there working, and would have masked a real failure in the noise.
    """
    calls = []

    def fake_api(path, method="GET", payload=None):
        calls.append((path, method))
        raise http.client.RemoteDisconnected("closed")

    monkeypatch.setattr(routes, "_api", fake_api)
    routes._apply("/routes", "POST", {"serverAddress": "a", "backend": "b"})
    assert calls == [("/routes", "POST")]


def test_a_real_transport_failure_still_raises(monkeypatch):
    def fake_api(path, method="GET", payload=None):
        raise OSError("connection refused")

    monkeypatch.setattr(routes, "_api", fake_api)
    with pytest.raises(OSError):
        routes._apply("/routes", "POST", {})


def test_routes_exist_only_while_a_world_is_running(monkeypatch):
    """The whole rule, in one assertion.

    Anything not RUNNING must have no route, so it falls through to mc-router's
    DEFAULT -- the offline-placeholder -- which is what holds a joining player
    while the world wakes. A route pointing at a stopped container would give
    the player a refused connection with nothing able to explain it.
    """
    from open_worlds.common.models import WorldState

    monkeypatch.setattr(
        routes, "get_settings", lambda: type("S", (), {"servers_domain": "servers.test"})()
    )
    db = _Db(
        [
            _World("live", "MINECRAFT", WorldState.RUNNING),
        ]
    )
    got = routes.desired_routes(db)
    assert got == {"live.servers.test": "game-world-live:25565"}


def test_publishing_is_skipped_when_no_router_is_configured(monkeypatch):
    """A dev run without mc-router should not spend a request every 20 seconds."""
    monkeypatch.setattr(
        routes, "minecraft_config", lambda: type("S", (), {"mc_router_api_url": ""})()
    )
    assert routes.sync_routes(_Db([])) == 0
