"""The worker: a scheduler for periodic work, and a job consumer that runs up
to ``job_concurrency`` jobs at once, never two on the same world."""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select

from ..common.config import get_settings
from ..common.db import session_scope
from ..common.games import all_adapters
from ..common.logging import configure_logging, get_logger
from ..common.metrics import (
    backup_staging_free_bytes,
    jobs_pending,
    jobs_processed_total,
    start_metrics_server,
)
from ..common.models import (
    AutoBackupInterval,
    Backup,
    BackupKind,
    BackupStatus,
    Job,
    JobStatus,
    JobType,
    World,
)
from .backups import run_backup, run_import
from .failures import check_startup_failures
from .installs import advance_finished_installs
from .maintenance import make_orchestrator, run_maintenance
from .players import probe_pending_worlds, refresh_player_counts

log = get_logger(__name__)

_INTERVAL_DELTA = {
    AutoBackupInterval.DAILY: timedelta(days=1),
    AutoBackupInterval.WEEKLY: timedelta(weeks=1),
    AutoBackupInterval.MONTHLY: timedelta(days=30),
}


# Backoff after failed auto-backups: doubles per failure up to a day, so a
# durable failure (Drive full, token dead) costs one attempt a day and a blip is
# retried within the hour.
AUTO_BACKUP_RETRY_BASE = timedelta(minutes=15)
AUTO_BACKUP_RETRY_CAP = timedelta(hours=24)


def auto_backup_retry_delay(failures: int) -> timedelta:
    """How long after the last failed attempt the next one may run."""
    if failures <= 0:
        return timedelta(0)
    # Clamped so a long-failing world cannot overflow timedelta.
    return min(AUTO_BACKUP_RETRY_BASE * (2 ** min(failures - 1, 16)), AUTO_BACKUP_RETRY_CAP)


def auto_backup_due(world: World, now: datetime) -> bool:
    """Whether a world's schedule calls for an AUTO backup now: the interval
    since the last success has passed, and any backoff since the last attempt."""
    due_after = _INTERVAL_DELTA[world.auto_backup_interval]
    last = world.last_auto_backup_at
    if last is not None and (now - last) < due_after:
        return False
    attempted = world.auto_backup_last_attempt_at
    if world.auto_backup_failures > 0 and attempted is not None:
        if (now - attempted) < auto_backup_retry_delay(world.auto_backup_failures):
            return False
    return True


def enqueue_due_auto_backups() -> None:
    """Create AUTO backup + job records for worlds whose interval has elapsed."""
    now = datetime.now(UTC)
    with session_scope() as db:
        worlds = list(
            db.execute(select(World).where(World.auto_backup_enabled.is_(True))).scalars()
        )
        for world in worlds:
            owner = world.owner
            if not owner or not owner.drive_linked:
                continue
            if not auto_backup_due(world, now):
                continue
            active = db.execute(
                select(Job).where(
                    Job.world_id == world.id,
                    Job.type == JobType.BACKUP,
                    Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                )
            ).first()
            if active is not None:
                continue
            backup = Backup(
                world_id=world.id,
                kind=BackupKind.AUTO,
                status=BackupStatus.PENDING,
                filename=_auto_filename(world.subdomain, now),
            )
            db.add(backup)
            db.flush()
            db.add(
                Job(
                    type=JobType.BACKUP,
                    status=JobStatus.PENDING,
                    world_id=world.id,
                    payload=str(backup.id),
                )
            )
            log.info("auto-backup enqueued", extra={"world_id": str(world.id)})


def _auto_filename(subdomain: str, when: datetime) -> str:
    return f"{subdomain}-auto-{when.strftime('%Y%m%dT%H%M%SZ')}.tar.gz"


# Jobs this process is running, job id -> world id. The claim skips their
# worlds (jobs on one world contend for its data dir), and the reaper skips
# them so a long job is not mistaken for an orphan. In-process because there is
# one worker; a second replica would need this in the claim query.
_inflight_lock = threading.Lock()
_inflight: dict[uuid.UUID, uuid.UUID | None] = {}


