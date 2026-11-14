"""Game adapter registry: the one place that maps a world's game to its
implementation.

Adapters are found through the ``open_worlds.games`` entry-point group, as the
built-in games are (see pyproject.toml).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import entry_points

from .base import (
    AdminClient,
    AdminUnavailable,
    GameAdapter,
    GameSettingsConflict,
    GameSettingsError,
    ImportedSettings,
    ImportPolicy,
    PeriodicJob,
    PlayerInfo,
)

ENTRY_POINT_GROUP = "open_worlds.games"

_adapters: dict[str, GameAdapter] | None = None


class UnknownGame(ValueError):
    """No adapter is registered for the requested game."""


def _load() -> dict[str, GameAdapter]:
    global _adapters
    if _adapters is None:
        found: dict[str, GameAdapter] = {}
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            adapter = ep.load()()
            if not isinstance(adapter, GameAdapter):
                raise TypeError(f"entry point {ep.name} is not a GameAdapter")
            if adapter.game != adapter.game.upper() or adapter.game in found:
                # Upper case because it is the stored value and the name in
                # the admin's Games list; unique because it is the world's key.
                raise ValueError(f"game id {adapter.game!r} is not upper case or not unique")
            found[adapter.game] = adapter
        if not found:
            # Every world would be unknown. The package metadata is missing,
            # usually an install that skipped `uv sync`.
            raise RuntimeError(f"no games registered under {ENTRY_POINT_GROUP}")
        _adapters = found
    return _adapters


def adapter_for(game: str) -> GameAdapter:
    """The adapter for a game id, as stored on the world row."""
    try:
        return _load()[str(game).upper()]
    except KeyError as exc:
        raise UnknownGame(f"No game called {game!r} is installed.") from exc


def all_adapters() -> list[GameAdapter]:
    """Every registered adapter, in a stable order."""
    return [a for _, a in sorted(_load().items())]


def game_ids() -> set[str]:
    return set(_load())


@contextmanager
def registered(adapter: GameAdapter) -> Iterator[GameAdapter]:
    """Register an adapter for the duration of a block. For tests."""
    adapters = _load()
    if adapter.game in adapters:
        raise ValueError(f"{adapter.game} is already registered")
    adapters[adapter.game] = adapter
    try:
        yield adapter
    finally:
        adapters.pop(adapter.game, None)


__all__ = [
    "AdminClient",
    "AdminUnavailable",
    "GameAdapter",
    "GameSettingsConflict",
    "GameSettingsError",
    "ImportPolicy",
    "ImportedSettings",
    "PeriodicJob",
    "PlayerInfo",
    "UnknownGame",
    "adapter_for",
    "all_adapters",
    "game_ids",
    "registered",
]
