"""The icon routes over the real HTTP stack: upload, read back, remove.

Who may call them is the permission matrix's job (test_collaborator_authz.py);
this is about what they do for someone who may.
"""

from __future__ import annotations

import os
import uuid

import pytest
from tests.conftest_routes import CSRF, login
from tests.test_collaborator_authz import _make_world
from tests.test_world_icon import png

pytest_plugins = ["tests.conftest_routes"]


@pytest.fixture
def owned_world(app_client, make_user):
    client, db_module = app_client
    owner = make_user()
    world_id = _make_world(db_module, owner["id"])
    login(client, owner)
    return client, world_id


def test_icon_round_trip(owned_world):
    client, world_id = owned_world
    url = f"/api/worlds/{world_id}/icon"

    assert client.get(url).status_code == 404
    assert client.get(f"/api/worlds/{world_id}").json()["icon_version"] is None

    res = client.put(url, headers=CSRF, files={"file": ("i.png", png(), "image/png")})
    assert res.status_code == 200, res.text
    assert res.json()["icon_version"] is not None

    got = client.get(url)
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"
    assert got.content == png()

    res = client.delete(url, headers=CSRF)
    assert res.status_code == 200
    assert res.json()["icon_version"] is None
    assert client.get(url).status_code == 404


@pytest.mark.parametrize("body", [png(128, 128), b"not a png"])
def test_a_bad_icon_is_refused_with_a_reason(owned_world, body):
    client, world_id = owned_world
    res = client.put(
        f"/api/worlds/{world_id}/icon", headers=CSRF, files={"file": ("i.png", body, "image/png")}
    )
    assert res.status_code == 400
    assert "icon" in res.json()["detail"].lower()
    assert client.get(f"/api/worlds/{world_id}/icon").status_code == 404


def test_api_responses_are_never_cached(owned_world):
    client, world_id = owned_world
    assert client.get("/api/auth/me").headers["cache-control"] == "no-store"
    assert client.get(f"/api/worlds/{world_id}").headers["cache-control"] == "no-store"
    # A route that sets its own policy keeps it.
    client.put(
        f"/api/worlds/{world_id}/icon", headers=CSRF, files={"file": ("i.png", png(), "image/png")}
    )
    assert (
        client.get(f"/api/worlds/{world_id}/icon").headers["cache-control"] == "private, no-cache"
    )


def test_clear_deletes_the_save_icon_too(owned_world, app_client):
    client, world_id = owned_world
    _, db_module = app_client
    from open_worlds.common.config import get_settings
    from open_worlds.common.models import World

    with db_module.session_scope() as db:
        sub = db.get(World, uuid.UUID(world_id)).subdomain
    save = os.path.join(get_settings().worlds_dir, sub, "data", "world")
    os.makedirs(save)
    with open(os.path.join(save, "icon.png"), "wb") as f:
        f.write(png())
    url = f"/api/worlds/{world_id}/icon"
    client.put(url, headers=CSRF, files={"file": ("i.png", png(), "image/png")})

    files = client.get(f"{url}/files").json()
    assert [(f["kind"], f["path"], f["valid"]) for f in files] == [
        ("server", "server-icon.png", True),
        ("save", "world/icon.png", True),
    ]
    assert client.get(url, params={"kind": "save"}).content == png()

    res = client.delete(url, headers=CSRF)
    assert res.status_code == 200
    assert res.json()["icon_version"] is None
    assert client.get(f"{url}/files").json() == []
    assert not os.path.exists(os.path.join(save, "icon.png"))


def test_preflight_says_what_will_happen_to_the_icons(owned_world, tmp_path):
    import tarfile

    client, world_id = owned_world
    src = tmp_path / "data"
    (src / "world").mkdir(parents=True)
    (src / "world" / "level.dat").write_bytes(b"lvl")
    (src / "world" / "icon.png").write_bytes(png())
    archive = tmp_path / "w.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        t.add(src, arcname="data")

    res = client.post(
        f"/api/worlds/{world_id}/import/preflight",
        headers=CSRF,
        files={"file": ("w.tar.gz", archive.read_bytes(), "application/gzip")},
    )
    assert res.status_code == 201, res.text
    assert res.json()["icon_changes"] == [
        {"action": "move", "path": "world/icon.png", "to": "server-icon.png"}
    ]
    # The staged view says the same, read from the same archive.
    staged = client.get(f"/api/worlds/{world_id}/import/staged").json()
    assert staged["icon_changes"] == res.json()["icon_changes"]
