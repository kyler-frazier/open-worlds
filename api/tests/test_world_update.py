"""Tests for editable-world logic: change/risk assessment and the update
endpoint's confirm + must-be-off guards. DB/authz are faked."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from open_worlds.api.routers import worlds
from open_worlds.api.routers.worlds import _assess_world_changes, update_world
from open_worlds.api.schemas import WorldUpdate
from open_worlds.common.authz import AccessLevel, Principal
from open_worlds.common.crypto import encrypt
from open_worlds.common.models import AutoBackupInterval, WorldState

# --- change / risk assessment --------------------------------------------


def _world(game="PALWORLD", **kw):
    base = dict(
        id=uuid.uuid4(),
        game=game,
        subdomain="w1",
        display_name="Old",
        game_port=8211 if game == "PALWORLD" else None,
        game_settings=(
            {"version": "1.21.4", "server_type": "VANILLA"} if game == "MINECRAFT" else {}
        ),
        reported_version=None,
        memory_mb=8192,
        max_players=16,
        state=WorldState.OFF,
        server_password_enc=None,
        started_at=None,
        last_active_at=None,
        ready_at=None,
        player_count=0,
        players_updated_at=None,
        owner=SimpleNamespace(drive_linked=True, email=None),
        owner_id=uuid.uuid4(),
        auto_backup_enabled=False,
        auto_backup_interval=AutoBackupInterval.DAILY,
        auto_backup_max=7,
        created_at=datetime.now(UTC),
        egress_log_only=False,
        last_error=None,
        last_error_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_no_changes_detected():
    changed, risky = _assess_world_changes(_world(), WorldUpdate())
    assert changed == set()
    assert risky == []


def test_display_name_change_is_not_risky():
    changed, risky = _assess_world_changes(_world(), WorldUpdate(display_name="New"))
    assert changed == {"display_name"}
    assert risky == []


def test_memory_change_is_not_risky():
    changed, risky = _assess_world_changes(_world(), WorldUpdate(memory_mb=16384))
    assert changed == {"memory_mb"}
    assert risky == []


def test_lowering_the_player_cap_is_risky():
    changed, risky = _assess_world_changes(_world(max_players=16), WorldUpdate(max_players=4))
    assert changed == {"max_players"}
    assert len(risky) == 1


def test_raising_the_player_cap_is_not_risky():
    changed, risky = _assess_world_changes(_world(max_players=8), WorldUpdate(max_players=16))
    assert changed == {"max_players"}
    assert risky == []


# --- endpoint guards ------------------------------------------------------


class _DB:
    def commit(self):
        pass

    def refresh(self, _obj):
        pass


def _principal():
    return Principal(user_id=str(uuid.uuid4()), google_sub="s", is_admin=False)


def _patch_load(monkeypatch, world):
    # The loader now returns (world, level) so callers cannot forget the
    # level; OWNER here because these tests are about update semantics, not
    # authorization -- the tiers are covered by the permission matrix.
    monkeypatch.setattr(worlds, "load_world_for", lambda *_a, **_k: (world, AccessLevel.OWNER))


def test_change_without_confirm_is_rejected(monkeypatch):
    world = _world()
    _patch_load(monkeypatch, world)
    with pytest.raises(HTTPException) as exc:
        update_world("wid", WorldUpdate(display_name="New"), _principal(), _DB())
    assert exc.value.status_code == 409


def test_spec_change_requires_world_off(monkeypatch):
    world = _world(state=WorldState.RUNNING)
    _patch_load(monkeypatch, world)
    with pytest.raises(HTTPException) as exc:
        update_world("wid", WorldUpdate(memory_mb=16384, confirm=True), _principal(), _DB())
    assert exc.value.status_code == 409
    assert "off" in exc.value.detail.lower()


def test_confirmed_spec_change_applies_when_off(monkeypatch):
    world = _world(state=WorldState.OFF)
    _patch_load(monkeypatch, world)
    out = update_world("wid", WorldUpdate(memory_mb=16384, confirm=True), _principal(), _DB())
    assert world.memory_mb == 16384
    assert out.memory_mb == 16384


@pytest.mark.parametrize("game", ["PALWORLD", "MINECRAFT"])
def test_display_name_can_change_while_running(monkeypatch, game):
    # Display name is pure platform metadata for every game: no adapter derives
    # anything baked into a container from it (Palworld's SERVER_NAME comes from
    # the immutable subdomain). So a rename applies immediately, running or not.
    world = _world(game=game, state=WorldState.RUNNING)
    _patch_load(monkeypatch, world)
    out = update_world("wid", WorldUpdate(display_name="New", confirm=True), _principal(), _DB())
    assert world.display_name == "New"
    assert out.display_name == "New"


def test_setting_a_join_password_requires_the_world_off(monkeypatch):
    # SERVER_PASSWORD is container environment, so it only takes effect on
    # recreation -- accepting it live would leave the owner believing the world
    # is locked while it is still open.
    world = _world(state=WorldState.RUNNING)
    _patch_load(monkeypatch, world)
    with pytest.raises(HTTPException) as exc:
        update_world(
            "wid", WorldUpdate(server_password="hunter2", confirm=True), _principal(), _DB()
        )
    assert exc.value.status_code == 409
    assert "off" in exc.value.detail.lower()


def test_join_password_round_trips_through_encryption(monkeypatch):
    world = _world(state=WorldState.OFF)
    _patch_load(monkeypatch, world)
    out = update_world(
        "wid", WorldUpdate(server_password="hunter2", confirm=True), _principal(), _DB()
    )
    # Stored encrypted, but readable back: the owner has to know what to share.
    assert world.server_password_enc is not None
    assert world.server_password_enc != "hunter2"
    assert out.server_password == "hunter2"


def test_clearing_the_join_password_opens_the_world(monkeypatch):
    world = _world(state=WorldState.OFF, server_password_enc=encrypt("hunter2"))
    _patch_load(monkeypatch, world)
    changed, risky = _assess_world_changes(world, WorldUpdate(server_password=""))
    assert changed == {"server_password"}
    assert any("open" in r for r in risky)
    out = update_world("wid", WorldUpdate(server_password="", confirm=True), _principal(), _DB())
    assert world.server_password_enc is None
    assert out.server_password == ""


def test_omitting_the_password_leaves_it_untouched(monkeypatch):
    world = _world(state=WorldState.OFF, server_password_enc=encrypt("hunter2"))
    _patch_load(monkeypatch, world)
    changed, _ = _assess_world_changes(world, WorldUpdate(display_name="New"))
    assert "server_password" not in changed
    out = update_world("wid", WorldUpdate(display_name="New", confirm=True), _principal(), _DB())
    assert out.server_password == "hunter2"


def test_minecraft_rejects_a_join_password(monkeypatch):
    # Minecraft has no such concept; silently ignoring it would leave the owner
    # thinking the world is password-protected.
    world = _world(game="MINECRAFT", state=WorldState.OFF)
    _patch_load(monkeypatch, world)
    with pytest.raises(HTTPException) as exc:
        update_world(
            "wid", WorldUpdate(server_password="hunter2", confirm=True), _principal(), _DB()
        )
    assert exc.value.status_code == 400


def test_player_cap_change_requires_off_for_every_game(monkeypatch):
    # MAX_PLAYERS/PLAYERS is baked into the container environment for both games,
    # so accepting the edit on a running world would promise a setting that
    # silently does nothing until the container is next recreated.
    for game in ("PALWORLD", "MINECRAFT"):
        world = _world(game=game, state=WorldState.RUNNING)
        _patch_load(monkeypatch, world)
        with pytest.raises(HTTPException) as exc:
            update_world("wid", WorldUpdate(max_players=32, confirm=True), _principal(), _DB())
        assert exc.value.status_code == 409
        assert "off" in exc.value.detail.lower()


def test_backup_settings_change_while_running(monkeypatch):
    # Auto-backup settings are not part of the container spec, so they can be
    # changed at any time and need no confirmation.
    world = _world(state=WorldState.RUNNING)
    _patch_load(monkeypatch, world)
    out = update_world("wid", WorldUpdate(auto_backup_enabled=True), _principal(), _DB())
    assert world.auto_backup_enabled is True
    assert out.auto_backup_enabled is True
