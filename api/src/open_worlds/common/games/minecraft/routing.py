"""Publish Minecraft routes to mc-router, so it needs no Docker access.

A route exists exactly while a world is RUNNING. Anything else falls through
to mc-router's default backend, the placeholder, which explains the state to
the player and can wake the world.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import session_scope
from ...logging import get_logger
from ...models import World, WorldState
from ...orchestrator import backend_address, make_orchestrator
from ...stopping import perform_stop
from .adapter import MinecraftAdapter
from .config import minecraft_config

GAME = MinecraftAdapter.game

# Whether the last attempt reached mc-router; None before the first.
_router_reachable: bool | None = None

log = get_logger(__name__)


def _api(path: str, method: str = "GET", payload: dict | None = None) -> object:
    base = minecraft_config().mc_router_api_url.rstrip("/")
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{base}{path}", data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=minecraft_config().mc_router_timeout_seconds) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _apply(path: str, method: str, payload: dict | None = None) -> None:
    """A route change, where "no response" means it worked.

    mc-router closes the connection without answering a successful write. The
    next cycle re-reads /routes either way.
    """
    try:
        _api(path, method, payload)
    except http.client.RemoteDisconnected:
        pass


def desired_routes(db: Session) -> dict[str, str]:
    """hostname -> backend, for every Minecraft world currently RUNNING."""
    s = get_settings()
    rows = db.execute(
        select(World).where(World.game == GAME, World.state == WorldState.RUNNING)
    ).scalars()
    return {f"{w.subdomain}.{s.servers_domain}": backend_address(w.subdomain, GAME) for w in rows}


def sync_routes(db: Session) -> int:
    """Make mc-router's table match the database. Returns the number of changes.

    A diff on a timer corrects itself where a missed event would not, and
    repopulates mc-router's in-memory table after it restarts.
    """
    if not minecraft_config().mc_router_api_url:
        return 0
    global _router_reachable
    try:
        current_raw = _api("/routes") or {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Once per outage: a host without the router would log this forever.
        if _router_reachable is not False:
            log.warning("could not read mc-router routes", extra={"error": str(exc)})
        _router_reachable = False
        return 0
    if _router_reachable is False:
        log.info("mc-router is reachable again")
    _router_reachable = True
    current = {
        host: (info or {}).get("backend", "") for host, info in cast(dict, current_raw).items()
    }
    wanted = desired_routes(db)

    changes = 0
    for host, backend in wanted.items():
        if current.get(host) == backend:
            continue
        try:
            _apply("/routes", "POST", {"serverAddress": host, "backend": backend})
            changes += 1
            log.info("published route", extra={"host": host, "backend": backend})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("could not publish route", extra={"host": host, "error": str(exc)})

    for host in current:
        if host in wanted:
            continue
        try:
            _apply(f"/routes/{host}", "DELETE")
            changes += 1
            log.info("withdrew route", extra={"host": host})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("could not withdraw route", extra={"host": host, "error": str(exc)})

    return changes


def sleep_idle_minecraft_worlds(db: Session, orch) -> int:  # type: ignore[no-untyped-def]
    """Stop Minecraft worlds nobody has been on for ``auto_stop_after_seconds``.

    Idle means RUNNING with nobody on since the cutoff (``last_active_at``, or
    ``started_at`` if nobody joined this run), and up longer than the wake
    grace, so a world still booting is not put back to sleep. The player count
    must also be recent: a stale count is not evidence that nobody is playing.
    """
    s = get_settings()
    now = datetime.now(UTC)
    idle_cutoff = now - timedelta(seconds=s.auto_stop_after_seconds)
    boot_cutoff = now - timedelta(seconds=s.wake_grace_seconds)
    # Tolerate one missed tick before treating the count as unusable.
    fresh_cutoff = now - timedelta(seconds=max(60, s.player_poll_seconds * 3))

    rows = db.execute(
        select(World).where(
            World.game == GAME,
            World.state == WorldState.RUNNING,
            World.player_count == 0,
            World.started_at.is_not(None),
            World.started_at < boot_cutoff,
            World.players_updated_at.is_not(None),
            World.players_updated_at >= fresh_cutoff,
            func.coalesce(World.last_active_at, World.started_at) < idle_cutoff,
        )
    ).scalars()

    slept = 0
    for world in list(rows):
        log.info("sleeping idle world", extra={"subdomain": world.subdomain})
        try:
            perform_stop(orch, world.subdomain, WorldState.SLEEPING)
        except Exception:
            log.exception("could not sleep idle world", extra={"subdomain": world.subdomain})
            continue
        world.state = WorldState.SLEEPING
        world.stop_target = None
        slept += 1
    if slept:
        db.commit()
    return slept


def run_sync_routes() -> None:
    """Keep mc-router's table matching the database; never raise into the scheduler."""
    try:
        with session_scope() as db:
            sync_routes(db)
    except Exception:
        log.exception("could not sync mc-router routes")


def run_sleep_idle() -> None:
    """Stop Minecraft worlds nobody is on; never raise into the scheduler."""
    try:
        with session_scope() as db:
            sleep_idle_minecraft_worlds(db, make_orchestrator())
    except Exception:
        log.exception("could not sleep idle Minecraft worlds")
