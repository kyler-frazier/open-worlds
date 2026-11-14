"""The games this host runs, and what each one supports.

Generated from the adapters, so the UI can build a game's forms without
knowing it in advance: a game with no web module of its own still gets a
working create and edit form from ``settings_schema``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ...common.config import get_settings
from ...common.db import get_db
from ...common.games import UnknownGame, adapter_for
from ...common.models import User
from ..deps import get_current_user
from ..schemas import GameOut
from .worlds import games_out

router = APIRouter(prefix="/games", tags=["games"])


@router.get("/{game}/art")
def game_art(game: str, _: User = Depends(get_current_user)) -> FileResponse:
    """The game's card artwork, when the operator supplied some.

    Served by the api rather than shipped in the web build: it is the games'
    own art, so each operator brings it (PLATFORM_GAME_ART_DIR).
    """
    try:
        path = adapter_for(game).art_path()
    except UnknownGame:
        path = None
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # The URL carries the file's mtime, so it can be cached for good.
    return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@router.get("", response_model=list[GameOut])
def list_games(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[GameOut]:
    return games_out(db, get_settings())
