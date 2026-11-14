"""Host memory budget: refuse a start that would oversubscribe the machine.

Worlds start on an owner's click and on a player connecting, so without a
budget a busy evening ends in swap or the OOM killer. RUNNING and STOPPING
worlds hold their limit, and so do SLEEPING ones: they wake when a player
connects, so their memory is already promised. Only OFF worlds are free.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .models import World, WorldState

# States that hold memory, or can claim it without anyone pressing Turn on.
MEMORY_HOLDING_STATES = (WorldState.RUNNING, WorldState.STOPPING, WorldState.SLEEPING)


class CapacityExceeded(RuntimeError):
    """Starting this world would push the host past its memory budget."""


def memory_headroom_mb(in_use_mb: int, budget_mb: int | None) -> int | None:
    """How much of the budget is left, or None when there is no budget."""
    if budget_mb is None:
        return None
    return budget_mb - in_use_mb


def check_memory_budget(in_use_mb: int, needed_mb: int, budget_mb: int | None) -> None:
    """Pure rule: raise if ``needed_mb`` does not fit in what is left.

    ``in_use_mb`` must exclude the world being started, so it cannot refuse
    itself.
    """
    headroom = memory_headroom_mb(in_use_mb, budget_mb)
    if headroom is None or needed_mb <= headroom:
        return
    raise CapacityExceeded(
        f"The server is at capacity: {in_use_mb} MB of its {budget_mb} MB world budget "
        f"is in use or reserved by sleeping worlds, and this world needs {needed_mb} MB. "
        f"Try again once another world has been turned off, or ask an administrator."
    )


def memory_in_use_mb(db: Session, exclude_world_id: uuid.UUID | None = None) -> int:
    """Sum of the limits of every world holding or reserving memory."""
    stmt = select(func.coalesce(func.sum(World.memory_mb), 0)).where(
        World.state.in_(MEMORY_HOLDING_STATES)
    )
    if exclude_world_id is not None:
        stmt = stmt.where(World.id != exclude_world_id)
    return int(db.execute(stmt).scalar_one())


def ensure_capacity_for(world: World, db: Session, s: Settings) -> None:
    """Refuse to start ``world`` if the host's memory budget cannot take it."""
    if s.host_memory_budget_mb is None:
        return
    check_memory_budget(
        memory_in_use_mb(db, exclude_world_id=world.id), world.memory_mb, s.host_memory_budget_mb
    )
