"""Which games the platform offers, and the rules an admin's change must keep.

Against a database of its own: the rules count worlds per game, and the shared
route-test database holds whatever other tests created. Skipped without Docker.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

pytest_plugins = ["tests.conftest_routes"]


@pytest.fixture
def db(pg_url, monkeypatch, tmp_path):
    from open_worlds.common import config, runtime_config

    name = f"avail{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(sa.text(f"CREATE DATABASE {name}"))
    url = pg_url.rsplit("/", 1)[0] + f"/{name}"
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        env={**os.environ, "PLATFORM_DATABASE_URL": url},
    )
    assert done.returncode == 0, done.stderr[-1000:]

    monkeypatch.setenv("PLATFORM_RUNTIME_CONFIG_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("PLATFORM_COMPOSE_PROFILES", "minecraft")
    monkeypatch.setenv("PLATFORM_MC_ROUTER_API_URL", "http://mc-router:26660")
    config.get_settings.cache_clear()
    monkeypatch.setattr(runtime_config, "_cache", runtime_config._Cache())

    engine = sa.create_engine(url)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()
    with admin.connect() as c:
        c.execute(sa.text(f"DROP DATABASE {name} WITH (FORCE)"))
    admin.dispose()
    config.get_settings.cache_clear()


def _world(db, game):
    from open_worlds.common.models import User, World, WorldState

    owner = User(id=uuid.uuid4(), google_sub=f"av-{uuid.uuid4().hex[:12]}")
    db.add(owner)
    db.flush()
    db.add(
        World(
            id=uuid.uuid4(),
            owner_id=owner.id,
            subdomain=f"av{uuid.uuid4().hex[:12]}",
            display_name="x",
            game=game,
            state=WorldState.SLEEPING,
            memory_mb=2048,
            max_players=4,
        )
    )
    db.commit()


def test_a_fresh_platform_offers_minecraft(db):
    from open_worlds.common.game_availability import enabled_games

    assert enabled_games(db) == {"MINECRAFT"}


def test_a_game_with_worlds_is_offered_before_anyone_chooses(db):
    """What keeps existing worlds working when the setting first appears."""
    from open_worlds.common.game_availability import enabled_games

    _world(db, "PALWORLD")
    assert enabled_games(db) == {"MINECRAFT", "PALWORLD"}


def test_the_list_cannot_be_empty(db):
    from open_worlds.common.game_availability import GameAvailabilityError, set_enabled_games

    with pytest.raises(GameAvailabilityError, match="At least one"):
        set_enabled_games(db, [])


def test_a_game_with_worlds_cannot_be_removed_whatever_their_state(db):
    from open_worlds.common.game_availability import GameAvailabilityError, set_enabled_games

    _world(db, "MINECRAFT")
    with pytest.raises(GameAvailabilityError, match=r"MINECRAFT \(1 world\)"):
        set_enabled_games(db, ["PALWORLD"])


def test_a_game_cannot_be_added_while_its_router_is_missing(db):
    from open_worlds.common.game_availability import GameAvailabilityError, set_enabled_games

    # Only the minecraft profile runs on this "host".
    with pytest.raises(GameAvailabilityError, match="add palworld to COMPOSE_PROFILES"):
        set_enabled_games(db, ["MINECRAFT", "PALWORLD"])


def test_a_game_can_be_added_once_its_router_runs(db, monkeypatch):
    from open_worlds.common import config
    from open_worlds.common.game_availability import enabled_games, set_enabled_games

    monkeypatch.setenv("PLATFORM_COMPOSE_PROFILES", "minecraft,palworld")
    config.get_settings.cache_clear()
    set_enabled_games(db, ["MINECRAFT", "PALWORLD"])
    assert enabled_games(db) == {"MINECRAFT", "PALWORLD"}
    # And taken away again, since it has no worlds.
    set_enabled_games(db, ["PALWORLD"])
    assert enabled_games(db) == {"PALWORLD"}


def test_an_already_offered_game_is_not_rechecked(db, monkeypatch):
    """Saving the list must not fail over a game that was offered already;
    its problems show beside it instead."""
    from open_worlds.common import config
    from open_worlds.common.game_availability import game_statuses, set_enabled_games

    monkeypatch.setenv("PLATFORM_MC_ROUTER_API_URL", "")
    config.get_settings.cache_clear()
    set_enabled_games(db, ["MINECRAFT"])
    mc = next(g for g in game_statuses(db) if g.game == "MINECRAFT")
    assert mc.enabled and any("MC_ROUTER_API_URL" in p for p in mc.problems)


def test_unknown_games_are_refused(db):
    from open_worlds.common.game_availability import GameAvailabilityError, set_enabled_games

    with pytest.raises(GameAvailabilityError, match="Not installed"):
        set_enabled_games(db, ["MINECRAFT", "TETRIS"])


def test_the_retired_environment_variable_refuses_to_start(monkeypatch):
    from open_worlds.common.config import Settings

    monkeypatch.setenv("PLATFORM_ENABLED_GAMES", "MINECRAFT")
    with pytest.raises(ValueError, match="Admin > Settings > Games"):
        Settings()


# --- Through the API --------------------------------------------------------------


def test_only_offered_games_can_be_created(app_client, make_user):
    from tests.conftest_routes import CSRF, login

    client, _ = app_client
    login(client, make_user())
    res = client.post(
        "/api/worlds",
        json={"game": "FAKE", "subdomain": f"x{uuid.uuid4().hex[:8]}", "display_name": "x"},
        headers=CSRF,
    )
    assert res.status_code == 400


def test_admins_see_and_change_the_list_and_others_cannot(app_client, make_user):
    from tests.conftest_routes import CSRF, login, logout

    client, _ = app_client
    login(client, make_user())
    assert client.get("/api/admin/games").status_code == 403
    logout(client)
    login(client, make_user(role="ADMIN"))
    games = {g["game"]: g for g in client.get("/api/admin/games").json()}
    assert set(games) >= {"MINECRAFT", "PALWORLD"}
    res = client.put("/api/admin/games", json={"enabled": []}, headers=CSRF)
    assert res.status_code == 409 and "At least one" in res.json()["detail"]


def test_game_art_is_served_to_signed_in_users_only(app_client, make_user, monkeypatch, tmp_path):
    from tests.conftest_routes import login, logout

    from open_worlds.common import config

    (tmp_path / "minecraft.png").write_bytes(b"\x89PNG fake")
    monkeypatch.setenv("PLATFORM_GAME_ART_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    client, _ = app_client
    logout(client)
    assert client.get("/api/games/minecraft/art").status_code == 401
    login(client, make_user())
    res = client.get("/api/games/minecraft/art")
    assert res.status_code == 200 and res.content == b"\x89PNG fake"
    assert client.get("/api/games/palworld/art").status_code == 404
    art = {g["game"]: g["art_url"] for g in client.get("/api/games").json()}
    assert art["MINECRAFT"].startswith("/api/games/minecraft/art?v=")
