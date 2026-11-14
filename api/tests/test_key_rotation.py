"""Rotating the Fernet key or the session secret breaks nothing stored."""

from __future__ import annotations

import base64
import json
import os
import uuid

import itsdangerous
import pytest
from cryptography.fernet import Fernet, InvalidToken
from tests.conftest_routes import login

from open_worlds.common import config, crypto

pytest_plugins = ["tests.conftest_routes"]


# The key the rest of the suite encrypted its rows with.
SUITE_KEY = os.environ["PLATFORM_FERNET_KEY"]


@pytest.fixture
def keys(monkeypatch):
    def use(current: str, old: str = "") -> None:
        monkeypatch.setenv("PLATFORM_FERNET_KEY", current)
        monkeypatch.setenv("PLATFORM_FERNET_OLD_KEYS", old)
        config.get_settings.cache_clear()

    yield use
    config.get_settings.cache_clear()


def test_a_token_from_the_old_key_still_decrypts_and_rotates(keys):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    keys(old)
    token = crypto.encrypt("refresh-token")

    keys(new, old)
    assert crypto.decrypt(token) == "refresh-token"
    rotated = crypto.reencrypt(token)

    # Once rotated, the old key can go.
    keys(new)
    assert crypto.decrypt(rotated) == "refresh-token"
    with pytest.raises(InvalidToken):
        crypto.decrypt(token)


def test_rotate_all_rewrites_every_stored_secret(app_client, make_user, keys):
    from open_worlds.common.db import session_scope
    from open_worlds.common.models import User
    from open_worlds.rotate_keys import rotate_all

    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    keys(old)
    user = make_user()
    with session_scope() as db:
        db.get(User, uuid.UUID(user["id"])).drive_refresh_token_enc = crypto.encrypt("r")

    # Rows other tests left are under the suite's key; list it too.
    keys(new, f"{old},{SUITE_KEY}")
    assert rotate_all() >= 1
    keys(new)
    with session_scope() as db:
        assert crypto.decrypt(db.get(User, uuid.UUID(user["id"])).drive_refresh_token_enc) == "r"


def _cookie(secret: str, user_id: str) -> str:
    data = base64.b64encode(
        json.dumps({"user_id": user_id, "session_version": 0, "sid": uuid.uuid4().hex}).encode()
    )
    return itsdangerous.TimestampSigner(secret).sign(data).decode()


@pytest.mark.parametrize(("old_secrets", "accepted"), [("retired-secret", True), ("", False)])
def test_a_cookie_signed_with_an_old_session_secret(monkeypatch, make_user, old_secrets, accepted):
    """Accepted while the old secret is listed, so a rotation signs nobody out."""
    from fastapi.testclient import TestClient

    from open_worlds.api import main as api_main

    monkeypatch.setenv("PLATFORM_SESSION_OLD_SECRETS", old_secrets)
    config.get_settings.cache_clear()
    user = make_user()
    with TestClient(api_main.create_app()) as client:
        s = config.get_settings()
        client.cookies.set(s.session_cookie_name, _cookie("retired-secret", user["id"]))
        assert (client.get("/api/auth/me").status_code == 200) is accepted
        # New cookies are signed with the current secret, which login() forges.
        login(client, user)
        assert client.get("/api/auth/me").status_code == 200
    config.get_settings.cache_clear()
