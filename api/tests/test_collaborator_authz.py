"""The permission matrix: who may call what, over the real HTTP stack.

A tier missed on one route is a privilege escalation, so exhaustiveness matters
more than depth:

* :func:`test_permission_matrix`: every world route crossed with every kind of
  caller, asserting the refusal (or its absence).
* :func:`test_every_world_route_is_in_the_matrix`: fails if a route exists that
  the matrix does not mention.
"""

from __future__ import annotations

import uuid

import pytest
from tests.conftest_routes import CSRF, login, logout

pytest_plugins = ["tests.conftest_routes"]

# (method, path template, level required, owner-only-even-for-admins)
#
# Keep this in the same order as the routers so a reviewer can read them side
# by side. "OPERATOR" is the floor: anything a collaborator can reach at all.
WORLD_ROUTES: list[tuple[str, str, str, bool]] = [
    ("GET", "/api/worlds/{world_id}", "OPERATOR", False),
    ("POST", "/api/worlds/{world_id}/start", "OPERATOR", False),
    ("POST", "/api/worlds/{world_id}/stop", "OPERATOR", False),
    ("GET", "/api/worlds/{world_id}/backups", "OPERATOR", False),
    ("PATCH", "/api/worlds/{world_id}", "MANAGER", False),
    # The icon: seeing it is seeing the world; changing it is a settings edit.
    ("GET", "/api/worlds/{world_id}/icon", "OPERATOR", False),
    ("GET", "/api/worlds/{world_id}/icon/files", "OPERATOR", False),
    ("PUT", "/api/worlds/{world_id}/icon", "MANAGER", False),
    ("DELETE", "/api/worlds/{world_id}/icon", "MANAGER", False),
    ("DELETE", "/api/worlds/{world_id}", "OWNER", False),
    ("POST", "/api/worlds/{world_id}/backups", "OWNER", False),
    ("DELETE", "/api/worlds/{world_id}/backups/{backup_id}", "OWNER", False),
    # Imports and restores are the owner's alone -- admins are refused too.
    ("POST", "/api/worlds/{world_id}/import/preflight", "OWNER", True),
    ("POST", "/api/worlds/{world_id}/import/preflight-from-drive", "OWNER", True),
    ("GET", "/api/worlds/{world_id}/import/staged", "OWNER", True),
    ("DELETE", "/api/worlds/{world_id}/import/staged", "OWNER", True),
    ("POST", "/api/worlds/{world_id}/import/commit", "OWNER", True),
    # Content add-ons: reading what is installed is OPERATOR, changing it MANAGER.
    ("GET", "/api/worlds/{world_id}/content", "OPERATOR", False),
    ("GET", "/api/worlds/{world_id}/content/available", "OPERATOR", False),
    ("POST", "/api/worlds/{world_id}/content", "MANAGER", False),
    ("DELETE", "/api/worlds/{world_id}/content/{catalog_id}", "MANAGER", False),
    ("GET", "/api/worlds/{world_id}/jobs/{job_id}", "OPERATOR", False),
    # Seeing who shares a world is open to everyone who shares it; changing
    # who shares it is the owner's alone, admins included.
    ("GET", "/api/worlds/{world_id}/collaborators", "OPERATOR", False),
    ("POST", "/api/worlds/{world_id}/collaborators", "OWNER", True),
    ("PATCH", "/api/worlds/{world_id}/collaborators/{user_id}", "OWNER", True),
    # DELETE is the exception: an admin may revoke, and a collaborator may
    # remove themselves. Covered by its own tests rather than the matrix,
    # whose actor model has no notion of "acting on your own row".
    ("DELETE", "/api/worlds/{world_id}/collaborators/{user_id}", "OWNER", False),
]

RANK = {"NONE": 0, "OPERATOR": 1, "MANAGER": 2, "OWNER": 3}


def _make_world(db_module, owner_id: str, subdomain: str | None = None) -> str:
    # The Postgres container is session-scoped while app_client is not, so
    # rows outlive each test. Subdomains are globally unique, and this is
    # called once per parametrised case.
    subdomain = subdomain or f"mx{uuid.uuid4().hex[:12]}"
    from open_worlds.common.models import World, WorldState

    with db_module.session_scope() as db:
        w = World(
            id=uuid.uuid4(),
            owner_id=uuid.UUID(owner_id),
            subdomain=subdomain,
            display_name="Matrix",
            game="MINECRAFT",
            state=WorldState.OFF,
            memory_mb=2048,
            max_players=10,
        )
        db.add(w)
        db.flush()
        return str(w.id)


