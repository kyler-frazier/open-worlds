"""The local-only dev login, and the guards that keep it off real deployments.

The route tests need Docker for a throwaway Postgres; the guard tests need
nothing.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from open_worlds.api import dev_login
from open_worlds.common.config import Settings

pytest_plugins = ["tests.conftest_routes"]

REPO = Path(__file__).resolve().parents[2]

LOCAL = {
    "dev_login": True,
    "web_origin": "http://localhost:5173",
    "oauth_redirect_base": "http://localhost:5173",
    "database_url": "postgresql+psycopg://dev:dev@127.0.0.1:5432/dev",
    "cookie_secure": False,
}


@pytest.fixture(autouse=True)
def _not_in_container(monkeypatch, tmp_path):
    # Point the markers somewhere that does not exist, so these tests mean the
    # same thing on a laptop, in CI and inside a dev container.
    monkeypatch.setattr(dev_login, "CONTAINER_MARKERS", (str(tmp_path / "absent"),))


def test_local_config_is_accepted():
    assert dev_login.dev_login_problems(Settings(**LOCAL)) == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("web_origin", "https://games.example.com", "PLATFORM_WEB_ORIGIN"),
        ("oauth_redirect_base", "https://games.example.com", "PLATFORM_OAUTH_REDIRECT_BASE"),
        # Prod's database host is `db`, inside the compose network.
        ("database_url", "postgresql+psycopg://games:x@db:5432/games", "PLATFORM_DATABASE_URL"),
        (
            "database_url",
            "postgresql+psycopg://games:x@10.0.0.5:5432/g",
            "PLATFORM_DATABASE_URL",
        ),
        ("cookie_secure", True, "PLATFORM_COOKIE_SECURE"),
    ],
)
def test_each_non_local_setting_refuses_to_start(field, value, reason):
    s = Settings(**{**LOCAL, field: value})
    with pytest.raises(RuntimeError, match=reason):
        dev_login.check_dev_login_safe(s)


def test_refuses_inside_a_container(monkeypatch, tmp_path):
    marker = tmp_path / ".dockerenv"
    marker.touch()
    monkeypatch.setattr(dev_login, "CONTAINER_MARKERS", (str(marker),))
    with pytest.raises(RuntimeError, match="container"):
        dev_login.check_dev_login_safe(Settings(**LOCAL))


def test_default_is_off():
    # The field default, not Settings(): a developer's own api/.env may turn it on.
    assert Settings.model_fields["dev_login"].default is False


def test_compose_never_passes_the_flag_through():
    compose = (REPO / "deploy" / "docker-compose.yml").read_text()
    assert "DEV_LOGIN" not in compose


def test_preflight_refuses_a_deploy_env_that_sets_it():
    preflight = (REPO / "deploy" / "scripts" / "preflight.sh").read_text()
    assert "^PLATFORM_DEV_LOGIN=" in preflight


# --- Routes ------------------------------------------------------------------


def _build_app(monkeypatch, pg_url, tmp_path, **env):
    from open_worlds.common import config
    from open_worlds.common import db as db_module

    monkeypatch.setenv("PLATFORM_DATABASE_URL", pg_url)
    monkeypatch.setenv("PLATFORM_WORLD_MANAGER_URL", "")
    monkeypatch.setenv("PLATFORM_COOKIE_SECURE", "false")
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("PLATFORM_WORLDS_DIR", str(tmp_path / "worlds"))
    monkeypatch.setenv("PLATFORM_BACKUP_STAGING_DIR", str(tmp_path / "staging"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config.get_settings.cache_clear()
    importlib.reload(db_module)
    from open_worlds.api import main as api_main

    return importlib.reload(api_main).create_app()


@pytest.fixture(autouse=True)
def _fresh_settings():
    yield
    from open_worlds.common import config

    config.get_settings.cache_clear()


@pytest.fixture
def dev_env():
    return {
        "PLATFORM_DEV_LOGIN": "true",
        "PLATFORM_WEB_ORIGIN": "http://localhost:5173",
        "PLATFORM_OAUTH_REDIRECT_BASE": "http://localhost:5173",
    }


def test_route_does_not_exist_when_off(monkeypatch, pg_url, tmp_path):
    from fastapi.testclient import TestClient

    app = _build_app(monkeypatch, pg_url, tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        assert c.get("/api/auth/dev/login", follow_redirects=False).status_code == 404


def test_prod_shaped_config_will_not_build(monkeypatch, pg_url, tmp_path):
    with pytest.raises(RuntimeError, match="PLATFORM_DEV_LOGIN"):
        _build_app(monkeypatch, pg_url, tmp_path, PLATFORM_DEV_LOGIN="true")


@pytest.mark.parametrize(("role", "is_admin"), [("admin", True), ("user", False)])
def test_signs_in_from_loopback(monkeypatch, pg_url, tmp_path, dev_env, role, is_admin):
    from fastapi.testclient import TestClient

    app = _build_app(monkeypatch, pg_url, tmp_path, **dev_env)
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        r = c.get(f"/api/auth/dev/login?role={role}", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "http://localhost:5173"
        me = c.get("/api/auth/me").json()
        assert me["google_sub"] == f"dev:{role}"
        assert me["is_admin"] is is_admin


def test_refuses_a_non_loopback_peer(monkeypatch, pg_url, tmp_path, dev_env):
    from fastapi.testclient import TestClient

    app = _build_app(monkeypatch, pg_url, tmp_path, **dev_env)
    with TestClient(app, client=("203.0.113.50", 50000)) as c:
        assert c.get("/api/auth/dev/login", follow_redirects=False).status_code == 404


def test_rejects_unknown_role(monkeypatch, pg_url, tmp_path, dev_env):
    from fastapi.testclient import TestClient

    app = _build_app(monkeypatch, pg_url, tmp_path, **dev_env)
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        assert c.get("/api/auth/dev/login?role=root", follow_redirects=False).status_code == 400
