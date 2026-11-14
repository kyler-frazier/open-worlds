"""Start the server of a world whose install just finished.

A game with an install stage boots in two containers (see orchestrator.py):
the install exits when the server is on disk, and the world is still RUNNING
and not ready. This hands it to the server, behind the play egress list.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.games.base import InstallIncomplete
from ..common.logging import get_logger
from ..common.models import World, WorldState
from ..common.orchestrator import Orchestrator, OrchestratorUnavailable, WorldImageUnavailable
from ..common.specs import spec_for
from ..common.world_status import apply_state

log = get_logger(__name__)


def advance_finished_installs(db: Session, orch: Orchestrator) -> int:
    """Start the server for every running world whose install exited cleanly;
    returns how many were started.

    Every RUNNING world, not only those still booting: a world can be marked
    ready from an earlier boot when its start installs (its server exited and
    Start was pressed before reconcile noticed).
    """
    running = {
        w.subdomain: w
        for w in db.execute(select(World).where(World.state == WorldState.RUNNING)).scalars()
    }
    if not running:
        return 0
    started = 0
    for status in orch.list_managed():
        world = running.get(status.subdomain)
        if world is None or not status.install_finished:
            continue
        log.info("install finished; starting the server", extra={"subdomain": world.subdomain})
        try:
            orch.start(spec_for(world, db))
        except (WorldImageUnavailable, OrchestratorUnavailable):
            # Not the world's fault; the next tick tries again.
            log.warning("could not start the server yet", extra={"subdomain": world.subdomain})
            continue
        except Exception as exc:  # noqa: BLE001 - the world's card says why
            reason = (
                str(exc)
                if isinstance(exc, InstallIncomplete)
                else "The server failed to start after installing."
            )
            log.exception("server start after install failed", extra={"subdomain": world.subdomain})
            world.last_error = reason
            world.last_error_at = datetime.now(UTC)
            apply_state(world, WorldState.ERROR)
            continue
        # A new boot: readiness is the server's to prove, not the last one's,
        # and the idle window starts now, not when the install did.
        world.ready_at = None
        world.started_at = datetime.now(UTC)
        started += 1
    return started
