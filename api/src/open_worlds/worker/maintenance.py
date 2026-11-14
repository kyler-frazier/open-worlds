"""Worker self-healing sweep: reconcile world state, reap orphaned jobs, and
finish wedged deletes and stops."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..common.config import Settings, get_settings
from ..common.db import session_scope
from ..common.logging import get_logger
from ..common.metrics import (
    deletes_finished_total,
    jobs_reaped_total,
    orphan_containers_removed_total,
    world_unexpected_exits_total,
    worlds_by_state,
    worlds_reconciled_total,
)
from ..common.models import (
    Backup,
    BackupStatus,
    Job,
    JobStatus,
    JobType,
    StagedUpload,
    World,
    WorldState,
)
from ..common.orchestrator import Orchestrator, RunState
from ..common.orchestrator import make_orchestrator as _make_orchestrator
from ..common.stopping import perform_stop
from ..common.world_status import (
    GRACEFUL_EXIT_CODES,
    SIGKILL_EXIT_CODE,
    TRANSIENT_STATES,
    apply_state,
    reconciled_state,
)
from .failures import explain_crash

log = get_logger(__name__)


def run_maintenance(
    orch: Orchestrator,
    crash_seen: set[tuple[str | None, str | None]],
    in_flight: set | None = None,
) -> None:
    """Run one full maintenance pass. Each concern uses its own transaction so a
    failure in one does not roll back the others."""
    s = get_settings()
    with session_scope() as db:
        reap_stuck_jobs(db, s, in_flight)
    with session_scope() as db:
        finish_stuck_deletes(db, orch, s)
    with session_scope() as db:
        finish_stuck_stops(db, orch, s)
    with session_scope() as db:
        cleanup_stale_staged_uploads(db, s)
    with session_scope() as db:
        reconcile_worlds(db, orch, crash_seen)


def reap_stuck_jobs(db: Session, s: Settings, in_flight: set | None = None) -> int:
    """Requeue (or fail) jobs left RUNNING by a crashed worker.

    ``in_flight`` is the jobs this process is running: age alone cannot tell a
    long job from an orphan, and requeueing a live one runs it twice.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=s.job_stuck_after_seconds)
    stmt = select(Job).where(
        Job.status == JobStatus.RUNNING,
        or_(Job.started_at < cutoff, Job.started_at.is_(None)),
    )
    if in_flight:
        stmt = stmt.where(Job.id.not_in(in_flight))
    stuck = list(db.execute(stmt).scalars())
    for job in stuck:
        if job.attempts >= s.job_max_attempts:
            job.status = JobStatus.FAILED
            job.error = ((job.error or "") + " [reaped: exceeded max attempts]").strip()
            job.finished_at = datetime.now(UTC)
            _fail_associated(db, job)
            jobs_reaped_total.labels(disposition="failed").inc()
            log.warning("reaped stuck job as failed", extra={"job_id": str(job.id)})
        else:
            # Back to PENDING; the claimer re-increments attempts on the retry.
            job.status = JobStatus.PENDING
            jobs_reaped_total.labels(disposition="requeued").inc()
            log.warning("requeued stuck job", extra={"job_id": str(job.id)})
    return len(stuck)


def _fail_associated(db: Session, job: Job) -> None:
    """Best-effort cleanup for a permanently failed job."""
    try:
        if job.type is JobType.BACKUP and job.payload:
            backup = db.get(Backup, uuid.UUID(job.payload))
            if backup is not None and backup.status in (
                BackupStatus.PENDING,
                BackupStatus.RUNNING,
            ):
                backup.status = BackupStatus.FAILED
                backup.error = "Worker crashed while running this backup"
                backup.completed_at = datetime.now(UTC)
    except Exception:  # never let cleanup abort the reaper
        log.exception("failed to clean up after reaped job", extra={"job_id": str(job.id)})


def finish_stuck_deletes(db: Session, orch: Orchestrator, s: Settings) -> int:
    """Complete teardown for worlds wedged in DELETING (idempotent)."""
    cutoff = datetime.now(UTC) - timedelta(seconds=s.delete_stuck_after_seconds)
    stuck = list(
        db.execute(
            select(World).where(World.state == WorldState.DELETING, World.updated_at < cutoff)
        ).scalars()
    )
    finished = 0
    for world in stuck:
        try:
            orch.delete(world.subdomain)
            data_root = os.path.join(s.worlds_dir, world.subdomain)
            if os.path.isdir(data_root):
                # A failure keeps the row DELETING for a retry, rather than
                # leaving files on disk with no record.
                shutil.rmtree(data_root)
            db.delete(world)
            deletes_finished_total.inc()
            finished += 1
            log.info("finished wedged delete", extra={"subdomain": world.subdomain})
        except Exception:
            log.exception("could not finish delete", extra={"subdomain": world.subdomain})
    return finished