def _grant(db_module, world_id: str, user_id: str, level: str) -> None:
    from open_worlds.common.models import CollaboratorLevel, WorldCollaborator

    with db_module.session_scope() as db:
        db.add(
            WorldCollaborator(
                id=uuid.uuid4(),
                world_id=uuid.UUID(world_id),
                user_id=uuid.UUID(user_id),
                level=CollaboratorLevel(level),
            )
        )


@pytest.fixture
def world_and_actors(app_client, make_user):
    """One world, and one of every kind of caller against it."""
    client, db_module = app_client
    owner = make_user()
    world_id = _make_world(db_module, owner["id"])
    actors = {
        "owner": owner,
        "admin": make_user(role="ADMIN"),
        "stranger": make_user(),
        "operator": make_user(),
        "manager": make_user(),
    }
    _grant(db_module, world_id, actors["operator"]["id"], "OPERATOR")
    _grant(db_module, world_id, actors["manager"]["id"], "MANAGER")
    return client, world_id, actors


# A minimally valid body per route.
#
# This is load-bearing, not convenience. FastAPI validates the request body
# BEFORE the route function runs, so a route called with no body returns 422
# and the authorization guard never executes. Without these, every such case
# would "pass" a laxer assertion while testing nothing but Pydantic.
BODIES: dict[tuple[str, str], dict] = {
    ("PATCH", "/api/worlds/{world_id}"): {},
    ("POST", "/api/worlds/{world_id}/backups"): {"kind": "MANUAL"},
    ("POST", "/api/worlds/{world_id}/content"): {"catalog_id": str(uuid.uuid4())},
    ("POST", "/api/worlds/{world_id}/import/commit"): {},
    ("POST", "/api/worlds/{world_id}/import/preflight-from-drive"): {
        "backup_id": str(uuid.uuid4())
    },
    ("POST", "/api/worlds/{world_id}/collaborators"): {
        "email": "nobody@example.test",
        "level": "OPERATOR",
    },
    ("PATCH", "/api/worlds/{world_id}/collaborators/{user_id}"): {"level": "MANAGER"},
}


@pytest.mark.parametrize("method,path,need,owner_only", WORLD_ROUTES)
@pytest.mark.parametrize("actor", ["stranger", "operator", "manager", "owner", "admin"])
def test_permission_matrix(world_and_actors, method, path, need, owner_only, actor):
    """Every route, every caller, asserting only the authorization outcome.

    Routes refuse for other reasons too (a missing backup is 404, a PATCH
    without `confirm` is 400), so only a 403 and the specific "world not found"
    404 are pinned.
    """
    client, world_id, actors = world_and_actors
    logout(client)
    login(client, actors[actor])

    url = path.format(
        world_id=world_id,
        backup_id=uuid.uuid4(),
        catalog_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
    )
    kwargs: dict = {"headers": CSRF}
    if (method, path) in BODIES:
        kwargs["json"] = BODIES[(method, path)]
    if path.endswith("/import/preflight"):
        # Multipart rather than JSON; the file just has to exist.
        kwargs["files"] = {"file": ("w.tar.gz", b"not-a-real-archive", "application/gzip")}
    if method == "PUT" and path.endswith("/icon"):
        kwargs["files"] = {"file": ("icon.png", b"not-a-png", "image/png")}
    res = client.request(method, url, **kwargs)

    hidden = res.status_code == 404 and res.json().get("detail") == "World not found"

    if actor == "stranger":
        assert hidden, (
            f"{method} {path}: a stranger must not learn the world exists; "
            f"got {res.status_code} {res.text[:160]}"
        )
        return

    level = {"operator": "OPERATOR", "manager": "MANAGER", "owner": "OWNER", "admin": "OWNER"}[
        actor
    ]
    allowed = RANK[level] >= RANK[need] and not (owner_only and actor == "admin")

    assert res.status_code != 422, (
        f"{method} {path}: got 422, so the request never reached the authorization "
        f"guard and this case proved nothing. Add a valid body to BODIES. "
        f"{res.text[:160]}"
    )

    if allowed:
        assert res.status_code != 403 and not hidden, (
            f"{method} {path}: {actor} should have been allowed, got {res.status_code} "
            f"{res.text[:160]}"
        )
    else:
        assert res.status_code == 403, (
            f"{method} {path}: {actor} should have been refused with 403, "
            f"got {res.status_code} {res.text[:160]}"
        )


