"""A third game, defined entirely in this file, through a world's whole life.

Adding a game should need only an adapter, registered. The fake game is driven
through the real routes and worker (create, start, back up, stop) with only
Docker and Drive faked; an ``if game == ...`` anywhere else fails here. See
docs/adding-a-game.md. Needs Docker for the throwaway Postgres.
"""

from __future__ import annotations

import os
import tarfile
import uuid
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict
from tests.conftest_routes import CSRF, login, logout

from open_worlds.common.games import (
    AdminClient,
    GameAdapter,
    ImportPolicy,
    PlayerInfo,
    registered,
)
from open_worlds.common.orchestrator import ContainerStatus, RunState

pytest_plugins = ["tests.conftest_routes"]


# --- The game ------------------------------------------------------------------


class FakeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    difficulty: Literal["peaceful", "hard"] = "peaceful"


class FakeAdmin:
    def __init__(self, log: list[str]):
        self._log = log

    def save_flush(self) -> None:
        self._log.append("flush")

    def save_resume(self) -> None:
        self._log.append("resume")

    def players(self) -> list[PlayerInfo]:
        return []

    def server_version(self) -> str | None:
        return "fake-1.0"

    def close(self) -> None:
        self._log.append("close")


class FakeGame(GameAdapter):
    game = "FAKEGAME"
    display_name = "Fake Game"
    image = "example.invalid/fake-game@sha256:" + "0" * 64
    data_mount = "/game"
    container_caps = ()
    allocates_port = False
    container_port = 4000
    stop_timeout_seconds = 30
    stops_only_via_platform = True
    readiness_timeout_seconds = 60
    default_memory_mb = 2048
    supports_version_pinning = False
    supports_content_catalog = False
    supports_server_password = False
    spec_fields = ("memory_mb", "difficulty")
    settings_model = FakeSettings
    import_policy = ImportPolicy(
        known_root_entries=frozenset({"save"}),
        world_child_markers=frozenset({"world.dat"}),
        data_root_markers=frozenset({"save"}),
        executable_suffixes=(".so",),
        bare_world_marker="world.dat",
        backup_exclude_dirs=frozenset({"cache"}),
    )

    def __init__(self) -> None:
        self.admin_log: list[str] = []

    def environment(self, spec) -> dict[str, str]:  # type: ignore[no-untyped-def]
        return {"DIFFICULTY": spec.settings.get("difficulty", "peaceful")}

    def admin_client(self, host: str, password: str) -> AdminClient:
        return FakeAdmin(self.admin_log)

    def connect_address(self, subdomain: str, domain: str, game_port: int | None) -> str:
        return f"{subdomain}.{domain}:{self.container_port}"


# --- Harness -------------------------------------------------------------------


class FakeOrchestrator:
    """Docker, as far as the routes and the worker can tell."""

    def __init__(self) -> None:
        self.specs: list = []
        self.running: set[str] = set()

    def start(self, spec):  # type: ignore[no-untyped-def]
        self.specs.append(spec)
        self.running.add(spec.subdomain)
        return self.inspect(spec.subdomain)

    def stop(self, subdomain: str, graceful: bool = True) -> None:
        self.running.discard(subdomain)

    def sleep(self, subdomain: str, graceful: bool = True) -> None:
        self.running.discard(subdomain)

    def inspect(self, subdomain: str) -> ContainerStatus:
        state = RunState.RUNNING if subdomain in self.running else RunState.ABSENT
        return ContainerStatus(subdomain=subdomain, world_id=None, run_state=state)

    def list_managed(self) -> list[ContainerStatus]:
        return [self.inspect(s) for s in self.running]


class FakeDrive:
    uploads: list[tuple[str, list[str]]] = []

    def __init__(self, _token: str) -> None:
        pass

    def world_folder(self, game: str, subdomain: str) -> str:
        return f"{game}/{subdomain}"

    def upload(self, path: str, filename: str, folder: str) -> str:
        with tarfile.open(path) as tar:
            FakeDrive.uploads.append((folder, sorted(tar.getnames())))
        return "drive-file-1"


@pytest.fixture
def fake_game():
    with registered(FakeGame()) as adapter:
        yield adapter


