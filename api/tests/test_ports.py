"""Per-world game-port allocation: the range is a hard cap, a freed port is
reused last, and the world that freed it can reclaim it."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from open_worlds.common.config import Settings
from open_worlds.common.ports import NoPortAvailable, allocate_port


def _settings(start: int = 8211, end: int = 8220) -> Settings:
    return Settings(
        session_secret="s",
        fernet_key="k",
        google_client_id="i",
        google_client_secret="c",
        game_port_range_start=start,
        game_port_range_end=end,
    )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self._rows

    def all(self):
        return self._rows


class _FakeDB:
    """Answers the two queries allocate_port makes, in the order it makes them."""

    def __init__(self, taken, released=None):
        self._taken = taken
        self._released = released or {}
        self._calls = 0

    def execute(self, _stmt):
        self._calls += 1
        if self._calls == 1:
            return _FakeResult(list(self._taken))
        return _FakeResult(list(self._released.items()))


def test_allocates_the_lowest_never_used_port():
    assert allocate_port(_FakeDB(taken=[]), _settings()) == 8211


def test_skips_ports_already_taken():
    assert allocate_port(_FakeDB(taken=[8211, 8212, 8214]), _settings()) == 8213


def test_range_exhaustion_is_the_platform_cap():
    db = _FakeDB(taken=list(range(8211, 8221)))
    with pytest.raises(NoPortAvailable):
        allocate_port(db, _settings())


def test_a_freed_port_is_not_reused_while_fresh_ones_remain():
    # 8211 was released; 8212 was never used. The never-used one wins, so a
    # deleted world's address doesn't immediately resolve to a different world.
    db = _FakeDB(taken=[], released={8211: datetime.now(UTC)})
    assert allocate_port(db, _settings()) == 8212


def test_oldest_release_wins_when_every_port_has_been_used():
    now = datetime.now(UTC)
    db = _FakeDB(
        taken=[],
        released={
            8211: now - timedelta(days=1),
            8212: now - timedelta(days=30),
            8213: now - timedelta(hours=2),
        },
    )
    # With a range of three, all released, the longest-idle port is reissued.
    assert allocate_port(db, _settings(8211, 8213)) == 8212


class _ReclaimDB:
    """Fake keyed on the query's shape rather than call order.

    allocate_port issues a different number of queries depending on whether a
    claimant was supplied, so counting calls would silently test the wrong thing.
    """

    def __init__(self, taken, released=None, claims=None):
        self._taken = taken
        self._released = released or {}
        # {(subdomain, owner_id): port} -- what each world last released.
        self._claims = claims or {}

    def execute(self, stmt):
        sql = str(stmt).lower()
        if "port_releases" not in sql:
            return _FakeResult(list(self._taken))
        if "order by" in sql and "limit" in sql:
            return _ClaimResult(self._claim_for(stmt))
        return _FakeResult(list(self._released.items()))

    def _claim_for(self, stmt):
        params = stmt.compile().params
        key = (params.get("subdomain_1"), params.get("owner_id_1"))
        port = self._claims.get(key)
        # Only reclaimable while the port is actually free.
        return port if port is not None and port not in self._taken else None


class _ClaimResult:
    def __init__(self, port):
        self._port = port

    def scalar_one_or_none(self):
        return self._port


OWNER = uuid.uuid4()
OTHER_OWNER = uuid.uuid4()


def test_a_recreated_world_reclaims_the_port_it_used_to_have():
    # The whole point: players save "world.example.com:8213", so recreating the
    # same world under the same name must not hand it a different address.
    db = _ReclaimDB(taken=[8211], claims={("myworld", OWNER): 8213})
    assert allocate_port(db, _settings(), "myworld", OWNER) == 8213


def test_reclaim_beats_a_never_used_port():
    # Without reclaim this would return 8212, the lowest never-used port.
    db = _ReclaimDB(taken=[8211], claims={("myworld", OWNER): 8219})
    assert allocate_port(db, _settings(), "myworld", OWNER) == 8219


def test_a_different_owner_reusing_the_name_gets_no_reclaim():
    # Subdomains are globally unique but reusable. Handing a stranger the
    # previous owner's address is the bookmark hijack the delay rule prevents.
    db = _ReclaimDB(taken=[], claims={("myworld", OWNER): 8213})
    assert allocate_port(db, _settings(), "myworld", OTHER_OWNER) == 8211


def test_the_same_owner_with_a_different_name_gets_no_reclaim():
    db = _ReclaimDB(taken=[], claims={("myworld", OWNER): 8213})
    assert allocate_port(db, _settings(), "otherworld", OWNER) == 8211


def test_a_reclaimable_port_that_is_taken_falls_back_to_normal_allocation():
    # Someone else holds the old port now, so the claim cannot be honoured.
    db = _ReclaimDB(taken=[8213], claims={("myworld", OWNER): 8213})
    assert allocate_port(db, _settings(), "myworld", OWNER) == 8211


def test_no_claimant_supplied_behaves_exactly_as_before():
    db = _ReclaimDB(taken=[], claims={("myworld", OWNER): 8213})
    assert allocate_port(db, _settings()) == 8211


def test_reclaim_still_respects_range_exhaustion():
    db = _ReclaimDB(taken=list(range(8211, 8221)), claims={("myworld", OWNER): 8213})
    with pytest.raises(NoPortAvailable):
        allocate_port(db, _settings(), "myworld", OWNER)


def test_max_worlds_follows_the_range():
    assert _settings(8211, 8220).max_worlds == 10
    assert _settings(8211, 8211).max_worlds == 1


def test_invalid_ranges_are_refused_at_startup():
    with pytest.raises(ValueError):
        _settings(8220, 8211)  # end below start
    with pytest.raises(ValueError):
        _settings(80, 8220)  # reaches into privileged ports
