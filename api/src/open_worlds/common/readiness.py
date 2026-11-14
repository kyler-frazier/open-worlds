"""Whether a running world is actually joinable yet.

RUNNING means the container is up, not that the server has booted (30-60s for
Minecraft, often more for Palworld). Docker's healthcheck cannot tell: Palworld's
is ``pgrep``, which passes as soon as the binary launches. Instead a world is
ready once its admin interface (RCON, or Palworld's REST API) answers, which
works the same for every game.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from .models import World, WorldState


class Readiness(str, enum.Enum):
    """How far along a world is toward accepting players."""

    #: Not running at all; readiness is not a meaningful question.
    NOT_RUNNING = "NOT_RUNNING"
    #: Container up, admin interface has not answered yet. Still booting.
    STARTING = "STARTING"
    #: Admin interface answered. A player can join.
    READY = "READY"
    #: Up well past the boot window and the admin interface has never answered.
    #: A real fault, shown rather than hidden.
    UNRESPONSIVE = "UNRESPONSIVE"


def readiness_for(world: World, timeout_seconds: int, now: datetime | None = None) -> Readiness:
    """Classify a world's readiness from its start/ready stamps.

    ``timeout_seconds`` should be generously longer than a normal boot: calling
    a slow world unresponsive is the worse error.
    """
    if world.state is not WorldState.RUNNING:
        return Readiness.NOT_RUNNING
    if world.ready_at is not None:
        return Readiness.READY
    started = world.started_at
    if started is None:
        # An adopted container: nothing to time out against.
        return Readiness.STARTING
    elapsed = ((now or datetime.now(UTC)) - started).total_seconds()
    return Readiness.STARTING if elapsed < timeout_seconds else Readiness.UNRESPONSIVE