def test_every_world_route_is_in_the_matrix():
    """A new per-world route cannot land without someone deciding its tier.

    Read from the router objects: the api is a mounted sub-app whose routes
    are not traversable from `app.routes`, and the OpenAPI schema is off.
    """
    import importlib
    import pkgutil

    import fastapi

    from open_worlds.api import routers
    from open_worlds.common.games import all_adapters

    declared = {(m, p) for m, p, *_ in WORLD_ROUTES}
    found: set[tuple[str, str]] = set()
    # Every module in the routers package, discovered rather than listed. An
    # earlier version named (worlds, content) explicitly and so was blind to a
    # whole new router file -- it passed while four undeclared routes existed,
    # which is worse than having no check at all.
    candidates: list[fastapi.APIRouter] = []
    for info in pkgutil.iter_modules(routers.__path__):
        mod = importlib.import_module(f"{routers.__name__}.{info.name}")
        candidates += [o for o in vars(mod).values() if isinstance(o, fastapi.APIRouter)]
    # And every route a game brings with it, which is mounted the same way.
    for adapter in all_adapters():
        candidates += adapter.api_routers()
    for obj in candidates:
        for route in obj.routes:
            path = getattr(route, "path", "")
            if "{world_id}" not in path:
                continue
            for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
                found.add((method, "/api" + path))

    assert found, "enumerated no per-world routes -- this walk has gone stale"
    missing = found - declared
    assert not missing, (
        "these per-world routes have no declared access tier; add them to "
        f"WORLD_ROUTES with the level they require: {sorted(missing)}"
    )
    stale = declared - found
    assert not stale, f"WORLD_ROUTES lists routes that no longer exist: {sorted(stale)}"


def test_cannot_restore_another_owners_backup_into_my_world(app_client, make_user):
    """Trap 2: the source-world check is inline, not behind the loader.

    A manager on someone's world must not be able to pull a backup out of a
    world they merely have access to. This check lives at the point of use
    rather than in a loader, so no structural mechanism guards it -- this test
    is the only thing standing between it and a quiet relaxation.
    """
    from open_worlds.common.models import Backup, BackupKind, BackupStatus

    client, db_module = app_client
    victim = make_user()
    attacker = make_user()

    # The route checks Drive linkage before it validates the source world, so
    # without a token the attacker is stopped by a 400 and the check under test
    # never runs -- the test would pass while proving nothing.
    with db_module.session_scope() as db:
        from open_worlds.common.crypto import encrypt
        from open_worlds.common.models import User

        u = db.get(User, uuid.UUID(attacker["id"]))
        u.drive_linked = True
        u.drive_refresh_token_enc = encrypt("fake-refresh-token")

    target = _make_world(db_module, attacker["id"], subdomain=f"atk{uuid.uuid4().hex[:10]}")
    source = _make_world(db_module, victim["id"], subdomain=f"vic{uuid.uuid4().hex[:10]}")

    with db_module.session_scope() as db:
        b = Backup(
            id=uuid.uuid4(),
            world_id=uuid.UUID(source),
            kind=BackupKind.MANUAL,
            status=BackupStatus.SUCCESS,
            filename="victim.tar.gz",
            drive_file_id="drive-xyz",
        )
        db.add(b)
        db.flush()
        backup_id = str(b.id)

    login(client, attacker)
    res = client.post(
        f"/api/worlds/{target}/import/preflight-from-drive",
        json={"backup_id": backup_id},
        headers=CSRF,
    )
    assert res.status_code == 404, (
        "a backup belonging to another owner's world must not be reachable; "
        f"got {res.status_code} {res.text[:200]}"
    )


# --- the PATCH field split ---------------------------------------------------


def test_every_updatable_field_has_an_authorization_tier():
    """A new WorldUpdate field cannot default to manager-writable.

    Without this, adding a field is enough to hand it to managers silently --
    the route-level tier says MANAGER and the field gate only knows about the
    fields someone remembered to list.
    """
    from open_worlds.api.routers.worlds import (
        _EGRESS_POLICY_UPDATE_FIELDS,
        _MANAGER_UPDATE_FIELDS,
        _OWNER_ONLY_UPDATE_FIELDS,
    )
    from open_worlds.api.schemas import WorldUpdate

    tiers = [_OWNER_ONLY_UPDATE_FIELDS, _MANAGER_UPDATE_FIELDS, _EGRESS_POLICY_UPDATE_FIELDS]
    classified = set().union(*tiers) | {"confirm"}
    fields = set(WorldUpdate.model_fields)
    assert fields == classified, (
        "every WorldUpdate field needs a tier. "
        f"unclassified={sorted(fields - classified)} "
        f"stale={sorted(classified - fields)}"
    )
    assert sum(len(t) for t in tiers) == len(set().union(*tiers)), "a field is in two tiers"


