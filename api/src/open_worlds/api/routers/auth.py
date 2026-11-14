"""Authentication routes: Google login, session, and incremental Drive linking.

Closed-garden rule: logging in with Google never mutates state unless the user
is already approved. Unapproved subjects are redirected to a pending screen with
their `sub` to hand to an admin, and no database row is created for them. This
keeps unapproved sign-in attempts entirely side-effect free.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...common.authz import is_allowed
from ...common.config import get_settings
from ...common.crypto import encrypt
from ...common.db import get_db
from ...common.logging import get_logger
from ...common.models import Role, User
from ..deps import (
    SESSION_USER_KEY,
    end_all_sessions,
    end_this_session,
    establish_session,
    get_current_user,
    require_admin,
    require_csrf,
    session_is_live,
)
from ..oauth import drive_authorize_params, init_oauth
from ..schemas import MeOut, PendingOut

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)


@router.get("/google/login")
async def google_login(request: Request):
    oauth = init_oauth()
    return await oauth.google.authorize_redirect(request, get_settings().google_redirect_uri)


@router.get("/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    s = get_settings()
    oauth = init_oauth()
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        log.warning("oauth error", extra={"error": str(exc)})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OAuth failed") from exc

    userinfo = token.get("userinfo") or {}
    google_sub = userinfo.get("sub")
    if not google_sub:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No subject in token")

    email = userinfo.get("email")
    name = userinfo.get("name")

    existing = db.execute(select(User).where(User.google_sub == google_sub)).scalar_one_or_none()
    approved_subs = {existing.google_sub} if existing else set()

    if not is_allowed(google_sub, approved_subs, s.admin_subs):
        # No side effects: do not create a user row for unapproved subjects.
        log.info("pending access request", extra={"google_sub": google_sub})
        return RedirectResponse(
            url=f"{s.web_origin.rstrip('/')}/pending?sub={google_sub}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    user = existing
    if user is None:
        # Only reachable for seeded admins on first login.
        user = User(google_sub=google_sub, role=Role.ADMIN)
        db.add(user)
    if google_sub in s.admin_subs and user.role is not Role.ADMIN:
        user.role = Role.ADMIN
    user.email = email
    user.display_name = name
    user.last_login_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)

    establish_session(request, user)
    return RedirectResponse(url=s.web_origin, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)) -> MeOut:
    return MeOut(
        id=user.id,
        google_sub=user.google_sub,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        drive_linked=user.drive_linked,
        is_admin=user.role is Role.ADMIN,
    )


# Response header carrying the Grafana login. The edge copies it into the
# header Grafana trusts; the client never gets to set that one.
GRAFANA_USER_HEADER = "X-Grafana-User"


@router.get("/grafana", status_code=status.HTTP_204_NO_CONTENT)
def grafana_gate(user: User = Depends(require_admin)) -> Response:
    """Target of the edge's ``auth_request`` for ``/grafana/``.

    Every Grafana request is checked here first, so this is the boundary: the
    same session check as every other route (signature, user still exists,
    ``session_version``), then admin only. 401/403 come from the dependencies.
    """
    login = user.email or f"admin-{user.id}"
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={GRAFANA_USER_HEADER: login})


@router.get("/pending", response_model=PendingOut)
def pending(sub: str) -> PendingOut:
    """Echo endpoint the frontend can use to render the pending screen."""
    return PendingOut(google_sub=sub)


@router.post("/logout", dependencies=[Depends(require_csrf)])
def logout(request: Request, db: Session = Depends(get_db)):
    # Ends the cookie itself, not just the browser's copy: a stolen copy
    # stops working too.
    end_this_session(request, db)
    db.commit()
    return {"message": "logged out"}


@router.post("/logout-all", dependencies=[Depends(require_csrf)])
def logout_all(
    request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Sign out of every browser, this one included.

    Plain logout only clears this browser's cookie; any other copy of it stays
    valid for the rest of its week. This bumps the user's session version so
    every existing cookie is refused from now on.
    """
    end_all_sessions(user)
    db.commit()
    request.session.clear()
    log.info("user signed out everywhere", extra={"user_id": str(user.id)})
    return {"message": "signed out everywhere"}


@router.get("/drive/link")
async def drive_link(request: Request, user: User = Depends(get_current_user)):
    s = get_settings()
    oauth = init_oauth()
    return await oauth.google.authorize_redirect(
        request, s.google_drive_redirect_uri, **drive_authorize_params()
    )


@router.get("/drive/callback")
async def drive_callback(request: Request, db: Session = Depends(get_db)):
    s = get_settings()
    oauth = init_oauth()
    # Drive linking requires an active session (user must be logged in).
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OAuth failed") from exc

    # The Drive consent has to come from the account this user signed in
    # with. Linking a different Google account's Drive would put their backups
    # somewhere they cannot see from the dashboard's own identity. The link
    # asks for openid, so a missing sub is refused rather than waved through.
    linked_sub = (token.get("userinfo") or {}).get("sub")
    user_for_check = db.get(User, uuid.UUID(str(user_id)))
    if not linked_sub or (user_for_check is not None and linked_sub != user_for_check.google_sub):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Link Drive with the same Google account you sign in with here.",
        )

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        # Without a refresh token we cannot perform background backups.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Drive did not return a refresh token; please retry and grant offline access.",
        )

    user = db.get(User, uuid.UUID(str(user_id)))
    if user is None or not session_is_live(request.session, user, db):
        # Same rule as get_current_user: a cookie signed out everywhere must
        # not be able to attach a Drive to the account.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user.drive_refresh_token_enc = encrypt(refresh_token)
    user.drive_linked = True
    db.commit()
    return RedirectResponse(
        url=f"{s.web_origin.rstrip('/')}/?drive=linked", status_code=status.HTTP_303_SEE_OTHER
    )
