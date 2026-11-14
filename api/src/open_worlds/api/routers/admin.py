"""Admin routes: allow-list management and a global view of all worlds.

Approval is the single side-effecting step of the closed garden: an admin pastes
a Google `sub` and we create that user's row, which is what grants access.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.authz import AccessLevel
from ...common.config import get_settings
from ...common.db import get_db
from ...common.game_availability import GameAvailabilityError, game_statuses, set_enabled_games
from ...common.logging import get_logger
from ...common.models import User, World
from ...common.orchestrator import Orchestrator
from ...common.runtime_config import get_runtime_config, update_runtime_config
from ..deps import end_all_sessions, get_orchestrator, require_admin, require_csrf
from ..schemas import (
    AddUserIn,
    AdminGameOut,
    AdminGamesUpdate,
    MessageOut,
    RuntimeSettingsOut,
    RuntimeSettingsUpdate,
    UserOut,
    WorldOut,
)
from .worlds import WorldDeleteError, reconcile_rows, teardown_world, world_to_out

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_csrf)])
log = get_logger(__name__)


@router.get("/settings", response_model=RuntimeSettingsOut)
def get_settings_endpoint(_: User = Depends(require_admin)) -> RuntimeSettingsOut:
    cfg = get_runtime_config()
    return RuntimeSettingsOut(**cfg.model_dump())


@router.put("/settings", response_model=RuntimeSettingsOut)
def update_settings_endpoint(
    payload: RuntimeSettingsUpdate, admin: User = Depends(require_admin)
) -> RuntimeSettingsOut:
    cfg = update_runtime_config(**payload.model_dump(exclude_unset=True))
    log.info("runtime settings updated", extra={"by": str(admin.id)})
    return RuntimeSettingsOut(**cfg.model_dump())


@router.get("/games", response_model=list[AdminGameOut])
def list_games(
    _: User = Depends(require_admin), db: Session = Depends(get_db)
) -> list[AdminGameOut]:
    """Every installed game: offered or not, its worlds, and what it lacks."""
    return [AdminGameOut(**vars(g)) for g in game_statuses(db)]


@router.put("/games", response_model=list[AdminGameOut])
def update_games(
    payload: AdminGamesUpdate, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> list[AdminGameOut]:
    """Replace the offered games. See common/game_availability.py for the rules."""
    try:
        enabled = set_enabled_games(db, payload.enabled)
    except GameAvailabilityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    log.warning("offered games changed", extra={"games": sorted(enabled), "by": str(admin.id)})
    return [AdminGameOut(**vars(g)) for g in game_statuses(db)]


@router.get("/users", response_model=list[UserOut])
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[User]:
    return list(db.execute(select(User).order_by(User.created_at)).scalars())


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def add_user(
    payload: AddUserIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> User:
    sub = payload.google_sub.strip()
    existing = db.execute(select(User).where(User.google_sub == sub)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already approved")
    user = User(google_sub=sub, role=payload.role)
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info("user approved", extra={"by": str(admin.id), "google_sub": sub})
    return user


@router.post("/users/{user_id}/logout", response_model=MessageOut)
def force_logout(
    user_id: uuid.UUID, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> MessageOut:
    """End every session a user holds without touching their account.

    The lever between "do nothing" and "revoke": a shared machine left signed
    in, a cookie that may have leaked, or simply wanting someone out now. The
    user signs back in normally and keeps everything.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    end_all_sessions(user)
    db.commit()
    log.info("user force-logged-out", extra={"by": str(admin.id), "user_id": str(user_id)})
    return MessageOut(message="User signed out everywhere")


@router.delete("/users/{user_id}", response_model=MessageOut)
def revoke_user(
    user_id: uuid.UUID,
    delete_worlds: bool = Query(
        default=False,
        description="Tear down every world the user owns before removing them.",
    ),
    admin: User = Depends(require_admin),
    orch: Orchestrator = Depends(get_orchestrator),
    db: Session = Depends(get_db),
) -> MessageOut:
    """Remove a user from the allow-list.

    A user who still owns worlds is refused with the list unless
    ``delete_worlds`` is set; then each world gets the owner's Delete teardown
    first, so a failure midway leaves the user intact for a retry.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot revoke your own access"
        )
    if user.google_sub in get_settings().admin_subs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot revoke a seeded admin"
        )

    owned = list(
        db.execute(
            select(World).where(World.owner_id == user.id).order_by(World.created_at)
        ).scalars()
    )
    if owned and not delete_worlds:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "This user still owns worlds. Delete them to revoke access.",
                "worlds": [
                    world_to_out(w, AccessLevel.OWNER).model_dump(mode="json") for w in owned
                ],
            },
        )
    for index, world in enumerate(owned):
        try:
            teardown_world(world, orch, db)
        except WorldDeleteError as exc:
            remaining = len(owned) - index
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f'Could not delete "{world.display_name}": {exc} The user was not '
                    f"revoked and {remaining} of their worlds remain. Retry once the "
                    f"cause is fixed."
                ),
            ) from exc
        log.info(
            "world deleted on revoke",
            extra={"by": str(admin.id), "user_id": str(user_id), "world_id": str(world.id)},
        )

    db.delete(user)
    db.commit()
    log.info(
        "user revoked",
        extra={"by": str(admin.id), "user_id": str(user_id), "worlds_deleted": len(owned)},
    )
    return MessageOut(
        message="User access revoked" + (f" and {len(owned)} world(s) deleted" if owned else "")
    )


@router.get("/worlds", response_model=list[WorldOut])
def list_all_worlds(
    _: User = Depends(require_admin),
    orch: Orchestrator = Depends(get_orchestrator),
    db: Session = Depends(get_db),
) -> list[WorldOut]:
    rows = list(db.execute(select(World).order_by(World.created_at)).scalars())
    # Same one-listing reconcile as the owner's dashboard, so the admin view is
    # not stale until the worker's next sweep.
    reconcile_rows(rows, orch, db)
    # Shared builder rather than a second hand-rolled WorldOut: this list used to
    # construct its own and drifted out of sync the moment a field was added.
    return [world_to_out(w, AccessLevel.OWNER) for w in rows]
