"""Which games this platform offers, as an admin sets it.

Stored in the runtime config (Admin > Settings > Games), since it is a platform
decision rather than a host one. The rules:

* At least one game is always offered.
* A game with worlds, in any state, stays offered, even before the list is
  first saved.
* A game is added only if nothing it needs is missing on this host
  (``GameAdapter.enable_problems``), above all its router.

Unset, the list is every game whose adapter sets ``enabled_by_default``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .games import all_adapters, game_ids
from .models import World
from .runtime_config import get_runtime_config, update_runtime_config


class GameAvailabilityError(ValueError):
    """An admin's change to the offered games was refused. Meant for the user."""


def world_counts(db: Session) -> dict[str, int]:
    """How many worlds each game has, whatever their state."""
    rows = db.execute(select(World.game, func.count()).group_by(World.game)).all()
    return {game: int(n) for game, n in rows}


def _chosen() -> set[str]:
    cfg = get_runtime_config().enabled_games
    if cfg is None:
        return {a.game for a in all_adapters() if a.enabled_by_default}
    return {g.upper() for g in cfg}


def enabled_games(db: Session) -> set[str]:
    """Games worlds may be created and started for."""
    return (_chosen() | set(world_counts(db))) & game_ids()


@dataclass(frozen=True)
class GameStatus:
    game: str
    display_name: str
    enabled: bool
    world_count: int
    #: Why it cannot be enabled (or, if enabled, what is broken). Empty: fine.
    problems: list[str]


def game_statuses(db: Session) -> list[GameStatus]:
    counts = world_counts(db)
    enabled = enabled_games(db)
    return [
        GameStatus(
            game=a.game,
            display_name=a.display_name,
            enabled=a.game in enabled,
            world_count=counts.get(a.game, 0),
            problems=a.enable_problems(),
        )
        for a in all_adapters()
    ]


def set_enabled_games(db: Session, requested: list[str]) -> set[str]:
    """Replace the offered games, or raise GameAvailabilityError saying why not."""
    wanted = {g.strip().upper() for g in requested if g.strip()}
    unknown = wanted - game_ids()
    if unknown:
        names = ", ".join(sorted(unknown))
        raise GameAvailabilityError(f"Not installed on this platform: {names}.")
    if not wanted:
        raise GameAvailabilityError("At least one game must stay available.")
    counts = world_counts(db)
    kept = sorted(g for g in set(counts) - wanted)
    if kept:
        detail = ", ".join(f"{g} ({counts[g]} world{'s' if counts[g] != 1 else ''})" for g in kept)
        raise GameAvailabilityError(
            f"Can't remove a game that still has worlds: {detail}. Delete its worlds first."
        )
    current = enabled_games(db)
    for adapter in all_adapters():
        if adapter.game in wanted - current:
            problems = adapter.enable_problems()
            if problems:
                raise GameAvailabilityError(
                    f"{adapter.display_name} can't be enabled yet: {' '.join(problems)}"
                )
    update_runtime_config(enabled_games=sorted(wanted))
    return wanted
