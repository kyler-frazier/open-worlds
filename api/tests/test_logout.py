"""Logout ends the cookie it was sent with, not only the browser's copy."""

from __future__ import annotations

from tests.conftest_routes import CSRF, login

pytest_plugins = ["tests.conftest_routes"]


def test_a_copy_of_a_signed_out_cookie_is_refused(app_client, make_user):
    client, _ = app_client
    user = make_user()
    login(client, user, sid="stolen-copy")
    assert client.get("/api/auth/me").status_code == 200

    assert client.post("/api/auth/logout", headers=CSRF).status_code == 200

    # The attacker's copy: the same signed cookie, replayed after logout.
    login(client, user, sid="stolen-copy")
    assert client.get("/api/auth/me").status_code == 401


def test_logout_leaves_the_users_other_browsers_signed_in(app_client, make_user):
    client, _ = app_client
    user = make_user()
    login(client, user, sid="laptop")
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 200

    login(client, user, sid="phone")
    assert client.get("/api/auth/me").status_code == 200


def test_a_cookie_without_a_session_id_is_refused(app_client, make_user):
    """Issued before ids existed; signing in again issues one."""
    client, _ = app_client
    login(client, make_user(), sid="")
    assert client.get("/api/auth/me").status_code == 401
