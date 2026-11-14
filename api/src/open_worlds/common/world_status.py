"""Map a container's run state to the persisted WorldState, in one place, so
the API's reads and the worker's sweep never disagree."""

from __future__ import annotations

from datetime import UTC, datetime

from .games import adapter_for
from .logging import get_logger
from .metrics import world_unexpected_exits_total
from .models import World, WorldState
from .orchestrator import RunState

log = get_logger(__name__)

# Never overwritten from the container alone: the platform is mid-operation.
TRANSIENT_STATES = frozenset({WorldState.DELETING})

# A clean shutdown: a normal exit, or 128 + SIGTERM. Anything else is a crash
# (ERROR), not a SLEEPING world that never wakes.
GRACEFUL_EXIT_CODES = frozenset({0, 143})

# 128 + SIGKILL: an OOM kill, or a stop that outran its timeout. ``OOMKilled``
# tells them apart.
SIGKILL_EXIT_CODE = 137


def reconciled_state(
    current: WorldState,
    run_state: RunState,
    exit_code: int | None = None,
    oom_killed: bool = False,
) -> WorldState:
    """The state a world should be in, given its state and its container.

    The stored state wins for a world mid-delete, or STOPPING while its
    container still runs.
    """
    if current in TRANSIENT_STATES:
        return current
    if current is WorldState.STOPPING and run_state is RunState.RUNNING:
        return current
    if current is WorldState.ERROR and run_state is not RunState.RUNNING:
        # Until someone starts it again. The worker's stop of a failed server
        # exits cleanly, which alone would read as a sleep.
        return current
    return world_state_for(run_state, exit_code, oom_killed)


def world_state_for(
    run_state: RunState, exit_code: int | None, oom_killed: bool = False
) -> WorldState:
    """The world state implied by a container's run state and exit code.

    An exit is a sleep unless its code says crash. A missing code counts as
    clean, so an inspect gap never invents an error. 137 is a crash only with
    the OOM killer involved: a slow save SIGKILLed during a requested stop is
    not, and an ERROR world loses its waker port.
    """
    if run_state is RunState.RUNNING:
        return WorldState.RUNNING
    if run_state is RunState.ABSENT:
        return WorldState.OFF
    if exit_code == SIGKILL_EXIT_CODE and not oom_killed:
        return WorldState.SLEEPING
    if exit_code is not None and exit_code not in GRACEFUL_EXIT_CODES:
        return WorldState.ERROR
    return WorldState.SLEEPING


def apply_state(world: World, new_state: WorldState, now: datetime | None = None) -> bool:
    """Write a world's state, keeping the readiness stamps honest. Every path
    that changes ``state`` goes through here. Returns whether anything changed.

    Entering RUNNING restarts the readiness clock, however it got there: a
    fresh container is not joinable yet.
    """
    if new_state is world.state:
        return False
    if _exit_was_unrequested(world, new_state):
        world_unexpected_exits_total.inc()
        log.warning(
            "world exited unexpectedly",
            extra={"subdomain": world.subdomain, "reason": "exit nobody requested"},
        )
    if new_state is WorldState.RUNNING:
        world.started_at = now or datetime.now(UTC)
        world.ready_at = None
        # A new boot; the last error is history.
        world.last_error = None
        world.last_error_at = None
    if new_state is not WorldState.STOPPING:
        world.stop_target = None
    world.state = new_state
    return True


def _exit_was_unrequested(world: World, new_state: WorldState) -> bool:
    """Whether this is a container exit the platform never asked for.

    For a game whose stops all go through the platform, RUNNING goes to
    STOPPING first. Checked here because the API reconciles within seconds of
    an exit, before the worker's sweep could see it. ERROR is counted by the
    sweep.
    """
    if world.state is not WorldState.RUNNING:
        return False
    if new_state not in (WorldState.SLEEPING, WorldState.OFF):
        return False
    return adapter_for(world.game).stops_only_via_platform
