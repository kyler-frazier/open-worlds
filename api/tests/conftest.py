"""Test configuration: seed the environment with dummy secrets before imports.

The application refuses to start without required secrets, so we inject valid
placeholder values here. No real database, Docker daemon or host directory is
touched by the unit tests.
"""

from __future__ import annotations

import os
import tempfile

from cryptography.fernet import Fernet

os.environ.setdefault("PLATFORM_SESSION_SECRET", "test-session-secret")
os.environ.setdefault("PLATFORM_FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("PLATFORM_GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("PLATFORM_GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault(
    "PLATFORM_DATABASE_URL", "postgresql+psycopg://games:games@localhost:5432/games_test"
)
os.environ.setdefault("PLATFORM_ADMIN_GOOGLE_SUBS", "admin-sub-1")
os.environ.setdefault("PLATFORM_INTERNAL_SECRET", "test-internal-secret")
# Starting a world now writes into its data directory before the container comes
# up (Palworld renders its settings file there), so the default /srv/games/worlds
# would make orchestrator tests depend on a real, writable system path.
os.environ.setdefault("PLATFORM_WORLDS_DIR", tempfile.mkdtemp(prefix="gp-worlds-"))
# Every world gets an egress guard in production. Tests about the guard switch
# it on themselves (see test_orchestrator's `guarded` fixture); everything else
# exercises the world container alone, as the fakes are built to.
os.environ.setdefault("PLATFORM_EGRESS_GUARD_ENABLED", "false")