def finish_stuck_stops(db: Session, orch: Orchestrator, s: Settings) -> int:
    """Complete stops the API began but did not live to finish.

    The API runs stops as background tasks, so a restart mid-shutdown leaves
    the row STOPPING. The stop is redone toward its target (idempotent). Only
    rows older than the longest graceful window are touched.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=s.stop_stuck_after_seconds)
    stuck = list(
        db.execute(
            select(World).where(World.state == WorldState.STOPPING, World.updated_at < cutoff)
        ).scalars()
    )
    finished = 0
    for world in stuck:
        # A lost target resolves to OFF, which cannot wake anything by surprise.
        target = world.stop_target or WorldState.OFF
        try:
            perform_stop(orch, world.subdomain, target, graceful=world.ready_at is not None)
        except Exception:
            log.exception("could not finish stuck stop", extra={"subdomain": world.subdomain})
            world.state = WorldState.ERROR
            world.stop_target = None
            continue
        world.state = target
        world.stop_target = None
        finished += 1
        log.warning(
            "finished stuck stop", extra={"subdomain": world.subdomain, "state": target.value}
        )
    return finished


def cleanup_stale_staged_uploads(db: Session, s: Settings) -> int:
    """Sweep uncommitted uploads older than the TTL (remove row + staged file)."""
    cutoff = datetime.now(UTC) - timedelta(seconds=s.staged_upload_ttl_seconds)
    stale = list(db.execute(select(StagedUpload).where(StagedUpload.created_at < cutoff)).scalars())
    for staged in stale:
        if staged.path and os.path.exists(staged.path):
            try:
                os.remove(staged.path)
            except OSError:
                log.warning("could not remove stale staged file", extra={"path": staged.path})
        db.delete(staged)
        log.info("swept abandoned staged upload", extra={"world_id": str(staged.world_id)})
    return len(stale)


def reconcile_worlds(
    db: Session,
    orch: Orchestrator,
    crash_seen: set[tuple[str | None, str | None]],
) -> int:
    """Sync every world's stored state to its live container and refresh the
    per-state gauge. Also counts unexpected container exits (crashes/OOM)."""
    try:
        managed = {cs.subdomain: cs for cs in orch.list_managed()}
    except Exception:
        log.exception("reconcile: could not list managed containers")
        return 0

    worlds = list(db.execute(select(World)).scalars())
    reconciled = 0
    counts: dict[str, int] = {state.value: 0 for state in WorldState}

    remove_orphan_containers(orch, managed, {w.subdomain for w in worlds})

    for world in worlds:
        if world.state in TRANSIENT_STATES:
            counts[world.state.value] += 1
            continue

        status = managed.get(world.subdomain)
        run_state = status.run_state if status else RunState.ABSENT

        # As in world_state_for: a SIGKILL from our own stop timeout is not a
        # crash; an OOM kill is.
        if (
            status is not None
            and run_state is RunState.EXITED
            and status.exit_code is not None
            and status.exit_code not in GRACEFUL_EXIT_CODES
            and not (status.exit_code == SIGKILL_EXIT_CODE and not status.oom_killed)
        ):
            key = (status.container_id, status.finished_at)
            if key not in crash_seen:
                crash_seen.add(key)
                world_unexpected_exits_total.inc()
                log.warning(
                    "world exited unexpectedly",
                    extra={"subdomain": world.subdomain, "exit_code": status.exit_code},
                )

        # A non-graceful exit reads as ERROR, so a crash-looping world looks
        # broken rather than asleep.
        mapped = reconciled_state(
            world.state,
            run_state,
            status.exit_code if status is not None else None,
            status.oom_killed if status is not None else False,
        )
        if mapped is WorldState.ERROR and world.state is not WorldState.ERROR:
            # The container is still there, exited, with its log: say why.
            explain_crash(world, orch)
        if apply_state(world, mapped):
            reconciled += 1
            worlds_reconciled_total.inc()
        counts[world.state.value] += 1

    for state_value, n in counts.items():
        worlds_by_state.labels(state=state_value).set(n)

    # Bound memory: crash de-dup only needs recent events.
    if len(crash_seen) > 10_000:
        crash_seen.clear()

    return reconciled


def remove_orphan_containers(
    orch: Orchestrator, managed: Mapping[str, object], known_subdomains: set[str]
) -> int:
    """Stop and remove managed containers whose world row no longer exists.

    Left alone one keeps its memory and port, and a new world with the same
    subdomain would adopt it, old env and all. Data on disk is left for the
    operator. A row always exists before its container, so this never catches
    a world mid-creation.
    """
    removed = 0
    for subdomain in sorted(set(managed) - known_subdomains):
        if not subdomain:
            continue
        try:
            orch.stop(subdomain)
        except Exception:
            log.exception("could not remove orphan container", extra={"subdomain": subdomain})
            continue
        removed += 1
        orphan_containers_removed_total.inc()
        log.warning(
            "removed orphan container with no world row",
            extra={"subdomain": subdomain},
        )
    return removed


def make_orchestrator() -> Orchestrator:
    # Re-exported so the worker's call sites stay put.
    return _make_orchestrator()
