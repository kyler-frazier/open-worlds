"""The OAuth callback branches: who gets an account, and whose Drive is linked.

Only Google's token exchange is stubbed; the allow-list, admin seeding,
subject comparison and session write are the real code.

Needs Docker for a throwaway Postgres; skipped without one.
"""

from __future__ import annotations

import uuid

import pytest
from tests.conftest_routes import login

pytest_plugins = ["tests.conftest_routes"]


@pytest.fixture
def client(app_client):
    c, _db = app_client
    return c


def _stub_google(monkeypatch, userinfo, refresh_token=None, fail=False):
    """Make the token exchange return what we say, without talking to Google."""
    from authlib.integrations.starlette_client import OAuthError

    from open_worlds.api.routers import auth as auth_router

    class _Google:
        async def authorize_access_token(self, _request):
            if fail:
                raise OAuthError("bad state")
            token = {"userinfo": userinfo}
            if refresh_token is not None:
                token["refresh_token"] = refresh_token
            return token

    class _OAuth:
        google = _Google()

    monkeypatch.setattr(auth_router, "init_oauth", lambda: _OAuth())
    return _OAuth


def _sub_of(client, user_id):
    from open_worlds.common.db import session_scope
    from open_worlds.common.models import User

    with session_scope() as db:
        u = db.get(User, uuid.UUID(user_id))
        return u.google_sub if u else None


# --- sign-in ---------------------------------------------------------------


def test_an_unapproved_subject_gets_no_account(client, monkeypatch):
    """The important half is the side effect that must not happen.

    Turning someone away is visible; quietly creating their user row is not,
    and it would put a stranger in the admin allow-list UI as though they had
    been approved.
    """
    _stub_google(monkeypatch, {"sub": "stranger-123", "email": "who@example.test"})

    resp = client.get("/api/auth/google/callback", follow_redirects=False)
    assert resp.status_code == 303
    assert "/pending" in resp.headers["location"]

    from sqlalchemy import select

    from open_worlds.common.db import session_scope
    from open_worlds.common.models import User

    with session_scope() as db:
        found = db.execute(
            select(User).where(User.google_sub == "stranger-123")
        ).scalar_one_or_none()
    assert found is None, "an unapproved sign-in must not create a user row"


def test_a_token_without_a_subject_is_refused(client, monkeypatch):
    """No sub, no identity. Accepting this would key an account on nothing."""
    _stub_google(monkeypatch, {"email": "nosub@example.test"})
    assert client.get("/api/auth/google/callback", follow_redirects=False).status_code == 400


def test_a_failed_exchange_is_refused(client, monkeypatch):
    _stub_google(monkeypatch, {}, fail=True)
    assert client.get("/api/auth/google/callback", follow_redirects=False).status_code == 400


def test_an_existing_user_is_signed_in(client, make_user, monkeypatch):
    user = make_user()
    sub = _sub_of(client, user["id"])
    _stub_google(monkeypatch, {"sub": sub, "email": "back@example.test", "name": "Back"})

    resp = client.get("/api/auth/google/callback", follow_redirects=False)
    assert resp.status_code == 303
    # The session was established, so the client is now authenticated.
    assert client.get("/api/auth/me").status_code == 200


def test_a_seeded_admin_is_created_on_first_login(client, monkeypatch):
    """The one path that may create a row for someone with no existing account."""
    from open_worlds.common import config

    seeded = "seeded-admin-sub"
    monkeypatch.setenv("PLATFORM_ADMIN_GOOGLE_SUBS", seeded)
    config.get_settings.cache_clear()
    _stub_google(monkeypatch, {"sub": seeded, "email": "admin@example.test"})

    resp = client.get("/api/auth/google/callback", follow_redirects=False)
    assert resp.status_code == 303
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["is_admin"] is True
    config.get_settings.cache_clear()


# --- Drive linking ---------------------------------------------------------


def test_drive_linking_requires_a_session(client, monkeypatch):
    _stub_google(monkeypatch, {"sub": "x"}, refresh_token="r")
    assert client.get("/api/auth/drive/callback", follow_redirects=False).status_code == 401


def test_drive_must_be_linked_with_the_same_google_account(client, make_user, monkeypatch):
    """The check that stops a user attaching someone else's Drive.

    Backups would then be written into an account the owner cannot see from the
    dashboard, using a refresh token this user handed over -- so the failure is
    silent and the data goes somewhere nobody is looking.
    """
    user = make_user()
    login(client, user)
    _stub_google(monkeypatch, {"sub": "a-different-google-account"}, refresh_token="r")

    resp = client.get("/api/auth/drive/callback", follow_redirects=False)
    assert resp.status_code == 400
    assert "same Google account" in resp.json()["detail"]


@pytest.mark.parametrize("userinfo", [{}, {"sub": ""}, None])
def test_drive_without_an_account_id_is_refused(client, make_user, monkeypatch, userinfo):
    """No sub means the account cannot be checked, so the link fails closed."""
    login(client, make_user())
    _stub_google(monkeypatch, userinfo, refresh_token="r")

    resp = client.get("/api/auth/drive/callback", follow_redirects=False)
    assert resp.status_code == 400


def test_drive_without_a_refresh_token_is_refused(client, make_user, monkeypatch):
    """Without one there is no background access, so the link would look
    successful and every scheduled backup would fail."""
    user = make_user()
    login(client, user)
    _stub_google(monkeypatch, {"sub": _sub_of(client, user["id"])}, refresh_token=None)

    resp = client.get("/api/auth/drive/callback", follow_redirects=False)
    assert resp.status_code == 400
    assert "refresh token" in resp.json()["detail"].lower()


def test_drive_links_for_the_matching_account(client, make_user, monkeypatch):
    user = make_user()
    login(client, user)
    _stub_google(monkeypatch, {"sub": _sub_of(client, user["id"])}, refresh_token="a-refresh-token")

    resp = client.get("/api/auth/drive/callback", follow_redirects=False)
    assert resp.status_code in (200, 303)

    from open_worlds.common.db import session_scope
    from open_worlds.common.models import User

    with session_scope() as db:
        refreshed = db.get(User, uuid.UUID(user["id"]))
        assert refreshed.drive_linked is True
        # Stored encrypted, never in the clear.
        assert refreshed.drive_refresh_token_enc
        assert "a-refresh-token" not in refreshed.drive_refresh_token_enc