def test_a_manager_may_change_settings_but_not_automatic_backups(app_client, make_user):
    """The two halves of one route, which is the whole reason for the split."""
    client, db_module = app_client
    owner = make_user()
    manager = make_user()
    world_id = _make_world(db_module, owner["id"])
    _grant(db_module, world_id, manager["id"], "MANAGER")

    login(client, manager)

    ok = client.patch(f"/api/worlds/{world_id}", json={"display_name": "Renamed"}, headers=CSRF)
    assert ok.status_code != 403, f"a manager may rename: {ok.status_code} {ok.text[:160]}"

    for body in (
        {"auto_backup_enabled": True},
        {"auto_backup_interval": "WEEKLY"},
        {"auto_backup_max": 99},
    ):
        res = client.patch(f"/api/worlds/{world_id}", json=body, headers=CSRF)
        assert res.status_code == 403, (
            f"a manager must not set {list(body)[0]}: {res.status_code} {res.text[:160]}"
        )
        assert "Drive" in res.json()["detail"], "the refusal should say why"


def test_an_explicit_null_still_counts_as_touching_an_owner_only_field(app_client, make_user):
    """`is not None` would wave this through; model_fields_set does not."""
    client, db_module = app_client
    owner = make_user()
    manager = make_user()
    world_id = _make_world(db_module, owner["id"])
    _grant(db_module, world_id, manager["id"], "MANAGER")

    login(client, manager)
    res = client.patch(f"/api/worlds/{world_id}", json={"auto_backup_max": None}, headers=CSRF)
    assert res.status_code == 403, f"got {res.status_code} {res.text[:160]}"


def test_the_owner_may_change_automatic_backups(app_client, make_user):
    client, db_module = app_client
    owner = make_user()
    world_id = _make_world(db_module, owner["id"])
    login(client, owner)
    res = client.patch(
        f"/api/worlds/{world_id}", json={"auto_backup_interval": "WEEKLY"}, headers=CSRF
    )
    assert res.status_code != 403, f"{res.status_code} {res.text[:160]}"


# --- managing collaborators --------------------------------------------------


def _set_email(db_module, user_id: str, email: str) -> None:
    from open_worlds.common.models import User

    with db_module.session_scope() as db:
        db.get(User, uuid.UUID(user_id)).email = email


def test_adding_by_email_is_case_insensitive(app_client, make_user):
    client, db_module = app_client
    owner, friend = make_user(), make_user()
    _set_email(db_module, friend["id"], "Friend@Example.Test")
    world_id = _make_world(db_module, owner["id"])

    login(client, owner)
    res = client.post(
        f"/api/worlds/{world_id}/collaborators",
        json={"email": "friend@example.TEST", "level": "OPERATOR"},
        headers=CSRF,
    )
    assert res.status_code == 201, res.text[:200]
    assert res.json()["level"] == "OPERATOR"


def test_a_user_who_never_signed_in_cannot_be_found_and_the_error_says_so(app_client, make_user):
    """users.email is only populated on first sign-in, so this is reachable.

    The message matters more than the status: an owner told only "not found"
    will go hunting for a bug that is not there.
    """
    client, db_module = app_client
    owner = make_user()
    never_signed_in = make_user()
    _set_email(db_module, never_signed_in["id"], None)
    world_id = _make_world(db_module, owner["id"])

    login(client, owner)
    res = client.post(
        f"/api/worlds/{world_id}/collaborators",
        json={"email": "ghost@example.test", "level": "OPERATOR"},
        headers=CSRF,
    )
    assert res.status_code == 404
    assert "sign in" in res.json()["detail"].lower()


def test_two_accounts_with_one_email_refuses_rather_than_guessing(app_client, make_user):
    """users.email is not unique -- google_sub is the identity."""
    client, db_module = app_client
    owner, a, b = make_user(), make_user(), make_user()
    _set_email(db_module, a["id"], "twin@example.test")
    _set_email(db_module, b["id"], "twin@example.test")
    world_id = _make_world(db_module, owner["id"])

    login(client, owner)
    res = client.post(
        f"/api/worlds/{world_id}/collaborators",
        json={"email": "twin@example.test", "level": "OPERATOR"},
        headers=CSRF,
    )
    assert res.status_code == 409, res.text[:200]


