"""The dashboard's bulk reconcile: one container listing, every world mapped."""

from __future__ import annotations

from types import SimpleNamespace

from open_worlds.api.routers.worlds import reconcile_rows
from open_worlds.common.models import WorldState
from open_worlds.common.orchestrator import ContainerStatus, RunState


class _DB:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _world(sub, state):
    return SimpleNamespace(
        game="MINECRAFT",
        subdomain=sub,
        state=state,
        started_at=None,
        ready_at=None,
        stop_target=None,
    )


def _orch(statuses, calls):
    def list_managed():
        calls.append("list")
        return statuses

    def inspect(_sub):
        raise AssertionError("bulk reconcile must not inspect per world")

    return SimpleNamespace(list_managed=list_managed, inspect=inspect)


def test_one_listing_serves_every_world():
    calls: list[str] = []
    orch = _orch(
        [
            ContainerStatus(subdomain="a", world_id="a", run_state=RunState.RUNNING),
            ContainerStatus(subdomain="b", world_id="b", run_state=RunState.EXITED, exit_code=143),
        ],
        calls,
    )
    rows = [
        _world("a", WorldState.SLEEPING),
        _world("b", WorldState.RUNNING),
        _world("c", WorldState.RUNNING),
    ]
    db = _DB()

    assert reconcile_rows(rows, orch, db) == 3
    assert calls == ["list"]
    assert [w.state for w in rows] == [WorldState.RUNNING, WorldState.SLEEPING, WorldState.OFF]
    assert rows[0].ready_at is None and rows[0].started_at is not None
    assert db.commits == 1


def test_no_change_means_no_commit():
    calls: list[str] = []
    orch = _orch([ContainerStatus(subdomain="a", world_id="a", run_state=RunState.RUNNING)], calls)
    db = _DB()
    assert reconcile_rows([_world("a", WorldState.RUNNING)], orch, db) == 0
    assert db.commits == 0


def test_transient_rows_are_skipped_and_listing_failure_is_harmless():
    rows = [_world("a", WorldState.DELETING), _world("b", WorldState.STOPPING)]
    orch = SimpleNamespace(list_managed=lambda: (_ for _ in ()).throw(RuntimeError("docker")))
    assert reconcile_rows(rows, orch, _DB()) == 0
    assert rows[0].state is WorldState.DELETING
    ok = SimpleNamespace(list_managed=lambda: [])
    assert reconcile_rows(rows, ok, _DB()) == 1  # STOPPING + no container -> OFF
    assert rows[1].state is WorldState.OFF
    assert rows[0].state is WorldState.DELETING


def test_empty_list_does_not_touch_docker():
    orch = SimpleNamespace(list_managed=lambda: (_ for _ in ()).throw(AssertionError("called")))
    assert reconcile_rows([], orch, _DB()) == 0
