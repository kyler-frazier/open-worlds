"""Local-only sign-in that skips Google, for clicking through the app on a dev box.

Off unless PLATFORM_DEV_LOGIN=true, and the app refuses to start with it on
anywhere that looks like a real deployment: a misplaced flag must crash at boot,
not become a public login button. Each layer is enough on its own:

* The router is mounted only when the flag is on.
* ``check_dev_login_safe`` refuses inside a container, and unless the web
  origin, OAuth redirect base and database are all loopback.
* ``cookie_secure`` must be off.
* The entrypoint binds 127.0.0.1 and ignores X-Forwarded-For in this mode
  (see __main__), so the loopback check sees the real peer.
* docker-compose.yml never passes the flag, and preflight refuses a deploy
  .env that sets it. A test pins both.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from ..common.config import Settings, get_settings
from ..common.db import get_db
from ..common.logging import get_logger
from ..common.models import Role, User
from .deps import establish_session

log = get_logger(__name__)

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Marker files Docker and Podman drop into every container.
CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")
# Fake Google subjects. The prefix cannot collide with a real `sub`, which is
# all digits, so a dev user can never be mistaken for a real account.
DEV_SUB_PREFIX = "dev:"
DEV_ROLES = {"admin": Role.ADMIN, "user": Role.USER}


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def dev_login_problems(s: Settings) -> list[str]:
    """Every reason this process must not serve a dev login. Empty means safe."""
    problems = []
    if any(os.path.exists(marker) for marker in CONTAINER_MARKERS):
        problems.append("running inside a container (production always is)")
    if _host(s.web_origin) not in LOOPBACK_HOSTS:
        problems.append(f"PLATFORM_WEB_ORIGIN is not loopback ({s.web_origin})")
    if _host(s.oauth_redirect_base) not in LOOPBACK_HOSTS:
        problems.append(f"PLATFORM_OAUTH_REDIRECT_BASE is not loopback ({s.oauth_redirect_base})")
    try:
        db_host = (make_url(s.database_url).host or "").lower()
    except Exception:
        db_host = ""
    if db_host not in LOOPBACK_HOSTS:
        problems.append(f"PLATFORM_DATABASE_URL host is not loopback ({db_host or 'unparseable'})")
    if s.cookie_secure:
        problems.append("PLATFORM_COOKIE_SECURE is on (local dev is plain http)")
    return problems


def check_dev_login_safe(s: Settings) -> None:
    problems = dev_login_problems(s)
    if problems:
        raise RuntimeError(
            "PLATFORM_DEV_LOGIN is on, but this does not look like a local dev box: "
            + "; ".join(problems)
            + ". Refusing to start."
        )


router = APIRouter(prefix="/auth/dev", tags=["dev"])


@router.get("/login")
def dev_login(request: Request, role: str = "admin", db: Session = Depends(get_db)):
    """Sign in as a fixed fake admin or user, then land on the SPA."""
    peer = request.client.host if request.client else ""
    if peer not in LOOPBACK_HOSTS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if role not in DEV_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"role must be one of {sorted(DEV_ROLES)}",
        )

    sub = f"{DEV_SUB_PREFIX}{role}"
    user = db.execute(select(User).where(User.google_sub == sub)).scalar_one_or_none()
    if user is None:
        user = User(google_sub=sub, email=f"dev-{role}@localhost", display_name=f"Dev {role}")
        db.add(user)
    user.role = DEV_ROLES[role]
    user.last_login_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)

    establish_session(request, user)
    log.warning("DEV LOGIN used", extra={"role": role, "user_id": str(user.id)})
    return RedirectResponse(url=get_settings().web_origin, status_code=status.HTTP_303_SEE_OTHER)
