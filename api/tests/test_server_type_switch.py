"""Switching a world's loader must not strand the content installed on it."""

from __future__ import annotations

import json
import uuid

import pytest
from tests.conftest_routes import CSRF, login, logout

pytest_plugins = ["tests.conftest_routes"]


def _make_world(db_module, owner_id: str, server_type: str = "FABRIC") -> str:
    from open_worlds.common.models import World, WorldState

    with db_module.session_scope() as db:
        w = World(
            id=uuid.uuid4(),
            owner_id=uuid.UUID(owner_id),
            subdomain=f"st{uuid.uuid4().hex[:12]}",
            display_name="Loader switch",
            game="MINECRAFT",
            game_settings={"server_type": server_type, "version": "1.21.1"},
            state=WorldState.OFF,
            memory_mb=2048,
            max_players=10,
        )
        db.add(w)
        db.flush()
        return str(w.id)


def _install(db_module, world_id: str, *, name: str, loaders: list[str], kind: str = "MOD") -> None:
    from open_worlds.common.models import (
        ContentCatalog,
        ContentKind,
        ContentSource,
        WorldContent,
    )

    with db_module.session_scope() as db:
        entry = ContentCatalog(
            id=uuid.uuid4(),
            kind=ContentKind(kind),
            source=ContentSource.MODRINTH,
            project_slug=name.lower().replace(" ", "-") + uuid.uuid4().hex[:6],
            name=name,
            cached_loaders=json.dumps(loaders),
            cached_game_versions=json.dumps(["1.21.1"]),
        )
        db.add(entry)
        db.flush()
        db.add(
            WorldContent(
                id=uuid.uuid4(),
                world_id=uuid.UUID(world_id),
                catalog_id=entry.id,
                kind=ContentKind(kind),
            )
        )


@pytest.fixture
def owner_client(app_client, make_user):
    client, db_module = app_client
    owner = make_user()
    logout(client)
    login(client, owner)
    return client, db_module, owner


def _patch(client, world_id: str, **body):
    return client.patch(
        f"/api/worlds/{world_id}", json={"confirm": True, "settings": body}, headers=CSRF
    )


def test_refuses_and_names_the_mod_to_remove(owner_client):
    """The reported bug: Fabric API survived a switch to Forge, doing nothing."""
    client, db_module, owner = owner_client
    world_id = _make_world(db_module, owner["id"], "FABRIC")
    _install(db_module, world_id, name="Fabric API", loaders=["fabric"])

    r = _patch(client, world_id, server_type="FORGE")

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    # Naming the item is the whole point -- a generic warning is what failed before.
    assert "Fabric API" in detail
    assert "FORGE" in detail


def test_lists_every_incompatible_item(owner_client):
    client, db_module, owner = owner_client
    world_id = _make_world(db_module, owner["id"], "FABRIC")
    _install(db_module, world_id, name="Fabric API", loaders=["fabric"])
    _install(db_module, world_id, name="FallingTree", loaders=["fabric"])

    r = _patch(client, world_id, server_type="FORGE")

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "Fabric API" in detail and "FallingTree" in detail
    assert "they are" in detail


def test_switch_allowed_once_the_content_is_gone(owner_client):
    client, db_module, owner = owner_client
    world_id = _make_world(db_module, owner["id"], "FABRIC")

    r = _patch(client, world_id, server_type="FORGE")

    assert r.status_code == 200, r.text
    assert r.json()["settings"]["server_type"] == "FORGE"


def test_content_that_works_on_both_does_not_block(owner_client):
    """A datapack is loader-agnostic, so it is not a reason to refuse."""
    client, db_module, owner = owner_client
    world_id = _make_world(db_module, owner["id"], "FABRIC")
    _install(db_module, world_id, name="Some Datapack", loaders=["datapack"], kind="DATAPACK")

    r = _patch(client, world_id, server_type="FORGE")

    assert r.status_code == 200, r.text


def test_unrelated_edits_still_work_with_content_installed(owner_client):
    """The guard must only fire on a server-type change, not on every PATCH."""
    client, db_module, owner = owner_client
    world_id = _make_world(db_module, owner["id"], "FABRIC")
    _install(db_module, world_id, name="Fabric API", loaders=["fabric"])

    r = client.patch(
        f"/api/worlds/{world_id}", json={"confirm": True, "display_name": "Renamed"}, headers=CSRF
    )

    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "Renamed"
