"""Tests for "is this world actually joinable yet".

RUNNING means the container is up. It does not mean the server inside is
accepting connections, and the whole point of this signal is to stop the
dashboard conflating the two.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from open_worlds.common.models import WorldState
from open_worlds.common.readiness import Readiness, readiness_for

TIMEOUT = 300


def _world(state=WorldState.RUNNING, started_ago=None, ready=False):
    now = datetime.now(UTC)
    return SimpleNamespace(
        state=state,
        subdomain="demo",
        started_at=None if started_ago is None else now - timedelta(seconds=started_ago),
        ready_at=now if ready else None,
    )


@pytest.mark.parametrize(
    "state",
    [WorldState.OFF, WorldState.SLEEPING, WorldState.ERROR, WorldState.DELETING],
)
def test_readiness_is_not_a_question_for_a_world_that_is_not_running(state):
    assert readiness_for(_world(state=state), TIMEOUT) is Readiness.NOT_RUNNING


def test_a_world_whose_admin_answered_is_ready():
    assert readiness_for(_world(started_ago=10, ready=True), TIMEOUT) is Readiness.READY


def test_a_recently_started_world_is_still_starting():
    assert readiness_for(_world(started_ago=5), TIMEOUT) is Readiness.STARTING


def test_a_world_silent_past_the_timeout_is_unresponsive():
    # The failure this exists to surface: container up, admin interface dead. It
    # would otherwise sit there looking healthy forever.
    assert readiness_for(_world(started_ago=TIMEOUT + 1), TIMEOUT) is Readiness.UNRESPONSIVE


def test_readiness_survives_a_slow_boot_right_up_to_the_timeout():
    # Calling a merely-slow world broken is the more damaging error, so the
    # boundary belongs on the generous side.
    assert readiness_for(_world(started_ago=TIMEOUT - 1), TIMEOUT) is Readiness.STARTING


def test_ready_wins_even_long_after_the_timeout():
    # Once proven joinable, a world does not become unresponsive just by ageing.
    assert readiness_for(_world(started_ago=TIMEOUT * 10, ready=True), TIMEOUT) is Readiness.READY


def test_a_running_world_with_no_recorded_start_is_starting_not_unresponsive():
    """Pre-migration rows, or a container adopted rather than started by us.

    There is nothing to measure a timeout against, and asserting a fault we
    cannot evidence is worse than admitting we are still waiting.
    """
    assert readiness_for(_world(started_ago=None), TIMEOUT) is Readiness.STARTING


def test_the_clock_runs_from_the_start_not_from_now():
    fixed_now = datetime.now(UTC)
    world = SimpleNamespace(
        state=WorldState.RUNNING,
        subdomain="demo",
        started_at=fixed_now - timedelta(seconds=TIMEOUT + 60),
        ready_at=None,
    )
    assert readiness_for(world, TIMEOUT, now=fixed_now) is Readiness.UNRESPONSIVE
