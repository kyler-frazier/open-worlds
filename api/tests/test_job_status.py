"""Following a queued content job to completion over the real HTTP stack.

Pins a terminal FAILED carrying its error, and a job scoped to its own world.
"""

from __future__ import annotations

import uuid

import pytest
from tests.conftest_routes import CSRF, login, logout

pytest_plugins = ["tests.conftest_routes"]


def _make_world(db_module, owner_id: str) -> str:
    from open_worlds.common.models import World, WorldState

    with db_module.session_scope() as db:
        w = World(
            id=uuid.uuid4(),
            owner_id=uuid.UUID(owner_id),
            subdomain=f"jb{uuid.uuid4().hex[:12]}",
            display_name="Jobs",
            game="MINECRAFT",
            state=WorldState.OFF,
            memory_mb=2048,
            max_players=10,
        )
        db.add(w)
        db.flush()
        return str(w.id)


def _make_job(db_module, world_id: str, *, status: str = "PENDING", error: str | None = None):
    from open_worlds.common.models import Job, JobStatus, JobType

    with db_module.session_scope() as db:
        job = Job(
            id=uuid.uuid4(),
            type=JobType.CONTENT,
            status=JobStatus(status),
            world_id=uuid.UUID(world_id),
            payload="{}",
            error=error,
        )
        db.add(job)
        db.flush()
        return str(job.id)


@pytest.fixture
def owned_world(app_client, make_user):
    client, db_module = app_client
    owner = make_user()
    return client, db_module, owner, _make_world(db_module, owner["id"])


def test_reports_a_pending_job(owned_world):
    client, db_module, owner, world_id = owned_world
    job_id = _make_job(db_module, world_id)
    logout(client)
    login(client, owner)

    r = client.get(f"/api/worlds/{world_id}/jobs/{job_id}", headers=CSRF)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == job_id
    assert body["status"] == "PENDING"
    assert body["type"] == "CONTENT"
    # The distinction the UI turns on: nothing terminal yet, and no error to show.
    assert body["error"] is None
    assert body["finished_at"] is None


def test_failed_job_surfaces_its_error(owned_world):
    """The case the whole endpoint exists for.

    Without this, a failure looks exactly like a job that has not finished, and
    the only difference a caller could observe is that it never does.
    """
    client, db_module, owner, world_id = owned_world
    job_id = _make_job(db_module, world_id, status="FAILED", error="checksum mismatch")
    logout(client)
    login(client, owner)

    r = client.get(f"/api/worlds/{world_id}/jobs/{job_id}", headers=CSRF)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "FAILED"
    assert r.json()["error"] == "checksum mismatch"


def test_job_from_another_world_reads_as_absent(app_client, make_user):
    """A job id must not be a bearer token.

    Looking one up through a world the caller *can* see must not reveal a job
    belonging to a world they cannot, so the mismatch is a 404 rather than a 403
    -- the same answer they would get for an id that never existed.
    """
    client, db_module = app_client
    owner = make_user()
    mine = _make_world(db_module, owner["id"])
    theirs = _make_world(db_module, make_user()["id"])
    someone_elses_job = _make_job(db_module, theirs)
    logout(client)
    login(client, owner)

    r = client.get(f"/api/worlds/{mine}/jobs/{someone_elses_job}", headers=CSRF)

    assert r.status_code == 404


def test_unknown_and_malformed_job_ids_are_404(owned_world):
    client, _db_module, owner, world_id = owned_world
    logout(client)
    login(client, owner)

    assert (
        client.get(f"/api/worlds/{world_id}/jobs/{uuid.uuid4()}", headers=CSRF).status_code == 404
    )
    # Not a UUID at all: still 404, not a 422 leaking that the id was parsed.
    assert client.get(f"/api/worlds/{world_id}/jobs/not-a-uuid", headers=CSRF).status_code == 404
