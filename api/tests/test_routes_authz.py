"""Authorization and CSRF, exercised through the HTTP stack.

A missing ``Depends(require_admin)`` is invisible to a unit test.

Needs Docker for a throwaway Postgres; skipped without one.
"""

from __future__ import annotations

import uuid

import pytest
from tests.conftest_routes import CSRF, login

pytest_plugins = ["tests.conftest_routes"]


@pytest.fixture
def client(app_client):
    c, _db = app_client
    return c


# --- authentication --------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/worlds"),
        ("POST", "/api/worlds"),
        ("GET", "/api/worlds/restorable-backups"),
        ("GET", "/api/worlds/drive-backups"),
        ("GET", "/api/admin/users"),
        ("GET", "/api/auth/me"),
    ],
)
def test_every_route_requires_a_session(client, method, path):
    """No cookie, no access -- including the routes added most recently, which
    are the ones most likely to have been wired up without a guard."""
    assert client.request(method, path, headers=CSRF).status_code == 401


def test_a_forged_session_cookie_is_rejected(client, make_user):
    """The cookie is signed. A value we did not sign must not authenticate,
    or the session is just a username in a header."""
    user = make_user()
    import base64
    import json

    from open_worlds.common.config import get_settings

    unsigned = base64.b64encode(json.dumps({"user_id": user["id"]}).encode()).decode()
    client.cookies.set(get_settings().session_cookie_name, unsigned)
    assert client.get("/api/auth/me").status_code == 401


def test_a_session_for_a_deleted_user_is_rejected(client, make_user):
    """Revoking a user has to take effect immediately, not at cookie expiry."""
    user = make_user()
    login(client, user)
    assert client.get("/api/auth/me").status_code == 200

    from open_worlds.common.db import session_scope
    from open_worlds.common.models import User

    with session_scope() as db:
        db.delete(db.get(User, uuid.UUID(user["id"])))

    assert client.get("/api/auth/me").status_code == 401


def test_a_stale_session_version_is_rejected(client, make_user):
    """ "Sign out everywhere" bumps the counter; old cookies must stop working."""
    user = make_user()
    login(client, user, session_version=0)
    assert client.get("/api/auth/me").status_code == 200

    from open_worlds.common.db import session_scope
    from open_worlds.common.models import User

    with session_scope() as db:
        db.get(User, uuid.UUID(user["id"])).session_version = 1

    assert client.get("/api/auth/me").status_code == 401


# --- admin -----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/admin/users", "/api/admin/settings"])
def test_admin_routes_refuse_a_normal_user(client, make_user, path):
    login(client, make_user(role="USER"))
    assert client.get(path).status_code == 403


def test_admin_routes_admit_an_admin(client, make_user):
    login(client, make_user(role="ADMIN"))
    assert client.get("/api/admin/users").status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/api/admin/users/not-a-uuid/logout"), ("DELETE", "/api/admin/users/not-a-uuid")],
)
def test_a_malformed_user_id_is_a_422_not_a_500(client, make_user, method, path):
    login(client, make_user(role="ADMIN"))
    assert client.request(method, path, headers=CSRF).status_code == 422


# --- CSRF ------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/worlds"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/logout-all"),
    ],
)
def test_mutating_routes_require_the_csrf_header(client, make_user, method, path):
    """The cookie rides along on a cross-site form post; a custom header does
    not. Its absence is the whole defence, so its absence must be refused."""
    login(client, make_user())
    assert client.request(method, path, json={}).status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"sec-fetch-site": "cross-site"},
    ],
)
def test_a_mutation_from_another_site_is_refused_even_with_the_header(client, make_user, headers):
    """The header is only a defence while CORS stays strict; where the request
    came from is checked as well."""
    login(client, make_user())
    resp = client.post("/api/auth/logout", headers={**CSRF, **headers})
    assert resp.status_code == 403


def test_a_mutation_from_the_dashboard_passes(client, make_user):
    from open_worlds.common.config import get_settings

    login(client, make_user())
    headers = {**CSRF, "origin": get_settings().web_origin, "sec-fetch-site": "same-origin"}
    assert client.post("/api/auth/logout", headers=headers).status_code == 200


def test_reads_do_not_require_the_csrf_header(client, make_user):
    login(client, make_user())
    assert client.get("/api/worlds").status_code == 200


def test_logout_expires_the_session_cookie(client, make_user):
    """Asserted on the response, not on a follow-up request.

    Starlette clears a session by sending the cookie back already expired, and a
    browser drops it. httpx2's jar does not drop a cookie that was set on the
    client directly, which is how these tests sign in -- so a follow-up request
    would still carry it and prove nothing about the app.
    """
    login(client, make_user())
    resp = client.post("/api/auth/logout", headers=CSRF)
    assert resp.status_code in (200, 204)

    set_cookie = resp.headers.get("set-cookie", "")
    assert "01 Jan 1970" in set_cookie, set_cookie
