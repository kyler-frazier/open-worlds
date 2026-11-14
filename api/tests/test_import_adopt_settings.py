"""Which settings an imported archive is allowed to take over.

The archive fills gaps and never overwrites:

1. An empty value is not adopted. PalWorldSettings.ini writes every key, so
   "no password" is ServerPassword="", indistinguishable from unset.
2. A value the owner chose on the create form is not replaced.
"""

from __future__ import annotations

import uuid

import pytest

from open_worlds.api.routers import worlds as worlds_router
from open_worlds.common.crypto import decrypt, encrypt
from open_worlds.common.games.base import ImportedSettings
from open_worlds.common.models import World, WorldState


def _world(**kw):
    defaults = dict(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        subdomain="adopt",
        display_name="Adopt",
        game="PALWORLD",
        state=WorldState.OFF,
        memory_mb=4096,
        max_players=16,
    )
    defaults.update(kw)
    return World(**defaults)


@pytest.fixture
def imported(monkeypatch):
    """Feed _adopt_imported_settings a chosen ImportedSettings, no archive."""

    def _set(server_description=None, **kw):
        # The description is a Palworld game setting, carried in .settings.
        game_settings = {"server_description": server_description} if server_description else {}
        settings = ImportedSettings(values={}, settings=game_settings, **kw)
        real = worlds_router.adapter_for

        def fake(game):
            adapter = real(game)

            class _Wrapped:
                def __getattr__(self, name):
                    return getattr(adapter, name)

                def settings_from_archive(self, _path):
                    return settings

            return _Wrapped()

        monkeypatch.setattr(worlds_router, "adapter_for", fake)

    return _set


def test_an_empty_password_in_the_archive_does_not_clear_the_one_we_have(imported):
    """A world created with a password, then imported into, keeps it when the
    archive's ini says ServerPassword=""."""
    w = _world(server_password_enc=encrypt("hunter2"))
    imported(server_password="", server_description="")
    worlds_router._adopt_imported_settings(w, "/nonexistent.zip")
    assert decrypt(w.server_password_enc) == "hunter2"


def test_a_password_in_the_archive_is_adopted(imported):
    """The case adoption exists for: the archive's world was protected."""
    w = _world(server_password_enc=None)
    imported(server_password="fromarchive")
    worlds_router._adopt_imported_settings(w, "/nonexistent.zip")
    assert decrypt(w.server_password_enc) == "fromarchive"


def test_the_password_i_typed_on_the_create_form_survives_the_import(imported):
    """The owner's choice beats the archive's. Reported from the UI.

    A world created from an upload ran this seconds after the create form was
    submitted, so anything typed there was replaced by the archive's copy.
    """
    w = _world(server_password_enc=encrypt("mine"))
    imported(server_password="fromarchive")
    worlds_router._adopt_imported_settings(w, "/nonexistent.zip")
    assert decrypt(w.server_password_enc) == "mine"


def test_the_description_i_typed_survives_the_import(imported):
    w = _world(game_settings={"server_description": "Mine"})
    imported(server_description="From the archive")
    worlds_router._adopt_imported_settings(w, "/nonexistent.zip")
    assert w.game_settings["server_description"] == "Mine"


def test_a_player_cap_i_chose_survives_the_import(imported):
    """Anything other than the platform default counts as a choice."""
    from open_worlds.common.config import get_settings

    chosen = get_settings().default_max_players + 7
    w = _world(max_players=chosen)
    imported(max_players=32)
    worlds_router._adopt_imported_settings(w, "/nonexistent.zip")
    assert w.max_players == chosen


def test_an_empty_description_does_not_wipe_the_one_we_have(imported):
    w = _world(game_settings={"server_description": "Typed by the owner"})
    imported(server_description="")
    worlds_router._adopt_imported_settings(w, "/nonexistent.zip")
    assert w.game_settings["server_description"] == "Typed by the owner"


def test_a_description_in_the_archive_is_adopted(imported):
    w = _world(game_settings={})
    imported(server_description="From the archive")
    worlds_router._adopt_imported_settings(w, "/nonexistent.zip")
    assert w.game_settings["server_description"] == "From the archive"


def test_max_players_is_adopted_when_left_at_the_default(imported):
    """No opinion expressed, so the archive's cap is better information."""
    from open_worlds.common.config import get_settings

    w = _world(max_players=get_settings().default_max_players)
    imported(max_players=32)
    worlds_router._adopt_imported_settings(w, "/nonexistent.zip")
    assert w.max_players == 32


def test_minecraft_never_adopts_a_password(imported):
    """Minecraft has no join password; adopting one would be meaningless."""
    w = _world(game="MINECRAFT", server_password_enc=None)
    imported(server_password="nope")
    worlds_router._adopt_imported_settings(w, "/nonexistent.zip")
    assert w.server_password_enc is None