def _inflight_job_ids() -> set[uuid.UUID]:
    with _inflight_lock:
        return set(_inflight)


def _inflight_world_ids() -> set[uuid.UUID]:
    with _inflight_lock:
        # A None would turn the claim's NOT IN into NULL and match nothing.
        return {w for w in _inflight.values() if w is not None}


def _claim_next_job() -> tuple[uuid.UUID, uuid.UUID | None] | None:
    """Claim the oldest pending job whose world is free. Returns (job, world)."""
    busy = _inflight_world_ids()
    with session_scope() as db:
        stmt = select(Job).where(Job.status == JobStatus.PENDING)
        if busy:
            stmt = stmt.where(Job.world_id.not_in(busy))
        job = db.execute(
            stmt.order_by(Job.created_at).with_for_update(skip_locked=True).limit(1)
        ).scalar_one_or_none()
        if job is None:
            return None
        job.status = JobStatus.RUNNING
        job.attempts += 1
        job.started_at = datetime.now(UTC)
        claimed = (job.id, job.world_id)
    # After the claim commits: a crash between the two leaves a job the reaper
    # recovers, not a world wedged as busy.
    with _inflight_lock:
        _inflight[claimed[0]] = claimed[1]
    return claimed


def _run_claimed_job(job_id: uuid.UUID) -> None:
    """Run a job and release its world, whatever happens."""
    try:
        _process_job(job_id)
    finally:
        with _inflight_lock:
            _inflight.pop(job_id, None)


def _process_job(job_id: uuid.UUID) -> None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        result = "success"
        try:
            if job.type is JobType.BACKUP:
                run_backup(db, uuid.UUID(job.payload))
            elif job.type is JobType.IMPORT:
                if job.world_id is None or job.payload is None:
                    raise ValueError("import job has no world or payload")
                run_import(db, job.world_id, job.payload)
            else:
                # Other types belong to a game. Unhandled is a bug, not a no-op.
                _game_job_handler(job.type.value)(db, job)
            job.status = JobStatus.SUCCESS
        except Exception as exc:
            result = "error"
            job.status = JobStatus.FAILED
            job.error = str(exc)[:2000]
            log.exception("job failed", extra={"job_id": str(job_id)})
        finally:
            job.finished_at = datetime.now(UTC)
            jobs_processed_total.labels(type=job.type.value, result=result).inc()


def _game_job_handler(job_type: str):  # type: ignore[no-untyped-def]
    for adapter in all_adapters():
        handler = adapter.job_handlers().get(job_type)
        if handler is not None:
            return handler
    raise RuntimeError(f"no game handles {job_type} jobs")


def _update_pending_gauge() -> None:
    # COUNT in the database: this runs every loop, and a backlog is when the
    # queue is longest.
    with session_scope() as db:
        pending = db.execute(
            select(func.count()).select_from(Job).where(Job.status == JobStatus.PENDING)
        ).scalar_one()
        jobs_pending.set(pending)


def _poll_players() -> None:
    """Scheduled wrapper: never let a polling failure kill the scheduler job."""
    try:
        with session_scope() as db:
            refresh_player_counts(db)
    except Exception:
        log.exception("player count refresh failed")


def _probe_readiness() -> None:
    """Scheduled wrapper for the fast readiness probe."""
    try:
        with session_scope() as db:
            probe_pending_worlds(db)
    except Exception:
        log.exception("readiness probe failed")


# Drive uploads need the Fernet key and the OAuth client, and nothing else.
WORKER_SECRETS = ("fernet_key", "google_client_id", "google_client_secret")


def _publish_disk_free() -> None:
    """Report free space where backups are staged; never raise into the scheduler."""
    try:
        s = get_settings()
        os.makedirs(s.backup_staging_dir, exist_ok=True)
        backup_staging_free_bytes.set(shutil.disk_usage(s.backup_staging_dir).free)
    except Exception:
        log.exception("could not read free disk for the backup staging directory")


