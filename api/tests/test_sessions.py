"""Session versioning: a cookie is only good at the user's current version."""

from __future__ import annotations

from types import SimpleNamespace

from open_worlds.api.deps import (
    SESSION_ID_KEY,
    SESSION_USER_KEY,
    SESSION_VERSION_KEY,
    end_all_sessions,
    establish_session,
    session_is_current,
)


def test_cookie_at_the_current_version_is_valid():
    assert session_is_current(3, 3)
    assert session_is_current("3", 3)


def test_bumping_the_version_invalidates_every_older_cookie():
    user = SimpleNamespace(id="u1", session_version=3)
    end_all_sessions(user)
    assert user.session_version == 4
    assert not session_is_current(3, user.session_version)


def test_a_cookie_without_a_version_is_refused():
    """Predates the field, or was tampered with; either way sign in again."""
    assert not session_is_current(None, 0)
    assert not session_is_current("", 0)
    assert not session_is_current("x", 0)


def test_establish_session_writes_id_and_version_and_nothing_else():
    session: dict = {"stale": "value"}
    request = SimpleNamespace(session=session)
    user = SimpleNamespace(id="u1", session_version=7)
    establish_session(request, user)
    sid = session.pop(SESSION_ID_KEY)
    assert isinstance(sid, str) and len(sid) >= 32
    assert session == {SESSION_USER_KEY: "u1", SESSION_VERSION_KEY: 7}
