"""Jobs run in parallel, but never two on the same world.

Jobs on different worlds share nothing (staging is keyed by backup id). Jobs on
one world can wipe or write into each other's tree.
"""

from __future__ import annotations

import uuid

from open_worlds.worker import main as worker_main

pytest_plugins = ["tests.conftest_routes"]


def test_a_world_already_in_flight_is_skipped(monkeypatch):
    """The exclusion that makes concurrency safe.

    Without it, a backup and an import of one world can run together and the
    import wipes the directory the archive is being read from -- producing a
    backup that looks successful and contains half a world.
    """
    world_a, world_b = uuid.uuid4(), uuid.uuid4()
    seen_filters = {}

    class _Q:
        def where(self, *criteria):
            seen_filters["where"] = seen_filters.get("where", 0) + 1
            return self

        def order_by(self, *_a):
            return self

        def with_for_update(self, **_k):
            return self

        def limit(self, _n):
            return self

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _Db:
        def execute(self, _stmt):
            return _Result()

    class _Scope:
        def __enter__(self):
            return _Db()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(worker_main, "session_scope", lambda: _Scope())
    monkeypatch.setattr(worker_main, "select", lambda _m: _Q())

    # Nothing in flight: one filter (status == PENDING).
    worker_main._inflight.clear()
    seen_filters.clear()
    worker_main._claim_next_job()
    assert seen_filters["where"] == 1

    # A world in flight adds the exclusion.
    worker_main._inflight[uuid.uuid4()] = world_a
    seen_filters.clear()
    worker_main._claim_next_job()
    assert seen_filters["where"] == 2, "a busy world must be excluded from the claim"
    worker_main._inflight.clear()
    assert world_b  # the other world stays claimable; nothing excludes it


def test_a_world_is_released_even_when_its_job_raises(monkeypatch):
    """A job that throws must not wedge its world as permanently busy.

    Leaving it registered would make that world's queue stop forever, which
    looks like the platform ignoring the user rather than a failed job.
    """
    job_id, world_id = uuid.uuid4(), uuid.uuid4()
    worker_main._inflight[job_id] = world_id

    def _boom(_job_id):
        raise RuntimeError("job blew up")

    monkeypatch.setattr(worker_main, "_process_job", _boom)

    try:
        worker_main._run_claimed_job(job_id)
    except RuntimeError:
        pass

    assert job_id not in worker_main._inflight
    assert world_id not in worker_main._inflight_world_ids()


def test_the_reaper_leaves_jobs_this_process_is_running_alone(app_client, make_user):
    """Age stopped being proof of an orphan the moment jobs run beside the sweep.

    A large import passes the 30 minute stuck threshold while perfectly healthy.
    Requeueing it would run the same import over the same world a second time,
    so this drives the real query against a real database rather than asserting
    the parameter exists.
    """
    from datetime import UTC, datetime, timedelta

    from open_worlds.common.config import get_settings
    from open_worlds.common.db import session_scope
    from open_worlds.common.models import Job, JobStatus, JobType, World
    from open_worlds.worker.maintenance import reap_stuck_jobs

    owner = make_user()
    long_ago = datetime.now(UTC) - timedelta(seconds=99999)

    with session_scope() as db:
        world = World(
            id=uuid.uuid4(),
            owner_id=uuid.UUID(owner["id"]),
            subdomain=f"w{uuid.uuid4().hex[:8]}",
            display_name="Reaper Test",
            game="MINECRAFT",
            memory_mb=2048,
        )
        db.add(world)
        db.flush()
        job = Job(
            id=uuid.uuid4(),
            world_id=world.id,
            type=JobType.BACKUP,
            status=JobStatus.RUNNING,
            started_at=long_ago,
        )
        db.add(job)
        db.flush()
        job_id = job.id

    s = get_settings()

    # Told it is in flight: left alone, however old it looks.
    with session_scope() as db:
        assert reap_stuck_jobs(db, s, {job_id}) == 0
    with session_scope() as db:
        assert db.get(Job, job_id).status is JobStatus.RUNNING

    # Not in flight: genuinely an orphan, so it is recovered.
    with session_scope() as db:
        assert reap_stuck_jobs(db, s, set()) == 1
    with session_scope() as db:
        assert db.get(Job, job_id).status is not JobStatus.RUNNING


def test_a_failed_backup_fails_its_job(app_client, make_user, monkeypatch):
    """A failed backup fails its job, so the job-failure alert fires."""
    from open_worlds.common.db import session_scope
    from open_worlds.common.models import (
        Job,
        JobStatus,
        JobType,
        World,
    )
    from open_worlds.worker import main as worker_main

    owner = make_user()
    with session_scope() as db:
        world = World(
            id=uuid.uuid4(),
            owner_id=uuid.UUID(owner["id"]),
            subdomain=f"w{uuid.uuid4().hex[:8]}",
            display_name="Backup Fail",
            game="MINECRAFT",
            memory_mb=2048,
        )
        db.add(world)
        db.flush()
        job = Job(
            id=uuid.uuid4(),
            world_id=world.id,
            type=JobType.BACKUP,
            status=JobStatus.RUNNING,
            payload=str(uuid.uuid4()),
        )
        db.add(job)
        db.flush()
        job_id = job.id

    def _blow_up(_db, _backup_id):
        raise RuntimeError("drive is on fire")

    monkeypatch.setattr(worker_main, "run_backup", _blow_up)
    worker_main._process_job(job_id)

    with session_scope() as db:
        assert db.get(Job, job_id).status is JobStatus.FAILED
        assert "drive is on fire" in (db.get(Job, job_id).error or "")