def _advance_installs() -> None:
    """Start the server of each world that finished installing; never raise."""
    try:
        with session_scope() as db:
            advance_finished_installs(db, make_orchestrator())
    except Exception:
        log.exception("install hand-off failed")


def _check_startup_failures() -> None:
    """Stop and explain worlds whose server failed to start; never raise."""
    try:
        with session_scope() as db:
            check_startup_failures(db, make_orchestrator())
    except Exception:
        log.exception("startup failure check failed")


def run_forever(poll_interval: float = 5.0) -> None:
    s = get_settings()
    s.require(*WORKER_SECRETS)
    configure_logging(level=s.log_level, json_output=s.log_json)
    start_metrics_server(8001, s.games_subnet)
    log.info("worker metrics server listening", extra={"port": 8001})

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(enqueue_due_auto_backups, "interval", minutes=5, id="auto_backups")
    # One admin call per running world. max_instances=1 everywhere so a slow
    # round cannot overlap itself.
    scheduler.add_job(
        _poll_players,
        "interval",
        seconds=s.player_poll_seconds,
        id="player_counts",
        max_instances=1,
    )
    # Fast, and cheap: only worlds still booting are probed.
    scheduler.add_job(
        _probe_readiness,
        "interval",
        seconds=s.readiness_probe_interval_seconds,
        id="readiness_probe",
        max_instances=1,
    )
    # As fast as readiness: the install's exit is the world's boot stalling.
    scheduler.add_job(
        _advance_installs,
        "interval",
        seconds=s.readiness_probe_interval_seconds,
        id="install_handoff",
        max_instances=1,
    )
    # The only source for the low-disk alert.
    scheduler.add_job(
        _publish_disk_free,
        "interval",
        seconds=60,
        id="disk_free",
        max_instances=1,
    )
    # Even for games no longer offered: their worlds still need these.
    for adapter in all_adapters():
        for job in adapter.periodic_jobs():
            scheduler.add_job(job.run, "interval", seconds=job.seconds, id=job.id, max_instances=1)
    # A booting server that logged its own failure is stopped, with the reason.
    scheduler.add_job(
        _check_startup_failures,
        "interval",
        seconds=s.startup_failure_check_seconds,
        id="startup_failures",
        max_instances=1,
    )
    # Once now: an interval job waits a whole interval, and a gauge at 0 would
    # trip the low-disk alert.
    _publish_disk_free()
    scheduler.start()

    orch = make_orchestrator()
    # Crash events seen, by (container_id, finished_at).
    crash_seen: set[tuple[str | None, str | None]] = set()
    # So one long backup does not hold up everything else.
    pool = ThreadPoolExecutor(max_workers=s.job_concurrency, thread_name_prefix="job")
    running: set[Future] = set()
    # Maintenance runs first: reap orphans, reconcile what drifted while down.
    last_maintenance = 0.0
    log.info("worker started")

    try:
        while True:
            try:
                now = time.monotonic()
                if now - last_maintenance >= s.maintenance_interval_seconds:
                    # Told what is in flight, so long jobs are not reaped.
                    run_maintenance(orch, crash_seen, _inflight_job_ids())
                    last_maintenance = now
                _update_pending_gauge()

                running = {f for f in running if not f.done()}
                claimed_any = False
                while len(running) < s.job_concurrency:
                    claimed = _claim_next_job()
                    if claimed is None:
                        break
                    claimed_any = True
                    running.add(pool.submit(_run_claimed_job, claimed[0]))

                if not claimed_any:
                    # Refill promptly while work is in flight.
                    time.sleep(0.5 if running else poll_interval)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                # Survive transient failures (the DB not up yet).
                log.exception("worker loop error; backing off")
                time.sleep(poll_interval)
    except (KeyboardInterrupt, SystemExit):
        log.info("worker shutting down")
    finally:
        scheduler.shutdown(wait=False)
        # Let jobs finish: a killed backup or import leaves partial files.
        pool.shutdown(wait=True)
