"""Tests for the settings guards that exist to stop a config mistake failing quietly.

Both guards here defend against the same class of bug: a setting that looks set
but isn't read, so the process starts happily with a wrong value.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from open_worlds.common import config
from open_worlds.common.config import Settings


def _settings(**kw) -> Settings:
    base = dict(
        session_secret="s",
        fernet_key="k",
        google_client_id="i",
        google_client_secret="c",
        internal_secret="x",
    )
    base.update(kw)
    return Settings(**base)


def test_a_retired_env_var_in_the_process_environment_is_rejected(monkeypatch):
    # PLATFORM_MC_RUN_UID was renamed to PLATFORM_RUN_UID. `extra="ignore"` means
    # the old name is silently dropped, so world containers would run as the
    # default uid while the api/worker used PUID -- invisible unless PUID != 1000.
    monkeypatch.setenv("PLATFORM_MC_RUN_UID", "1500")
    with pytest.raises(ValidationError) as exc:
        _settings()
    assert "PLATFORM_MC_RUN_UID -> PLATFORM_RUN_UID" in str(exc.value)


def test_a_retired_name_in_the_dotenv_file_is_rejected(monkeypatch):
    """The dotenv file is checked too, not just os.environ.

    Compose's `env_file:` injects into the process environment, so os.environ
    alone would catch the deployed case. But someone running the API directly
    against a local .env never touches os.environ -- and that is exactly the
    setup most likely to still be carrying a stale name.
    """
    monkeypatch.delenv("PLATFORM_MC_RUN_GID", raising=False)
    monkeypatch.setattr(config, "_dotenv_keys", lambda: {"PLATFORM_MC_RUN_GID": "1500"})
    with pytest.raises(ValidationError) as exc:
        _settings()
    assert "PLATFORM_MC_RUN_GID -> PLATFORM_RUN_GID" in str(exc.value)


def test_clean_configuration_is_accepted(monkeypatch):
    monkeypatch.setattr(config, "_dotenv_keys", dict)
    for name in ("PLATFORM_MC_RUN_UID", "PLATFORM_MC_RUN_GID"):
        monkeypatch.delenv(name, raising=False)
    assert _settings().run_uid == 1000


def test_an_empty_retired_var_is_not_treated_as_set(monkeypatch):
    # Compose writes `FOO=` for an unset variable; that is absence, not a stale
    # setting, and failing startup over it would be its own footgun.
    monkeypatch.setenv("PLATFORM_MC_RUN_UID", "")
    monkeypatch.setattr(config, "_dotenv_keys", lambda: {"PLATFORM_MC_RUN_GID": ""})
    assert _settings().run_uid == 1000


def test_memory_floor_may_not_exceed_the_ceiling():
    with pytest.raises(ValidationError) as exc:
        _settings(min_memory_mb=8192, max_memory_mb=4096)
    assert "MIN_MEMORY_MB" in str(exc.value)


@pytest.mark.parametrize("default_mb", [512, 65536])
def test_default_memory_must_sit_inside_the_bounds(default_mb):
    # A default outside its own bounds would fail every create that accepted it,
    # which is a misconfiguration worth catching at startup rather than per-request.
    with pytest.raises(ValidationError) as exc:
        _settings(min_memory_mb=1024, max_memory_mb=8192, default_memory_mb=default_mb)
    assert "DEFAULT_MEMORY_MB" in str(exc.value)


def test_consistent_memory_bounds_are_accepted():
    s = _settings(min_memory_mb=2048, max_memory_mb=8192, default_memory_mb=4096)
    assert (s.min_memory_mb, s.default_memory_mb, s.max_memory_mb) == (2048, 4096, 8192)


def test_no_global_default_is_the_normal_case():
    """Unset is not out-of-bounds -- it means "let each game decide".

    The bounds check must not read None as a value and reject a deployment that
    simply never set one, which is what every default install looks like.
    """
    s = _settings(min_memory_mb=2048, max_memory_mb=8192)
    assert s.default_memory_mb is None


def test_each_process_requires_only_its_own_secrets():
    """Secrets are per process now, so no container is handed all of them.

    The guarantee that mattered -- a deployment missing a secret fails at
    startup rather than limping -- moves from Settings validation to each
    process's ``require`` call, listed beside its factory.
    """
    from open_worlds.api.main import API_SECRETS, INTERNAL_API_SECRETS
    from open_worlds.worker.main import WORKER_SECRETS

    s = _settings(session_secret="", internal_secret="")
    with pytest.raises(RuntimeError) as exc:
        s.require(*API_SECRETS)
    assert "PLATFORM_SESSION_SECRET" in str(exc.value)
    with pytest.raises(RuntimeError) as exc:
        s.require(*INTERNAL_API_SECRETS)
    assert "PLATFORM_INTERNAL_SECRET" in str(exc.value)
    s.require(*WORKER_SECRETS)  # needs neither

    # Whitespace is not a value.
    with pytest.raises(RuntimeError):
        _settings(fernet_key="  ").require("fernet_key")


def test_secret_sets_do_not_hand_every_process_everything():
    from open_worlds.api.main import API_SECRETS, INTERNAL_API_SECRETS
    from open_worlds.worker.main import WORKER_SECRETS

    assert "internal_secret" not in API_SECRETS
    assert "session_secret" not in WORKER_SECRETS
    assert "google_client_secret" not in INTERNAL_API_SECRETS


def test_an_empty_optional_number_means_unset(monkeypatch):
    """compose's ``${VAR:-}`` passes an unset optional as empty; that must mean
    unset, not crash every app service on int parsing."""
    monkeypatch.setenv("PLATFORM_DEFAULT_MEMORY_MB", "")
    monkeypatch.setenv("PLATFORM_HOST_MEMORY_BUDGET_MB", "")
    s = _settings()
    assert s.default_memory_mb is None
    assert s.host_memory_budget_mb is None


def test_a_real_optional_number_still_parses(monkeypatch):
    monkeypatch.setenv("PLATFORM_HOST_MEMORY_BUDGET_MB", "24576")
    assert _settings().host_memory_budget_mb == 24576


@pytest.mark.parametrize(
    "target,foreign_secrets",
    [
        ("app", ("PLATFORM_INTERNAL_SECRET",)),
        (
            "internal_app",
            (
                "PLATFORM_SESSION_SECRET",
                "PLATFORM_GOOGLE_CLIENT_ID",
                "PLATFORM_GOOGLE_CLIENT_SECRET",
            ),
        ),
    ],
)
def test_each_asgi_target_builds_without_the_other_process_secrets(
    monkeypatch, target, foreign_secrets
):
    """Importing the module must not demand the other process's secrets.

    Both ASGI apps live in one module, but compose hands each service only its
    own secrets. Building both at import time made the module require the
    union, so `api` crash-looped on the internal secret it is deliberately not
    given and `internal-api` crash-looped on the session and OAuth secrets.
    """
    import importlib

    for name in foreign_secrets:
        monkeypatch.delenv(name, raising=False)
    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "_dotenv_keys", dict)

    from open_worlds.api import main as api_main

    api_main = importlib.reload(api_main)
    try:
        assert getattr(api_main, target) is not None
    finally:
        config.get_settings.cache_clear()


def test_forwarded_headers_are_trusted_only_from_loopback_by_default(monkeypatch):
    """Losing the compose override must not mean trusting every peer's
    X-Forwarded-For."""
    monkeypatch.delenv("PLATFORM_FORWARDED_ALLOW_IPS", raising=False)
    assert _settings().forwarded_allow_ips == "127.0.0.1"
