"""Version pinning + server-side-only badge logic.

Worlds must be pinned to a *specific* Minecraft version — a release or a specific
snapshot/pre-release — never the rolling LATEST/SNAPSHOT aliases, so they never
auto-update into a breaking game version. The default offered at create-time is
the latest stable release (Mojang manifest, with a fallback).
"""

from __future__ import annotations

import time

import pytest

from open_worlds.common.games.minecraft import versions as mcversion
from open_worlds.common.games.minecraft.config import minecraft_config
from open_worlds.common.games.minecraft.content_api import _server_side_only
from open_worlds.common.models import ContentCatalog, ContentKind, ContentSource


def _validate(**settings):
    from open_worlds.common.games import adapter_for

    return adapter_for("MINECRAFT").validate_settings(settings)


def test_settings_reject_rolling_aliases():
    # The rolling aliases auto-update and are rejected; garbage is too.
    from open_worlds.common.games import GameSettingsError

    for bad in ("LATEST", "latest", "SNAPSHOT", "snapshot", "banana", "1.x"):
        with pytest.raises(GameSettingsError):
            _validate(version=bad)


def test_settings_allow_blank_release_and_snapshot():
    # Blank means "resolve to latest server-side" (new_settings fills it in).
    assert _validate()["version"] == ""
    assert _validate(version="1.21.4")["version"] == "1.21.4"
    # Specific snapshots / pre-releases are pinnable (they don't auto-update).
    assert _validate(version="24w14a")["version"] == "24w14a"
    assert _validate(version="1.20.2-pre1")["version"] == "1.20.2-pre1"


def test_an_update_cannot_blank_the_version():
    from open_worlds.common.games import GameSettingsError, adapter_for

    current = {"server_type": "VANILLA", "version": "1.21.4"}
    with pytest.raises(GameSettingsError):
        adapter_for("MINECRAFT").updated_settings(current, {"version": ""})


def test_is_pinnable_version_matrix():
    for good in ("1.21.4", "1.20", "24w14a", "1.20.2-pre1", "1.21-rc1"):
        assert mcversion.is_pinnable_version(good), good
    for bad in ("", "LATEST", "SNAPSHOT", "24W14A", "banana", "1.20-beta1"):
        assert not mcversion.is_pinnable_version(bad), bad


class _Resp:
    def __init__(self, data: object) -> None:
        self._data = data

    def raise_for_status(self) -> None:  # noqa: D401 - stub
        return None

    def json(self) -> object:
        return self._data


def _client_returning(data: object):
    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a) -> bool:
            return False

        def get(self, *a, **k):
            return _Resp(data)

    return _Client


def test_latest_release_parses_manifest(monkeypatch):
    mcversion._reset_cache_for_tests()
    monkeypatch.setattr(
        mcversion.httpx2, "Client", _client_returning({"latest": {"release": "1.99.9"}})
    )
    assert mcversion.latest_release() == "1.99.9"
    mcversion._reset_cache_for_tests()


def test_latest_release_falls_back_on_error(monkeypatch):
    mcversion._reset_cache_for_tests()

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a) -> bool:
            return False

        def get(self, *a, **k):
            raise mcversion.httpx2.ConnectError("boom")

    monkeypatch.setattr(mcversion.httpx2, "Client", _Client)
    assert mcversion.latest_release() == minecraft_config().default_world_version_fallback
    mcversion._reset_cache_for_tests()


def _failing_client():
    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a) -> bool:
            return False

        def get(self, *a, **k):
            raise mcversion.httpx2.ConnectError("boom")

    return _Client


def test_a_failed_refresh_never_downgrades_a_resolved_version(monkeypatch):
    """An outage must not replace a real version with the configured fallback.

    Stale-while-revalidate re-fetches on every expiry, so without this a single
    Mojang blip would swap a correct 1.99.9 for the fallback and keep it.
    """
    mcversion._reset_cache_for_tests()
    monkeypatch.setattr(
        mcversion.httpx2, "Client", _client_returning({"latest": {"release": "1.99.9"}})
    )
    assert mcversion.latest_release() == "1.99.9"

    # Mojang goes down; a refresh runs and fails.
    monkeypatch.setattr(mcversion.httpx2, "Client", _failing_client())
    assert mcversion._fetch_latest_release() == "1.99.9"
    assert mcversion.latest_release() == "1.99.9"
    mcversion._reset_cache_for_tests()


