"""Tests for the worker's fast readiness probe.

The probe is affordable at a 3s interval only because its working set is worlds
that are running but not yet confirmed joinable -- normally none. These pin that
property down, since losing it turns a cheap loop into an expensive one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from open_worlds.common.games import AdminUnavailable
from open_worlds.common.models import WorldState
from open_worlds.worker import players


class _Admin:
    def __init__(self, reachable: bool, version: str | None = None) -> None:
        self._reachable = reachable
        self._version = version
        self.closed = False
        self.version_reads = 0

    def players(self):
        if not self._reachable:
            raise AdminUnavailable("still booting")
        return [SimpleNamespace(name="alice"), SimpleNamespace(name="bob")]

    def server_version(self):
        self.version_reads += 1
        if self._version == "unavailable":
            raise AdminUnavailable("no info endpoint")
        return self._version

    def close(self):
        self.closed = True


class _DB:
    """Captures the filters the probe selects on, so scoping can be asserted."""

    def __init__(self, worlds):
        self._worlds = worlds
        self.selected_with = None

    def execute(self, stmt):
        self.selected_with = str(stmt)
        return SimpleNamespace(scalars=lambda: list(self._worlds))


def _world(**kw):
    base = dict(
        subdomain="demo",
        game="PALWORLD",
        state=WorldState.RUNNING,
        admin_password_enc="enc",
        reported_version=None,
        player_count=0,
        players_updated_at=None,
        started_at=None,
        ready_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _stub_admin(monkeypatch):
    monkeypatch.setattr(players, "decrypt", lambda _v: "pw")
    monkeypatch.setattr(
        players,
        "get_settings",
        lambda: SimpleNamespace(container_name_prefix="game-world-"),
    )


def _patch_adapter(
    monkeypatch,
    reachable: bool,
    *,
    version: str | None = None,
    pins_versions: bool = False,
) -> _Admin:
    admin = _Admin(reachable, version)
    monkeypatch.setattr(
        players,
        "adapter_for",
        lambda _g: SimpleNamespace(
            admin_client=lambda *_a: admin,
            supports_version_pinning=pins_versions,
        ),
    )
    return admin


def test_probe_marks_a_world_ready_once_its_admin_answers(monkeypatch):
    _patch_adapter(monkeypatch, reachable=True)
    world = _world()
    assert players.probe_pending_worlds(_DB([world])) == 1
    assert world.ready_at is not None
    # A successful probe is also a free player count; no reason to discard it.
    assert world.player_count == 2


def test_probe_leaves_a_still_booting_world_alone(monkeypatch):
    _patch_adapter(monkeypatch, reachable=False)
    world = _world()
    assert players.probe_pending_worlds(_DB([world])) == 0
    assert world.ready_at is None


def test_probe_always_closes_the_admin_connection(monkeypatch):
    # RCON holds a real socket; leaking one per probe at 3s intervals would add up.
    admin = _patch_adapter(monkeypatch, reachable=False)
    players.probe_pending_worlds(_DB([_world()]))
    assert admin.closed


def test_probe_only_looks_at_running_worlds_that_are_not_ready(monkeypatch):
    _patch_adapter(monkeypatch, reachable=True)
    db = _DB([])
    players.probe_pending_worlds(db)
    sql = db.selected_with.lower()
    assert "state" in sql
    assert "ready_at is null" in sql


def test_probe_does_nothing_when_no_world_is_pending(monkeypatch):
    # The steady state, and the reason a 3s interval is affordable at all.
    called = False

    def _boom(*_a, **_k):
        nonlocal called
        called = True
        raise AssertionError("should not contact any world")

    monkeypatch.setattr(players, "adapter_for", _boom)
    assert players.probe_pending_worlds(_DB([])) == 0
    assert not called


def test_readiness_is_not_stamped_twice(monkeypatch):
    """First contact wins, so ready_at records when it became joinable."""
    _patch_adapter(monkeypatch, reachable=True)
    world = _world()
    players.probe_pending_worlds(_DB([world]))
    first = world.ready_at
    players.probe_pending_worlds(_DB([world]))
    assert world.ready_at == first


def test_the_slow_player_poll_also_stamps_readiness(monkeypatch):
    """A successful count is the same evidence; no reason to wait for a probe."""
    _patch_adapter(monkeypatch, reachable=True)
    world = _world()
    assert players.refresh_player_counts(_DB([world])) == 1
    assert world.ready_at is not None
    assert world.player_count == 2


def test_a_world_with_no_admin_password_is_never_marked_ready(monkeypatch):
    _patch_adapter(monkeypatch, reachable=True)
    world = _world(admin_password_enc=None)
    assert players.probe_pending_worlds(_DB([world])) == 0
    assert world.ready_at is None


# --- detected server version ------------------------------------------------
# Palworld ships no version manifest and Steam updates it in place, so the only
# way to know what a world runs is to ask it while it is up.


def test_a_world_that_cannot_pin_records_the_version_it_reports(monkeypatch):
    _patch_adapter(monkeypatch, reachable=True, version="v0.6.2")
    world = _world()
    assert players.refresh_player_counts(_DB([world])) == 1
    assert world.reported_version == "v0.6.2"


def test_the_version_is_not_read_for_games_that_pin_it(monkeypatch):
    """Minecraft's version is what the owner chose; the server cannot overrule it."""
    admin = _patch_adapter(monkeypatch, reachable=True, version="1.21.1", pins_versions=True)
    world = _world()
    assert players.refresh_player_counts(_DB([world])) == 1
    assert admin.version_reads == 0
    assert world.reported_version is None


def test_an_unreadable_version_still_counts_the_world_as_up(monkeypatch):
    """The player count already proved it. Discarding that over a missing
    version would delay readiness for no reason."""
    _patch_adapter(monkeypatch, reachable=True, version="unavailable")
    world = _world()
    assert players.refresh_player_counts(_DB([world])) == 1
    assert world.ready_at is not None
    assert world.reported_version is None


def test_an_unreachable_world_closes_its_admin_client(monkeypatch):
    """The failure path is the common one while booting, and Minecraft's client
    holds an RCON socket, so it must not leak."""
    admin = _patch_adapter(monkeypatch, reachable=False)
    assert players.refresh_player_counts(_DB([_world()])) == 0
    assert admin.closed
