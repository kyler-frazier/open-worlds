"""Internal routes for on-host infrastructure, guarded by the shared secret.

Reached over the private `games` network only; the public proxy blocks
`/api/internal/`. Consumers:

* **pal-waker** reads the port map and asks to wake or sleep Palworld worlds.
  It has no Docker access, so container control stays here, beside the state.
* **the Minecraft placeholder** asks whether a subdomain is a real world, to
  tell an OFF world from a deleted or bogus host.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.capacity import CapacityExceeded, ensure_capacity_for
from ...common.config import get_settings
from ...common.db import get_db
from ...common.game_availability import enabled_games
from ...common.logging import get_logger
from ...common.metrics import world_actions_total
from ...common.models import World, WorldState
from ...common.orchestrator import Orchestrator, WorldImageUnavailable, backend_address
from ...common.specs import spec_for
from ...common.stopping import begin_stop, finish_stop
from ...common.subdomain import normalize_subdomain
from ...common.world_status import apply_state
from ..deps import get_orchestrator
from ..schemas import RouteStatusOut, WakeTargetOut, WakeTargetsOut

log = get_logger(__name__)

# Mid-transition, or stopped by the owner and staying off until they start it.
_NOT_WAKEABLE = (WorldState.DELETING, WorldState.STOPPING, WorldState.OFF)


def require_internal_secret(x_internal_secret: str | None = Header(default=None)) -> None:
    """Reject internal calls lacking the shared secret.

    This is the real boundary: every untrusted world container is on the same
    network, so reachability proves nothing about the caller. An empty secret
    refuses every call.
    """
    expected = get_settings().internal_secret
    if (
        not expected
        or x_internal_secret is None
        or not secrets.compare_digest(x_internal_secret, expected)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


router = APIRouter(
    prefix="/internal", tags=["internal"], dependencies=[Depends(require_internal_secret)]
)


@router.get("/route-status/{subdomain}", response_model=RouteStatusOut)
def route_status(subdomain: str, db: Session = Depends(get_db)) -> RouteStatusOut:
    """Whether a subdomain still maps to a world.

    A 404 makes the placeholder drop the connection, so the client shows a plain
    "Can't connect to server" rather than the "world is off" MOTD.
    """
    sub = normalize_subdomain(subdomain)
    state = db.execute(select(World.state).where(World.subdomain == sub)).scalar_one_or_none()
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown world")
    return RouteStatusOut(exists=True, state=state.value)


@router.get("/wake-targets", response_model=WakeTargetsOut)
def wake_targets(db: Session = Depends(get_db)) -> WakeTargetsOut:
    """The port map: which world owns which port, and its state.

    pal-waker listens on SLEEPING worlds' ports, relays for RUNNING ones and
    binds nothing for OFF ones. Hostname-routed games (Minecraft) have no port
    and do not appear.
    """
    worlds = db.scalars(
        select(World).where(World.game_port.is_not(None)).order_by(World.game_port)
    ).all()
    now = datetime.now(UTC)
    return WakeTargetsOut(
        targets=[
            WakeTargetOut(
                subdomain=w.subdomain,
                game_port=w.game_port,
                state=w.state.value,
                players_idle_seconds=players_idle_seconds(w, now),
            )
            for w in worlds
        ]
    )


def players_idle_seconds(world: World, now: datetime) -> float | None:
    """Seconds since the server last reported a player, or None when its
    count is stale or it has never been seen up."""
    s = get_settings()
    updated = world.players_updated_at
    if updated is None or (now - updated).total_seconds() > 3 * s.player_poll_seconds + 30:
        return None
    if world.player_count:
        return 0.0
    since = [t for t in (world.last_active_at, world.started_at) if t is not None]
    return (now - max(since)).total_seconds() if since else None


def _load(subdomain: str, db: Session) -> World:
    world = db.execute(
        select(World).where(World.subdomain == normalize_subdomain(subdomain))
    ).scalar_one_or_none()
    if world is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown world")
    return world


@router.post("/wake/{subdomain}", response_model=WakeTargetOut)
def wake(
    subdomain: str,
    db: Session = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
) -> WakeTargetOut:
    """Start a sleeping world because a player just tried to connect to it."""
    world = _load(subdomain, db)
    if world.state in _NOT_WAKEABLE:
        # The waker races owner actions; a world that turned OFF stays off.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"World is {world.state.value} and cannot be woken.",
        )
    if world.state is WorldState.RUNNING:
        return _target(world)
    if world.game not in enabled_games(db):
        # Same refusal as the owner's start.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{world.game} worlds are not enabled on this host.",
        )
    # Same budget as the owner's start. A 409 just leaves the port listening.
    try:
        ensure_capacity_for(world, db, get_settings())
    except CapacityExceeded as exc:
        world_actions_total.labels(action="wake", result="capacity").inc()
        log.warning("wake refused: host at capacity", extra={"subdomain": world.subdomain})
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    try:
        orch.start(spec_for(world, db))
    except WorldImageUnavailable:
        # A registry problem says nothing about the world, so its state is
        # left alone; ERROR would take it off the network until noticed.
        world_actions_total.labels(action="wake", result="error").inc()
        log.exception(
            "wake blocked by unavailable game image", extra={"subdomain": world.subdomain}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The game image is unavailable; the world was not started.",
        ) from None
    except Exception:
        world.state = WorldState.ERROR
        db.commit()
        world_actions_total.labels(action="wake", result="error").inc()
        log.exception("wake failed", extra={"subdomain": world.subdomain})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to wake world"
        ) from None

    # Resets readiness, as an owner's start does.
    apply_state(world, WorldState.RUNNING)
    db.commit()
    world_actions_total.labels(action="wake", result="success").inc()
    log.info("woke world on connect", extra={"subdomain": world.subdomain})
    return _target(world)


@router.post("/sleep/{subdomain}", response_model=WakeTargetOut)
def sleep(
    subdomain: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
) -> WakeTargetOut:
    """Stop an idle world, keeping its container so it can be woken again.

    Unlike OFF, which removes the container. Returns at once with the world
    STOPPING; the shutdown runs after the response, and the waker rebinds the
    port once it reads SLEEPING.
    """
    world = _load(subdomain, db)
    if world.state is not WorldState.RUNNING:
        return _target(world)

    begin_stop(world, WorldState.SLEEPING, db)
    background.add_task(finish_stop, world.id, world.subdomain, WorldState.SLEEPING, orch)
    world_actions_total.labels(action="sleep", result="accepted").inc()
    log.info("sleeping idle world", extra={"subdomain": world.subdomain})
    return _target(world)


def _target(world: World) -> WakeTargetOut:
    return WakeTargetOut(
        subdomain=world.subdomain,
        game_port=world.game_port,
        state=world.state.value,
        backend=backend_address(world.subdomain, world.game),
    )