def test_adding_the_owner_is_refused(app_client, make_user):
    client, db_module = app_client
    owner = make_user()
    _set_email(db_module, owner["id"], "me@example.test")
    world_id = _make_world(db_module, owner["id"])

    login(client, owner)
    res = client.post(
        f"/api/worlds/{world_id}/collaborators",
        json={"email": "me@example.test", "level": "MANAGER"},
        headers=CSRF,
    )
    assert res.status_code == 400
    assert "already has" in res.json()["detail"]


def test_adding_the_same_person_twice_is_refused(app_client, make_user):
    client, db_module = app_client
    owner, friend = make_user(), make_user()
    _set_email(db_module, friend["id"], "dup@example.test")
    world_id = _make_world(db_module, owner["id"])

    login(client, owner)
    body = {"email": "dup@example.test", "level": "OPERATOR"}
    assert (
        client.post(f"/api/worlds/{world_id}/collaborators", json=body, headers=CSRF).status_code
        == 201
    )
    second = client.post(f"/api/worlds/{world_id}/collaborators", json=body, headers=CSRF)
    assert second.status_code == 409, second.text[:200]


def test_a_collaborator_can_remove_themselves_but_not_each_other(app_client, make_user):
    client, db_module = app_client
    owner, alice, bob = make_user(), make_user(), make_user()
    world_id = _make_world(db_module, owner["id"])
    _grant(db_module, world_id, alice["id"], "MANAGER")
    _grant(db_module, world_id, bob["id"], "OPERATOR")

    login(client, alice)
    # A manager outranks an operator but still may not evict them: access is
    # the owner's to grant and revoke, not other collaborators'.
    evict = client.delete(f"/api/worlds/{world_id}/collaborators/{bob['id']}", headers=CSRF)
    assert evict.status_code == 403, evict.text[:200]

    leave = client.delete(f"/api/worlds/{world_id}/collaborators/{alice['id']}", headers=CSRF)
    assert leave.status_code == 200, leave.text[:200]
    # Gone means gone: the world stops being visible at all.
    assert client.get(f"/api/worlds/{world_id}").status_code == 404


def test_an_admin_may_revoke_but_not_grant(app_client, make_user):
    """Asymmetric on purpose; see the router docstring."""
    client, db_module = app_client
    owner, admin, friend = make_user(), make_user(role="ADMIN"), make_user()
    _set_email(db_module, friend["id"], "pal@example.test")
    world_id = _make_world(db_module, owner["id"])
    _grant(db_module, world_id, friend["id"], "OPERATOR")

    login(client, admin)
    grant = client.post(
        f"/api/worlds/{world_id}/collaborators",
        json={"email": "pal@example.test", "level": "MANAGER"},
        headers=CSRF,
    )
    assert grant.status_code == 403, grant.text[:200]

    revoke = client.delete(f"/api/worlds/{world_id}/collaborators/{friend['id']}", headers=CSRF)
    assert revoke.status_code == 200, revoke.text[:200]


# --- the dashboard list ------------------------------------------------------


def test_the_world_list_includes_shared_worlds_exactly_once(app_client, make_user):
    client, db_module = app_client
    owner, friend, stranger = make_user(), make_user(), make_user()
    mine = _make_world(db_module, friend["id"])
    shared = _make_world(db_module, owner["id"])
    theirs = _make_world(db_module, stranger["id"])
    _grant(db_module, shared, friend["id"], "OPERATOR")
    # Being both owner and collaborator is what a join would duplicate.
    _grant(db_module, mine, friend["id"], "MANAGER")

    login(client, friend)
    res = client.get("/api/worlds")
    assert res.status_code == 200
    ids = [w["id"] for w in res.json()]
    assert sorted(ids) == sorted([mine, shared]), ids
    assert len(ids) == len(set(ids)), f"a world appears more than once: {ids}"
    assert theirs not in ids

    by_id = {w["id"]: w for w in res.json()}
    assert by_id[mine]["access_level"] == "OWNER", "owning beats a collaborator row"
    assert by_id[shared]["access_level"] == "OPERATOR"
    # owner_id is now always present; the UI must never have to infer it.
    assert all(w["owner_id"] for w in res.json())


