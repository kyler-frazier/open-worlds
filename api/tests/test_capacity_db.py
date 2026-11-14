"""What the memory budget counts, against a real database.

The rule in test_capacity.py is pure; which worlds feed it is one SQL WHERE
clause, so it is tested the way test_idle_sleep.py tests its query: real rows,
real predicate. Skipped without Docker.
"""

from __future__ import annotations

import uuid

import pytest

pytest_plugins = ["tests.conftest_routes"]


@pytest.fixture
def db_session(pg_url, monkeypatch):
    monkeypatch.setenv("PLATFORM_DATABASE_URL", pg_url)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(pg_url)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


def _world(db, state, memory_mb):
    from open_worlds.common.models import User, World

    owner = User(id=uuid.uuid4(), google_sub=f"cap-{uuid.uuid4().hex[:12]}")
    db.add(owner)
    db.flush()
    w = World(
        id=uuid.uuid4(),
        owner_id=owner.id,
        subdomain=f"cap{uuid.uuid4().hex[:12]}",
        display_name="Cap",
        game="MINECRAFT",
        state=state,
        memory_mb=memory_mb,
        max_players=10,
    )
    db.add(w)
    db.flush()
    return w


@pytest.mark.parametrize(
    ("state_name", "counted"),
    [
        ("RUNNING", True),
        ("STOPPING", True),
        # Wakes on its own when a player connects, so its memory is promised.
        ("SLEEPING", True),
        ("OFF", False),
        ("ERROR", False),
        ("DELETING", False),
    ],
)
def test_which_states_count(db_session, state_name, counted):
    from open_worlds.common.capacity import memory_in_use_mb
    from open_worlds.common.models import WorldState

    # Other tests share this database, so measure the change, not the total.
    before = memory_in_use_mb(db_session)
    _world(db_session, WorldState[state_name], 3072)
    assert memory_in_use_mb(db_session) - before == (3072 if counted else 0)


def test_sleeping_worlds_can_fill_the_budget(db_session):
    from open_worlds.common.capacity import (
        CapacityExceeded,
        ensure_capacity_for,
        memory_in_use_mb,
    )
    from open_worlds.common.config import Settings
    from open_worlds.common.models import WorldState

    sleeper = _world(db_session, WorldState.SLEEPING, 4096)
    starting = _world(db_session, WorldState.OFF, 4096)
    others = memory_in_use_mb(db_session, exclude_world_id=starting.id)
    s = Settings(host_memory_budget_mb=others + 4095)
    with pytest.raises(CapacityExceeded, match="reserved by sleeping worlds"):
        ensure_capacity_for(starting, db_session, s)

    # The sleeper waking is not refused by its own reservation.
    ensure_capacity_for(sleeper, db_session, Settings(host_memory_budget_mb=others))
