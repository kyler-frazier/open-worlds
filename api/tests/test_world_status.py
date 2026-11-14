"""Run-state -> WorldState mapping (shared by the API read path and the worker).

A crashed world (non-graceful exit) must surface as ERROR, not the SLEEPING it
would never wake from; a graceful auto-sleep must stay SLEEPING.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from open_worlds.common.metrics import world_unexpected_exits_total
from open_worlds.common.models import WorldState
from open_worlds.common.orchestrator import RunState
from open_worlds.common.world_status import apply_state, reconciled_state, world_state_for


def test_running_maps_to_running():
    assert world_state_for(RunState.RUNNING, None) is WorldState.RUNNING


def test_absent_maps_to_off():
    assert world_state_for(RunState.ABSENT, None) is WorldState.OFF


def test_graceful_exit_is_sleeping():
    for code in (0, 143):
        assert world_state_for(RunState.EXITED, code) is WorldState.SLEEPING


def test_crash_exit_is_error():
    # A mod-load failure (1) or an abort (134) is a genuine crash.
    for code in (1, 134):
        assert world_state_for(RunState.EXITED, code) is WorldState.ERROR


def test_an_oom_kill_is_a_crash():
    assert world_state_for(RunState.EXITED, 137, oom_killed=True) is WorldState.ERROR


def test_a_sigkill_we_caused_is_not_a_crash():
    """137 without an OOM kill means our own stop timeout escalated to SIGKILL.

    Nothing crashed. ERROR would misreport a routine sleep, and pal-waker binds
    no port for an ERROR world.
    """
    assert world_state_for(RunState.EXITED, 137, oom_killed=False) is WorldState.SLEEPING


def test_exited_without_exit_code_defaults_to_sleeping():
    # A transient inspect gap (no exit code) must not fabricate an ERROR.
    assert world_state_for(RunState.EXITED, None) is WorldState.SLEEPING


# --- reconciled_state: the stored state can outrank the container -------------


def test_transient_states_are_never_overwritten():
    for state in (WorldState.DELETING,):
        assert reconciled_state(state, RunState.ABSENT) is state
        assert reconciled_state(state, RunState.RUNNING) is state


def test_stopping_holds_while_the_container_is_still_running():
    """A stop in progress is not drift. Reading the running container as
    RUNNING would flip the state back and fight the stop the whole way."""
    assert reconciled_state(WorldState.STOPPING, RunState.RUNNING) is WorldState.STOPPING


def test_stopping_resolves_once_the_container_has_gone_down():
    assert reconciled_state(WorldState.STOPPING, RunState.ABSENT) is WorldState.OFF
    assert reconciled_state(WorldState.STOPPING, RunState.EXITED, 143) is WorldState.SLEEPING
    # A crash during shutdown is still a crash.
    assert reconciled_state(WorldState.STOPPING, RunState.EXITED, 1) is WorldState.ERROR


def test_other_states_follow_the_container():
    assert reconciled_state(WorldState.SLEEPING, RunState.RUNNING) is WorldState.RUNNING
    assert reconciled_state(WorldState.RUNNING, RunState.ABSENT) is WorldState.OFF


# --- apply_state: readiness stamps follow the transition ---------------------


def _row(state, **kw):
    base = dict(
        state=state,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ready_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        stop_target=None,
        subdomain="w",
        game="MINECRAFT",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_entering_running_restarts_the_readiness_clock():
    """A Minecraft world woken by mc-router kept last boot's ready_at and read
    as joinable while still starting. Any path into RUNNING resets it."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    w = _row(WorldState.SLEEPING)
    assert apply_state(w, WorldState.RUNNING, now=now)
    assert w.state is WorldState.RUNNING
    assert w.started_at == now
    assert w.ready_at is None


def test_staying_running_keeps_the_stamps():
    w = _row(WorldState.RUNNING)
    before = (w.started_at, w.ready_at)
    assert not apply_state(w, WorldState.RUNNING)
    assert (w.started_at, w.ready_at) == before


def test_leaving_running_keeps_the_stamps_for_the_record():
    w = _row(WorldState.RUNNING)
    assert apply_state(w, WorldState.SLEEPING)
    assert w.ready_at is not None


def test_leaving_stopping_clears_its_target():
    w = _row(WorldState.STOPPING, stop_target=WorldState.OFF)
    apply_state(w, WorldState.OFF)
    assert w.stop_target is None


# --- Exits nobody asked for ---------------------------------------------------
#
# Palworld's image exits 0 even after a segfault, so for a game whose every stop
# goes through the platform the exit code cannot tell a crash from a clean stop.
# Skipping STOPPING is what gives it away.


def _exits() -> float:
    return world_unexpected_exits_total._value.get()


def test_palworld_leaving_running_without_a_stop_is_a_crash():
    before = _exits()
    w = _row(WorldState.RUNNING, game="PALWORLD")
    # What the reconcile maps a segfaulted Palworld container to: exit 0 -> SLEEPING.
    assert apply_state(w, reconciled_state(w.state, RunState.EXITED, 0))
    assert w.state is WorldState.SLEEPING
    assert _exits() == before + 1


def test_palworld_container_vanishing_while_running_is_a_crash():
    before = _exits()
    apply_state(_row(WorldState.RUNNING, game="PALWORLD"), WorldState.OFF)
    assert _exits() == before + 1


def test_palworld_sleep_the_platform_asked_for_is_not_a_crash():
    before = _exits()
    w = _row(WorldState.STOPPING, game="PALWORLD", stop_target=WorldState.SLEEPING)
    apply_state(w, WorldState.SLEEPING)
    assert _exits() == before


def test_palworld_error_is_left_to_the_exit_code_count():
    # The sweep already counts non-graceful exit codes; counting here too would
    # double every such crash.
    before = _exits()
    apply_state(_row(WorldState.RUNNING, game="PALWORLD"), WorldState.ERROR)
    assert _exits() == before


def test_minecraft_router_sleep_is_not_a_crash():
    before = _exits()
    apply_state(_row(WorldState.RUNNING, game="MINECRAFT"), WorldState.SLEEPING)
    assert _exits() == before