# --- egress override ---------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "allowed"),
    [
        ("none", set()),
        ("admin", {"admin"}),
        ("owner_and_manager", {"owner", "admin", "manager"}),
    ],
)
def test_who_may_allow_unknown_network_connections(world_and_actors, monkeypatch, mode, allowed):
    """Set by the operator: nobody by default, admins only, or everyone who
    manages the world. An operator never."""
    from open_worlds.common.config import get_settings

    monkeypatch.setenv("PLATFORM_EGRESS_OVERRIDE_ACTORS", mode)
    get_settings.cache_clear()
    client, world_id, actors = world_and_actors
    body = {"egress_log_only": True, "confirm": True}
    try:
        for who in ("owner", "admin", "manager", "operator"):
            login(client, actors[who])
            res = client.patch(f"/api/worlds/{world_id}", json=body, headers=CSRF)
            if who in allowed:
                assert res.status_code == 200, f"{mode}/{who}: {res.status_code} {res.text[:160]}"
                assert res.json()["egress_log_only"] is True
                # Put it back so the next actor's attempt is a real change.
                client.patch(
                    f"/api/worlds/{world_id}",
                    json={"egress_log_only": False, "confirm": True},
                    headers=CSRF,
                )
            else:
                assert res.status_code == 403, f"{mode}/{who}: {res.status_code} {res.text[:160]}"
            logout(client)
    finally:
        get_settings.cache_clear()


def test_allowing_unknown_connections_needs_confirmation(world_and_actors, monkeypatch):
    from open_worlds.common.config import get_settings

    monkeypatch.setenv("PLATFORM_EGRESS_OVERRIDE_ACTORS", "admin")
    get_settings.cache_clear()
    client, world_id, actors = world_and_actors
    try:
        login(client, actors["admin"])
        res = client.patch(f"/api/worlds/{world_id}", json={"egress_log_only": True}, headers=CSRF)
        assert res.status_code == 409
        assert "any address" in res.json()["detail"]
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("default", ["true", "false"])
def test_new_worlds_take_the_blocking_default(app_client, make_user, monkeypatch, default):
    from open_worlds.common.config import get_settings

    monkeypatch.setenv("PLATFORM_EGRESS_BLOCKING_DEFAULT", default)
    get_settings.cache_clear()
    client, _ = app_client
    try:
        login(client, make_user())
        res = client.post(
            "/api/worlds",
            json={
                "game": "MINECRAFT",
                "subdomain": f"eg{uuid.uuid4().hex[:10]}",
                "display_name": "Default",
                "settings": {"version": "1.21.4"},
            },
            headers=CSRF,
        )
        assert res.status_code == 201, f"{res.status_code} {res.text[:200]}"
        assert res.json()["egress_log_only"] is (default == "false")
    finally:
        get_settings.cache_clear()


def test_a_worlds_own_value_applies_even_when_nobody_may_change_it(
    app_client, world_and_actors, monkeypatch
):
    """The override decides who may change it, not whether it counts."""
    import uuid as _uuid

    from open_worlds.api.routers.worlds import spec_for
    from open_worlds.common.config import get_settings
    from open_worlds.common.models import World

    _, db_module = app_client
    _, world_id, _ = world_and_actors
    monkeypatch.setenv("PLATFORM_EGRESS_OVERRIDE_ACTORS", "none")
    get_settings.cache_clear()
    try:
        for stored in (True, False):
            with db_module.session_scope() as db:
                db.get(World, _uuid.UUID(world_id)).egress_log_only = stored
            with db_module.session_scope() as db:
                spec = spec_for(db.get(World, _uuid.UUID(world_id)), db)
                assert spec.egress_log_only is stored
    finally:
        get_settings.cache_clear()


def test_an_operator_sees_neither_the_join_password_nor_the_raw_error(world_and_actors):
    """Operators start and stop. The join password is for whoever may change
    it, and the raw log tail can carry paths; operators get the summary."""
    from open_worlds.common import db as db_module
    from open_worlds.common.crypto import encrypt
    from open_worlds.common.models import World

    client, world_id, actors = world_and_actors
    with db_module.session_scope() as db:
        w = db.get(World, uuid.UUID(world_id))
        w.server_password_enc = encrypt("join-me")
        w.last_error = "Traceback ... /srv/games/worlds/x/data/server.jar"

    seen = {}
    for name in ("operator", "manager", "owner", "admin"):
        logout(client)
        login(client, actors[name])
        body = client.get(f"/api/worlds/{world_id}").json()
        seen[name] = (body["server_password"], body["last_error"])
    assert seen["operator"] == ("", None)
    for name in ("manager", "owner", "admin"):
        assert seen[name][0] == "join-me"
        assert seen[name][1].startswith("Traceback")
