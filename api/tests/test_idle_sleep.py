"""Idle-sleep for Minecraft worlds, against a real database.

The decision is one SQL WHERE clause, which a fake Session would pass whatever
the predicate, so these set timestamps, run the real query and check what it
selected.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytest_plugins = ["tests.conftest_routes"]


@pytest.fixture
def db_session(pg_url, monkeypatch):
    monkeypatch.setenv("PLATFORM_DATABASE_URL", pg_url)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(pg_url)
    maker = sessionmaker(bind=engine)
    with maker() as session:
        yield session
    engine.dispose()


def _owner(db) -> uuid.UUID:
    from open_worlds.common.models import User

    u = User(id=uuid.uuid4(), google_sub=f"idle-{uuid.uuid4().hex[:12]}")
    db.add(u)
    db.flush()
    return u.id


def _world(db, owner_id, *, state, started_min_ago, polled_s_ago, active_min_ago, players=0):
    """A Minecraft world with its clocks set explicitly.

    ``active_min_ago=None`` leaves last_active_at NULL -- a world nobody has
    joined since it started, which must still sleep on the started_at fallback.
    """
    from open_worlds.common.models import World

    now = datetime.now(UTC)
    w = World(
        id=uuid.uuid4(),
        owner_id=owner_id,
        subdomain=f"idle{uuid.uuid4().hex[:12]}",
        display_name="Idle",
        game="MINECRAFT",
        state=state,
        memory_mb=2048,
        max_players=10,
        player_count=players,
        started_at=now - timedelta(minutes=started_min_ago),
        players_updated_at=now - timedelta(seconds=polled_s_ago),
        last_active_at=None if active_min_ago is None else now - timedelta(minutes=active_min_ago),
    )
    db.add(w)
    db.flush()
    return w


def _selected(db, monkeypatch):
    """Run the real query; return the subdomains it chose, stopping nothing."""
    from open_worlds.common.games.minecraft import routing as routes

    stopped: list[str] = []
    monkeypatch.setattr(routes, "perform_stop", lambda _o, sub, _s: stopped.append(sub))
    routes.sleep_idle_minecraft_worlds(db, object())
    return stopped


def test_an_empty_world_sleeps_once_the_idle_window_passes(db_session, monkeypatch):
    """Polling keeps players_updated_at fresh; it must not matter."""
    from open_worlds.common.models import WorldState

    owner = _owner(db_session)
    w = _world(
        db_session,
        owner,
        state=WorldState.RUNNING,
        started_min_ago=30,
        polled_s_ago=9,  # just polled, exactly as in production
        active_min_ago=20,  # last player left 20 minutes ago
    )
    assert _selected(db_session, monkeypatch) == [w.subdomain]


def test_a_world_nobody_ever_joined_sleeps_on_the_started_at_fallback(db_session, monkeypatch):
    """last_active_at is NULL for a world started and never joined."""
    from open_worlds.common.models import WorldState

    owner = _owner(db_session)
    w = _world(
        db_session,
        owner,
        state=WorldState.RUNNING,
        started_min_ago=30,
        polled_s_ago=5,
        active_min_ago=None,
    )
    assert _selected(db_session, monkeypatch) == [w.subdomain]


def test_a_world_with_a_recent_player_is_left_alone(db_session, monkeypatch):
    from open_worlds.common.models import WorldState

    owner = _owner(db_session)
    _world(
        db_session,
        owner,
        state=WorldState.RUNNING,
        started_min_ago=60,
        polled_s_ago=5,
        active_min_ago=2,
    )
    assert _selected(db_session, monkeypatch) == []


def test_a_world_with_players_on_it_is_left_alone(db_session, monkeypatch):
    from open_worlds.common.models import WorldState

    owner = _owner(db_session)
    _world(
        db_session,
        owner,
        state=WorldState.RUNNING,
        started_min_ago=60,
        polled_s_ago=5,
        active_min_ago=30,
        players=3,
    )
    assert _selected(db_session, monkeypatch) == []


def test_a_freshly_started_world_is_inside_the_boot_grace(db_session, monkeypatch):
    """Woken seconds ago, still booting, reporting zero. Must not be re-slept."""
    from open_worlds.common.models import WorldState

    owner = _owner(db_session)
    _world(
        db_session,
        owner,
        state=WorldState.RUNNING,
        started_min_ago=1,
        polled_s_ago=5,
        active_min_ago=None,
    )
    assert _selected(db_session, monkeypatch) == []


def test_a_stale_player_count_is_not_evidence_of_emptiness(db_session, monkeypatch):
    """Polling has been failing, so the zero we hold is not trustworthy."""
    from open_worlds.common.models import WorldState

    owner = _owner(db_session)
    _world(
        db_session,
        owner,
        state=WorldState.RUNNING,
        started_min_ago=60,
        polled_s_ago=600,
        active_min_ago=30,
    )
    assert _selected(db_session, monkeypatch) == []


def test_only_running_worlds_are_considered(db_session, monkeypatch):
    from open_worlds.common.models import WorldState

    owner = _owner(db_session)
    for state in (WorldState.SLEEPING, WorldState.OFF):
        _world(
            db_session,
            owner,
            state=state,
            started_min_ago=60,
            polled_s_ago=5,
            active_min_ago=30,
        )
    assert _selected(db_session, monkeypatch) == []


def test_palworld_is_left_to_pal_waker(db_session, monkeypatch):
    """Palworld sleeps via the UDP relay; this query must not double up on it."""
    from open_worlds.common.models import World, WorldState

    owner = _owner(db_session)
    now = datetime.now(UTC)
    db_session.add(
        World(
            id=uuid.uuid4(),
            owner_id=owner,
            subdomain=f"idle{uuid.uuid4().hex[:12]}",
            display_name="Pal",
            game="PALWORLD",
            state=WorldState.RUNNING,
            memory_mb=2048,
            max_players=10,
            player_count=0,
            started_at=now - timedelta(minutes=60),
            players_updated_at=now - timedelta(seconds=5),
            last_active_at=now - timedelta(minutes=30),
        )
    )
    db_session.flush()
    assert _selected(db_session, monkeypatch) == []
