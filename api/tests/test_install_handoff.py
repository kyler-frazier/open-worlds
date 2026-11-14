"""The worker hands a world whose install finished to its server.

Nothing else would: the install container exits cleanly, inspect reports the
world as still starting, and only a start() replaces it with the server.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from open_worlds.common.games.base import InstallIncomplete
from open_worlds.common.models import WorldState
from open_worlds.common.orchestrator import (
    ContainerStatus,
    OrchestratorUnavailable,
    RunState,
)
from open_worlds.worker import installs


class _DB:
    def __init__(self, worlds):
        self._worlds = worlds
        self.selected_with = None

    def execute(self, stmt):
        self.selected_with = str(stmt)
        return SimpleNamespace(scalars=lambda: list(self._worlds))


def _world(subdomain="demo"):
    return SimpleNamespace(
        subdomain=subdomain,
        game="PALWORLD",
        state=WorldState.RUNNING,
        ready_at=None,
        started_at=None,
        last_error=None,
        last_error_at=None,
        stop_target=None,
    )


def _status(subdomain="demo", finished=True):
    return ContainerStatus(
        subdomain=subdomain,
        world_id="w",
        run_state=RunState.RUNNING,
        stage="install",
        install_finished=finished,
    )


@pytest.fixture(autouse=True)
def _spec(monkeypatch):
    monkeypatch.setattr(installs, "spec_for", lambda world, db: f"spec:{world.subdomain}")


def test_only_running_worlds_are_considered():
    db = _DB([])
    orch = MagicMock()

    assert installs.advance_finished_installs(db, orch) == 0

    # The working set is normally empty, so Docker is not asked at all.
    orch.list_managed.assert_not_called()
    assert "worlds.state" in db.selected_with


def test_a_world_still_marked_ready_from_its_last_boot_is_handed_off_too():
    # Its server exited and Start was pressed before reconcile noticed; the
    # start installed. Waiting for ready_at to clear would wait forever.
    from datetime import UTC, datetime

    world = _world()
    world.ready_at = datetime.now(UTC)
    orch = MagicMock()
    orch.list_managed.return_value = [_status()]

    assert installs.advance_finished_installs(_DB([world]), orch) == 1
    # The server's boot is tracked as a boot.
    assert world.ready_at is None


def test_the_idle_window_starts_when_the_server_does():
    # Otherwise a slow install eats the time a player has to join before an
    # idle world is put back to sleep.
    from datetime import UTC, datetime, timedelta

    world = _world()
    world.started_at = datetime.now(UTC) - timedelta(minutes=9)
    orch = MagicMock()
    orch.list_managed.return_value = [_status()]

    installs.advance_finished_installs(_DB([world]), orch)

    assert world.started_at > datetime.now(UTC) - timedelta(seconds=5)


def test_a_finished_install_is_started_as_the_server():
    orch = MagicMock()
    orch.list_managed.return_value = [_status("demo"), _status("other", finished=False)]
    db = _DB([_world("demo"), _world("other")])

    assert installs.advance_finished_installs(db, orch) == 1

    orch.start.assert_called_once_with("spec:demo")


def test_a_finished_install_of_a_world_no_longer_booting_is_left():
    # Turned off or errored meanwhile: the stop path owns it.
    orch = MagicMock()
    orch.list_managed.return_value = [_status("demo")]

    assert installs.advance_finished_installs(_DB([_world("other")]), orch) == 0
    orch.start.assert_not_called()


def test_an_incomplete_install_errors_the_world_with_the_reason():
    world = _world()
    orch = MagicMock()
    orch.list_managed.return_value = [_status()]
    orch.start.side_effect = InstallIncomplete("the paper 1.21.4 install left no server to run")

    installs.advance_finished_installs(_DB([world]), orch)

    assert world.state is WorldState.ERROR
    assert world.last_error == "the paper 1.21.4 install left no server to run"
    assert world.last_error_at is not None


def test_an_unreachable_world_manager_is_retried_not_blamed_on_the_world():
    world = _world()
    orch = MagicMock()
    orch.list_managed.return_value = [_status()]
    orch.start.side_effect = OrchestratorUnavailable("down")

    assert installs.advance_finished_installs(_DB([world]), orch) == 0
    assert world.state is WorldState.RUNNING
    assert world.last_error is None
