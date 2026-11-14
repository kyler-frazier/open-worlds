"""Managing who, besides the owner, may act on a world.

Granting is the owner's alone; admins are excluded, as from imports, since
giving a third party access to someone else's world is not an operational
decision. Admins may revoke.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...common.authz import AccessLevel, Principal, is_world_owner
from ...common.db import get_db
from ...common.logging import get_logger
from ...common.models import CollaboratorLevel, User, World, WorldCollaborator
from ..deps import get_principal, load_world_for, require_csrf
from ..schemas import CollaboratorAdd, CollaboratorOut, CollaboratorUpdate, MessageOut

router = APIRouter(prefix="/worlds", tags=["collaborators"], dependencies=[Depends(require_csrf)])
log = get_logger(__name__)


def _require_owner(world: World, principal: Principal) -> None:
    """Strict ownership, admins excluded. See the module docstring."""
    if not is_world_owner(principal, str(world.owner_id)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the world's owner can change who has access to it.",
        )


def _out(row: WorldCollaborator, user: User) -> CollaboratorOut:
    return CollaboratorOut(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        level=row.level,
        created_at=row.created_at,
    )


@router.get("/{world_id}/collaborators", response_model=list[CollaboratorOut])
def list_collaborators(
    world_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[CollaboratorOut]:
    """Who can act on this world. Visible to anyone who can act on it.

    Not owner-only: if several people share a world, each of them seeing who
    else has a key is the normal expectation, and hiding it invites the "who
    turned the server off" question that nobody can answer.
    """
    world, _ = load_world_for(world_id, principal, db, need=AccessLevel.OPERATOR)
    rows = db.execute(
        select(WorldCollaborator, User)
        .join(User, User.id == WorldCollaborator.user_id)
        .where(WorldCollaborator.world_id == world.id)
        .order_by(WorldCollaborator.created_at)
    ).all()
    return [_out(row, user) for row, user in rows]


@router.post(
    "/{world_id}/collaborators",
    response_model=CollaboratorOut,
    status_code=status.HTTP_201_CREATED,
)
def add_collaborator(
    world_id: str,
    payload: CollaboratorAdd,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> CollaboratorOut:
    world, _ = load_world_for(world_id, principal, db, need=AccessLevel.OWNER)
    _require_owner(world, principal)

    email = payload.email.strip()
    # Case-insensitively, because nobody types their own address consistently
    # and Google hands it back in whatever case the account was created with.
    matches = (
        db.execute(select(User).where(func.lower(User.email) == email.lower())).scalars().all()
    )
    if not matches:
        # users.email is nullable and only populated on first sign-in, so an
        # approved-but-never-signed-in account genuinely cannot be found this
        # way. Say so, or the owner goes looking for a bug that is not there.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No approved user with that email. They need to sign in at least "
                "once before they can be added."
            ),
        )
    if len(matches) > 1:
        # users.email is not unique -- google_sub is the identity. Refusing is
        # the only safe answer; picking one would be a coin flip over who gets
        # access to someone's world.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="More than one account uses that email; ask an admin to sort it out.",
        )
    target = matches[0]

    if target.id == world.owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The owner already has full access.",
        )

    row = WorldCollaborator(
        world_id=world.id,
        user_id=target.id,
        level=payload.level,
        added_by_id=uuid.UUID(principal.user_id),
    )
    db.add(row)
    try:
        # Let the unique constraint decide rather than pre-checking: a check
        # followed by an insert is a race, and the constraint is the thing that
        # is actually true.
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That person already has access to this world.",
        ) from exc
    db.commit()
    db.refresh(row)
    log.info(
        "collaborator added",
        extra={
            "world_id": str(world.id),
            "user_id": str(target.id),
            "level": payload.level.value,
            "by": principal.user_id,
        },
    )
    return _out(row, target)


@router.patch("/{world_id}/collaborators/{user_id}", response_model=CollaboratorOut)
def update_collaborator(
    world_id: str,
    user_id: str,
    payload: CollaboratorUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> CollaboratorOut:
    world, _ = load_world_for(world_id, principal, db, need=AccessLevel.OWNER)
    _require_owner(world, principal)
    row, user = _find(world, user_id, db)
    row.level = payload.level
    db.commit()
    db.refresh(row)
    log.info(
        "collaborator level changed",
        extra={
            "world_id": str(world.id),
            "user_id": str(user.id),
            "level": payload.level.value,
            "by": principal.user_id,
        },
    )
    return _out(row, user)


@router.delete("/{world_id}/collaborators/{user_id}", response_model=MessageOut)
def remove_collaborator(
    world_id: str,
    user_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MessageOut:
    """Revoke access. The owner, an admin, or the collaborator themselves.

    Does not bump session_version, which would sign the person out
    everywhere; their next request to this world simply 404s.
    """
    leaving = user_id == principal.user_id
    need = AccessLevel.OPERATOR if leaving else AccessLevel.OWNER
    world, _ = load_world_for(world_id, principal, db, need=need)
    row, user = _find(world, user_id, db)
    db.delete(row)
    db.commit()
    log.info(
        "collaborator removed",
        extra={
            "world_id": str(world.id),
            "user_id": str(user.id),
            "by": principal.user_id,
            "self": leaving,
        },
    )
    return MessageOut(message="Access removed.")


def _find(world: World, user_id: str, db: Session) -> tuple[WorldCollaborator, User]:
    try:
        uid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not a collaborator on this world."
        ) from exc
    found = db.execute(
        select(WorldCollaborator, User)
        .join(User, User.id == WorldCollaborator.user_id)
        .where(WorldCollaborator.world_id == world.id, WorldCollaborator.user_id == uid)
    ).first()
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not a collaborator on this world."
        )
    return found[0], found[1]


__all__ = ["router", "CollaboratorLevel"]