@pytest.fixture
def env(fake_game, app_client, make_user, monkeypatch):
    from open_worlds.api import deps
    from open_worlds.common.crypto import encrypt
    from open_worlds.common.models import User
    from open_worlds.worker import backups

    client, db_module = app_client
    orch = FakeOrchestrator()
    monkeypatch.setattr(deps, "_orchestrator", orch)
    monkeypatch.setattr(backups, "make_orchestrator", lambda: orch)
    monkeypatch.setattr(backups, "DriveClient", FakeDrive)
    # The headroom check reads the real disk, which a test machine's tmp may
    # not have; it is covered on its own elsewhere.
    monkeypatch.setattr(backups, "_check_free_space", lambda *a, **k: None)
    FakeDrive.uploads = []

    # Offered the way an admin offers a game, rules and all: it needs nothing
    # this host lacks, so it may be added.
    from open_worlds.common import runtime_config
    from open_worlds.common.game_availability import enabled_games, set_enabled_games

    monkeypatch.setattr(runtime_config, "_cache", runtime_config._Cache())
    with db_module.session_scope() as db:
        set_enabled_games(db, sorted(enabled_games(db) | {"FAKEGAME"}))

    owner = make_user()
    with db_module.session_scope() as db:
        user = db.get(User, uuid.UUID(owner["id"]))
        user.drive_linked = True
        user.drive_refresh_token_enc = encrypt("refresh-token")
    logout(client)
    login(client, owner)
    return client, db_module, orch, fake_game


# --- The life of a world --------------------------------------------------------


def test_the_api_describes_the_game_from_its_adapter(env):
    client, *_ = env
    games = {g["game"]: g for g in client.get("/api/games").json()}
    fake = games["FAKEGAME"]
    assert fake["display_name"] == "Fake Game"
    assert fake["settings_schema"]["properties"]["difficulty"]["enum"] == ["peaceful", "hard"]
    assert "difficulty" in fake["spec_fields"]


def test_a_setting_the_game_does_not_have_is_refused(env):
    client, *_ = env
    r = client.post(
        "/api/worlds",
        json={"game": "FAKEGAME", "display_name": "x", "subdomain": "fk-bad", "settings": {"x": 1}},
        headers=CSRF,
    )
    assert r.status_code == 400, r.text


def test_create_start_back_up_and_stop(env):
    client, db_module, orch, game = env
    from open_worlds.common.config import get_settings
    from open_worlds.common.models import Backup
    from open_worlds.worker import backups

    # Create, with one of the game's own settings.
    r = client.post(
        "/api/worlds",
        json={
            "game": "FAKEGAME",
            "display_name": "Fake",
            "subdomain": "fk-life",
            "settings": {"difficulty": "hard"},
        },
        headers=CSRF,
    )
    assert r.status_code == 201, r.text
    world = r.json()
    assert world["settings"] == {"difficulty": "hard"}
    assert world["memory_mb"] == 2048  # the game's own default
    assert world["connect_address"].endswith(":4000")

    # Start: the container spec carries the settings, and the adapter turns
    # them into the container's environment.
    r = client.post(f"/api/worlds/{world['id']}/start", headers=CSRF)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "RUNNING"
    spec = orch.specs[-1]
    assert (spec.game, spec.settings) == ("FAKEGAME", {"difficulty": "hard"})
    assert game.environment(spec) == {"DIFFICULTY": "hard"}

    # A spec field cannot change while the world is up.
    r = client.patch(
        f"/api/worlds/{world['id']}",
        json={"settings": {"difficulty": "peaceful"}, "confirm": True},
        headers=CSRF,
    )
    assert r.status_code == 409, r.text

    # Back up: the worker freezes through the game's admin client and archives
    # by the game's own import policy, which leaves its cache out.
    data = os.path.join(get_settings().worlds_dir, "fk-life", "data")
    os.makedirs(os.path.join(data, "save"), exist_ok=True)
    os.makedirs(os.path.join(data, "cache"), exist_ok=True)
    with open(os.path.join(data, "save", "world.dat"), "wb") as fh:
        fh.write(b"world")
    with open(os.path.join(data, "cache", "big.bin"), "wb") as fh:
        fh.write(b"x" * 1000)
    r = client.post(f"/api/worlds/{world['id']}/backups", json={}, headers=CSRF)
    assert r.status_code == 202, r.text
    with db_module.session_scope() as db:
        backups.run_backup(db, uuid.UUID(r.json()["id"]))
    with db_module.session_scope() as db:
        assert db.get(Backup, uuid.UUID(r.json()["id"])).status.value == "SUCCESS"
    folder, names = FakeDrive.uploads[-1]
    assert folder == "FAKEGAME/fk-life"
    assert any(n.endswith("save/world.dat") for n in names)
    assert not any("cache" in n for n in names)
    assert game.admin_log == ["flush", "resume", "close"]

    # Stop, then the setting can change.
    r = client.post(f"/api/worlds/{world['id']}/stop", headers=CSRF)
    assert r.status_code == 200, r.text
    assert client.get(f"/api/worlds/{world['id']}").json()["state"] == "OFF"
    r = client.patch(
        f"/api/worlds/{world['id']}",
        json={"settings": {"difficulty": "peaceful"}, "confirm": True},
        headers=CSRF,
    )
    assert r.status_code == 200, r.text
    assert r.json()["settings"] == {"difficulty": "peaceful"}
