"""Asynchronous world stop: request now, finish in the background.

A graceful shutdown can take minutes (Palworld's save-and-exit), and nothing
user-facing waits on it. :func:`begin_stop` marks the row STOPPING with its
target inside the request; :func:`finish_stop` does the Docker work from a
background task, or the worker's stuck-stop sweep if the API died first.

Both "Turn off" (OFF: container removed) and idle sleep (SLEEPING: container
kept, wakeable) go through here.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy.orm import Session

from . import db as db_module
from .logging import get_logger
from .metrics import world_actions_total
from .models import World, WorldState
from .orchestrator import Orchestrator

log = get_logger(__name__)

# The only two things a stop can resolve to.
STOP_TARGETS = (WorldState.OFF, WorldState.SLEEPING)


def begin_stop(world: World, target: WorldState, db: Session) -> None:
    """Mark a world as stopping toward ``target`` and commit. Cheap and fast."""
    if target not in STOP_TARGETS:
        raise ValueError(f"Not a stop target: {target}")
    world.state = WorldState.STOPPING
    world.stop_target = target
    db.commit()


def perform_stop(
    orch: Orchestrator, subdomain: str, target: WorldState, graceful: bool = True
) -> None:
    """The Docker half: remove the container for OFF, just stop it for SLEEPING.

    ``graceful=False`` for a server that never became ready: it has nothing to
    save and may never answer SIGTERM.
    """
    if target is WorldState.OFF:
        orch.stop(subdomain, graceful=graceful)
    else:
        orch.sleep(subdomain, graceful=graceful)


def finish_stop(
    world_id: uuid.UUID,
    subdomain: str,
    target: WorldState,
    orch: Orchestrator,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    graceful: bool = True,
) -> WorldState | None:
    """Complete a stop begun by :func:`begin_stop`; returns the resolved state.

    Opens its own session, since the request's has closed. Only a row still
    STOPPING is touched, as a delete outranks a stop. A Docker failure sets
    ERROR, and the next reconcile corrects it if the container still runs.
    """
    # Looked up per call, not bound at import.
    session_factory = session_factory or db_module.SessionLocal
    action = "stop" if target is WorldState.OFF else "sleep"
    try:
        perform_stop(orch, subdomain, target, graceful=graceful)
    except Exception:
        world_actions_total.labels(action=action, result="error").inc()
        log.exception("stop failed", extra={"subdomain": subdomain, "target": target.value})
        return _resolve(world_id, WorldState.ERROR, session_factory)
    world_actions_total.labels(action=action, result="success").inc()
    log.info("world stopped", extra={"subdomain": subdomain, "state": target.value})
    return _resolve(world_id, target, session_factory)


def finish_stop_within(
    world_id: uuid.UUID,
    subdomain: str,
    target: WorldState,
    orch: Orchestrator,
    wait_seconds: float,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
) -> WorldState | None:
    """Run :func:`finish_stop`, waiting up to ``wait_seconds`` for it.

    Returns the resolved state, or None if the wait runs out; the stop carries
    on in its own daemon thread and the row stays STOPPING until it is done.
    Lets a normally instant stop (removing a sleeping world's container) answer
    with the final state.
    """
    result: list[WorldState | None] = []

    def run() -> None:
        result.append(finish_stop(world_id, subdomain, target, orch, session_factory))

    thread = threading.Thread(target=run, name=f"stop-{subdomain}", daemon=True)
    thread.start()
    thread.join(wait_seconds)
    return result[0] if result else None


def _resolve(
    world_id: uuid.UUID,
    final: WorldState,
    session_factory: Callable[[], AbstractContextManager[Session]],
) -> WorldState | None:
    with session_factory() as db:
        world = db.get(World, world_id)
        if world is None:
            return None
        if world.state is WorldState.STOPPING:
            world.state = final
            world.stop_target = None
            db.commit()
        return world.state
