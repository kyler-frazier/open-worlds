"""A world whose server failed to start is explained, stopped and marked ERROR.

The model case: Forge logs "Failed to load datapacks, can't proceed with
server load" and its JVM stays alive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from open_worlds.common import startup_errors
from open_worlds.common.models import WorldState
from open_worlds.common.orchestrator import RunState
from open_worlds.common.world_status import apply_state, reconciled_state
from open_worlds.worker import failures

FORGE_LOG = "\n".join(
    [
        "[init] Starting the Minecraft server...",
        "[10:15:52] [main/INFO]: Loading Minecraft 26.3",
        "\x1b[32m[10:15:57] [main/WARN] [minecraft/MinecraftServer]: Missing data pack paper\x1b[m",
        "[10:15:58] [Worker-Main-9/ERROR]: Unable to read or access the world gen settings file!",
        "[10:15:58] [main/WARN] [minecraft/Main]: Failed to load datapacks, can't proceed with "
        "server load. You can either fix your datapacks or reset to vanilla with --safeMode",
        "java.util.concurrent.ExecutionException: java.lang.IllegalStateException: Overworld "
        "settings missing",
        "\tat java.base/java.util.concurrent.CompletableFuture.wrapInExecutionException",
        "Caused by: java.lang.IllegalStateException: Overworld settings missing",
    ]
)
PATTERNS = ("Failed to load datapacks, can't proceed with server load",)


# --- reading the log ---------------------------------------------------------


def test_the_excerpt_starts_just_before_the_fatal_line_and_keeps_the_trace():
    excerpt = startup_errors.excerpt_from_fatal(FORGE_LOG, PATTERNS)
    assert excerpt is not None
    assert excerpt.splitlines()[0].startswith("\x1b") is False  # ANSI stripped
    assert "Missing data pack paper" in excerpt  # context before
    assert "Caused by: java.lang.IllegalStateException" in excerpt  # trace after
    assert "[init] Starting" not in excerpt  # more than CONTEXT_BEFORE lines up


def test_the_summary_is_the_fatal_line():
    excerpt = startup_errors.excerpt_from_fatal(FORGE_LOG, PATTERNS)
    assert startup_errors.summary(excerpt, PATTERNS).endswith("--safeMode")


def test_no_known_line_means_no_fatal_excerpt():
    assert startup_errors.excerpt_from_fatal("all fine\nDone (3.2s)!", PATTERNS) is None


def test_secrets_never_reach_the_stored_excerpt():
    raw = (
        "launching with --rcon-password hunter2\n"
        "Failed to load datapacks, can't proceed with server load"
    )
    excerpt = startup_errors.excerpt_from_fatal(raw, PATTERNS, redact=("hunter2",))
    assert "hunter2" not in excerpt and "********" in excerpt


def test_the_tail_is_capped_in_lines_and_characters():
    raw = "\n".join(f"line {i} " + "x" * 400 for i in range(500))
    tail = startup_errors.excerpt_from_tail(raw)
    # The newest lines survive the character cap; a tail is read bottom-up.
    assert tail.rstrip().split("\n")[-1].startswith("line 499")
    assert "line 400 " not in tail
    assert len(tail) <= startup_errors.MAX_CHARS


# --- the world's state -------------------------------------------------------


def test_an_errored_world_stays_errored_when_its_container_exits_cleanly():
    """The worker's own stop exits 143, which alone would read as a sleep."""
    assert reconciled_state(WorldState.ERROR, RunState.EXITED, 143) is WorldState.ERROR
    assert reconciled_state(WorldState.ERROR, RunState.ABSENT) is WorldState.ERROR
    # Started again by someone: running is running.
    assert reconciled_state(WorldState.ERROR, RunState.RUNNING) is WorldState.RUNNING


def test_a_new_boot_clears_the_last_error():
    world = _world(state=WorldState.ERROR, last_error="boom", last_error_at=datetime.now(UTC))
    apply_state(world, WorldState.RUNNING)
    assert world.last_error is None and world.last_error_at is None


# --- the worker check --------------------------------------------------------


def _world(**kw):
    base = dict(
        id="w1",
        subdomain="mc-test",
        game="MINECRAFT",
        state=WorldState.RUNNING,
        stop_target=None,
        started_at=datetime.now(UTC) - timedelta(seconds=30),
        ready_at=None,
        admin_password_enc=None,
        server_password_enc=None,
        last_error=None,
        last_error_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _db(*worlds):
    db = MagicMock()
    db.execute.return_value.scalars.return_value = list(worlds)
    return db


def test_a_logged_fatal_error_stops_the_world_at_once_and_marks_it_error():
    world = _world()
    orch = MagicMock()
    orch.logs.return_value = FORGE_LOG

    assert failures.check_startup_failures(_db(world), orch) == 1

    assert world.state is WorldState.ERROR
    assert "Failed to load datapacks" in world.last_error
    assert world.last_error_at is not None
    # Kept (not removed) so the full log stays readable, and without the
    # shutdown window: the server has nothing to save and may never answer.
    orch.sleep.assert_called_once_with("mc-test", graceful=False)
    orch.stop.assert_not_called()


def test_a_booting_world_with_a_clean_log_is_left_alone():
    world = _world()
    orch = MagicMock()
    orch.logs.return_value = "[init] Starting the Minecraft server...\nPreparing level"

    assert failures.check_startup_failures(_db(world), orch) == 0
    assert world.state is WorldState.RUNNING and world.last_error is None
    orch.sleep.assert_not_called()


def test_past_its_boot_window_a_silent_world_gets_its_log_tail_but_keeps_running():
    world = _world(started_at=datetime.now(UTC) - timedelta(minutes=10))
    orch = MagicMock()
    orch.logs.return_value = "Preparing level\nstill going\nnothing since"

    failures.check_startup_failures(_db(world), orch)

    assert world.state is WorldState.RUNNING  # slow is still possible
    assert world.last_error.endswith("nothing since")
    orch.sleep.assert_not_called()


def test_the_same_error_is_not_re_announced_on_every_pass():
    first = datetime(2026, 10, 5, 10, 16, tzinfo=UTC)
    world = _world(state=WorldState.RUNNING)
    excerpt = startup_errors.excerpt_from_fatal(
        FORGE_LOG, failures.adapter_for("MINECRAFT").fatal_log_patterns
    )
    world.last_error, world.last_error_at = excerpt, first
    orch = MagicMock()
    orch.logs.return_value = FORGE_LOG

    failures.check_startup_failures(_db(world), orch)
    assert world.last_error_at == first


def test_a_crashed_container_is_explained_from_its_log():
    world = _world(state=WorldState.RUNNING)
    orch = MagicMock()
    orch.logs.return_value = "booting\n[init] [ERROR] Failed to install Forge"
    failures.explain_crash(world, orch)
    assert world.last_error.endswith("Failed to install Forge")
