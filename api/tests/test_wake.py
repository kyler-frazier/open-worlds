"""Tests for the internal wake/sleep endpoints pal-waker drives.

These encode the sleep/off distinction the whole design rests on: an idle world
is stopped but kept (so a packet can wake it), while an OFF world was stopped by
its owner and must stay off no matter who knocks.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from open_worlds.api.routers import internal
from open_worlds.common.models import WorldState
from open_worlds.common.orchestrator import WorldImageUnavailable


class _DB:
    def __init__(self, world=None):
        self.world = world
        self.committed = False

    def execute(self, _stmt):
        # Serves both the world lookup and spec_for's resource-pack query; the
        # latter only runs for games with a content catalog, which Palworld
        # is not, so returning the world here is unambiguous.
        world = self.world
        return SimpleNamespace(scalar_one_or_none=lambda: world)

    def commit(self):
        self.committed = True


class _Orch:
    def __init__(self, fail=False):
        self.started = []
        self.slept = []
        self._fail = fail

    def start(self, spec):
        if self._fail:
            raise self._failure()
        self.started.append(spec.subdomain)

    def sleep(self, subdomain, graceful=True):
        if self._fail:
            raise self._failure()
        self.slept.append(subdomain)

    def _failure(self):
        # `fail=True` means a generic Docker problem; passing an exception
        # instead lets a test pick a specific failure mode.
        if isinstance(self._fail, Exception):
            return self._fail
        return RuntimeError("docker is unhappy")


@pytest.fixture(autouse=True)
def _games_offered(monkeypatch):
    # The fake DB answers only the world lookup; which games are offered is
    # tested against Postgres in test_game_availability.
    monkeypatch.setattr(internal, "enabled_games", lambda _db: {"MINECRAFT", "PALWORLD"})


def _world(state=WorldState.SLEEPING, **kw):
    base = dict(
        id=uuid.uuid4(),
        game="PALWORLD",
        owner_id=uuid.uuid4(),
        game_settings={},
        reported_version=None,
        owner=None,
        subdomain="demo",
        display_name="Demo",
        game_port=8213,
        memory_mb=8192,
        max_players=16,
        state=state,
        stop_target=None,
        admin_password_enc=None,
        server_password_enc=None,
        started_at=None,
        ready_at=None,
        egress_log_only=False,
        last_error=None,
        last_error_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_wake_starts_a_sleeping_world():
    world = _world(WorldState.SLEEPING)
    db, orch = _DB(world), _Orch()
    out = internal.wake("demo", db, orch)
    assert orch.started == ["demo"]
    assert world.state is WorldState.RUNNING
    assert out.game_port == 8213
    assert db.committed


def test_wake_is_idempotent_for_a_running_world():
    world = _world(WorldState.RUNNING)
    orch = _Orch()
    out = internal.wake("demo", _DB(world), orch)
    assert orch.started == []
    assert out.state == "RUNNING"


def test_wake_refuses_an_off_world():
    # The owner turned this world off. A connection attempt must not undo that,
    # which is exactly why OFF removes the container instead of stopping it.
    world = _world(WorldState.OFF)
    orch = _Orch()
    with pytest.raises(HTTPException) as exc:
        internal.wake("demo", _DB(world), orch)
    assert exc.value.status_code == 409
    assert orch.started == []


@pytest.mark.parametrize("state", [WorldState.DELETING, WorldState.STOPPING])
def test_wake_refuses_a_world_mid_transition(state):
    orch = _Orch()
    with pytest.raises(HTTPException) as exc:
        internal.wake("demo", _DB(_world(state)), orch)
    assert exc.value.status_code == 409
    assert orch.started == []


def test_wake_failure_marks_the_world_errored():
    world = _world(WorldState.SLEEPING)
    db = _DB(world)
    with pytest.raises(HTTPException) as exc:
        internal.wake("demo", db, _Orch(fail=True))
    assert exc.value.status_code == 502
    assert world.state is WorldState.ERROR


def test_unknown_world_is_404():
    with pytest.raises(HTTPException) as exc:
        internal.wake("nope", _DB(None), _Orch())
    assert exc.value.status_code == 404


def test_sleep_marks_the_world_stopping_and_defers_the_docker_work():
    """The graceful shutdown runs after the response, not inside it.

    Palworld takes minutes to save and exit; doing that inside the request
    forced the waker to wait out a six-minute HTTP timeout. The route now
    records the intent (STOPPING toward SLEEPING) and hands the real stop to a
    background task.
    """
    world = _world(WorldState.RUNNING)
    orch, background = _Orch(), BackgroundTasks()
    out = internal.sleep("demo", background, _DB(world), orch)
    assert out.state == "STOPPING"
    assert world.state is WorldState.STOPPING
    assert world.stop_target is WorldState.SLEEPING
    # Nothing touched Docker yet; the background task will.
    assert orch.slept == []
    assert len(background.tasks) == 1
    task = background.tasks[0]
    assert task.args[1] == "demo" and task.args[2] is WorldState.SLEEPING


def test_sleep_is_a_no_op_for_a_world_that_is_not_running():
    world = _world(WorldState.OFF)
    orch = _Orch()
    out = internal.sleep("demo", BackgroundTasks(), _DB(world), orch)
    assert orch.slept == []
    assert out.state == "OFF"


def test_wake_does_not_error_a_world_when_the_image_is_unavailable():
    # A registry problem says nothing about the world, and pal-waker binds no
    # port for an ERROR world.
    world = _world(WorldState.SLEEPING)
    db = _DB(world)
    with pytest.raises(HTTPException) as exc:
        internal.wake("demo", db, _Orch(fail=WorldImageUnavailable("no such image")))
    assert exc.value.status_code == 503
    assert world.state is WorldState.SLEEPING


@pytest.mark.parametrize("supplied", [None, "", "wrong", "expected-but-longer"])
def test_internal_secret_is_required(monkeypatch, supplied):
    """Every wrong or missing secret is rejected -- there is no unset escape hatch.

    internal-api sits on the same Docker network as untrusted world containers,
    so this check is the boundary, not a second layer behind one.
    """
    monkeypatch.setattr(
        internal, "get_settings", lambda: SimpleNamespace(internal_secret="expected")
    )
    with pytest.raises(HTTPException) as exc:
        internal.require_internal_secret(supplied)
    assert exc.value.status_code == 401


def test_internal_secret_accepts_the_configured_value(monkeypatch):
    monkeypatch.setattr(
        internal, "get_settings", lambda: SimpleNamespace(internal_secret="expected")
    )
    internal.require_internal_secret("expected")


def test_wake_refuses_when_the_host_is_at_capacity(monkeypatch):
    """A player connecting must not oversubscribe the host any more than the
    owner pressing Turn on can. 409 leaves the waker listening; nothing starts."""
    from open_worlds.common.capacity import CapacityExceeded

    def _full(_world, _db, _s):
        raise CapacityExceeded("full")

    monkeypatch.setattr(internal, "ensure_capacity_for", _full)
    world = _world(WorldState.SLEEPING)
    orch = _Orch()
    with pytest.raises(HTTPException) as exc:
        internal.wake("demo", _DB(world), orch)
    assert exc.value.status_code == 409
    assert orch.started == []
    assert world.state is WorldState.SLEEPING


def test_internal_app_refuses_to_start_without_the_secret(monkeypatch):
    """The guarantee moved from Settings validation to the internal app factory.

    Secrets are per process now, so the setting has a default -- but the one
    process that serves /api/internal/* must still refuse to come up without
    it, or an empty value would be an open wake/sleep API on the network every
    untrusted world container shares.
    """
    from open_worlds.api import main as api_main
    from open_worlds.common.config import get_settings

    empty = get_settings().model_copy(update={"internal_secret": ""})
    monkeypatch.setattr(api_main, "get_settings", lambda: empty)
    with pytest.raises(RuntimeError) as exc:
        api_main.create_internal_app()
    assert "PLATFORM_INTERNAL_SECRET" in str(exc.value)


def test_an_empty_configured_secret_rejects_every_call(monkeypatch):
    # Defence in depth behind the factory check: nothing matches an empty secret.
    monkeypatch.setattr(internal, "get_settings", lambda: SimpleNamespace(internal_secret=""))
    with pytest.raises(HTTPException):
        internal.require_internal_secret("")


# --- How long a port-routed world has had no players ------------------------
# pal-waker sleeps on this rather than on raw UDP, which a spoofed source can
# send forever.


def _seen(count, updated_ago, active_ago=None, started_ago=None):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)

    def ago(s):
        return None if s is None else now - timedelta(seconds=s)

    w = SimpleNamespace(
        player_count=count,
        players_updated_at=ago(updated_ago),
        last_active_at=ago(active_ago),
        started_at=ago(started_ago),
    )
    return internal.players_idle_seconds(w, now)


def test_players_on_means_not_idle():
    assert _seen(2, updated_ago=5, active_ago=5, started_ago=900) == 0.0


def test_idle_counts_from_the_last_player():
    assert _seen(0, updated_ago=5, active_ago=700, started_ago=3000) == pytest.approx(700)


def test_idle_counts_from_the_start_when_nobody_ever_joined():
    assert _seen(0, updated_ago=5, started_ago=400) == pytest.approx(400)


@pytest.mark.parametrize("updated_ago", [None, 3600])
def test_a_stale_or_missing_count_is_unknown(updated_ago):
    """Then pal-waker falls back to traffic rather than sleeping a busy world."""
    assert _seen(0, updated_ago=updated_ago, started_ago=4000) is None
