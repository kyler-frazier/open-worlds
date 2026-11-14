"""Tests for the two-phase stop: begin in the request, finish in the background."""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from open_worlds.common.models import WorldState
from open_worlds.common.stopping import begin_stop, finish_stop, finish_stop_within


class _DB:
    def __init__(self, world):
        self.world = world
        self.commits = 0

    def get(self, _model, _key):
        return self.world

    def commit(self):
        self.commits += 1


def _factory(db):
    @contextmanager
    def make():
        yield db

    return make


class _Orch:
    def __init__(self, fail=False):
        self.stopped, self.slept = [], []
        self._fail = fail

    def stop(self, sub, graceful=True):
        if self._fail:
            raise RuntimeError("docker down")
        self.stopped.append(sub)

    def sleep(self, sub, graceful=True):
        if self._fail:
            raise RuntimeError("docker down")
        self.slept.append(sub)


def _world(state=WorldState.RUNNING):
    return SimpleNamespace(id=uuid.uuid4(), subdomain="demo", state=state, stop_target=None)


def test_begin_stop_records_intent_and_target():
    world, db = _world(), _DB(None)
    begin_stop(world, WorldState.OFF, db)
    assert world.state is WorldState.STOPPING
    assert world.stop_target is WorldState.OFF
    assert db.commits == 1


def test_begin_stop_rejects_a_non_target():
    with pytest.raises(ValueError):
        begin_stop(_world(), WorldState.RUNNING, _DB(None))


def test_finish_stop_off_removes_and_resolves():
    world = _world(WorldState.STOPPING)
    world.stop_target = WorldState.OFF
    orch, db = _Orch(), _DB(world)
    assert finish_stop(world.id, "demo", WorldState.OFF, orch, _factory(db)) is WorldState.OFF
    assert orch.stopped == ["demo"] and orch.slept == []
    assert world.state is WorldState.OFF
    assert world.stop_target is None


def test_finish_stop_sleeping_keeps_the_container():
    world = _world(WorldState.STOPPING)
    orch, db = _Orch(), _DB(world)
    finish_stop(world.id, "demo", WorldState.SLEEPING, orch, _factory(db))
    assert orch.slept == ["demo"] and orch.stopped == []
    assert world.state is WorldState.SLEEPING


def test_finish_stop_does_not_override_a_newer_state():
    """The owner deleted the world while it was stopping: a delete outranks."""
    world = _world(WorldState.DELETING)
    db = _DB(world)
    out = finish_stop(world.id, "demo", WorldState.OFF, _Orch(), _factory(db))
    assert out is WorldState.DELETING
    assert db.commits == 0


def test_finish_stop_failure_lands_in_error_not_stopping_forever():
    world = _world(WorldState.STOPPING)
    db = _DB(world)
    out = finish_stop(world.id, "demo", WorldState.OFF, _Orch(fail=True), _factory(db))
    assert out is WorldState.ERROR
    assert world.state is WorldState.ERROR


def test_finish_stop_tolerates_a_deleted_row():
    out = finish_stop(uuid.uuid4(), "demo", WorldState.OFF, _Orch(), _factory(_DB(None)))
    assert out is None


class _SlowOrch(_Orch):
    """Removal that blocks until the test lets it finish."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def stop(self, sub, graceful=True):
        self.release.wait(5)
        super().stop(sub)


def test_finish_stop_within_answers_with_the_final_state():
    """Turning off a sleeping world is a removal; the caller gets OFF back."""
    world = _world(WorldState.STOPPING)
    orch, db = _Orch(), _DB(world)
    out = finish_stop_within(world.id, "demo", WorldState.OFF, orch, 5.0, _factory(db))
    assert out is WorldState.OFF
    assert orch.stopped == ["demo"]
    assert world.state is WorldState.OFF


def test_a_slow_removal_stays_stopping_until_it_is_done():
    """Past the wait, the caller gets None and the row stays STOPPING -- the
    state that refuses a start -- until the removal actually completes."""
    world = _world(WorldState.STOPPING)
    orch, db = _SlowOrch(), _DB(world)
    out = finish_stop_within(world.id, "demo", WorldState.OFF, orch, 0.05, _factory(db))
    assert out is None
    assert world.state is WorldState.STOPPING
    assert orch.stopped == []

    orch.release.set()
    for t in threading.enumerate():
        if t.name == "stop-demo":
            t.join(5)
    assert orch.stopped == ["demo"]
    assert world.state is WorldState.OFF


def test_finish_stop_within_still_lands_a_failure_in_error():
    world = _world(WorldState.STOPPING)
    db = _DB(world)
    out = finish_stop_within(world.id, "demo", WorldState.OFF, _Orch(fail=True), 5.0, _factory(db))
    assert out is WorldState.ERROR
