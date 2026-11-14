"""Turning a world off, through the route and against a real database.

Turning off a sleeping world is only a removal, so the route finishes it and
answers OFF. The world still reads STOPPING until the container is gone, so a
start in the meantime is refused.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from tests.conftest_routes import CSRF, login, logout

pytest_plugins = ["tests.conftest_routes"]


class _Orch:
    """Records Docker calls; ``stop`` can be held open to make it slow."""

    def __init__(self, hold: bool = False):
        self.stopped: list[str] = []
        self.release = threading.Event()
        if not hold:
            self.release.set()

    def stop(self, subdomain: str, graceful=True) -> None:
        self.release.wait(10)
        self.stopped.append(subdomain)

    def list_managed(self):
        return []


@pytest.fixture
def owner_client(app_client, make_user, monkeypatch):
    client, db_module = app_client
    owner = make_user()
    logout(client)
    login(client, owner)
    return client, db_module, owner


def _use(monkeypatch, orch: _Orch) -> None:
    from open_worlds.api import deps

    monkeypatch.setattr(deps, "_orchestrator", orch)


def _make_world(db_module, owner_id: str, state: str) -> tuple[str, str]:
    from open_worlds.common.models import World, WorldState

    with db_module.session_scope() as db:
        w = World(
            id=uuid.uuid4(),
            owner_id=uuid.UUID(owner_id),
            subdomain=f"stop{uuid.uuid4().hex[:12]}",
            display_name="Stop me",
            game="MINECRAFT",
            game_settings={"server_type": "VANILLA", "version": "1.21.1"},
            state=WorldState(state),
            memory_mb=2048,
            max_players=10,
        )
        db.add(w)
        db.flush()
        return str(w.id), w.subdomain


def _state(db_module, world_id: str) -> str:
    from open_worlds.common.models import World

    with db_module.session_scope() as db:
        return db.get(World, uuid.UUID(world_id)).state.value


def test_turning_off_a_sleeping_world_answers_off(owner_client, monkeypatch):
    client, db_module, owner = owner_client
    orch = _Orch()
    _use(monkeypatch, orch)
    world_id, sub = _make_world(db_module, owner["id"], "SLEEPING")

    resp = client.post(f"/api/worlds/{world_id}/stop", headers=CSRF)

    assert resp.status_code == 200
    assert resp.json()["state"] == "OFF"
    assert orch.stopped == [sub]
    assert _state(db_module, world_id) == "OFF"


def test_a_slow_removal_answers_stopping_and_refuses_a_start(owner_client, monkeypatch):
    from open_worlds.api.routers import worlds

    client, db_module, owner = owner_client
    orch = _Orch(hold=True)
    _use(monkeypatch, orch)
    monkeypatch.setattr(worlds, "_INLINE_STOP_WAIT_SECONDS", 0.1)
    world_id, sub = _make_world(db_module, owner["id"], "SLEEPING")

    resp = client.post(f"/api/worlds/{world_id}/stop", headers=CSRF)
    assert resp.status_code == 200
    assert resp.json()["state"] == "STOPPING"

    # The container still exists: a start now must be refused, not raced.
    started = client.post(f"/api/worlds/{world_id}/start", headers=CSRF)
    assert started.status_code == 409

    orch.release.set()
    for t in threading.enumerate():
        if t.name == f"stop-{sub}":
            t.join(10)
    assert orch.stopped == [sub]
    assert _state(db_module, world_id) == "OFF"


def test_a_running_world_still_stops_in_the_background(owner_client, monkeypatch):
    """A running world's shutdown takes minutes; it must not hold the request."""
    client, db_module, owner = owner_client
    _use(monkeypatch, _Orch())
    world_id, _sub = _make_world(db_module, owner["id"], "RUNNING")

    resp = client.post(f"/api/worlds/{world_id}/stop", headers=CSRF)

    assert resp.status_code == 200
    assert resp.json()["state"] == "STOPPING"
