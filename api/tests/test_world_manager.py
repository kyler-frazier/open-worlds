"""The split that keeps the Docker credential out of the api process: which
implementation each process gets, and what crosses the wire."""

from __future__ import annotations

import pytest

from open_worlds.common import config
from open_worlds.common.games.base import InstallIncomplete
from open_worlds.common.orchestrator import (
    ContainerNameConflict,
    DockerOrchestrator,
    OrchestratorUnavailable,
    RemoteOrchestrator,
    RunState,
    WorldImageUnavailable,
    make_orchestrator,
)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_without_a_world_manager_the_process_drives_docker(monkeypatch):
    """The single-process arrangement, still what a dev run and the tests use."""
    monkeypatch.delenv("PLATFORM_WORLD_MANAGER_URL", raising=False)
    assert isinstance(make_orchestrator(), DockerOrchestrator)


def test_with_one_configured_the_process_asks_instead(monkeypatch):
    monkeypatch.setenv("PLATFORM_WORLD_MANAGER_URL", "http://world-manager:8000")
    monkeypatch.setenv("PLATFORM_WORLD_MANAGER_SECRET", "s3cret")
    assert isinstance(make_orchestrator(), RemoteOrchestrator)


def test_a_configured_manager_without_a_secret_refuses_to_start(monkeypatch):
    """Failing here beats sending every lifecycle call unauthenticated."""
    monkeypatch.setenv("PLATFORM_WORLD_MANAGER_URL", "http://world-manager:8000")
    monkeypatch.setenv("PLATFORM_WORLD_MANAGER_SECRET", "")
    with pytest.raises(RuntimeError, match="WORLD_MANAGER_SECRET"):
        make_orchestrator()


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload


def _remote(monkeypatch, resp):
    captured = {}

    def fake_request(method, url, json=None, headers=None, timeout=None):
        captured.update(method=method, url=url, json=json, headers=headers, timeout=timeout)
        return resp

    monkeypatch.setattr("open_worlds.common.orchestrator.httpx2.request", fake_request)
    return RemoteOrchestrator("http://world-manager:8000", "s3cret"), captured


def test_every_call_carries_the_secret(monkeypatch):
    orch, captured = _remote(monkeypatch, _Resp(204))
    orch.stop("demo")
    assert captured["headers"]["X-World-Manager-Secret"] == "s3cret"
    assert captured["url"] == "http://world-manager:8000/worlds/demo/stop"


def test_the_timeout_outlasts_the_longest_graceful_stop(monkeypatch):
    """Palworld may take 300s to save. A shorter timeout would report a
    successful, slow stop as a failure and leave the row wrong."""
    orch, captured = _remote(monkeypatch, _Resp(204))
    orch.sleep("demo")
    assert captured["timeout"] > 300


def test_a_status_round_trips(monkeypatch):
    orch, _ = _remote(
        monkeypatch,
        _Resp(
            200,
            {
                "subdomain": "demo",
                "world_id": "w1",
                "run_state": "RUNNING",
                "container_id": "cid",
                "labels": {"a": "b"},
                "exit_code": 137,
                "finished_at": "2026-01-01T00:00:00Z",
                "oom_killed": True,
                "stage": "install",
                "install_finished": True,
            },
        ),
    )
    cs = orch.inspect("demo")
    assert cs.run_state is RunState.RUNNING
    # oom_killed and exit_code drive crash reporting; dropping them in transit
    # would turn every OOM kill into a silent restart.
    assert cs.oom_killed is True
    assert cs.exit_code == 137
    # Without these the worker never hands a finished install to the server.
    assert cs.stage == "install"
    assert cs.install_finished is True


@pytest.mark.parametrize(
    "code,expected",
    [
        (409, ContainerNameConflict),
        (503, WorldImageUnavailable),
        (422, InstallIncomplete),
        (500, OrchestratorUnavailable),
    ],
)
def test_failures_keep_their_meaning_across_the_wire(monkeypatch, code, expected):
    """Callers branch on these. Flattening them to one error would turn "that
    name is taken" and "the image will not pull" into the same message."""
    orch, _ = _remote(monkeypatch, _Resp(code, {"detail": "nope"}, text="nope"))
    with pytest.raises(expected):
        orch.inspect("demo")


def test_an_unreachable_manager_is_not_mistaken_for_a_missing_world(monkeypatch):
    import httpx2

    def boom(*a, **k):
        raise httpx2.ConnectError("refused")

    monkeypatch.setattr("open_worlds.common.orchestrator.httpx2.request", boom)
    orch = RemoteOrchestrator("http://world-manager:8000", "s3cret")
    with pytest.raises(OrchestratorUnavailable):
        orch.inspect("demo")


@pytest.fixture
def manager(monkeypatch):
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from open_worlds.world_manager import main as wm

    monkeypatch.setenv("PLATFORM_WORLD_MANAGER_SECRET", "s3cret")
    orch = MagicMock()
    monkeypatch.setattr(wm, "DockerOrchestrator", lambda: orch)
    client = TestClient(wm.create_app(), headers={"X-World-Manager-Secret": "s3cret"})
    return client, orch


@pytest.mark.parametrize("bad", ["..", "...", "Demo", "-demo", "a_b", "x" * 64])
def test_a_subdomain_that_is_not_a_dns_label_never_reaches_docker(manager, bad):
    """It becomes a path under the worlds dir: `..` would be the dir's parent."""
    client, orch = manager
    for method, path in (
        ("POST", f"/worlds/{bad}/stop"),
        ("POST", f"/worlds/{bad}/sleep"),
        ("GET", f"/worlds/{bad}/logs"),
        ("DELETE", f"/worlds/{bad}"),
        ("GET", f"/worlds/{bad}"),
    ):
        assert client.request(method, path).status_code in (404, 422), (method, path)
    spec = {
        "game": "minecraft",
        "world_id": "w",
        "owner_id": "o",
        "subdomain": bad,
        "display_name": "D",
        "memory_mb": 2048,
        "max_players": 4,
        "admin_password": "pw",
    }
    assert client.post("/worlds/start", json=spec).status_code == 400
    assert not orch.method_calls


def test_a_well_formed_subdomain_is_passed_through(manager):
    client, orch = manager
    assert client.post("/worlds/demo-1/stop").status_code == 204
    orch.stop.assert_called_once_with("demo-1", graceful=True)
