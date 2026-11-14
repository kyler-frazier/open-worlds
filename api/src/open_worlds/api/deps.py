"""Shared FastAPI dependencies: current user resolution and admin enforcement."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..common.authz import AccessLevel, Principal, at_least, resolve_access
from ..common.config import get_settings
from ..common.db import get_db
from ..common.models import Role, SignedOutSession, User, World, WorldCollaborator
from ..common.orchestrator import Orchestrator, make_orchestrator

SESSION_USER_KEY = "user_id"
# The user's session_version at login. A cookie whose version is behind the
# row's has been signed out everywhere and is refused, whatever its signature.
SESSION_VERSION_KEY = "session_version"
# A random id per sign-in, so logout can end this one cookie and no other.
SESSION_ID_KEY = "sid"


def establish_session(request: Request, user: User) -> None:
    """Sign a user in: the only place the session's contents are written."""
    request.session.clear()
    request.session[SESSION_USER_KEY] = str(user.id)
    request.session[SESSION_VERSION_KEY] = int(user.session_version)
    request.session[SESSION_ID_KEY] = secrets.token_urlsafe(24)


def end_this_session(request: Request, db: Session) -> None:
    """Sign out the cookie this request carries, wherever copies of it are.
    Caller commits."""
    sid = request.session.get(SESSION_ID_KEY)
    now = datetime.now(UTC)
    if isinstance(sid, str) and sid and db.get(SignedOutSession, sid) is None:
        ttl = timedelta(seconds=get_settings().session_max_age_seconds)
        db.add(SignedOutSession(session_id=sid, expires_at=now + ttl))
    # Rows past their cookie's lifetime protect nothing.
    db.execute(delete(SignedOutSession).where(SignedOutSession.expires_at < now))
    request.session.clear()


def end_all_sessions(user: User) -> None:
    """Invalidate every cookie this user holds. Caller commits."""
    user.session_version = int(user.session_version) + 1


# One shared orchestrator for the process, used by every router.
_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    """The world manager client, or direct Docker when none is configured
    (tests and dev runs)."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = make_orchestrator()
    return _orchestrator


# CSRF defence in depth beside SameSite=Lax and origin-locked CORS: a
# cross-site request cannot set a custom header without a preflight, which CORS
# rejects. The SPA sends it on every request.
CSRF_HEADER = "x-requested-with"
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def require_csrf(request: Request) -> None:
    if request.method.upper() in _CSRF_SAFE_METHODS:
        return
    # The header alone holds only while CORS stays strict, so also check where
    # the request came from. Browsers send Origin on every mutation and
    # Sec-Fetch-Site on every request; non-browser clients send neither.
    origin = request.headers.get("origin")
    ours = get_settings().web_origin.rstrip("/")
    cross_origin = origin is not None and origin.rstrip("/") != ours
    if (
        not request.headers.get(CSRF_HEADER)
        or cross_origin
        or request.headers.get("sec-fetch-site") == "cross-site"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid CSRF header.",
        )


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        uid = uuid.UUID(str(user_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        ) from exc
    user = db.get(User, uid)
    if user is None:
        # The user no longer exists (e.g. access revoked).
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if not session_is_live(request.session, user, db):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Signed out")
    return user


def session_is_live(session: dict, user: User, db: Session) -> bool:
    """Not signed out everywhere since it was issued, and not this cookie
    signed out on its own. A cookie without an id predates the id and is
    refused: signing in again issues one."""
    if not session_is_current(session.get(SESSION_VERSION_KEY), user.session_version):
        return False
    sid = session.get(SESSION_ID_KEY)
    if not isinstance(sid, str) or not sid:
        return False
    return db.get(SignedOutSession, sid) is None


def session_is_current(cookie_version: object, user_version: int) -> bool:
    """A cookie is valid only at the user's current version; none is refused."""
    try:
        return int(cookie_version) == int(user_version)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return False


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role is not Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def get_principal(user: User = Depends(get_current_user)) -> Principal:
    return Principal(
        user_id=str(user.id),
        google_sub=user.google_sub,
        is_admin=user.role is Role.ADMIN,
    )


def world_access(principal: Principal, world: World, db: Session) -> AccessLevel:
    """What this principal may do to this already-loaded world."""
    level = db.execute(
        select(WorldCollaborator.level).where(
            WorldCollaborator.world_id == world.id,
            WorldCollaborator.user_id == uuid.UUID(principal.user_id),
        )
    ).scalar_one_or_none()
    return resolve_access(principal, str(world.owner_id), level.value if level else None)


def load_world_for(
    world_id: str,
    principal: Principal,
    db: Session,
    *,
    need: AccessLevel,
) -> tuple[World, AccessLevel]:
    """Load a world, or refuse. The one place world authorization is decided.

    ``need`` has no default, so a route that forgets its tier does not run
    rather than inheriting the most permissive one.

    No access at all is a 404, so a stranger cannot learn the world exists.
    Some access but not enough is a 403: a collaborator already sees the world.
    """
    try:
        wid = uuid.UUID(world_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="World not found"
        ) from exc
    world = db.get(World, wid)
    if world is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="World not found")

    level = world_access(principal, world, db)
    if level is AccessLevel.NONE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="World not found")
    if not at_least(level, need):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_insufficient_detail(level, need),
        )
    return world, level


def _insufficient_detail(have: AccessLevel, need: AccessLevel) -> str:
    """Say what the caller has and who could do this; shown verbatim in the UI."""
    if need is AccessLevel.OWNER:
        return "Only the world's owner can do this."
    return (
        f"You have {have.value.lower()} access to this world. "
        "Only a manager or its owner can change its settings."
    )
