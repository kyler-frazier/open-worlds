"""Tests for the worker self-healing sweep: reaping orphaned jobs, reconciling
world state (incl. crash detection), and finishing wedged deletes.

Follows the suite convention of mocking rather than touching a real DB/Docker."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from open_worlds.common.models import (
    BackupStatus,
    JobStatus,
    JobType,
    WorldState,
)
from open_worlds.common.orchestrator import ContainerStatus, RunState
from open_worlds.worker import maintenance


class _FakeResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return list(self._items)


class _FakeSession:
    """Returns preconfigured result-sets per execute() call, in order."""

    def __init__(self, result_sets):
        self._results = list(result_sets)
        self.store: dict = {}
        self.deleted: list = []

    def execute(self, _query):
        return _FakeResult(self._results.pop(0))

    def get(self, _model, key):
        return self.store.get(key)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        pass


def _job(**kw):
    base = dict(
        id=uuid.uuid4(),
        status=JobStatus.RUNNING,
        attempts=1,
        started_at=None,
        error=None,
        finished_at=None,
        type=JobType.BACKUP,
        payload=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _settings(**kw):
    base = dict(job_stuck_after_seconds=1800, job_max_attempts=3, delete_stuck_after_seconds=300)
    base.update(kw)
    return SimpleNamespace(**base)


# --- Reaper ---------------------------------------------------------------


def test_reap_requeues_when_attempts_remain():
    job = _job(attempts=1)
    db = _FakeSession([[job]])
    n = maintenance.reap_stuck_jobs(db, _settings())
    assert n == 1
    assert job.status is JobStatus.PENDING


def test_reap_fails_and_marks_backup_when_attempts_exhausted():
    backup_id = uuid.uuid4()
    backup = SimpleNamespace(status=BackupStatus.RUNNING, error=None, completed_at=None)
    job = _job(attempts=3, type=JobType.BACKUP, payload=str(backup_id))
    db = _FakeSession([[job]])
    db.store[backup_id] = backup

    maintenance.reap_stuck_jobs(db, _settings())

    assert job.status is JobStatus.FAILED
    assert "max attempts" in (job.error or "")
    assert backup.status is BackupStatus.FAILED


# --- Reconcile + crash detection -----------------------------------------


def _crashed(subdomain="w1", exit_code=1, finished_at="2026-01-01T00:00:00Z", cid="c1"):
    return ContainerStatus(
        subdomain=subdomain,
        world_id="w1",
        run_state=RunState.EXITED,
        container_id=cid,
        exit_code=exit_code,
        finished_at=finished_at,
    )


def test_reconcile_maps_state_and_counts_crash_once():
    world = SimpleNamespace(subdomain="w1", state=WorldState.RUNNING, game="MINECRAFT")
    orch = SimpleNamespace(list_managed=lambda: [_crashed()])
    crash_seen: set = set()

    before = maintenance.world_unexpected_exits_total._value.get()
    maintenance.reconcile_worlds(_FakeSession([[world]]), orch, crash_seen)

    assert world.state is WorldState.ERROR  # non-graceful exit (crash) -> error
    assert maintenance.world_unexpected_exits_total._value.get() == before + 1

    # Same crash event (same container_id + finished_at) is not double-counted.
    world2 = SimpleNamespace(subdomain="w1", state=WorldState.ERROR, game="MINECRAFT")
    maintenance.reconcile_worlds(_FakeSession([[world2]]), orch, crash_seen)
    assert maintenance.world_unexpected_exits_total._value.get() == before + 1


def test_reconcile_graceful_exit_is_not_a_crash():
    world = SimpleNamespace(subdomain="w1", state=WorldState.RUNNING, game="MINECRAFT")
    orch = SimpleNamespace(list_managed=lambda: [_crashed(exit_code=143)])
    crash_seen: set = set()

    before = maintenance.world_unexpected_exits_total._value.get()
    maintenance.reconcile_worlds(_FakeSession([[world]]), orch, crash_seen)

    assert world.state is WorldState.SLEEPING
    assert maintenance.world_unexpected_exits_total._value.get() == before


def test_reconcile_skips_transient_states():
    world = SimpleNamespace(subdomain="w1", state=WorldState.DELETING)
    orch = SimpleNamespace(list_managed=lambda: [])  # container gone
    maintenance.reconcile_worlds(_FakeSession([[world]]), orch, set())
    # DELETING is in-flight; the sweep must not flip it to OFF.
    assert world.state is WorldState.DELETING


def test_reconcile_absent_container_maps_to_off():
    world = SimpleNamespace(subdomain="w1", state=WorldState.RUNNING, game="MINECRAFT")
    orch = SimpleNamespace(list_managed=lambda: [])
    maintenance.reconcile_worlds(_FakeSession([[world]]), orch, set())
    assert world.state is WorldState.OFF


# --- Delete finisher ------------------------------------------------------


def test_finish_stuck_deletes_removes_container_data_and_row(tmp_path):
    (tmp_path / "w1" / "data").mkdir(parents=True)
    world = SimpleNamespace(subdomain="w1", state=WorldState.DELETING)
    called: list = []
    orch = SimpleNamespace(delete=lambda sub: called.append(sub))
    db = _FakeSession([[world]])

    n = maintenance.finish_stuck_deletes(db, orch, _settings(worlds_dir=str(tmp_path)))

    assert n == 1
    assert called == ["w1"]
    assert world in db.deleted
    assert not (tmp_path / "w1").exists()


def test_reconcile_removes_containers_with_no_world_row():
    """A labelled container with no row is an orphan: stop+remove it, leave data."""
    stopped: list[str] = []
    orch = SimpleNamespace(
        list_managed=lambda: [
            ContainerStatus(subdomain="kept", world_id="w1", run_state=RunState.RUNNING),
            ContainerStatus(subdomain="orphan", world_id="gone", run_state=RunState.RUNNING),
        ],
        stop=lambda sub: stopped.append(sub),
    )
    world = SimpleNamespace(subdomain="kept", state=WorldState.RUNNING, game="MINECRAFT")

    maintenance.reconcile_worlds(_FakeSession([[world]]), orch, set())

    assert stopped == ["orphan"]
    assert world.state is WorldState.RUNNING


def test_orphan_sweep_survives_a_failed_stop():
    def _stop(sub):
        if sub == "bad":
            raise RuntimeError("docker down")

    orch = SimpleNamespace(stop=_stop)
    managed = {"bad": object(), "good": object(), "known": object()}
    assert maintenance.remove_orphan_containers(orch, managed, {"known"}) == 1


class _StopOrch:
    def __init__(self, fail=False):
        self.stopped, self.slept = [], []
        self._fail = fail

    def stop(self, sub, graceful=True):
        if self._fail:
            raise RuntimeError("docker down")
        self.stopped.append(sub)

    def sleep(self, sub, graceful=True):
        self.slept.append(sub)


def test_finish_stuck_stops_resolves_toward_the_recorded_target():
    """An API process that died mid-stop leaves STOPPING behind; the sweep
    redoes the stop toward whatever the owner (or the waker) asked for."""
    off = SimpleNamespace(
        subdomain="a", state=WorldState.STOPPING, stop_target=WorldState.OFF, ready_at=None
    )
    nap = SimpleNamespace(
        subdomain="b", state=WorldState.STOPPING, stop_target=WorldState.SLEEPING, ready_at=None
    )
    legacy = SimpleNamespace(
        subdomain="c", state=WorldState.STOPPING, stop_target=None, ready_at=None
    )
    orch = _StopOrch()
    s = SimpleNamespace(stop_stuck_after_seconds=600)

    assert maintenance.finish_stuck_stops(_FakeSession([[off, nap, legacy]]), orch, s) == 3

    assert orch.stopped == ["a", "c"]  # no target recorded -> OFF, the safe reading
    assert orch.slept == ["b"]
    assert off.state is WorldState.OFF and nap.state is WorldState.SLEEPING
    assert legacy.state is WorldState.OFF
    assert off.stop_target is None


def test_finish_stuck_stops_marks_error_when_docker_refuses():
    world = SimpleNamespace(subdomain="a", state=WorldState.STOPPING, stop_target=WorldState.OFF)
    s = SimpleNamespace(stop_stuck_after_seconds=600)
    assert maintenance.finish_stuck_stops(_FakeSession([[world]]), _StopOrch(fail=True), s) == 0
    assert world.state is WorldState.ERROR


def test_reconcile_leaves_a_stopping_world_with_a_running_container_alone():
    world = SimpleNamespace(subdomain="w1", state=WorldState.STOPPING, stop_target=WorldState.OFF)
    orch = SimpleNamespace(
        list_managed=lambda: [
            ContainerStatus(subdomain="w1", world_id="w1", run_state=RunState.RUNNING)
        ],
        stop=lambda _sub: None,
    )
    maintenance.reconcile_worlds(_FakeSession([[world]]), orch, set())
    assert world.state is WorldState.STOPPING
    assert world.stop_target is WorldState.OFF


def test_reconcile_into_running_resets_readiness():
    """mc-router starts Minecraft containers itself; the platform only learns of
    it here. The world must then read as starting, not as ready from last time."""
    world = SimpleNamespace(
        subdomain="w1",
        state=WorldState.SLEEPING,
        started_at=None,
        ready_at="stale",
        stop_target=None,
    )
    orch = SimpleNamespace(
        list_managed=lambda: [
            ContainerStatus(subdomain="w1", world_id="w1", run_state=RunState.RUNNING)
        ],
        stop=lambda _sub: None,
    )
    maintenance.reconcile_worlds(_FakeSession([[world]]), orch, set())
    assert world.state is WorldState.RUNNING
    assert world.ready_at is None
    assert world.started_at is not None
