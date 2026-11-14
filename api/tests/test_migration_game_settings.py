"""Migration 0008 against real rows: per-game columns into worlds.game_settings.

The route tests migrate an empty database, which proves the DDL applies and
nothing about the data. Production has worlds, so this migrates a populated
0007 database up and back down. Skipped without Docker.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest
import sqlalchemy as sa

pytest_plugins = ["tests.conftest_routes"]


def _alembic(url: str, *args: str) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "PLATFORM_DATABASE_URL": url},
    )
    assert done.returncode == 0, done.stderr[-2000:]


@pytest.fixture
def scratch_url(pg_url):
    """A database of its own, so stepping migrations cannot disturb pg_url's."""
    name = f"mig{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(pg_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(sa.text(f"CREATE DATABASE {name}"))
    url = pg_url.rsplit("/", 1)[0] + f"/{name}"
    try:
        yield url
    finally:
        with admin.connect() as c:
            c.execute(sa.text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


def test_per_game_columns_move_into_settings_and_back(scratch_url):
    _alembic(scratch_url, "upgrade", "0007_world_last_error")
    engine = sa.create_engine(scratch_url)
    owner = uuid.uuid4()
    with engine.begin() as c:
        c.execute(
            sa.text("INSERT INTO users (id, google_sub, role) VALUES (:id, 'mig', 'USER')"),
            {"id": owner},
        )
        rows = [
            # subdomain, game, server_type, version, description, port
            ("mc", "MINECRAFT", "FABRIC", "1.21.4", None, None),
            ("pal", "PALWORLD", None, "v0.6.2", "Be nice", 8211),
            ("palbare", "PALWORLD", None, None, None, 8212),
            ("palblank", "PALWORLD", None, None, "", 8213),
        ]
        for sub, game, stype, version, desc, port in rows:
            c.execute(
                sa.text(
                    "INSERT INTO worlds (id, owner_id, subdomain, display_name, game,"
                    " server_type, version, server_description, game_port, memory_mb,"
                    " max_players, state)"
                    " VALUES (:id, :owner, :sub, :sub, :game, :stype, :version, :desc,"
                    " :port, 4096, 10, 'OFF')"
                ),
                {
                    "id": uuid.uuid4(),
                    "owner": owner,
                    "sub": sub,
                    "game": game,
                    "stype": stype,
                    "version": version,
                    "desc": desc,
                    "port": port,
                },
            )

    _alembic(scratch_url, "upgrade", "0008_world_game_settings")
    with engine.connect() as c:
        got = {
            r.subdomain: (r.game, r.game_settings, r.reported_version, r.game_port)
            for r in c.execute(
                sa.text(
                    "SELECT subdomain, game, game_settings, reported_version, game_port FROM worlds"
                )
            )
        }
        game_type = c.execute(
            sa.text(
                "SELECT data_type FROM information_schema.columns"
                " WHERE table_name='worlds' AND column_name='game'"
            )
        ).scalar()
    assert got == {
        "mc": ("MINECRAFT", {"server_type": "FABRIC", "version": "1.21.4"}, None, None),
        "pal": ("PALWORLD", {"server_description": "Be nice"}, "v0.6.2", 8211),
        "palbare": ("PALWORLD", {}, None, 8212),
        "palblank": ("PALWORLD", {}, None, 8213),
    }
    assert game_type == "character varying"

    # Every migrated row is one its adapter accepts as it stands.
    from open_worlds.common.games import adapter_for

    for game, settings, _, _ in got.values():
        assert adapter_for(game).validate_settings(settings) == {
            **adapter_for(game).validate_settings({}),
            **settings,
        }

    _alembic(scratch_url, "downgrade", "0007_world_last_error")
    with engine.connect() as c:
        back = {
            r.subdomain: (r.game, r.server_type, r.version, r.server_description)
            for r in c.execute(
                sa.text(
                    "SELECT subdomain, game::text AS game, server_type::text AS server_type,"
                    " version, server_description FROM worlds"
                )
            )
        }
    assert back == {
        "mc": ("MINECRAFT", "FABRIC", "1.21.4", None),
        "pal": ("PALWORLD", None, "v0.6.2", "Be nice"),
        "palbare": ("PALWORLD", None, None, None),
        "palblank": ("PALWORLD", None, None, None),
    }
    engine.dispose()
