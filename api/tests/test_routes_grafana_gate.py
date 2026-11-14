"""The edge's auth_request target for /grafana/.

nginx passes a Grafana request only on a 204, so the refusals matter, and the
identity handed to Grafana must come from the session, never the request.

Needs Docker for a throwaway Postgres; skipped without one.
"""

from __future__ import annotations

import uuid

import pytest
from tests.conftest_routes import login

pytest_plugins = ["tests.conftest_routes"]

GATE = "/api/auth/grafana"


@pytest.fixture
def client(app_client):
    c, _db = app_client
    return c


def test_anonymous_is_refused(client):
    assert client.get(GATE).status_code == 401


def test_a_normal_user_is_refused(client, make_user):
    login(client, make_user(role="USER"))
    r = client.get(GATE)
    assert r.status_code == 403
    assert "x-grafana-user" not in r.headers


def test_an_admin_is_let_through_as_themselves(client, make_user):
    login(client, make_user(role="ADMIN", email="root@example.test"))
    r = client.get(GATE)
    assert r.status_code == 204
    assert r.headers["x-grafana-user"] == "root@example.test"


def test_signing_out_everywhere_cuts_off_grafana(client, make_user):
    user = make_user(role="ADMIN")
    login(client, user, session_version=0)
    assert client.get(GATE).status_code == 204

    from open_worlds.common.db import session_scope
    from open_worlds.common.models import User

    with session_scope() as db:
        db.get(User, uuid.UUID(user["id"])).session_version = 1

    assert client.get(GATE).status_code == 401


def test_losing_admin_cuts_off_grafana(client, make_user):
    user = make_user(role="ADMIN")
    login(client, user)
    assert client.get(GATE).status_code == 204

    from open_worlds.common.db import session_scope
    from open_worlds.common.models import Role, User

    with session_scope() as db:
        db.get(User, uuid.UUID(user["id"])).role = Role.USER

    assert client.get(GATE).status_code == 403


def test_a_client_supplied_user_header_has_no_effect(client, make_user):
    """Whatever the caller claims, the answer follows the session."""
    claim = {"X-Grafana-User": "root@example.test", "X-WEBAUTH-USER": "root@example.test"}
    assert client.get(GATE, headers=claim).status_code == 401

    login(client, make_user(role="USER"))
    assert client.get(GATE, headers=claim).status_code == 403

    login(client, make_user(role="ADMIN", email="me@example.test"))
    r = client.get(GATE, headers=claim)
    assert r.status_code == 204
    assert r.headers["x-grafana-user"] == "me@example.test"
