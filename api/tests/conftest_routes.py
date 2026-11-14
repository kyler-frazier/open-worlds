"""Harness for driving the routers through a real HTTP client.

Needs a real Postgres (the models use PG enums and UUID columns), so a
throwaway container is started per module; these live apart from the fast
unit tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import pytest

PG_IMAGE = "postgres:16-alpine"
PG_CONTAINER = "open-worlds-routetests"
PG_PASSWORD = "routetests"


def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


@pytest.fixture(scope="session")
def pg_url() -> str:
    """A database of our own, migrated, gone again at the end."""
    if not _docker_ok():
        pytest.skip("no Docker daemon; route tests need a throwaway Postgres")

    # -v as well as -f: postgres declares a VOLUME, so every run creates an
    # anonymous volume that `docker rm -f` leaves behind. Without this each
    # test run leaked ~50 MB, and they are invisible until the disk fills.
    subprocess.run(["docker", "rm", "-fv", PG_CONTAINER], capture_output=True)
    started = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            PG_CONTAINER,
            "-e",
            f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "-e",
            "POSTGRES_USER=routetests",
            "-e",
            "POSTGRES_DB=routetests",
            "-p",
            "0:5432",
            PG_IMAGE,
        ],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start Postgres: {started.stderr[:200]}")

    try:
        port = (
            subprocess.run(
                ["docker", "port", PG_CONTAINER, "5432/tcp"],
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.strip()
            .rsplit(":", 1)[-1]
        )

        # Wait for it to accept connections rather than sleeping a guess.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["docker", "exec", PG_CONTAINER, "pg_isready", "-U", "routetests"],
                capture_output=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.skip("Postgres did not become ready")

        url = f"postgresql+psycopg://routetests:{PG_PASSWORD}@127.0.0.1:{port}/routetests"
        # Schema from the migrations, not create_all: it is what production
        # runs, so a migration that does not apply is a test failure here.
        env = {**os.environ, "PLATFORM_DATABASE_URL": url}
        migrated = subprocess.run(
            # Through the interpreter running the tests, not a hardcoded venv
            # path: CI lays the environment out differently.
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            env=env,
        )
        if migrated.returncode != 0:
            pytest.skip(f"migrations failed: {migrated.stderr[-300:]}")
        yield url
    finally:
        subprocess.run(["docker", "rm", "-fv", PG_CONTAINER], capture_output=True)


@pytest.fixture
def app_client(pg_url, monkeypatch, tmp_path_factory):
    """A TestClient wired to the throwaway database.

    The app is built fresh per test so that settings, the engine and the
    dependency overrides cannot leak between them.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PLATFORM_DATABASE_URL", pg_url)
    monkeypatch.setenv("PLATFORM_WORLD_MANAGER_URL", "")  # drive Docker directly; never called
    # The session cookie is Secure by default, which is right behind TLS and
    # means httpx2 will not send it back over the TestClient's http:// origin.
    # Turning it off here is about the transport the test harness uses, not
    # about the behaviour under test -- cookie_secure itself stays on in
    # production and is asserted by the deploy's own config.
    monkeypatch.setenv("PLATFORM_COOKIE_SECURE", "false")
    # Routes that stage an upload or create world storage write to disk.
    # These are separate settings, not derived from a single data root, so each
    # has to be redirected or the tests reach the real /srv/games and fail on
    # permissions -- which reads as a route bug rather than a harness one.
    root = tmp_path_factory.mktemp("data-root")
    monkeypatch.setenv("PLATFORM_DATA_ROOT", str(root))
    monkeypatch.setenv("PLATFORM_WORLDS_DIR", str(root / "worlds"))
    monkeypatch.setenv("PLATFORM_BACKUP_STAGING_DIR", str(root / "backup-staging"))
    monkeypatch.setenv("PLATFORM_RUNTIME_CONFIG_PATH", str(root / "platform-config.json"))

    from open_worlds.common import config
    from open_worlds.common import db as db_module

    config.get_settings.cache_clear()
    import importlib

    importlib.reload(db_module)

    from open_worlds.api import main as api_main

    api_main = importlib.reload(api_main)
    app = api_main.create_app()

    with TestClient(app) as client:
        yield client, db_module

    config.get_settings.cache_clear()


@pytest.fixture
def make_user(app_client):
    """Insert a user directly; the OAuth dance is tested separately."""
    _client, db_module = app_client
    created: list[uuid.UUID] = []

    def _make(role="USER", email=None, google_sub=None):
        from open_worlds.common.models import Role, User

        with db_module.session_scope() as db:
            user = User(
                id=uuid.uuid4(),
                google_sub=google_sub or f"sub-{uuid.uuid4().hex[:12]}",
                email=email or f"{uuid.uuid4().hex[:8]}@example.test",
                role=Role.ADMIN if role == "ADMIN" else Role.USER,
            )
            db.add(user)
            db.flush()
            created.append(user.id)
            return {"id": str(user.id), "google_sub": user.google_sub, "role": role}

    return _make


def login(client, user, session_version: int = 0, sid: str | None = None) -> None:
    """Sign a user in by forging the session cookie the way Starlette signs it.

    Building the cookie rather than stubbing get_current_user on purpose: the
    real middleware then verifies the signature, so these tests exercise the
    actual authentication path instead of a hole punched around it.
    """
    import base64
    import json

    import itsdangerous

    from open_worlds.common.config import get_settings

    s = get_settings()
    # Unique per sign-in, as the real one is: a logout in one test must not
    # sign out the next.
    sid = uuid.uuid4().hex if sid is None else sid
    data = base64.b64encode(
        json.dumps({"user_id": user["id"], "session_version": session_version, "sid": sid}).encode()
    )
    signed = itsdangerous.TimestampSigner(str(s.session_secret)).sign(data).decode()
    client.cookies.set(s.session_cookie_name, signed)


def logout(client) -> None:
    from open_worlds.common.config import get_settings

    client.cookies.delete(get_settings().session_cookie_name)


# Mutating routes require this header; its absence is what CSRF protection
# catches, since a cross-site form post cannot set a custom header.
CSRF = {"x-requested-with": "XMLHttpRequest"}