def test_a_failed_refresh_is_retried_sooner_than_a_full_ttl(monkeypatch):
    """The kept-stale value must not be pinned for the whole success TTL."""
    mcversion._reset_cache_for_tests()
    monkeypatch.setattr(
        mcversion.httpx2, "Client", _client_returning({"latest": {"release": "1.99.9"}})
    )
    mcversion.latest_release()
    fresh_stamp = mcversion._latest_cache[0]

    monkeypatch.setattr(mcversion.httpx2, "Client", _failing_client())
    mcversion._fetch_latest_release()
    retry_stamp = mcversion._latest_cache[0]

    # Backdated, so the entry expires in _REFRESH_RETRY_SECONDS rather than a TTL.
    assert retry_stamp < fresh_stamp
    ttl = minecraft_config().version_cache_ttl_seconds
    assert retry_stamp <= time.monotonic() - ttl + mcversion._REFRESH_RETRY_SECONDS
    mcversion._reset_cache_for_tests()


def test_latest_release_ignores_non_concrete_manifest_value(monkeypatch):
    mcversion._reset_cache_for_tests()
    monkeypatch.setattr(
        mcversion.httpx2, "Client", _client_returning({"latest": {"release": "SNAPSHOT"}})
    )
    assert mcversion.latest_release() == minecraft_config().default_world_version_fallback
    mcversion._reset_cache_for_tests()


def _mod(client_side: str | None) -> ContentCatalog:
    return ContentCatalog(
        kind=ContentKind.MOD,
        source=ContentSource.MODRINTH,
        project_slug="x",
        name="X",
        cached_client_side=client_side,
    )


def test_server_side_only_flag():
    assert _server_side_only(_mod("unsupported")) is True
    assert _server_side_only(_mod("optional")) is True
    assert _server_side_only(_mod("required")) is False
    # Unknown / missing / non-mod kinds carry no badge.
    assert _server_side_only(_mod("unknown")) is None
    assert _server_side_only(_mod(None)) is None
    datapack = ContentCatalog(
        kind=ContentKind.DATAPACK,
        source=ContentSource.MODRINTH,
        project_slug="d",
        name="D",
        cached_client_side="unsupported",
    )
    assert _server_side_only(datapack) is None


def _manifest(latest="1.21.4", ids=("1.21.4", "1.21.3", "24w14a")):
    return {"latest": {"release": latest}, "versions": [{"id": i} for i in ids]}


def test_version_exists_checks_the_manifest(monkeypatch):
    """A well-formed but nonexistent version is refused rather than failing at boot."""
    mcversion._reset_cache_for_tests()
    monkeypatch.setattr(mcversion.httpx2, "Client", _client_returning(_manifest()))
    assert mcversion.version_exists("1.21.4") is True
    assert mcversion.version_exists("24w14a") is True
    assert mcversion.version_exists("1.21.99") is False
    assert mcversion.known_versions() == frozenset({"1.21.4", "1.21.3", "24w14a"})
    mcversion._reset_cache_for_tests()


def test_version_exists_is_unknown_without_a_manifest(monkeypatch):
    # Fallback only: refuse nothing, since we cannot tell a typo from a real
    # version we have simply never seen.
    mcversion._reset_cache_for_tests()
    monkeypatch.setattr(mcversion.httpx2, "Client", _failing_client())
    assert mcversion.version_exists("1.21.99") is None
    assert mcversion.known_versions() is None
    mcversion._reset_cache_for_tests()


def test_a_manifest_without_a_version_list_is_treated_as_unknown(monkeypatch):
    mcversion._reset_cache_for_tests()
    monkeypatch.setattr(
        mcversion.httpx2, "Client", _client_returning({"latest": {"release": "1.99.9"}})
    )
    assert mcversion.latest_release() == "1.99.9"
    assert mcversion.version_exists("anything") is None
    mcversion._reset_cache_for_tests()


def test_a_failed_refresh_keeps_the_known_versions(monkeypatch):
    mcversion._reset_cache_for_tests()
    monkeypatch.setattr(mcversion.httpx2, "Client", _client_returning(_manifest()))
    assert mcversion.version_exists("1.21.3") is True
    # Expire the cache, then make the refresh fail.
    with mcversion._LATEST_LOCK:
        ts, v, ids = mcversion._latest_cache
        mcversion._latest_cache = (ts - 10 * 60 * 60, v, ids)
    monkeypatch.setattr(mcversion.httpx2, "Client", _failing_client())
    mcversion._fetch_latest_release()
    assert mcversion.version_exists("1.21.3") is True
    assert mcversion.version_exists("1.21.99") is False
    mcversion._reset_cache_for_tests()
