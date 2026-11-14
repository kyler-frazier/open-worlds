import pytest

from open_worlds.common.authz import (
    AccessLevel,
    Principal,
    at_least,
    is_allowed,
    is_world_owner,
    resolve_access,
)

OWNER = Principal(user_id="u1", google_sub="s1", is_admin=False)
STRANGER = Principal(user_id="u9", google_sub="s9", is_admin=False)
ADMIN = Principal(user_id="a1", google_sub="sa", is_admin=True)


def test_is_allowed_for_approved_subject():
    assert is_allowed("sub-a", {"sub-a"}, set())


def test_is_allowed_for_seeded_admin_even_if_not_in_approved():
    assert is_allowed("admin-1", set(), {"admin-1"})


def test_not_allowed_for_unknown_subject():
    assert not is_allowed("stranger", {"sub-a"}, {"admin-1"})


# --- the ownership axis (admins deliberately fail it) ------------------------


def test_admin_status_does_not_make_an_owner():
    """Imports and restores are the owner's alone; an admin manages, not fills."""
    assert not is_world_owner(ADMIN, "u2")
    assert is_world_owner(ADMIN, "a1")


# --- the ladder --------------------------------------------------------------


@pytest.mark.parametrize(
    "have,need,expected",
    [
        (AccessLevel.OWNER, AccessLevel.OPERATOR, True),
        (AccessLevel.OWNER, AccessLevel.OWNER, True),
        (AccessLevel.MANAGER, AccessLevel.OPERATOR, True),
        (AccessLevel.MANAGER, AccessLevel.OWNER, False),
        (AccessLevel.OPERATOR, AccessLevel.OPERATOR, True),
        (AccessLevel.OPERATOR, AccessLevel.MANAGER, False),
        (AccessLevel.NONE, AccessLevel.OPERATOR, False),
    ],
)
def test_at_least(have, need, expected):
    assert at_least(have, need) is expected


def test_owner_resolves_to_owner():
    assert resolve_access(OWNER, "u1", None) is AccessLevel.OWNER


def test_admin_resolves_to_owner_on_the_ladder():
    """Reproduces what can_manage_world allowed, so the refactor changes nothing."""
    assert resolve_access(ADMIN, "u2", None) is AccessLevel.OWNER


def test_stranger_with_no_grant_has_none():
    assert resolve_access(STRANGER, "u1", None) is AccessLevel.NONE


@pytest.mark.parametrize("level", ["OPERATOR", "MANAGER"])
def test_a_grant_confers_its_level(level):
    assert resolve_access(STRANGER, "u1", level) is AccessLevel(level)


def test_a_grant_never_demotes_the_owner():
    """The ordering inside resolve_access is the whole point of this test.

    An owner holding a stale OPERATOR row on their own world must still be the
    owner. Checking the row first would silently strip them of their own world.
    """
    assert resolve_access(OWNER, "u1", "OPERATOR") is AccessLevel.OWNER


def test_a_grant_never_demotes_an_admin():
    assert resolve_access(ADMIN, "u2", "OPERATOR") is AccessLevel.OWNER


def test_an_unrecognised_stored_level_fails_closed():
    """A value the code does not know must deny, never default to something."""
    assert resolve_access(STRANGER, "u1", "SUPERUSER") is AccessLevel.NONE
    assert resolve_access(STRANGER, "u1", "") is AccessLevel.NONE
